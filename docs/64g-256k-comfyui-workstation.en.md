<div align="center">

# 64 GB + 256K + ComfyUI · The Ultimate Local Workstation on Consumer Hardware

**English** | [简体中文](64g-256k-comfyui-workstation.md)

**On one machine: a 250K-token-context LLM, an 8 GB image backend, and VRAM time-sharing**

`Strata 0.1.58` · `Qwen3.8-Flash-Next-Gyro-M` · `ComfyUI 0.30.2` · `RTX 3080 Ti 12 GB`

</div>

---

> [!NOTE]
> Every number below was **measured on the machine** (engine log / proxy SSE / nvidia-smi), not estimated.
> When one log file spans several engine starts, values are re-attributed by their `expert cache auto` / `serve: N MiB of VRAM free` / `verify` segment and never mixed across starts. That rule matters more than any single figure.

---

## 1. 64 GB RAM adaptation: why the pagefile has to change

### Symptom

At 256K context, the model dies halfway through loading:

```
Windows commit: 4.2 GiB left of a 35.3 GiB limit
resident complement 20.88 GiB exceeds available commit (9.22 GiB) minus the 4 GiB safety headroom
```

Process exit code `0xC0000409` (STATUS_STACK_BUFFER_OVERRUN) — **looks like a stack overflow, is actually commit exhaustion**.

### Root cause

Windows has a **commit limit** on top of physical RAM: `RAM + pagefile`.

| Setup | Physical | pagefile | commit limit |
|---|---|---|---|
| Default (system managed) | 63.91 GiB | ≈4 GiB | **67.91 GiB** |
| Fixed 32768 MB | 63.91 GiB | 32 GiB | **95.91 GiB** |

256K needs 3.09 GiB of KV pinned in host RAM, plus 29.88 GiB of the 92 GB weights resident, plus the expert pool workers. A 4 GiB pagefile cannot cover that.

### What to do

**System Properties → Advanced → Performance Settings → Advanced → Virtual Memory → Change**:

1. Uncheck "Automatically manage paging file size for all drives"
2. Pick a drive **with an SSD and enough space** (this machine uses the system drive, NVMe PCIe 4.0 at 3985 MB/s)
3. Check "Custom size", set initial and maximum both to **32768 MB**
4. OK → **reboot**

> [!IMPORTANT]
> It must be **Custom size**, not System managed. The system-managed value is dynamic; Windows may hand you 4 GB and will not grow it for your process. Only a fixed value lifts the commit limit to 95.91 GiB.

### Verify

```powershell
# pagefile actual size (should be 34359738368 = 32 GiB)
Get-Item C:\pagefile.sys | Select-Object Length

# commit limit
(Get-CimInstance Win32_OperatingSystem).TotalVirtualMemorySize / 1024
```

After the engine starts, its log prints:

```
strata serve: Windows commit: 35.4 GiB left of a 95.9 GiB limit (RAM + page file)
```

That 95.9 GiB is it. Before the change this line said 35.3 GiB.

---

## 2. Upgrading to 256K context

### 2.1 Measured model load

| Item | Value |
|---|---|
| Model | `Qwen3.8-Flash-Next-Gyro-M-TQ2_0.gguf` |
| Bytes | **91,965,774,560** (≈91.97 GB) |
| `loaded ... GiB at ... GiB/s` | **29.88 GiB at 5.27–6.07 GiB/s** |
| Cold start to ready | **29–42 s** (not a fixed number) |

**"Cold start takes 42 seconds" is wrong.** Recomputing from `launch json` and `engine.log` mtimes gives roughly 20 / 18 / 29 seconds across three starts. The honest statement is a **29–42 s range**.

The key insight: **cold start barely depends on context size.** The bottleneck is reading 92 GB of weights (29.88 GiB resident at 5–6 GiB/s ≈ 5 seconds); the rest is host startup overhead.

Also, the `engine_ready_s: 1076` config value is **not a measured duration** — it is a formula-derived wait budget:

```python
# strata_cli/launch/runner.py:1058
start_wait() = min(runtime_max_wait(), max(600, 600 + (download_gb - 40.0) * 8.0))
# runner.py:1450
cfg["engine_ready_s"] = wait + 60
```

Gyro-M 92 GB → `600 + (92-40)*8 = 1016` → `+60` = **1076**. Treating it as a measurement sends troubleshooting in the wrong direction.

### 2.2 `--vram-reserve-mib` and expert cache are inversely related

Easy to get backwards.

```
larger reserve  →  less VRAM for the expert cache
```

Engine source, `serve/server.py:8237`, `desktop_vram_note()`: `the expert cache gets {...} GB less, a few % of speed`.

| `--vram-reserve-mib` | expert cache | VRAM free after load | verify window | Warning |
|---|---|---|---|---|
| **1728 (recommended)** | **1394 slots / 1.70 GiB** | 1217 MiB | **1123–1213 MiB** | none |
| 700 (default) | 2220 slots / 2.70 GiB | 387 MiB | 346 MiB | `WARNING: only 400 MiB of VRAM free (#2065)` |

**2220 slots belongs to reserve=700; 1728 is 1394 slots.** Swapping those two is a very common misreading — the two runs sit adjacent in the same engine.log.

1728 trades −1.0 GiB of expert cache for more than 3× the verify headroom. **On a 12 GB card that is the right trade.**

### 2.3 Tiered stress test

Direct calls to the proxy `/v1/chat/completions`, `stream: true` + `stream_options.include_usage`, `temperature: 0`.

| Tier | Measured prompt tok | TTFT | Total | finish_reason | Engine prefill | decode |
|---|---|---|---|---|---|---|
| 32k | **32,272** | 20.3 s | 35.6 s | **stop** | 20,299 ms → **1589.8 tok/s** | 68.2 tok/s |
| 128k | **129,850** | 85.9 s | 109.3 s | **stop** | 85,849 ms → **1512.5 tok/s** | 66.9 tok/s |
| 256k | **251,809** | 177.8 s | 200.1 s | **stop** | 177,656 ms → **1417.4 tok/s** | 70.2 tok/s |

- All runs show `cached_tokens = 0` → **fully cold prefill with zero prefix-cache benefit**, i.e. worst case.
- Prefill throughput decays −10.8% (1589.8 → 1417.4), matching linear attention cost over long sequences. No cliff.
- Decode holds at 66.9–70.2 tok/s throughout.

### 2.4 Probe hit rate: remote recall does not decay

Three secret tokens buried at **2% / 50% / 97%** of the filler; the question sits at the very end.

| Tier | Needle lines (of N) | Head | Middle | **Remote 97%** |
|---|---|---|---|---|
| 32k | 25 / 616 / 1195 (1,232 lines) | ✅ | ✅ | ✅ |
| 128k | 97 / 2423 / 4701 (4,846 lines) | ✅ | ✅ | ✅ |
| 256k | 187 / 4682 / **9082** (9,363 lines) | ✅ | ✅ | ✅ |

Actual 256k answer (20 characters, verbatim):

```
LAPIS-37, AMBER-91, INDIGO-52
```

> [!IMPORTANT]
> **This section was wrong the first time; the lesson is worth keeping.**
> The original probe generator computed insertion positions from a *budget-estimated* line count, but the filler loop had an early `total >= limit` break, so the real line count was always lower → the 97% needle's line number fell out of range and **was never actually written into the prompt**.
> It looked like "the model forgot the third needle", when in fact the question was never asked.
> Fix: render once with no needles to measure the real line count, place needles against that, and assert every needle appears in the prompt before sending.
> The same trap appears in chars/token calibration: an unrelated Chinese sample gave 2.021, the real template gave **1.397**. Generating against the former overshoots by 45%.

### 2.5 Resource usage (after the 256k run)

| Metric | Before 32k | After 256k |
|---|---|---|
| `commit_used` | 65,822 MiB | **68,452 MiB** |
| `commit_limit` | 98,214 MiB | **98,214 MiB (constant)** |
| Free physical RAM | 14,619 MiB | **12,008 MiB** |
| Engine process WS | 39,042 MiB | 41,782 MiB |
| KV streaming peak | — | **98.57% hit, only 636.9 MiB from RAM** |

**No OOM, and the pagefile never grew dynamically** — which is exactly the point of fixing it at 32768 MB.

---

## 3. Moving ComfyUI's model library: robocopy + mklink /J

### 3.1 Why not `extra_model_paths.yaml`

ComfyUI lets you add search paths via `extra_model_paths.yaml`, which looks like the more "official" route. It has a fatal gap:

`utils/extra_config.py:34` only calls `folder_paths.add_model_folder_path()` to change the **registry**, which does not cover nodes that build paths by string-concatenating `folder_paths.models_dir` directly (RMBG / LLM / LTXVideo / KJNodes / IPAdapter — 6 places). The result is a **half migration**: mainstream loaders use the new path, those plugins keep reading the old directory, and the old drive never gets clean.

On top of that, `add_model_folder_path()` is append-semantics: without `is_default: true` the original directory still ranks first and naturally falls back.

**A junction (`mklink /J`) is filesystem-level redirection, 100% transparent to everything above it** — every concatenation, every plugin, every `os.walk` sees one path. That is why it wins.

### 3.2 Sequence

```bat
REM 1. Copy first, leave the source untouched
robocopy "D:\ComfyUI\models" "B:\ComfyUI-models" /E /R:2 /W:5 /MT:4 /NP /TEE /LOG:"robocopy.log"
REM exit codes 0-7 are success (1 = files copied); 8+ is failure

REM 2. Verify per-subdirectory file count / bytes / name sets are identical, then continue

REM 3. Rename to keep a rollback point
move "D:\ComfyUI\models" "D:\ComfyUI\models.old"

REM 4. Create the junction in place
mklink /J "D:\ComfyUI\models" "B:\ComfyUI-models"
```

Measured (65.22 GiB / 51 files):

| Step | Duration |
|---|---|
| robocopy | **2 min 17 s**, 690 MB/s |
| rename | instant (same volume, no data movement) |
| junction | instant |
| read a large safetensors through the junction | header valid, path works |

### 3.3 Two absolute red lines

> [!CAUTION]
> **Red line 1: never `rm -rf` / `shutil.rmtree` a junction.**
> It follows the link and **deletes the real data on the target volume**. Remove a junction only with `rmdir` (no `/s`) or `Remove-Item` (no `-Recurse`).

> [!CAUTION]
> **Red line 2: never run `robocopy /MIR` against the junction source path.**
> `/MIR` is mirror semantics and will write **backwards** into the target — pointing it at an empty source wipes the target drive.

### 3.4 Acceptance

```bash
curl -s http://127.0.0.1:8188/object_info \
  | python -c "import sys,json; d=json.load(sys.stdin); print(len(d['CheckpointLoaderSimple']['input']['required']['ckpt_name'][0]))"
```

Compare ComfyUI's loader dropdowns against the on-disk file list. **Check both directions:**

- "on disk but not listed" → junction or path not effective
- "listed but not on disk" → cache residue or path mismatch

Two traps: ① `/object_info` may use `/` or `\` as separator — normalise before comparing; ② newer ComfyUI changed the dropdown structure from `[["a","b"], {...}]` to `["COMBO", {"options":["a","b"]}]`, so testing only `isinstance(entry[0], list)` **silently skips the newer nodes** and reports "node does not exist". Also, `pixel_space` in `VAELoader`'s list is a built-in virtual VAE with no file behind it and must be whitelisted, or you get a guaranteed false EXTRA.

---

## 4. VRAM time-sharing red line

### 4.1 One sentence

**Coexist for light work, go exclusive for long context.**

| Scenario | Action |
|---|---|
| Reading docs, writing prompts, joint debugging (short exchanges) | ✅ **Coexist as-is**, 54 tok/s is plenty, do not bother |
| Huge codebase / hundreds-of-thousands-of-words documents (> 10K tokens) | ❌ **Manually kill the ComfyUI process first** |
| Batch image generation | ComfyUI first; stop the engine to free VRAM |

### 4.2 Measurements

Setup: ComfyUI stays online (still ~1.8 GB after `POST /free`), Gyro-M started on top.

| Metric | Exclusive | Coexisting | Change |
|---|---|---|---|
| Cold start to ready | 42 s | **51 s** | +9 s |
| `expert cache slots` | **1394** | **894** | **−35.9%** |
| prompt path borrowed slots | 993 (1.21 GiB) | 676 (0.82 GiB) | −32% |
| prefill chunk | 2048 tokens | **1024 tokens** | halved |
| prefill time (≈32K tokens) | 20,299 ms | **37,780 ms** | **+86%** |
| prefill speed | 1589.8 tok/s | **863.6 tok/s** | **−45.7%** |
| decode speed | 68.2 tok/s | **54.3 tok/s** | −20.4% |
| cache hit rate | 68.2% | 55.2% | −13 pt |
| OOM / `#2065` warnings | — | **none** | — |

The engine notices the shortfall and shrinks itself:

```
expert cache auto: 3.91 GiB free, 1728 MiB reserved (...) -> 1394 slots
only 1169 MiB free once the slots are written (reserve 1728 MiB); shrinking the expert cache
expert cache 894 slots, 1.09 GiB of VRAM
```

That is a protection mechanism, not a failure. But it states plainly: **ComfyUI's 1.8 GB really does squeeze it.**

### 4.3 The key insight: `/free` cannot release the CUDA context

```bash
curl -X POST http://127.0.0.1:8188/free \
  -H "Content-Type: application/json" \
  -d '{"unload_models": true, "free_memory": true}'
```

Measured: 8435 MiB → 1811 MiB, 6624 MiB released, ComfyUI still online.

**The remaining ~1.8 GB is the CUDA context, and it cannot be released while the process lives.** So the assumption "just click `/free` and you are done" **does not hold**.

There is also an API subtlety: `/free` **is not synchronous**. It only sets a flag on `prompt_queue`, and the main loop applies it on the next pass:

```python
# main.py:401-408
flags = q.get_flags()
free_memory = flags.get("free_memory", False)
if flags.get("unload_models", free_memory):
    comfy.model_management.unload_all_models()
    need_gc = True
```

So any helper script must **poll `nvidia-smi`** instead of declaring victory right after the POST.

### 4.4 Launch flags you should not touch

| Flag | Verdict |
|---|---|
| `--lowvram` | **No-op.** `cli_args.py:169`: "Doesn't do anything if dynamic vram is enabled" — and dynamic vram is on here |
| `--normalvram` | **Does not exist.** The 0.30.2 vram mutex group only has `--gpu-only` / `--highvram` / `--lowvram` / `--novram` / `--cpu`; zero hits repo-wide |
| `--vram-headroom` / `--reserve-vram` | Makes image generation offload to RAM more readily and lose speed. Conflicts with "keep generation fast" |

Keep the existing `--use-sage-attention --cuda-malloc`.

---

## 5. Shape of the whole stack

```
┌────────────────────────────────────────────────────────────────────┐
│  DSH Desktop GUI                                                   │
│    ├─ Agent preset layer: maid-local-memory (13 tools / 2.9K)      │
│    └─ bili (billion-context): compression layer, ctx 262144, out 16384 │
└────────────────────────────┬───────────────────────────────────────┘
                             │ OpenAI-compatible /v1
                             ▼
┌────────────────────────────────────────────────────────────────────┐
│  Lazy-loading proxy  :8090                                         │
│    · first request → spawn engine → forward once ready             │
│    · SSE comment lines keep the client alive while waiting         │
│    · idle 900s → strata launch --stop                              │
└────────────────────────────┬───────────────────────────────────────┘
                             │ :8081
                             ▼
┌────────────────────────────────────────────────────────────────────┐
│  Strata Engine  gyro-m-256k                                        │
│    --max-context 262144 --kv int8 --kv-resident 32768              │
│    --vram-reserve-mib 1728  →  expert cache 1394 slots             │
│    92 GB weights, 29.88 GiB resident, 3.09 GiB KV in pinned RAM    │
└────────────────────────────────────────────────────────────────────┘

┌────────────────────────────────────────────────────────────────────┐
│  ComfyUI  :8188  (runs alongside, VRAM is time-shared)             │
│    · model library via junction onto the PCIe 4.0 drive            │
│    · 8 GB VRAM; stop engine before rendering / POST /free before chat │
└────────────────────────────────────────────────────────────────────┘
```

---

## 6. Drive allocation advice

| Drive | Type | Measured seq read | What goes there |
|---|---|---|---|
| System (NVMe PCIe 4.0) | 3985 MB/s | 3985 | **LLM weights + pack + pagefile** — prefill re-reads from disk, drive speed sets TTFT |
| Second NVMe (PCIe 4.0) | 2205 MB/s | 2205 | ComfyUI model library (cold data, read once then resident in VRAM) |
| SATA SSD | 478 MB/s | 478 | Backups, archives |
| USB external | slow | — | Cold archive |

**Reasoning:** during a 256K cold prefill the engine logs `file set 18.1 GiB does not fit the page cache 3.1 GiB -> about 116.5 GB read from the drive per 32768-token prompt`. At 116 GB per 32K-token prompt:

- 3985 MB/s → ~29 s
- 2205 MB/s → ~53 s
- 478 MB/s → ~4 min

So **the LLM must live on the fastest drive**; the ComfyUI library can be one tier down.

---

## 7. Takeaways worth stealing

1. **The commit limit is not physical RAM.** Before running a large model on Windows, check `TotalVirtualMemorySize` from `Get-CimInstance Win32_OperatingSystem` — not just RAM.
2. **One engine log file can span several starts.** Re-attribute numbers by `expert cache auto` / `serve: N MiB of VRAM free` / `verify` segment before quoting them.
3. **Place long-context probes by the line count actually written**, and assert every probe is present before sending the request.
4. **Take measurements from the layer that can return the truth.** A log that stores only headers will never give you `finish_reason`; the engine log and the proxy SSE are the sources of truth.
5. **Migrating a big directory: copy first, rename second, link last.** Always keep a `.old` rollback point and delete it only after the end-to-end path works.

---

<div align="center">

*A quarter-million tokens in, first word under three minutes, with an 8 GB image backend running alongside. Not a single OOM in between.*

</div>
