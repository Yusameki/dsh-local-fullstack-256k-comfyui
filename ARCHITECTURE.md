# Architecture

> Reference machine shape. Every path is a placeholder — substitute your own.

---

## 1. Layer map

```
┌──────────────────────────────────────────────────────────────────┐
│  L4  Client                                                       │
│      DSH Desktop GUI  ·  any OpenAI-compatible client             │
├──────────────────────────────────────────────────────────────────┤
│  L3  Agent layer                                                  │
│      · preset: maid / maid-local-lite / maid-local-memory         │
│      · tool budget: 2.1K (lite) or 2.9K (memory)                  │
│      · memory: SQLite at <workspace>/.dsh-meow/memory.db          │
├──────────────────────────────────────────────────────────────────┤
│  L2  Compression layer                                            │
│      bili (billion-context)  ·  admin endpoint :18788             │
│      · models.<id>.context = 262144                               │
│      · models.<id>.output  = 16384                                │
│      · maxContextLimit = 90%   (nudge / preflight threshold)      │
├──────────────────────────────────────────────────────────────────┤
│  L1  Control plane                                                │
│      Lazy-loading proxy  :8090  (pure stdlib)                     │
│      · spawns the engine detached on first request                │
│      · SSE keepalive while waiting                                │
│      · idle 900s → strata launch --stop                           │
│      · watchdog guard-proxy.vbs probes 8090 ONLY                  │
├──────────────────────────────────────────────────────────────────┤
│  L0  Data plane                                                   │
│      Strata engine  :8081                                         │
│      · --max-context 262144  --kv int8  --kv-resident 32768       │
│      · --vram-reserve-mib 1728  --spec 4  --vision                │
└──────────────────────────────────────────────────────────────────┘
```

**Rule:** L1 never manages L0's lifetime beyond start/stop-on-idle. The watchdog never probes 8081. Nothing is allowed to pin the engine in VRAM just to "stay online".

---

## 2. Hardware profile

| Component | Value | Why it matters |
|---|---|---|
| CPU | Ryzen 9 5950X, 16C / 32T | expert-pool workers bind one per physical core |
| RAM | 64 GB DDR4-3200 (63.91 GiB usable) | 92 GB model needs ~30 GiB resident + 3.09 GiB pinned KV |
| GPU | RTX 3080 Ti, **12 GB** | expert cache + draft head + verify windows all compete here |
| System drive | NVMe PCIe 4.0, **3985 MB/s** | prefill re-reads the drive; speed sets TTFT |
| Second drive | NVMe PCIe 4.0, 2205 MB/s | ComfyUI model library lives here |
| SATA SSD | 478 MB/s | backups, archives |
| OS | Windows 11 Build 26200 | commit limit semantics matter |

---

## 3. Memory / commit budget

| Item | Value | Source |
|---|---|---|
| Physical RAM | 63.91 GiB | `strata generate: memory guard: limit 63.9 GiB (source: meminfo)` |
| Pagefile | **32768 MB fixed** | Windows virtual-memory settings |
| Commit limit | **95.91~98.21 GiB** | `strata serve: Windows commit: ... of a 95.9 GiB limit` |
| Model resident | 29.88 GiB | `loaded 29.88 GiB at 5.27~6.07 GiB/s` |
| KV pinned (256K) | **3.09 GiB** | `KV streaming: 32768 of 262144 cells per QSA layer in VRAM, the K/V in 3.09 GiB of pinned RAM` |
| KV pinned (128K) | 1.55 GiB | same line at 131072 |

> The commit limit is the real ceiling, not physical RAM. With a system-managed pagefile (≈4 GB) the limit is 67.91 GiB and 256K dies with exit code `0xC0000409`.

---

## 4. VRAM budget on a 12 GB card

| Consumer | Typical size | Note |
|---|---|---|
| Weights resident | ~2.9 GiB | native projection matrices + PLE table |
| MTP draft head | 837 MiB | `draft layer loaded, 837 MiB of VRAM (experts 675, dense 111)` |
| Expert cache (`--vram-reserve-mib 1728`) | **1394 slots / 1.70 GiB** | reserve 700 → 2220 slots / 2.70 GiB |
| Prompt path borrow | 993 slots / 1.21 GiB | transient, per request |
| Verify windows | 1123–1213 MiB free | steady state, no warning at 1728 |

`--vram-reserve-mib` is **inverse** to the expert cache size: larger reserve → smaller cache. The engine source comment (`serve/server.py:8237`) says `the expert cache gets {...} GB less, a few % of speed`.

---

## 5. Disk allocation

| Drive | Holds | Rationale |
|---|---|---|
| System NVMe (3985 MB/s) | LLM weights, `packs/`, MTP draft, expert profile, pagefile | prefill reads ~116.5 GB per 32K-token prompt; at 3985 MB/s that is ~29 s |
| Second NVMe (2205 MB/s) | ComfyUI `models/` (via junction), ComfyUI install | cold data, read once per session |
| SATA SSD (478 MB/s) | archives, stress-test outputs | not on the hot path |

**ComfyUI model library migration:** `robocopy` to the second NVMe → rename source to `models.old` → `mklink /J` back in place. The junction is filesystem-level, so no plugin, no `os.walk`, no string-concatenated path can tell the difference. `extra_model_paths.yaml` was rejected because it only patches the folder registry and misses 6 places that concatenate `folder_paths.models_dir` directly.

**Two red lines:** never `rm -rf` a junction (it follows the link and deletes the target's real data); never `robocopy /MIR` against the junction source (mirror semantics wipe the target).

---

## 6. VRAM time-sharing with ComfyUI

**Coexist for light work, go exclusive for long context.**

| Mode | ComfyUI | expert cache | prefill (≈32K tok) | decode |
|---|---|---|---|---|
| Exclusive | stopped | 1394 slots | 20,299 ms | 68.2 tok/s |
| Coexisting | running, `/free`-ed | **894 slots** | **37,780 ms** | 54.3 tok/s |

`POST /free {"unload_models":true,"free_memory":true}` releases the weights (8435 → 1811 MiB measured) but **cannot release the ~1.8 GB CUDA context** while the process lives. It also only sets a queue flag; the main loop applies it on the next pass, so any helper script must poll `nvidia-smi`.

---

## 7. Ports and endpoints

| Port | Owner | Purpose |
|---|---|---|
| 8090 | lazy-loading proxy | OpenAI-compatible `/v1` for DSH |
| 8081 | Strata engine | upstream for the proxy |
| 18788 | bili admin | config / reload / inspect |
| 8188 | ComfyUI | image backend (time-shared VRAM) |
| random | DSH Desktop GUI | changes on every restart |

---

## 8. Failure modes and where to look

| Symptom | First place to look |
|---|---|
| Model dies while loading, exit `0xC0000409` | commit limit — `Get-CimInstance Win32_OperatingSystem` |
| Message spins forever with no output | proxy on 8090 down — run `start-proxy.vbs` |
| `only N MiB of VRAM free ... (#2065)` | raise `--vram-reserve-mib` |
| `engine_usable: false` from `strata launch --plan` | false alarm, ignore |
| Long input rejected with **400** (not 413) | context exceeded — read bili `[acp-usage] input=` |
| Decode suddenly few tok/s | VRAM exhausted by another process — check `nvidia-smi` |
| Images render slower than expected | another process holds VRAM |
| Model list in ComfyUI missing files | junction / path — compare `/object_info` against disk, both directions |

---

<div align="center">

*Control plane and data plane, separated. VRAM is supposed to be free when nothing is running — and time-shared, not fought over, when two things run.*

</div>
