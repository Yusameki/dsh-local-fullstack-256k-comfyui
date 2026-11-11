<div align="center">

# Laptop · Qwen3.6-35B-A3B Tuning Log

**prefill 309 → 1,427 tok/s (4.6×) · 256K context stable · 8 GB VRAM + 32 GB RAM**

`llama.cpp b12064` · `Qwen3.6-35B-A3B-abliterated.i1-Q4_K_M` · `RTX 4070 Laptop 8 GB` · `2026-11-11`

</div>

---

> [!NOTE]
> Every number here comes from llama.cpp's native `timings` field and live `/slots` readings, not estimates.
> The most valuable lesson is at the end: **before comparing two engines, first bring both to a good configuration** — otherwise you are measuring whose defaults are worse, not which engine is faster.

---

## 1. The machine

| Item | Value |
|---|---|
| GPU | RTX 4070 Laptop **8 GB** (reference configuration) |
| RAM | 32 GB |
| OS | Windows 11 |
| Engine | llama.cpp build `b12064-51fe42c5d` |
| Model | `Qwen3.6-35B-A3B-abliterated.i1-Q4_K_M.gguf` |
| Parameters | 34.66 B (MoE, ~3 B active) |
| Quantization | Q4_K_M, **21.2 GB** |
| Context | up to 262,144 (the model's native `n_ctx_train`) |
| Modalities | text + image + video |

**Key premise**: a 21.2 GB model does not fit in 8 GB of VRAM, so it must run with CPU offload. Everything below follows from that single fact.

---

## 2. The first lever: ubatch, not "put more experts on the card"

Grid test (96K context with vision, only parameters changed):

| Configuration | prefill | 12K text |
|---|---:|---:|
| `-ub 128` / `--n-cpu-moe 36` / `-tb 12` (old) | **309 t/s** | 39.3 s |
| `-ub 256` / `--n-cpu-moe 34` / `-tb 16` | 510 t/s | 23.9 s |
| `-ub 512` / `--n-cpu-moe 34` / `-tb 16` | 774 t/s | 15.8 s |
| `-ub 1024` / `--n-cpu-moe 34` / `-tb 16` | 1,105 t/s | 11.2 s |
| **`-ub 2048` / `--n-cpu-moe 34` / `-tb 16` (chosen)** | **1,427 t/s** | **8.7 s** |

**4.6×** — and VRAM only went from 6,088 to 6,662 MiB, **574 MiB more**.

The counter-intuitive part: moving 2 layers of experts (~1 GB) onto the GPU gained less than the single step from `-ub 512` to `1024`.

> **Why**: MoE prefill is "feed more tokens to the same batch of experts at once". Raising ubatch increases **compute intensity through kernel fusion and batched memory access** — it is not about putting weights into faster memory.

### 96K at full capacity

| Metric | Old | New |
|---|---:|---:|
| prompt | 96,343 tok | 96,051 tok |
| prefill | 177 t/s | **1,313 t/s** |
| total | 545 s | **73.5 s** |

**7.4× faster.**

---

## 3. The second lever: at long context, giving VRAM back beats taking it

Growing the context hit a **cliff** worth recording on its own:

| Context | Full prompt | prefill | Total | VRAM | Free RAM |
|---|---:|---:|---:|---:|---:|
| 96K | 96,051 | 1,313 t/s | 73.5 s | 6,656 MiB | 5.45 GB |
| 128K | 128,047 | 1,166 t/s | 110.3 s | 6,988 MiB | 6.01 GB |
| **192K (`-ngl` 34)** | 192,052 | **352 t/s ⚠️** | 546.2 s | 7,812 MiB | 5.56 GB |
| **192K (`-ngl` 38)** | 192,054 | **958 t/s** | 201.4 s | 6,152 MiB | 5.56 GB |
| 256K (chosen) | 262,050 | 800 t/s | 328.2 s | 7,074 MiB | 3.91 GB |

The first 192K attempt collapsed from 1,166 to **352 t/s** — 3.3× slower.

**Not a gradual decay: a cliff.** With only **339 MiB** of VRAM left, the engine fell into a degraded mode — no crash, but permanently struggling.

The fix: give 4 layers of experts back to RAM (34 → 38), freeing ~1.6 GB, and speed returned to 958 t/s.

> **Counter-intuitive but clear**: past 128K, giving VRAM back matters far more than pushing experts onto the card.
> The ubatch-era direction (experts onto the GPU) **stops working** at long context — KV cache and compute buffers fill the VRAM, so experts squeezed in there become a burden.

**So the same parameter has opposite optima in different scenarios**: below 128K, experts on the card help; above it, they hurt.

---

## 4. Two traps

### Trap 1 · `--n-cpu-moe 32` always crashes

```
CUDA error: cudaEventSynchronize
```

At 96K with vision there is not enough VRAM. **34 is the safe floor** (6 expert layers on GPU, 34 in RAM). Confirmed twice; not intermittent.

### Trap 2 · "Crash" is not the only failure mode

When VRAM is tight, the more common outcome is **no crash but collapsed speed** (the 352 t/s case above).

**That is harder to notice than a crash** — the service looks alive, and you will not know unless you measure. When speed suddenly looks wrong but the service is up, check the VRAM headroom first.

---

## 5. What 256K actually costs: only one thing

| Item | 96K | 256K |
|---|---:|---:|
| decode (short request) | 37.4 t/s | 33.9 t/s |
| vision | 9 s/image | 9.2 s/image |
| VRAM | 6,656 MiB | 7,074 MiB |
| Free RAM | 5.45 GB | 3.91 GB |

- **decode lost only 9.4%** while context grew 2.7× — confirming the decode bottleneck is expert transfer, not attention.
- **prefill decays smoothly**: 1,427 → 1,166 → 957 → 800, about −12% per doubling, a clean quadratic curve with no cliff.
- **262,050 is the end of the road**: only **94 tokens** below `n_ctx_train` of 262,144. Without rope scaling there is nowhere further to go.

---

## 6. Two independent findings about vision

| Finding | Data |
|---|---|
| Images cost almost no extra prefill | 20,222 tok text-only 328.2 vs 20,249 tok with image 327.6 t/s |
| Vision is a separate bottleneck, unrelated to ubatch | 501-token image prompt → 62.5 t/s, 8.7 s; still ~60 after ubatch tuning |

The bottleneck is **the vision tower on the CPU**. Moving it to the GPU could plausibly be 3-5× faster, but with only 1.5 GB of VRAM left it is not worth the risk.

---

## 7. Final configuration

```bat
llama-server.exe ^
  -m "Qwen3.6-35B-A3B-abliterated.i1-Q4_K_M.gguf" ^
  -c 262144 ^
  --n-cpu-moe 38 ^
  -ub 2048 ^
  -tb 16 ^
  --host 0.0.0.0 --port 8080
```

**Fallback**: when RAM is tight, set `-c` back to `131072` and `--n-cpu-moe` back to `34`; free RAM returns to 6.0 GB immediately.

---

## 8. How it feels inside DSH

DSH is a heavyweight agent framework. Measured fixed overhead per turn (`maid-local-lite`, brand-new session):

```
persona system prompt        10,796 chars
meow long-term memory        10,032 chars
tool definitions (10)         8,827 chars
AGENTS.md injection           5,179 chars
runtime context               1,000 chars
────────────────────────────────────
≈ 35,849 chars ≈ about 13K tokens
```

With the full `maid` preset (89 tools) the tool definitions alone are **100,384 chars**, pushing fixed overhead to **about 49K tokens**.

Measured TTFT:

| Scenario | Context | TTFT |
|---|---:|---:|
| Direct call, no agent framework | 44 tok | **1.2 s** |
| DSH + lite preset | ~18K tok | ~20 s |
| DSH + full maid | ~49K tok | 60 s cold / 1-8 s warm cache |

**Conclusion**: the bottleneck for local models is never "not fast enough" — it is **context too long**. Open a fresh session and switch to a light preset to go faster. Shrinking the persona saves little; tool schemas are the bulk.

---

## 9. Methodology (the most reusable part)

1. **Do not mistake "speed plateaued" for a physical limit**
   Stable prefill at some number tempts you to conclude "hardware bound". The default configuration may be 4-5× off optimum. **Run a parameter grid before concluding.**

2. **Bring both sides to a good configuration before comparing engines**
   An early measurement showed "engine A is 5× faster than B" — until it turned out B was simply running `-ub 128`. At 2048 they matched. **An untuned comparison measures defaults, not engines.**

3. **Failure has two modes; a crash is only one**
   Insufficient VRAM more often causes **silent degradation** (no error, 3× slower). Only a benchmark reveals it; service health will not.

4. **The same parameter can have opposite optima in different scenarios**
   `--n-cpu-moe` points opposite ways below and above 128K. Know your use case before tuning.

5. **Attribute images and text separately**
   Slow image prefill does not imply slow text prefill, or vice versa. Measure them apart, never together.

---

<div align="center">

*2026-11-11 · part of the local full-stack experiments*

</div>
