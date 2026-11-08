<div align="center">

# Gyro-M · 256K Long-Context Benchmark

**English** | [简体中文](gyro-m-256k-benchmark.md)

**251K-token cold prefill in 178 seconds · decode holds at 70 tok/s · 64 GB RAM + 32 GB pagefile never hit the ceiling**

`Strata 0.1.58` · `Qwen3.8-Flash-Next-Gyro-M-TQ2_0` · `RTX 3080 Ti 12 GB` · `2026-11-08`

</div>

---

> [!NOTE]
> Every number here comes from **engine logs and proxy SSE responses captured on the machine**, not from estimates.
> When one log file spans several engine starts, values are re-attributed by their `expert cache auto` / `serve: N MiB of VRAM free` / `verify` segment and never mixed across starts. That rule matters more than any single figure below.

---

## 1. Test machine

| Item | Value |
|---|---|
| CPU | Ryzen 9 5950X (16C / 32T) |
| RAM | 64 GB (4×16 GB DDR4-3200, 63.91 GiB usable) |
| GPU | RTX 3080 Ti, **12 GB** |
| OS | Windows 11 Build 26200 |
| Pagefile | **Fixed 32768 MB** (custom size, not system-managed) |
| Commit limit | **98,214 MiB** (was 67.91 GiB before the pagefile change) |
| Model | `Qwen3.8-Flash-Next-Gyro-M-TQ2_0.gguf`, **91,965,774,560 bytes** (≈91.97 GB) |
| Engine | Strata 0.1.58 |
| Context | `--max-context 262144`, `--kv int8`, `--kv-resident 32768`, `--vram-reserve-mib 1728` |

---

## 2. Tiered stress test

Method: direct calls to the 8090 lazy-load proxy at `/v1/chat/completions`, `stream: true` + `stream_options.include_usage`, `temperature: 0`, `max_tokens: 2048`.

| Tier | Target tok | **Measured prompt tok** | TTFT | Total | finish_reason | Engine prefill | decode |
|---|---|---|---|---|---|---|---|
| 32k | 32,768 | **32,272** | 20.3 s | 35.6 s | **stop** | 20,299 ms → **1589.8 tok/s** | 68.2 tok/s |
| 128k | 131,072 | **129,850** | 85.9 s | 109.3 s | **stop** | 85,849 ms → **1512.5 tok/s** | 66.9 tok/s |
| 256k | 253,952 | **251,809** | 177.8 s | 200.1 s | **stop** | 177,656 ms → **1417.4 tok/s** | 70.2 tok/s |
| 256k (rerun) | 253,952 | **251,813** | 184.1 s | 186.8 s | **stop** | 183,979 ms → **1368.7 tok/s** | 63.7 tok/s |

- All four runs: `saw[DONE] = True`, `error = None`, **`cached_tokens = 0`**
  → fully **cold prefill with no prefix-cache benefit**, i.e. worst-case numbers.
- Prefill throughput decays 1589.8 → 1512.5 → 1417.4 tok/s, **−10.8%**, consistent with linear attention cost over long sequences. No cliff.
- Decode holds at 63.7–70.2 tok/s, essentially identical to the 32k tier.

```
                          TTFT vs context length
  200s ┤                                      ●  177.8s @ 251,809 tok
       │
  150s ┤
       │
  100s ┤              ●   85.9s @ 129,850 tok
       │
   50s ┤
       │   ●  20.3s @ 32,272 tok
    0s └────┬──────────┬──────────┬──────────┬──
            0         64K        128K       256K
```

---

## 3. Long-context retrieval probes

Three secret tokens are buried at **2% / 50% / 97%** of the filler text; the question sits at the very end and asks for exactly three tokens.

| Tier | Needle lines (of N) | Lapis-37 | Amber-91 | **Indigo-52 (97% remote)** |
|---|---|---|---|---|
| 32k | 25 / 616 / 1195 (1,232 lines) | ✅ | ✅ | ✅ |
| 128k | 97 / 2423 / 4701 (4,846 lines) | ✅ | ✅ | ✅ |
| 256k | 187 / 4682 / **9082** (9,363 lines) | ✅ | ✅ | ✅ |

Actual 256k rerun answer (20 characters, verbatim):

```
青金石-三七, 琥珀-九一, 靛蓝-五二
```

→ Attention over the full 251K tokens — including the 97% mark — shows **no decay**. Remote retrieval works.

> [!IMPORTANT]
> **This section was wrong the first time, and that lesson is worth keeping.**
> The original `build_prompt()` computed needle positions from a *budget-estimated* line count, but the filler loop had an early `total >= limit` break, so the real line count was always lower than the estimate → the 97% needle's line number fell out of range and **was never actually written into the prompt**.
> Three runs confirmed `'靛蓝-五二' in prompt: False`; only the first two needles had been genuinely verified.
> Fix: render once with no needles to measure the real line count, place needles against that, and assert that every needle appears in the prompt — fail loudly instead of judging a model on a needle it never saw.

---

## 4. Resource usage

| Metric | Before 32k | After 256k | Note |
|---|---|---|---|
| `commit_used` | 65,822 MiB | **68,452 MiB** | +2.6 GiB only |
| `commit_limit` | 98,214 MiB | **98,214 MiB** | **Constant** — the pagefile never grew dynamically |
| Free physical RAM | 14,619 MiB | **12,008 MiB** | Still 12 GiB headroom |
| Engine process WS | 39,042 MiB | 41,782 MiB | +2.7 GiB only |
| Proxy WS | 29 MiB | 29 MiB | Constant |

**KV streaming peak** (256k run):

```
98.57% of 11,128,560 block reads hit VRAM, 636.9 MiB read from RAM
```

→ Only **636.9 MiB** of KV actually spilled to host memory. The `--kv-resident 32768` window scheme — keep 32768 of 262144 cells per QSA layer in VRAM, park the rest in pinned RAM — holds up at 256K.

**No OOM, no VRAM warnings.**

### expert cache vs. VRAM budget

| `--vram-reserve-mib` | expert cache | VRAM free after load | verify window | Warning |
|---|---|---|---|---|
| **1728 (recommended)** | **1394 slots / 1.70 GiB** | 1217 MiB | **1123–1213 MiB** | none |
| 700 (default) | 2220 slots / 2.70 GiB | 387 MiB | 346 MiB | `WARNING: only 400 MiB of VRAM free (#2065)` |

`vram-reserve-mib` semantics: **the larger the reserve, the less VRAM the expert cache gets.** Engine source, `serve/server.py:8237`, `desktop_vram_note()`: `the expert cache gets {...} GB less, a few % of speed`.

→ 1728 trades 1.0 GiB of expert cache for more than 3× the verify headroom. **On a 12 GB card that is the right trade.**

---

## 5. Calibrate chars/token against your own template

| Calibration source | Ratio | Consequence |
|---|---|---|
| The real stress-test template (2,028 chars) | **1.397** | Two runs both returned `prompt_tokens = 1452`; stable |
| An unrelated Chinese sample × 60 | 2.021 | Generated 66,241 chars → actually sent **46,798 tokens**; overshot |

→ Character-per-token for Chinese depends heavily on text structure (digits, punctuation, English terms). **Calibrate with your own prompt template**; do not reuse rule-of-thumb values.

---

## 6. Three false alarms, so you do not chase them

### False alarm 1: "client disconnected early" in the proxy log

The harness stops reading once it hits `max_tokens` and breaks the stream. The proxy logs a disconnect; the server returned **200** every time. Not a crash.

### False alarm 2: a 99.99%-reused follow-up request after each tier

```
prompt 131404 tokens = 131385 reused + 19 read in 199 ms
prompt 253363 tokens = 253345 reused + 18 read in 182 ms
```

This is a follow-up the proxy or the compression layer appends. **A 99.99% reuse rate proves the prefix cache is healthy** — the stress test itself simply takes the cold path.

### False truncation: output pinned at the ceiling

One run produced `output = 8176 / 8192`, 16 tokens short of the limit. The tail was a natural ending, but a pinned output is one long task away from a genuine `length` finish being misread as a truncation regression.
→ Raise `maxTokens` **before** long-output stress tests (this project went 8192 → 16384). Do not experiment at the edge of the ceiling.

---

## 7. Where to read the truth from

**bili's RES dump stores only response headers for streaming responses** — 318 bytes, ending at `content-type: text/event-stream`. Neither `finish_reason` nor `[DONE]` is in it; a full-text grep of bili.log for `finish_reason` returns zero hits.

So `stop` vs `length` can only be verified two ways:

1. **Hit the proxy directly and read the SSE** (recommended)
   - Normal end → `"finish_reason": "stop"` + `data: [DONE]`
   - `max_tokens: 16` forced cut → `"finish_reason": "length"` + `data: [DONE]`
2. **Read `turn/end.reason.kind`** in the DSH session record

For performance numbers, **the cleanest single source is this engine log line**:

```
strata serve: prompt 251809 tokens = 0 reused + 251809 read in 177656 ms (1417.4 tok/s),
               1537 generated in 21907 ms (70.2 tok/s), drafts accepted 898 of 1133, 6 checkpoints
```

It gives you everything at once: real prompt token count, prefill time and throughput, decode throughput, speculative-decoding acceptance.

> Aside: `/v1/tokenize` **does not exist** (404 `{"error": {"message": "not found"}}`). Counting tokens means sending a request and reading `usage`.

---

## 8. Reproduction

```bash
# 1. Start the engine (an existing pack skips preparation; cold start 29-42 s)
"C:\Strata\App\control\bin\strata.cmd" launch --family gyro --model Gyro-M \
  --model-path "C:\Strata\Models\gyro-m-abliterated\Qwen3.8-Flash-Next-Gyro-M-TQ2_0.gguf" \
  --data-dir "C:\Strata\Models" --context 256k --port 8081 --no-tui --yes \
  --set vram-reserve-mib=1728

# 2. Confirm the proxy is listening
curl --noproxy '*' http://127.0.0.1:8090/health

# 3. Calibrate chars/token with YOUR template, not a sample
# 4. Tiered run: 32k -> 128k -> 253952
```

**253952 = 262144 − 8192** — the classic ceiling that leaves room for output.

---

## 9. Misreadings this report corrects

| # | Common misreading | Measured truth |
|---|---|---|
| 1 | Cold start is a fixed 42 s | It is a **29–42 s range**, dominated by reading 92 GB of weights (`loaded 29.88 GiB at 5.97 GiB/s`), nearly independent of context size |
| 2 | `--vram-reserve-mib 1728` → 2220 slots / 2.70 GiB | **Backwards.** 2220 slots / 2.70 GiB belongs to **reserve=700**; 1728 is **1394 slots / 1.70 GiB** |
| 3 | The 1728 verify window is 346 MiB with a warning | **Misattributed.** 346/387/452 MiB and the `#2065` warning belong to reserve=700; 1728 is **1123–1213 MiB, no warning** |
| 4 | KV hit rate is 99.5% | Final value is **99.70%** (24,647,988 block reads, only 296.0 MiB from RAM); 99.5% was a midpoint |
| 5 | `engine_ready_s: 1076` is the measured cold start | It is a **formula-derived wait budget**: `max(600, 600 + (gb − 40) × 8) + 60`; 92 GB → 1076. Actual start takes 29–42 s |

---

## 10. Three transferable lessons

1. **One engine log file can span several starts.** Re-attribute numbers by `expert cache auto` / `serve: N MiB of VRAM free` / `verify` segment before quoting them, or you will mix two runs together.
2. **When building long-context probes, place needles by the line count actually written**, not the target count — and assert every needle is present before sending the request.
3. **Take measurements from the layer that can return the truth.** bili stores only headers, so do not look for `finish_reason` there; the engine log and the proxy SSE are the sources of truth.

---

<div align="center">

*251K tokens in, first word 178 seconds later, not a single OOM in between.*

</div>
