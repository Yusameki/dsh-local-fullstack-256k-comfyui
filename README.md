<div align="center">

# DSH Local FullStack · 128K

**English** | [简体中文](README_zh.md)

**Turning a consumer machine with 32 GB of RAM and 12 GB of VRAM into a local Agent workstation with a 128K context window, a 2.1K tool budget, persistent memory, and on-demand engine start/stop**

`DeepSeek Harness` · `Strata` · `billion-context` · `Qwen3.8-Flash-Next`

</div>

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

This repository is a complete answer to all four, plus a watchdog script that **never touches the engine**.

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
│  Strata Engine  gyro-s-128k                                          │
│  args: --max-context 131072 --kv int8 --kv-resident 32768            │
│        --resident-experts --vision --vram-reserve-mib 1728           │
│                                                                       │
│  Model: Qwen3.8-Flash-Next-Gyro-S, Q4 quantized, 58 GB               │
│  VRAM: about 1 GB left after loading on a 12 GB card                 │
│  RAM: resident experts capped by the Windows commit limit (35.3 GiB) │
└──────────────────────────────────────────────────────────────────────┘
```

**The key idea: control plane and data plane are fully decoupled.**
The watchdog script watches only 8090 (the proxy process) and never touches 8081 (the engine). The engine is started by the proxy when a request arrives and unloaded by the proxy's idle timer when it is no longer needed. Nothing is allowed to pin the engine in VRAM just to "stay online".

---

## The four pillars

### 1. 128K context

| Layer | Location | Value |
|---|---|---|
| Engine | `STRATA_LAUNCH_ARGS` in `proxy/strata-on-demand-proxy.py` | `"--context", "128k"` |
| Engine profile | `%LOCALAPPDATA%\Strata\launch\gyro-s-128k.json` | `--max-context 131072` |
| DSH | `llm-pi-ai` in `cordis.patch.yml` | `defaultContextWindow: 131072` / `contextWindow: 131072` |
| bili | `billion-context.json` | `context: 131072`, `output: 8192` |
| bili compression threshold | same file | `maxContextLimit: "90%"` |

- `maxTokens` / `output` stay at **8192** throughout — the output side does not need to grow with the window.
- Changing `--context` makes Strata create a new profile file (`gyro-s-32k` → `gyro-s-128k`); the old file is kept for rollback.
- **Order matters when changing parameters**: stop the engine first, then stop the proxy process, then edit the files. The proxy probes for an existing instance and will happily reuse a stale one — editing files alone changes nothing.

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
   ├─ No  → spawn `strata launch` detached (profile gyro-s-128k)
   │        → wait for ready (240s cap), emitting SSE comments meanwhile
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

32 GB of physical RAM with the default page file yields a 35.3 GiB commit limit. Running more comfortably means more RAM or a bigger page file — not a code change.

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

## Real usage red lines

Based on bili's actual `[acp-usage] input=`:

| Input size | Actual tokens | Behaviour |
|---|---|---|
| < 50K Chinese chars | < 40K | Fast |
| 50K–80K Chinese chars | 40K–80K | Normal |
| 80K–110K Chinese chars | 80K–110K | Slower |
| **> 120K tokens (~250K Chinese chars)** | **Wall** | Compression → timeout → 400 |

---

## Repository layout

```
.
├── README.md
├── README_zh.md                        简体中文版
├── AGENTS.md                           Workspace memory-writing rules (pairs with the three memory tools)
├── .gitignore
├── config/
│   ├── cordis.patch.example.yml        DSH profile config excerpt (model + meow-memory)
│   └── billion-context.example.json    bili compression threshold and model window
├── plugins/
│   ├── maid-local-lite/                Slim preset (10 tools)
│   │   ├── package.json
│   │   ├── cordis.patch.yml
│   │   └── lib/index.js
│   └── maid-local-memory/              Slim + memory preset (13 tools)
│       ├── package.json
│       ├── cordis.patch.yml
│       └── lib/index.js
└── proxy/
    ├── strata-on-demand-proxy.py      8090 lazy-loading proxy (stdlib only)
    ├── start-proxy.vbs                Silent launcher (pythonw, no console)
    └── guard-proxy.vbs                Watchdog (probes 8090 only)
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

---

## License

MIT. Configuration snippets and scripts are provided as-is, with no guarantee of fitting any particular machine.

---

<div align="center">

*Control plane and data plane, separated. The proxy minds the proxy, the engine minds the engine. VRAM is supposed to be free when nothing is running.*

</div>
