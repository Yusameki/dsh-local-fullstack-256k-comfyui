<div align="center">

# DSH Local FullStack · 256K + ComfyUI

**English** | [简体中文](README_zh.md)

**Turning a consumer machine with 64 GB of RAM and 12 GB of VRAM into a local Agent workstation with a 256K context window, a 2.1K tool budget, persistent memory, on-demand engine start/stop, and a time-shared ComfyUI image backend**

`DeepSeek Harness` · `Strata` · `billion-context` · `Qwen3.8-Flash-Next` · `ComfyUI`

</div>

---

> [!IMPORTANT]
> ## 🔴 VRAM time-sharing red line
>
> **Coexist for light work, go exclusive for long context.**
>
> - Reading docs, writing prompts, joint debugging → **run both**. Decode drops from 68.2 to 54.3 tok/s. Plenty.
> - Huge codebase or hundreds of thousands of words (> 10K tokens) → **manually kill the ComfyUI process first**, so the engine gets all 1394 expert-cache slots.
> - **`POST /free` is not enough.** It releases the model weights (8435 → 1811 MiB measured) but the remaining ~1.8 GB CUDA context cannot be released while the process lives.
>
> Measured on the same ≈32K-token prompt: expert cache **1394 → 894 (−35.9%)**, prefill **20,299 → 37,780 ms (+86%)**, decode 68.2 → 54.3 tok/s. No OOM, no warnings — the engine just shrinks itself. Full data: [`docs/64g-256k-comfyui-workstation.md`](docs/64g-256k-comfyui-workstation.md).

---

> [!WARNING]
> **This project contains local absolute paths, ports, and model-name configuration. Adjust them to your own environment before deploying.**
> Every `C:\Users\YOUR_USERNAME`, `STRATA_HOME`, and `YOUR_HOSTNAME` in this repository is a placeholder, not a runnable value.
> All configuration snippets are **excerpts**. Merge them into your own config files; do not overwrite whole files.


---

## What this project solves

Running a local LLM as an Agent usually gets stuck in four places:

| Symptom | Real cause |
|---|---|
| The model cannot remember something from three minutes ago | The context window is only 32K; any long document hits the wall |
| Every request burns 11K tokens on tool definitions alone | The default preset ships 65 tools — 15.2K tokens of schema |
| Switching sessions loses who you are | No cross-session persistent memory |
| VRAM is pinned by a resident model, so nothing else fits | The engine never leaves once it starts |
| You want to generate images on the same GPU | Two processes fighting over 12 GB of VRAM |

This repository is a complete answer to all five, plus a watchdog script that **never touches the engine**.

---

## Full pipeline

```
┌──────────────────────────────────────────────────────────────────────┐
│  DSH Desktop (GUI)                                                    │
│  http://127.0.0.1:RANDOM_PORT   changes on every restart              │
│                                                                       │
│  ┌────────────────────────────────────────────────────────────────┐  │
│  │  Agent preset layer                                              │  │
│  │                                                                  │  │
│  │   maid (default, 65 tools / 15.2K tokens)                        │  │
│  │   maid-local-lite    (10 tools / 2.1K)  ← slim presets only      │  │
│  │   maid-local-memory  (13 tools / 2.9K)  ← slim + memory ★        │  │
│  │                                                                  │  │
│  │   Slimming uses tools.restrict({deny}) + a self-healing state    │  │
│  │   machine — not preset-tree swapping                             │  │
│  └────────────────────────────────────────────────────────────────┘  │
│                              │                                        │
│                              │ OpenAI-compatible /v1                  │
│                              ▼                                        │
│  ┌────────────────────────────────────────────────────────────────┐  │
│  │  bili (billion-context)   admin endpoint 127.0.0.1:18788        │  │
│  │  · Compression: maxContextLimit = 90% (fires at ~118K of 131072)│  │
│  │  · Model window: models.MODEL_ID.context = 131072               │  │
│  │  · Context inspection / folding / injection-block management    │  │
│  └────────────────────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────────────────────┘
                               │
                               │ baseURL http://127.0.0.1:8090/v1
                               ▼
┌──────────────────────────────────────────────────────────────────────┐
│  Lazy-loading proxy  proxy/strata-on-demand-proxy.py  :8090          │
│  · First request → spawns the engine detached → forwards once ready   │
│  · Emits SSE comment lines while waiting (client never times out)     │
│  · Idle 900s (15 min) → `strata launch --stop`, engine unloaded      │
└──────────────────────────────────────────────────────────────────────┘
                               │
                               │ http://127.0.0.1:8081
                               ▼
┌──────────────────────────────────────────────────────────────────────┐
│  Strata Engine  gyro-m-256k                                          │
│  args: --max-context 262144 --kv int8 --kv-resident 32768            │
│        --spec 4 --vision --vram-reserve-mib 1728                     │
│                                                                       │
│  Model: Qwen3.8-Flash-Next-Gyro-M (abliterated), 92 GB               │
│  Resident: 29.88 GiB weights + 3.09 GiB KV in pinned RAM             │
│  expert cache: 1394 slots / 1.70 GiB (reserve 1728)                  │
│  Cold start: 29~42 s   ·   prefill 1417~1590 tok/s                   │
└──────────────────────────────────────────────────────────────────────┘

┌──────────────────────────────────────────────────────────────────────┐
│  ComfyUI  :8188   (runs alongside, VRAM is time-shared)              │
│  · model library via junction onto the PCIe 4.0 drive                 │
│  · 8 GB VRAM while generating; POST /free releases weights            │
│  · ~1.8 GB CUDA context stays until the process exits                 │
└──────────────────────────────────────────────────────────────────────┘
```

**The key idea: control plane and data plane are fully decoupled.**
The watchdog script watches only 8090 (the proxy process) and never touches 8081 (the engine). The engine is started by the proxy when a request arrives and unloaded by the proxy's idle timer when it is no longer needed. Nothing is allowed to pin the engine in VRAM just to "stay online".

**The second idea: VRAM is time-shared, not fought over.** ComfyUI and the engine can coexist on a 12 GB card — the engine simply shrinks its expert cache from 1394 to 894 slots and prefill gets 86% slower. For light work that is a fine trade; for long context you stop one of them.

---

## The five pillars

### 1. 256K context

The stack started at 128K and was raised to 262144. What the current configuration actually looks like:

| Layer | Location | Value |
|---|---|---|
| Engine | `STRATA_LAUNCH_ARGS` in `proxy/strata-on-demand-proxy.py` | `"--context", "256k"` |
| Engine profile | `%LOCALAPPDATA%\Strata\launch\gyro-m-256k.json` | `--max-context 262144` |
| DSH | `llm-pi-ai` in `cordis.patch.yml` | `defaultContextWindow: 262144` / `contextWindow: 262144` |
| bili | `billion-context.json` | `context: 262144`, `output: 16384` |
| bili compression threshold | same file | `maxContextLimit: "90%"` |

- **Order matters when changing parameters**: stop the engine first, then stop the proxy process, then edit the files. The proxy probes for an existing instance and will happily reuse a stale one — editing files alone changes nothing.
- Changing `--context` makes Strata create a new profile file (`gyro-m-256k`); the old one is kept for rollback.
- The practical ceiling is **253952 = 262144 − 8192** (window minus output headroom), not 262144.

#### Taking it to 256K

The same stack runs at 262144 once the pagefile is fixed at 32 GB and the launch args are raised. Measured on the reference machine: **251,813-token cold prefill in 184 s (1368 tok/s), 63.7 tok/s decode, `finish_reason: stop`, no OOM**, and all three recall probes recovered — including the one buried at 97% of the text.

| Change | From | To |
|---|---|---|
| pagefile | 4 GB (system managed) | **32768 MB fixed** → commit limit 67.91 → 95.91 GiB |
| `--context` | `128k` | `256k` |
| `STARTUP_TIMEOUT_SECONDS` | 600 | **1200** (a 92 GB model needs more than 10 min of headroom) |
| `defaultContextWindow` / `contextWindow` | 131072 | **262144** |
| bili `context` | 131072 | **262144** |
| `defaultMaxTokens` / `output` | 8192 | **16384** (only if you actually emit long answers) |

Practical ceiling is **253952 = 262144 − 8192**, not 262144. Full report, resource tables and the four false alarms you will hit: [`docs/gyro-m-256k-benchmark.md`](docs/gyro-m-256k-benchmark.md) ([English](docs/gyro-m-256k-benchmark.en.md)). Reproduce with `tools/stress-longctx.py`.

The whole 64 GB + 256K + ComfyUI story — pagefile sizing, drive allocation, VRAM time-sharing — is in [`docs/64g-256k-comfyui-workstation.md`](docs/64g-256k-comfyui-workstation.md) ([English](docs/64g-256k-comfyui-workstation.en.md)).

#### The commit limit is the real ceiling

The model needs 29.88 GiB resident plus 3.09 GiB of pinned KV. A system-managed pagefile (≈4 GB) on 64 GB of RAM gives a commit limit of only 67.91 GiB, and loading dies with exit code `0xC0000409` — which looks like a stack overflow but is commit exhaustion.

Fix: **fix the pagefile at 32768 MB** (System Properties → Advanced → Performance → Advanced → Virtual Memory → Custom size, both fields 32768 MB, reboot). That lifts the commit limit to **95.91 GiB**, and the engine log then prints:

```
strata serve: Windows commit: 35.4 GiB left of a 95.9 GiB limit (RAM + page file)
```

#### `--vram-reserve-mib` is inverse to the expert cache

| `--vram-reserve-mib` | expert cache | VRAM free after load | verify window |
|---|---|---|---|
| **1728 (recommended)** | **1394 slots / 1.70 GiB** | 1217 MiB | **1123–1213 MiB**, no warning |
| 700 (default) | 2220 slots / 2.70 GiB | 387 MiB | 346 MiB → `WARNING ... (#2065)` |

Larger reserve → smaller cache. The engine source puts it plainly (`serve/server.py:8237`): `the expert cache gets {...} GB less, a few % of speed`. 1728 trades 1.0 GiB of cache for more than 3× the verify headroom, which is the right call on a 12 GB card.

### 2. 2.1K tool budget

The default preset ships 65 tools / 15.2K tokens. The slim preset keeps only 10 tools / 2.1K.

```
maid-local-lite  KEEP (10):
  compress  edit  glob  grep  ops_bash  ops_discover_tools
  read  read_image  working_directory  write

maid-local-memory  KEEP (12 → 13 at runtime):
  the 10 above + memory_remember  memory_search  memory_read
```

**Why `tools.restrict({deny})` instead of swapping the preset tree:**
Creating a session in the GUI is a two-step flow — `session/create` (default preset is `maid`), then `agentPresets/select`. `recompose()` only rebinds the generation; it does not re-emit `agent/created`. The plugin still sees `maid` at creation time, returns early, and the deny layer is never built at all. The result is "looks slimmed, but all 45 tools are still there".

The self-healing state machine (both presets share one implementation):

| Component | Purpose |
|---|---|
| `wantState()` dual criteria | `composedPreset` wins; fall back to the session projection only when it is `undefined` |
| `syncingAgents` (WeakSet) | Per-agent reentrancy lock against recursive storms; not a global lock, so concurrent agents do not starve each other |
| Three maps: `seenMap` / `handlesMap` / `appliedMap` | `seen` (already-denied names) and `handles` (disposers to undo) are kept apart; releasing never clears `seen` |
| `live` Set + `alive()` | Agent scope keys are never rebuilt, so the deny layer outlives preset switches and must be disposed explicitly |

`agent/pre-step` calls `guarded()` **unconditionally** (no `synced`/`dirty` throttling) — throttling means the release for "switch back to the default preset" never fires.

### 3. Persistent memory

The backing store is the meow submodule bundled inside `dsh-maid-preset` (not a standalone package). It registers on the host global layer, which is why `restrict` can trim it.

Five pipelines:

```
(1) First-turn injection   agent/pre-step where priorUser === 0
                           → buildInjection → snapshot message inserted
                             before the first user message
                           (soul + user + global rules + project list)

(2) Explicit read/write    memory_remember / memory_search / memory_read
                           (the full version also has update / find_similar /
                            project / home / dream)

(3) Auto reflection        agent/turn-stopping: consecutive tool steps >=
                           reflectTurns and the last tool is not memory_*
                           → emits a [meow-memory-reflect] turn, runs
                             prompts/zh/reflect.md

(4) dream                  scheduleDream: fires after 180 min idle,
                           suppressed during peak hours

(5) Post-compaction re-injection
                           compaction/end → mark → re-inject on the next
                           pre-step
```

Storage: SQLite at `.dsh-meow/memory.db` (seven tables: soul / user / project / fact / lesson / topic / rules). Not a vector store.

**The token trade-off**: only three memory tools are installed (`remember` 428 + `search` 307 + `read` 69 = **804 tokens**). Deliberately dropped: `memory_update` (222) / `memory_find_similar` (131) / `memory_project` (122) / `memory_home` (131) / `memory_dream` (85) — 691 tokens in total.

The cost is that the reflection turn cannot rewrite an existing conclusion with `memory_update`. Compensation: five rules in `AGENTS.md` — search for duplicates first → if something close exists, write the new information into `content` prefixed with `★update` and call `remember` again (auto-merge kicks in at 0.8 similarity, which behaves like a soft update) → create a new entry only when nothing is close → never pad with noise → say "needs memory_project" when a full project view is required.

> A 60-token rule patch buys back 222 tokens of tool budget. Worth it.

### 4. On-demand engine start/stop

```
DSH sends a request
   ↓
Proxy on 8090: is the engine running?
   ├─ No  → spawn `strata launch` detached (profile gyro-m-256k)
   │        → wait for ready (1200s cap), emitting SSE comments meanwhile
   └─ Yes → forward directly
   ↓
Engine on 8081 handles it
   ↓
Request ends, last_activity reset
   ↓
idle_watcher ticks every 30s: idle >= 900s?
   ├─ Yes → claim the unload slot → `strata launch --stop` → VRAM freed
   └─ No  → keep sleeping
```

The watchdog `proxy/guard-proxy.vbs` probes only 8090 and silently re-runs `start-proxy.vbs` when it is down. It knows nothing about 8081 or the engine, and it will never "check whether the engine is still alive, just in case".

Measured:

| Scenario | Result |
|---|---|
| Kill the proxy process on 8090 | Watchdog restarts it within **24 seconds** |
| Port 8081 at the moment of self-healing | **Still empty** — proof the engine was untouched |
| After sending a message | Engine cold-starts on demand; 13 tools / 2982 tokens as expected |
| After going idle | Unloaded at about 16 minutes (900s threshold + 30s tick) |
| After unload | `strata status` → `{"running": []}`, VRAM back to **997 MiB / 12288 MiB** |

### 5. ComfyUI on the same GPU

The image backend (ComfyUI on 8188) lives on the same 12 GB card. Two details make it workable:

**Model library relocation.** 66 GB of models sitting on the system drive is a waste. They were copied to the second PCIe 4.0 drive with `robocopy /E /MT:4` (2 min 17 s, 690 MB/s), the source was renamed to `models.old` as a rollback point, and a **junction** (`mklink /J`) was created in place. A junction is filesystem-level redirection, so no plugin, no `os.walk`, and no string-concatenated path can tell the difference — unlike `extra_model_paths.yaml`, which only patches the folder registry and misses 6 places that concatenate `folder_paths.models_dir` directly.

> [!CAUTION]
> **Two red lines when using junctions.** Never `rm -rf` / `shutil.rmtree` a junction — it follows the link and deletes the **target's real data**. Never `robocopy /MIR` against the junction source — mirror semantics write backwards and wipe the target. Remove a junction only with `rmdir` (no `/s`) or `Remove-Item` (no `-Recurse`).

**VRAM time-sharing.** `/free` releases model weights (8435 → 1811 MiB measured) but leaves the ~1.8 GB CUDA context until the process exits. So:

| Task | Action |
|---|---|
| Short exchanges, prompt writing, joint debugging | **Run both.** Decode drops 68.2 → 54.3 tok/s |
| Long context (> 10K tokens) | **Kill ComfyUI first.** Gets back all 1394 cache slots |

Measured cost of coexisting on the same ≈32K-token prompt: prefill **20,299 → 37,780 ms (+86%)**, expert cache 1394 → 894. No OOM — the engine shrinks itself. Full numbers in [`docs/64g-256k-comfyui-workstation.md`](docs/64g-256k-comfyui-workstation.md).

Note that `/free` **is not synchronous**: it only sets a flag on the prompt queue, and the main loop applies it on the next pass (`main.py:401-408`). Any helper script must poll `nvidia-smi` rather than declaring victory right after the POST.

---

## Known pitfalls

### Pitfall 1: DSH under-reports tokens by 1.9–2.4×

| Same request | DSH `contextTimeline` | bili actual `[acp-usage] input=` |
|---|---|---|
| 124K Chinese chars | 37727 | 89323 |
| 209K Chinese chars | 76343 | 145986 |

DSH estimates by character count, which fragments Chinese far more than tokens. **To know whether you will hit the window, read the bili log — not the DSH number.** Roughly 64000 by DSH's count is where the real wall sits.

### Pitfall 2: Over-long input fails with 400, not 413

```
request exceeds the context window: prompt (145986 tokens)
leaves no room to answer in the context (131072); requests are never truncated
```

And bili's preflight compression is slow (187 seconds measured), so the client's 300s timeout cuts it off first — the net effect is "compression never finished, request rejected". That is why the threshold moved from 65% to 90%, pushing the trigger from ~80K tokens to ~118K so ordinary long documents take the fast path.

### Pitfall 3: Compression summaries get source numbers wrong

Measured: "lapis-37" came back as "crimson-17". So **very long contexts are unsuitable for exact-recall tasks** — once compression happens, the details are not trustworthy.

### Pitfall 4: VRAM headroom warnings

```
only 865 MiB of VRAM free at the start of this request (below 1024 MiB) ...
decode falls to a few tok/s and a window may never finish
```

`vram-reserve-mib` was raised from 1536 to **1728**. That is the floor for "does not hang", not a speed guarantee.

### Pitfall 5: The Windows commit limit is the real ceiling

```
resident complement 20.88 GiB exceeds available commit (9.22 GiB) minus the 4 GiB safety headroom
Windows commit: 4.2 GiB left of a 35.3 GiB limit
about 121.3 GB read from the drive per 32768-token prompt
conversation cache off: not enough free RAM
```

32 GB of physical RAM with the default page file yields a 35.3 GiB commit limit. **This machine is now at 64 GB with a fixed 32768 MB pagefile: commit limit 95.91 GiB, and the same log line reads `35.4 GiB left of a 95.9 GiB limit`. 256K no longer touches this wall.**

The conclusion still holds: running more comfortably means more RAM or a bigger page file — not a code change. If loading dies midway with exit code `0xC0000409`, check the commit limit before suspecting a stack overflow.

### Pitfall 6: `engine_usable: false` is a false alarm

`strata launch --plan` cannot read GPU information from some shell hosts and reports "did not pass Strata's check on this computer". The engine runs fine. Ignore it.

### Pitfall 7: The 8090 proxy does not come back after a DSH restart

Symptom: after sending a message the model spins for two minutes with no output.
Fix: run `start-proxy.vbs` once. This is exactly what `guard-proxy.vbs` exists to solve.

### Pitfall 8: The bili admin endpoint is not on the DSH port

`curl http://127.0.0.1:DSH_PORT/__bili/config` → `{"error":"admin relay disabled"}` (`security.adminRelay` defaults to off).
The correct address is **18788**: `curl http://127.0.0.1:18788/__bili/config`.

### Pitfall 9: `findstr` exit codes are unreliable for port checks in VBS

The nested quotes in `cmd /c "... | findstr ... >nul && exit 0 || exit 1"` get swallowed at the `cmd /c` layer, so an empty port can still return 0. The reliable approach is to dump `netstat` output to a temp file and check each line with `InStr`.

---

## Case study: retiring the legacy Ollama stack

Before this stack existed, the machine ran a 9B model on Ollama as an emergency fallback. Once the 128K Strata pipeline took over, every byte of it became dead weight. Here is the whole teardown, because "delete the old thing" is never as simple as it sounds.

**19.48 GiB reclaimed.** The assets were not where you would expect:

| Location | Size | What it was |
|---|---|---|
| `~/.ollama` | 7.44 GiB | 3 tags, all sharing one 7.36 GB weight blob + 624 MB mmproj |
| `<DSH_HOME>/our-free-model/_deploy` | 8.98 GiB | A **second copy** of the same GGUF (not a hardlink) + 1.6 GB installer |
| `%LOCALAPPDATA%/Programs/Ollama` | 3.06 GiB | The application itself |

The `_deploy` copy is the one that hides. It came from a manual `ollama create`, and because it is a plain copy rather than a hardlink, deleting the Ollama models alone would have left 7.5 GiB behind.

**The dependency check comes first.** Before deleting anything, verify nothing still points at the port:

```bash
grep -rn "11434" ~/.dsh/profiles/*/cordis.patch.yml
curl -s http://127.0.0.1:18788/__bili/config | grep 11434
```

In this case the DSH profile was clean, but two layers still referenced it: `our-free-model`'s `settings.json` (an `aggregate.providers.ollama` entry plus a fallback combo that made the 9B show up in the model catalog) and bili's config (three routes for `localhost`, `127.0.0.1`, and `mitm://`). **Clearing the files but leaving the routes would have kept the model visible in the picker and kept bili trying to compress against a window that no longer exists.**

**Teardown order** — `tools/cleanup-ollama-9b.py` automates this:

```
--backup   →  *.bak-<timestamp> for every file about to change
--config   →  drop the aggregate provider, the fallback combo, the 11434 routes
--stop     →  kill processes, run the uninstaller silently
--path     →  remove the PATH entry, the Startup shortcut, any OLLAMA_* vars
--archive  →  move one-off patch dirs and debug scripts out of the workspace
--purge    →  rmtree the caches (requires typing PURGE)
```

Two rules the script enforces that are worth stealing:

1. **It refuses to write the bili config if the 8090 provider has vanished.** A cleanup script that quietly deletes your production route is worse than no script.
2. **`--purge` demands the word `PURGE`.** Everything else runs from flags; the irreversible step does not.

**Verification after teardown:**

```bash
curl -s http://127.0.0.1:18788/__bili/config    # expect exactly 2 providers, 8090 with context 131072
ls ~/.ollama ~/.dsh/our-free-model/_deploy      # both gone
```

Note that `settings.json` for `our-free-model` is **not** hot-reloaded — the plugin reads it at apply time. If the 9B still appears in the model catalog after teardown, restart DSH once. bili *is* hot-reloaded: `POST http://127.0.0.1:18788/__bili/config/reload`.

**One thing deliberately left alone:** the `ofm-effort-menu-patch` directory was archived, but the patch inside it was **not** rolled back. It had already been applied to the plugin source and is harmless on its own; reverting it would have introduced a fresh variable into a system that was working. Archive the tooling, keep the running state.

---

## Real usage red lines

Based on bili's actual `[acp-usage] input=`:

| Input size | Actual tokens | Behaviour |
|---|---|---|
| < 50K Chinese chars | < 40K | Fast |
| 50K–80K Chinese chars | 40K–80K | Normal |
| 80K–110K Chinese chars | 80K–110K | Slower |
| **< 200K Chinese chars** | **< 245K** | **Works at 256K** — 251,813 measured cold prefill in 184 s |
| **> 245K tokens** | **Wall** | Fails with **400**, not 413 |

The old "wall at 120K tokens" line belonged to the 128K configuration. At 262144 the practical ceiling is **253952 = 262144 − 8192**.

---

## Repository layout

```
.
├── README.md
├── README_zh.md                        简体中文版
├── ARCHITECTURE.md                     层间关系、硬件画像、显存/内存预算、盘位分配
├── AGENTS.md                           Workspace memory-writing rules (pairs with the three memory tools)
├── .gitignore
├── config/
│   ├── cordis.patch.example.yml        DSH profile config excerpt (model + meow-memory)
│   └── billion-context.example.json    bili compression threshold and model window
├── docs/
│   ├── 64g-256k-comfyui-workstation.md      64G + 256K + ComfyUI 终极工作站（中）
│   ├── 64g-256k-comfyui-workstation.en.md   same report, English
│   ├── gyro-m-256k-benchmark.md        256K long-context benchmark (Simplified Chinese)
│   └── gyro-m-256k-benchmark.en.md     the same report in English
├── plugins/
│   ├── maid-local-lite/                Slim preset (10 tools)
│   │   ├── package.json
│   │   ├── cordis.patch.yml
│   │   └── lib/index.js
│   └── maid-local-memory/              Slim + memory preset (13 tools)
│       ├── package.json
│       ├── cordis.patch.yml
│       └── lib/index.js
├── proxy/
│   ├── strata-on-demand-proxy.py      8090 lazy-loading proxy (stdlib only)
│   ├── start-proxy.vbs                Silent launcher (pythonw, no console)
│   └── guard-proxy.vbs                Watchdog (probes 8090 only)
└── tools/
    ├── cleanup-ollama-9b.py           Teardown for a legacy Ollama fallback (dry-run by default)
    └── stress-longctx.py              Tiered long-context stress test (cold prefill, probes, finish_reason)
```

---

## Deployment

1. **Install the plugins**: copy `plugins/maid-local-lite` and `plugins/maid-local-memory` into your DSH workspace, point at them with `link:` in the profile's `package.json`, and add both package names to `dsh.bundle`.
2. **Merge the config**: merge the `llm-pi-ai` and `meow-memory` blocks from `config/cordis.patch.example.yml` into your own `~/.dsh/profiles/PROFILE/cordis.patch.yml`; merge `config/billion-context.example.json` into `~/.config/billion-context/billion-context.json`.
3. **Fix the paths**: replace `STRATA_HOME` in the three files under `proxy/` with your actual install path.
4. **Start the proxy**: double-click it or run `wscript.exe proxy/start-proxy.vbs`, then confirm 8090 is listening.
5. **Install the watchdog**: drop a shortcut to `guard-proxy.vbs` into `shell:startup` (target `wscript.exe`, argument is the full vbs path, working directory set to the proxy folder, window minimized).
6. **Restart DSH** and pick `maid-local-memory` for a new session.

---

## Acceptance baseline

| Item | Value |
|---|---|
| Default `maid` preset | 65 tools / 15162 tokens |
| `maid-local-lite` (header path) | 10 / 2142 |
| `maid-local-lite` (GUI two-step) | 12 / 2860 |
| `maid-local-memory` (both paths) | 13 / 2982 |
| Long input N=2500 | bili input 89323, all three canaries correct |
| Long input N=2300 (after 90% threshold) | bili input 83163, zero preflight, all three canaries correct |
| Long input N=4200 | bili input 145986, 400 — window exceeded |
| **256K cold prefill (251,813 tok)** | **184 s / 1368 tok/s, decode 63.7 tok/s, `finish_reason: stop`, all three probes found, no OOM** |
| **256K peak memory** | commit 69,045 MiB of a 98,214 MiB limit; only 636.9 MiB of KV spilled to RAM (98.57% hit) |
| **Tiered: 32k / 128k / 256k prefill** | 1589.8 / 1512.5 / 1417.4 tok/s, decode 68.2 / 66.9 / 70.2 tok/s, all `finish_reason: stop` |
| **ComfyUI models migrated** | 51 files / 65.22 GiB, robocopy 2 min 17 s @ 690 MB/s, junction verified both directions |
| **ComfyUI + engine coexisting** | expert cache 1394 → 894, prefill 20,299 → 37,780 ms, decode 68.2 → 54.3 tok/s, no OOM |

---

## License

MIT. Configuration snippets and scripts are provided as-is, with no guarantee of fitting any particular machine.

---

<div align="center">

*Control plane and data plane, separated. The proxy minds the proxy, the engine minds the engine. VRAM is supposed to be free when nothing is running — and time-shared, not fought over, when two things run.*

</div>
