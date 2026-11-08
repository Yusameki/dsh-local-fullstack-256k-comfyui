<div align="center">

# Gyro-M · 256K 长上下文实测报告

[English](gyro-m-256k-benchmark.en.md) | **简体中文**

**25 万 token 冷 prefill 178 秒 · decode 稳 70 tok/s · 64 GB RAM + 32 GB pagefile 全程未触顶**

`Strata 0.1.58` · `Qwen3.8-Flash-Next-Gyro-M-TQ2_0` · `RTX 3080 Ti 12 GB` · `2026-11-08`

</div>

---

> [!NOTE]
> 本文所有数字均来自**现场引擎日志与代理 SSE 响应**，不是估算。
> 凡是同一份日志里混了多次启动的，数值一律按 `expert cache auto` / `serve: N MiB of VRAM free` / `verify` 分段归位，不跨次混用 —— 这是这份报告最重要的一条方法论。

---

## 一、测试机器

| 项 | 值 |
|---|---|
| CPU | Ryzen 9 5950X（16C / 32T） |
| RAM | 64 GB（4×16 GB DDR4-3200，实测识别 63.91 GiB） |
| GPU | RTX 3080 Ti **12 GB** |
| 系统 | Windows 11 Build 26200 |
| Pagefile | **固定 32768 MB**（自定义大小，非系统管理） |
| Commit limit | **98,214 MiB**（改 pagefile 前是 67.91 GiB） |
| 模型 | `Qwen3.8-Flash-Next-Gyro-M-TQ2_0.gguf`，**91,965,774,560 字节**（≈91.97 GB） |
| 引擎 | Strata 0.1.58 |
| 上下文 | `--max-context 262144`、`--kv int8`、`--kv-resident 32768`、`--vram-reserve-mib 1728` |

---

## 二、分层压测主表

测试方式：直连 8090 懒加载代理的 `/v1/chat/completions`，`stream: true` + `stream_options.include_usage`，`temperature: 0`，`max_tokens: 2048`。

| 级别 | 目标 tok | **实测 prompt tok** | TTFT | 总耗时 | finish_reason | 引擎 prefill | decode |
|---|---|---|---|---|---|---|---|
| 32k | 32,768 | **32,272** | 20.3 s | 35.6 s | **stop** | 20,299 ms → **1589.8 tok/s** | 68.2 tok/s |
| 128k | 131,072 | **129,850** | 85.9 s | 109.3 s | **stop** | 85,849 ms → **1512.5 tok/s** | 66.9 tok/s |
| 256k | 253,952 | **251,809** | 177.8 s | 200.1 s | **stop** | 177,656 ms → **1417.4 tok/s** | 70.2 tok/s |
| 256k（复跑） | 253,952 | **251,813** | 184.1 s | 186.8 s | **stop** | 183,979 ms → **1368.7 tok/s** | 63.7 tok/s |

- 四次全部 `saw[DONE] = True`、`error = None`、**`cached_tokens = 0`**
  → 全是**冷 prefill，没有任何前缀缓存红利**，属于最坏情况下的数字。
- prefill 吞吐随长度衰减：1589.8 → 1512.5 → 1417.4 tok/s，**-10.8%**，符合长序列注意力的线性开销，无断崖。
- decode 全程稳在 63.7~70.2 tok/s，与 32k 时基本无差别。

```
                          TTFT 随上下文长度
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

## 三、长上下文检索探针

三根暗号分别埋在填充文本的 **2% / 50% / 97%** 位置，问句在文末，要求只输出三个暗号。

| 级别 | 针所在行（共 N 行） | 青金石-三七 | 琥珀-九一 | **靛蓝-五二（远端 97%）** |
|---|---|---|---|---|
| 32k | 25 / 616 / 1195（共 1232 行） | ✅ | ✅ | ✅ |
| 128k | 97 / 2423 / 4701（共 4846 行） | ✅ | ✅ | ✅ |
| 256k | 187 / 4682 / **9082**（共 9363 行） | ✅ | ✅ | ✅ |

256k 复跑的实际回答（全文 20 字符）：

```
青金石-三七, 琥珀-九一, 靛蓝-五二
```

→ 模型对 25 万 token 全段（含 97% 处）的注意力**没有衰减**，远端检索可用。

> [!IMPORTANT]
> **这一节第一次是错的，必须记下来。**
> 初版 `build_prompt()` 用「预算估算行数」去算针的位置，但写正文的循环里有 `total >= limit` 的提前 break，
> 实际行数永远少于估算值 → 97% 那根针的行号越界，**从来没有真正写进 prompt**。
> 三次实测 `'靛蓝-五二' in prompt: False`，当时只有前两根针是真验证。
> 修法是「先空跑一遍量出实际写入行数，再按实际行数算针位置」，并加断言：
> 针没写进 prompt 就直接退出，不许拿模型没看到的针判它失忆。

---

## 四、资源占用

| 指标 | 32k 前 | 256k 后 | 说明 |
|---|---|---|---|
| `commit_used` | 65,822 MiB | **68,452 MiB** | 只涨 2.6 GiB |
| `commit_limit` | 98,214 MiB | **98,214 MiB** | **恒定**，pagefile 没被动态扩容 |
| 物理内存剩余 | 14,619 MiB | **12,008 MiB** | 最低仍余 12 GiB |
| 引擎进程 WS | 39,042 MiB | 41,782 MiB | 只涨 2.7 GiB |
| 代理 WS | 29 MiB | 29 MiB | 恒定 |

**KV streaming 峰值**（256k 那次）：

```
98.57% of 11,128,560 block reads hit VRAM, 636.9 MiB read from RAM
```

→ 只有 **636.9 MiB** 的 KV 真的走了主机内存。`--kv-resident 32768` 这个「每个 QSA layer 只把 32768 / 262144 个 cell 的 KV 常驻显存，其余进 pinned RAM」的窗口机制，在 256K 下撑得住。

**无 OOM，无显存告警**。

### expert cache 与 VRAM 预算的取舍

| `--vram-reserve-mib` | expert cache | 加载后剩余 VRAM | verify window | 告警 |
|---|---|---|---|---|
| **1728（推荐）** | **1394 slots / 1.70 GiB** | 1217 MiB | **1123~1213 MiB** | 无 |
| 700（默认） | 2220 slots / 2.70 GiB | 387 MiB | 346 MiB | `WARNING: only 400 MiB of VRAM free (#2065)` |

`vram-reserve-mib` 语义：**reserve 越大，expert cache 拿到的 VRAM 越少**。引擎源码 `serve/server.py:8237` 的 `desktop_vram_note()` 原话：`the expert cache gets {...} GB less, a few % of speed`。

→ 1728 用 -1.0 GiB 的 expert cache 换来了 3 倍以上的 verify 余量，**在 12 GB 卡上是正确的取舍**。

---

## 五、字符 / token 换算必须用真实模板自校准

| 校准方式 | 比例 | 后果 |
|---|---|---|
| 用压测真实模板（2028 字符） | **1.397** | 两次都得到 `prompt_tokens = 1452`，稳定 |
| 用无关中文样本 × 60 | 2.021 | 按它生成 66,241 字符，实际打了 **46,798 tokens**，直接超标 |

→ 中文的字符/token 比例强依赖文本结构（数字、标点、英文术语占比），**必须拿你自己的 prompt 模板去校准**，不要套用网上的经验值。

---

## 六、两类「假异常」，别当故障查

### 假异常 1：代理日志里的「客户端提前断开」

压测脚本读满 `max_tokens` 后主动 `break` 断流，代理侧会记一条「客户端提前断开」。
服务端每次请求都是 **200**，不是崩溃。

### 假异常 2：每级跑完跟着一条 99.99% 复用的请求

```
prompt 131404 tokens = 131385 reused + 19 read in 199 ms
prompt 253363 tokens = 253345 reused + 18 read in 182 ms
```

这是 bili 或代理自动追加的续跑请求。**复用率 99.99% 恰恰证明前缀缓存工作良好**，只是压测本身走的是冷路径。

### 假截断：output 贴顶

曾出现 `output = 8176 / 8192`，离上限只差 16 token。虽然尾部是自然收尾，但这种「贴顶」在下一次长任务里极容易真撞 `length` 并被误判成截断复发。
→ 做法：**长输出压测前先把 `maxTokens` 放宽**（本项目从 8192 提到 16384），别在上限边缘做实验。

---

## 七、取证口径（这条最省时间）

**bili 的 RES dump 对流式响应只存响应头**（318 字节，到 `content-type: text/event-stream` 就结束），`finish_reason` 和 `[DONE]` 都不在里面，bili.log 全文 grep `finish_reason` 命中 0 次。

所以验 `stop` / `length` 只有两条路：

1. **直连代理看 SSE**（推荐）
   - 正常结束 → `"finish_reason": "stop"` + `data: [DONE]`
   - `max_tokens: 16` 强制截断 → `"finish_reason": "length"` + `data: [DONE]`
2. **看 DSH 会话记录**的 `turn/end.reason.kind`

而性能数字，**最干净的口径是引擎日志这一行**：

```
strata serve: prompt 251809 tokens = 0 reused + 251809 read in 177656 ms (1417.4 tok/s),
               1537 generated in 21907 ms (70.2 tok/s), drafts accepted 898 of 1133, 6 checkpoints
```

它一次给全：真实 prompt token 数、prefill 耗时与吞吐、decode 吞吐、投机解码接受率。

> 顺带：`/v1/tokenize` 端点**不存在**（返回 404 `{"error": {"message": "not found"}}`），算 token 只能发请求读 `usage`。

---

## 八、复现

```bash
# 1. 起引擎（pack 已存在会跳过准备阶段，冷启动 29~42 秒）
"C:\Strata\App\control\bin\strata.cmd" launch --family gyro --model Gyro-M \
  --model-path "C:\Strata\Models\gyro-m-abliterated\Qwen3.8-Flash-Next-Gyro-M-TQ2_0.gguf" \
  --data-dir "C:\Strata\Models" --context 256k --port 8081 --no-tui --yes \
  --set vram-reserve-mib=1728

# 2. 确认代理在听
curl --noproxy '*' http://127.0.0.1:8090/health

# 3. 校准字符/token 比例（用你自己的模板，别用样本）
# 4. 分级压测 32k → 128k → 253952
```

**253952 = 262144 − 8192**，是给输出留位的经典极限。

---

## 九、这份报告订正了哪些常见误读

| # | 常见误读 | 实测真相 |
|---|---|---|
| 1 | 冷启动是固定 42 秒 | 实为 **29~42 秒区间**，主要花在读 92 GB 权重（`loaded 29.88 GiB at 5.97 GiB/s`），与上下文大小几乎无关 |
| 2 | `--vram-reserve-mib 1728` → 2220 slots / 2.70 GiB | **反了**。2220 slots / 2.70 GiB 属于 **reserve=700**；1728 是 **1394 slots / 1.70 GiB** |
| 3 | 1728 的 verify window 是 346 MiB 并告警 | **错位**。346/387/452 MiB 与 `#2065` 告警全属 reserve=700；1728 是 **1123~1213 MiB 无告警** |
| 4 | KV 命中率 99.5% | 最终值 **99.70%**（24,647,988 block reads，仅 296.0 MiB 走 RAM），99.5% 只是中途某点 |
| 5 | `engine_ready_s: 1076` 是实测冷启动耗时 | 它是**公式算出来的等待预算**：`max(600, 600 + (gb − 40) × 8) + 60`，Gyro-M 92 GB → 1076。实测只要 29~42 秒 |

---

## 十、三条可以搬走的经验

1. **同一份引擎日志里混着多次启动**，读性能数字必须先按 `expert cache auto` / `serve: N MiB of VRAM free` / `verify` 分段归位，否则会把两次启动的数值张冠李戴。
2. **造长上下文探针时，插入位置必须基于「实际写入行数」**，不能基于目标行数；而且发请求前要先本地断言所有针都在 prompt 里。
3. **压测数据要从能拿到真值的那一层取**。bili 只存响应头，就别在那里找 `finish_reason`；引擎日志和代理 SSE 才是真值来源。

---

<div align="center">

*25 万字进去，178 秒后开始说话，中间没有一次 OOM。*

</div>
