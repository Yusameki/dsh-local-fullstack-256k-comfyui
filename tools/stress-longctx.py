"""Long-context stress test for a local Strata engine behind a lazy-load proxy.

Measures real TTFT over streaming, prompt tokens as reported by the engine,
prefill throughput from the engine log, decode throughput, finish_reason,
and host memory / commit usage before and after each tier.

Usage:
    python stress-longctx.py --calibrate
    python stress-longctx.py --level 32k
    python stress-longctx.py --all
    python stress-longctx.py --all --ratio 1.397 --max-tokens 2048

Environment (all optional):
    STRATA_BASE        default http://127.0.0.1:8090
    STRATA_MODEL       default qwen3.8-flash-next-gyro-gyro-m
    STRATA_ENGINE_LOG  path to the engine log; omit to skip log parsing

Notes that cost real time to learn:
  * `253952 = 262144 - 8192` leaves room for output; that is the practical ceiling.
  * Calibrate chars/token with THIS script's own prompt template, not with an
    unrelated Chinese sample: structure (digits, punctuation, English terms)
    changes the ratio a lot. On one machine 1.397 vs 2.021 was the difference
    between a clean 32k run and a 46,798-token overshoot.
  * Thinking models emit `reasoning_content` before `content`; a tiny
    max_tokens gets eaten by reasoning and leaves the answer empty.
  * bili (or any proxy that only dumps headers for streaming) cannot tell you
    finish_reason. Read it from the SSE stream, as this script does.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.request

try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

BASE = os.environ.get('STRATA_BASE', 'http://127.0.0.1:8090').rstrip('/')
URL = BASE + '/v1/chat/completions'
HEALTH = BASE + '/health'
MODEL = os.environ.get('STRATA_MODEL', 'qwen3.8-flash-next-gyro-gyro-m')
ENGINE_LOG = os.environ.get('STRATA_ENGINE_LOG', '')

OUT_DIR = os.path.dirname(os.path.abspath(__file__))
CTX_LIMIT = 262144
MAX_OUT = 16384
LEVELS = {'32k': 32768, '128k': 131072, '256k': 253952}

# Probes at 2% / 50% / 97% of the filler: the last one is the remote-retrieval check.
NEEDLES = [
    (0.02, 'LAPIS-37'),
    (0.50, 'AMBER-91'),
    (0.97, 'INDIGO-52'),
]


# ── environment sampling ────────────────────────────────────────────────────
def ps(cmd, timeout=20):
    try:
        r = subprocess.run(['powershell', '-NoProfile', '-Command', cmd],
                           capture_output=True, text=True, timeout=timeout,
                           encoding='utf-8', errors='replace')
        return (r.stdout or '').strip()
    except Exception as e:
        return 'ERR:%s' % e


def sample_env(tag):
    snap = {'tag': tag, 't': time.strftime('%H:%M:%S')}
    snap['proxy_pid'] = ps("(Get-NetTCPConnection -LocalPort 8090 -State Listen "
                           "-ErrorAction SilentlyContinue).OwningProcess")
    pid = snap['proxy_pid'].strip()
    if pid.isdigit():
        snap['proxy_ws_mib'] = ps(
            "[math]::Round((Get-Process -Id %s).WorkingSet64/1MB)" % pid)
    snap['engine_procs'] = ps(
        "Get-Process | Where-Object {$_.ProcessName -match 'strata|llama'} | "
        "ForEach-Object { \"$($_.Id):$($_.ProcessName):\" + "
        "[math]::Round($_.WorkingSet64/1MB) } | Out-String")
    snap['commit'] = ps(
        "$os = Get-CimInstance Win32_OperatingSystem; "
        "\"commit_used_mib=\" + [math]::Round(($os.TotalVirtualMemorySize - "
        "$os.FreeVirtualMemory)/1KB) + \" commit_limit_mib=\" + "
        "[math]::Round($os.TotalVirtualMemorySize/1KB) + \" free_phys_mib=\" + "
        "[math]::Round($os.FreePhysicalMemory/1KB)")
    snap['engine_log_size'] = (os.path.getsize(ENGINE_LOG)
                               if ENGINE_LOG and os.path.exists(ENGINE_LOG) else 0)
    return snap


def engine_log_since(offset):
    if not ENGINE_LOG or not os.path.exists(ENGINE_LOG):
        return ''
    with open(ENGINE_LOG, 'rb') as f:
        f.seek(offset)
        return f.read().decode('utf-8', errors='replace')


def parse_prefill(chunk):
    out = {'prompt_line': None, 'drive_read': None, 'cache_line': None, 'errors': []}
    for line in chunk.splitlines():
        s = line.strip()
        if 'prompt ' in s and ' tokens = ' in s and 'generated in' in s:
            out['prompt_line'] = s
        if 'read from the drive' in s:
            out['drive_read'] = s
        if 'expert cache' in s or 'page cache' in s.lower():
            out['cache_line'] = s
        if re.search(r'\b(error|Error|ERROR|failed|out of memory|OOM)\b', s):
            out['errors'].append(s[:200])
    return out


# ── prompt construction ─────────────────────────────────────────────────────
def build_prompt(target_chars, needles=True):
    """Fill roughly target_chars characters and bury the probes by ACTUAL line count.

    Rendering once without needles measures how many lines the budget really
    allows; probe positions are then computed from that number. Computing them
    from the estimated line count silently drops the 97% needle, which makes a
    healthy model look like it lost remote recall.
    """
    head = 'Below is a very long operations log. Read it, then answer only the final question.\n\n'
    tail_q = ('\n\nThe log ends here. Answer: what are the three secret tokens marked '
              '[SECRET] in the text? Output only the three tokens, comma separated, '
              'no explanation, do not quote the text.')
    budget = max(0, target_chars - len(head) - len(tail_q))

    tpl_normal = 'Record %d: service running normally, no anomalies, filler line.\n'
    tpl_needle = 'Record %d: [SECRET] the token on this line is "%s", remember it.\n'

    avg_len = max(1, len(tpl_normal % 1))
    limit = len(head) + budget

    def render(n_max, marks):
        parts = [head]
        total = len(head)
        stop_at = max(marks) if marks else 0
        for i in range(1, n_max + 1):
            line = tpl_needle % (i, marks[i]) if i in marks else tpl_normal % i
            parts.append(line)
            total += len(line)
            if total >= limit and i >= stop_at:
                break
        return parts

    n_actual = len(render(budget // avg_len + 16, {})) - 1

    marks = {}
    if needles:
        for frac, word in NEEDLES:
            pos = max(1, min(n_actual, int(round(n_actual * frac))))
            while pos in marks and pos < n_actual:
                pos += 1
            marks[pos] = word

    parts = render(n_actual, marks)
    parts.append(tail_q)
    text = ''.join(parts)

    missing = [w for _, w in NEEDLES if w not in text] if needles else []
    if missing:
        raise SystemExit('build_prompt self-check failed: needles missing %s' % missing)

    return text, len(parts) - 2, marks


# ── request ─────────────────────────────────────────────────────────────────
def health():
    try:
        with urllib.request.urlopen(HEALTH, timeout=10) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        return {'error': str(e)}


def post_stream(prompt, max_tokens=2048, timeout=1800):
    body = json.dumps({
        'model': MODEL,
        'messages': [{'role': 'user', 'content': prompt}],
        'max_tokens': max_tokens,
        'stream': True,
        'stream_options': {'include_usage': True},
        'temperature': 0.0,
    }).encode('utf-8')

    req = urllib.request.Request(URL, data=body, headers={
        'Content-Type': 'application/json', 'Accept': 'text/event-stream'})

    t0 = time.time()
    ttft = None
    first_content = None
    last_chunk = None
    chunks = 0
    text, reasoning = [], []
    usage = None
    finish_reason = None
    saw_done = False
    error = None

    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            for raw in r:
                line = raw.decode('utf-8', 'replace').strip()
                if not line.startswith('data:'):
                    continue
                payload = line[5:].strip()
                if payload == '[DONE]':
                    saw_done = True
                    break
                try:
                    obj = json.loads(payload)
                except Exception:
                    continue
                if obj.get('usage'):
                    usage = obj['usage']
                for ch in obj.get('choices') or []:
                    if ch.get('finish_reason'):
                        finish_reason = ch['finish_reason']
                    d = ch.get('delta') or {}
                    rc, cc = d.get('reasoning_content'), d.get('content')
                    if rc or cc:
                        now = time.time()
                        chunks += 1
                        last_chunk = now
                        if ttft is None:
                            ttft = now - t0
                    if rc:
                        reasoning.append(rc)
                    if cc:
                        if first_content is None:
                            first_content = time.time() - t0
                        text.append(cc)
    except Exception as e:
        error = '%s: %s' % (type(e).__name__, e)

    return {
        'ttft_s': round(ttft, 3) if ttft is not None else None,
        'first_content_s': round(first_content, 3) if first_content is not None else None,
        'total_s': round((last_chunk or time.time()) - t0, 3),
        'chunks': chunks,
        'text': ''.join(text),
        'reasoning': ''.join(reasoning),
        'usage': usage,
        'finish_reason': finish_reason,
        'saw_done': saw_done,
        'error': error,
    }


def run_level(label, target_tokens, ratio, max_tokens=2048):
    target_chars = int(target_tokens * ratio)
    prompt, n_lines, marks = build_prompt(target_chars)
    print('\n' + '=' * 66)
    print('tier %s  target tokens~%d  chars=%d  lines~%d  needles=%s'
          % (label, target_tokens, len(prompt), n_lines, list(marks.values())))
    print('=' * 66)

    before = sample_env('before')
    print('before: proxy_ws=%s MiB  %s' % (before.get('proxy_ws_mib'), before.get('commit')))

    log_off = before['engine_log_size']
    res = post_stream(prompt, max_tokens=max_tokens)
    after = sample_env('after')

    print('result: TTFT=%s s  first content=%s s  total=%s s  chunks=%d  error=%s'
          % (res['ttft_s'], res['first_content_s'], res['total_s'], res['chunks'], res['error']))
    print('finish_reason=%s  saw[DONE]=%s' % (res['finish_reason'], res['saw_done']))
    print('usage=%s' % json.dumps(res['usage'], ensure_ascii=False))
    print('reasoning %d chars / content %d chars' % (len(res['reasoning']), len(res['text'])))
    print('answer head: %s' % res['text'][:300].replace('\n', ' '))
    print('after: proxy_ws=%s MiB  %s' % (after.get('proxy_ws_mib'), after.get('commit')))

    pf = parse_prefill(engine_log_since(log_off))
    if pf['prompt_line']:
        print('engine: %s' % pf['prompt_line'])
    if pf['drive_read']:
        print('drive read: %s' % pf['drive_read'])
    if pf['errors']:
        print('log warnings: %s' % pf['errors'][:3])

    hit = {word: (word in res['text']) for _, word in NEEDLES}
    print('needle hits: %s' % hit)

    return {
        'level': label,
        'target_tokens': target_tokens,
        'prompt_chars': len(prompt),
        'prompt_tokens_reported': (res['usage'] or {}).get('prompt_tokens'),
        'completion_tokens': (res['usage'] or {}).get('completion_tokens'),
        'ttft_s': res['ttft_s'],
        'total_s': res['total_s'],
        'finish_reason': res['finish_reason'],
        'saw_done': res['saw_done'],
        'error': res['error'],
        'answer_head': res['text'][:400],
        'needle_hit': hit,
        'env_before': before,
        'env_after': after,
        'prefill': pf,
    }


def calibrate(samples=2):
    """Calibrate chars/token with the real prompt template."""
    text, _, _ = build_prompt(2000, needles=False)
    print('calibration sample chars = %d' % len(text))
    ratios = []
    for i in range(samples):
        r = post_stream(text, max_tokens=16, timeout=300)
        pt = (r['usage'] or {}).get('prompt_tokens')
        print('  run %d prompt_tokens=%s ttft=%s err=%s'
              % (i + 1, pt, r['ttft_s'], r['error']))
        if pt:
            ratios.append(len(text) / pt)
        time.sleep(1)
    if not ratios:
        raise SystemExit('calibration failed: no prompt_tokens')
    ratio = sum(ratios) / len(ratios)
    print('chars/token = %.3f (mean of %d)' % (ratio, len(ratios)))
    return ratio


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--calibrate', action='store_true')
    ap.add_argument('--level', choices=list(LEVELS))
    ap.add_argument('--all', action='store_true')
    ap.add_argument('--ratio', type=float, default=None)
    ap.add_argument('--max-tokens', type=int, default=2048,
                    help='default 2048: thinking models spend the first tokens on '
                         'reasoning, so a tiny budget leaves the answer empty')
    args = ap.parse_args()

    h = health()
    print('health: %s' % json.dumps(h, ensure_ascii=False))
    if h.get('error'):
        raise SystemExit('proxy unavailable, stop')
    if not h.get('loaded'):
        raise SystemExit('engine not loaded, warm it up first')

    ratio = args.ratio if args.ratio else calibrate()
    if args.calibrate and not (args.level or args.all):
        return

    plan = []
    if args.all:
        plan = [('32k', LEVELS['32k']), ('128k', LEVELS['128k']), ('256k', LEVELS['256k'])]
    elif args.level:
        plan = [(args.level, LEVELS[args.level])]
    if not plan:
        print('no tier specified. use --level 32k or --all')
        return

    results = []
    for label, tok in plan:
        try:
            results.append(run_level(label, tok, ratio, max_tokens=args.max_tokens))
        except Exception as e:
            print('!! tier %s crashed: %s: %s' % (label, type(e).__name__, e))
            results.append({'level': label, 'target_tokens': tok, 'error': str(e)})
        out = os.path.join(OUT_DIR, 'stress-longctx-result.json')
        with open(out, 'w', encoding='utf-8') as f:
            json.dump({'ratio': ratio, 'results': results}, f, ensure_ascii=False, indent=2)
        print('written -> %s' % out)

    print('\n' + '=' * 66)
    print('summary')
    print('=' * 66)
    for r in results:
        print('%-5s target=%-7s prompt_tok=%-7s ttft=%-8s total=%-8s finish=%-8s err=%s'
              % (r.get('level'), r.get('target_tokens'), r.get('prompt_tokens_reported'),
                 r.get('ttft_s'), r.get('total_s'), r.get('finish_reason'), r.get('error')))


if __name__ == '__main__':
    main()
