# 本地视觉模型在 DSH 里的性能排查报告

> 2026-11-11 · 台式机 DSH ⇄ 笔记本 llama.cpp
> 结论先行：**不是配置问题，是「18K 固定开销 + prefill 太慢」的架构性水土不服。**

---

## 一、被测对象

| 项 | 值 |
|---|---|
| 端点 | `http://<LAPTOP_IP>:8080/v1`（OpenAI 兼容） |
| 模型 id | `qwen3.6-vision` |
| 实际权重 | `Qwen3.6-35B-A3B-abliterated.i1-Q4_K_M.gguf` |
| 参数量 | 34.66B（MoE，A3B = 每次激活约 3B） |
| 量化 | Q4_K_M，**21.2 GB** |
| 上下文 | n_ctx 98304（训练窗口 262144） |
| 模态 | text + image + video |
| 笔记本侧占用 | 内存 26 GB / 显存 5 GB ← **关键：模型装不进 8G 显存，必须走 CPU offload** |

---

## 二、现象

在 DSH GUI 里选 `laptop-vision` 发一张 171×148 的小图（问「图里是什么」）：

- 界面转 **1 分 54 秒**
- 只吐出 **12 个字**：「主人，愛奈看看……（看了一眼）」
- 追问一次 → **0 token**，报 `❌ [ACP] stream error: the turn degenerated again after the continuation nudge`

---

## 三、排查过程与证据

### 3.1 先确认「是不是服务挂了」

```
GET /health           → {"status":"ok"}
GET /v1/models        → 200，qwen3.6-vision，multimodal
GET /slots            → is_processing=true, n_prompt_tokens=10725, processed=8677
```

→ 服务活着，**确实在跑 prefill**，不是卡死。

### 3.2 从会话日志抠真实数字

DSH 会话日志 `~/.dsh/sessions/<工作区>/<会话id>/session.v4.jsonl.zstd`
（注意：多帧 zstd，必须 Python `zstandard.stream_reader` 才解得全）。

```
request/context  → provider=laptop-vision, model=qwen3.6-vision, contextWindow=98304
assistant/message→ inputTokens=18213, outputTokens=12
                   replayState.response.stopReason = "stop"   ← 模型自己收尾，不是长度截断
turn/end         → reason.kind = "completed"
```

**TTFT = 113,815 ms ≈ 1 分 54 秒，decode = 37.62 tok/s。**

### 3.3 18,213 tokens 到底是谁吃的

会话是**新建的**（`isSeeded=false`，preset 选的是轻量的 `maid-local-lite`），没有历史包袱。逐块量：

```
女仆人设系统提示词        10,796 字符
长期记忆注入（meow-memory）10,032 字符
工具定义（10 个）          8,827 字符
AGENTS.md 注入             5,179 字符
运行时上下文               1,000 字符
主人的图 + 提问            5 字符 + 1 张图
────────────────────────────────────────
合计                      35,849 字符 ≈ 18,213 tokens
```

→ **换 preset、开新会话都救不了**，这 18K 是固定开销。

### 3.4 决定性对照实验（同一模型、同一张图）

| 组 | 上下文 | TTFT | decode | 输出 |
|---|---|---|---|---|
| **A 干净提示词、无工具** | 44 tok | **1,208 ms** | 40.5 tok/s | 50 tok，正常答完 |
| **B 加人设「30字限制」** | 119 tok | **1,638 ms** | 43.5 tok/s | 19 tok，正常 |
| **DSH 实际** | 18,213 tok | **113,815 ms** | 37.6 tok/s | 12 tok，自己停了 |

→ **差距 95 倍，全在 prefill 上。**

### 3.5 A 组识别质量（证明模型本身没问题）

> 「这是《明日方舟》里的角色**能天使（Exusiai）**的Q版形象。特征：金发、绿色眼睛、头顶有类似兔耳/天线的小装饰、背后背着双枪。」

金发、绿眼、猫耳状装饰、Q 版——特征全中。**看图能力正常。**

---

## 四、根因

两个物理事实叠加，不是软件 bug：

### ① prefill 只有 ~160 tok/s

18,213 ÷ 114 秒 ≈ **160 tok/s**。
原因：模型 21.2 GB 装不进 8G 显存 → CPU offload → 主要是内存带宽在跑 prefill，GPU 帮不上大忙（显存只占了 5 GB）。

### ② 长上下文让 35B 模型提前收尾

`stopReason="stop"` 说明模型**主动结束**，不是被 max_tokens 截断。同一个模型在 44 token 的干净上下文里能正常答 50 token，在 18K 里只答 12 个就收。

这是这套 MoE + Q4 量化在长上下文里的稳定性问题，调 `max_tokens` / `temperature` 都改不了。

---

## 五、结论

> **DSH 是给云端大模型设计的重度 Agent 框架（18K 固定开销）；笔记本 8G 显存跑 21.2 GB 的 35B 模型，prefill 物理上就是 160 tok/s。两者相乘 = 每次提问先等两分钟。**

调配置救不了。能动的只有三处：**换小模型**、**砍固定开销**、**绕开 agent 循环**。

---

## 六、三条改善路径（按性价比排序）

### 路径 1 · 换小模型（最彻底）

**核心判据不是参数量，而是「能不能整只塞进 8G 显存」。**

| 候选 | Q4 体积 | 能否进 8G 显存 | 视觉能力 | 备注 |
|---|---|---|---|---|
| **Qwen2.5-VL-7B-Instruct** | ≈ 4.7 GB | ✅ 能 | 强（OCR/图表/中文场景好） | 首选 |
| **MiniCPM-V 2.6**（8B） | ≈ 5.2 GB | ✅ 能（紧） | 强（单图 OCR 有口碑） | 次选 |
| Qwen2.5-VL-3B | ≈ 2.1 GB | ✅ 很宽松 | 够用 | 极速档 |
| 现在这个 35B-A3B | 21.2 GB | ❌ 不能 | 强 | prefill 160 tok/s |

**预期收益**（推算，非实测）：全 GPU prefill 通常比 CPU offload 快 5–10 倍，
18K 上下文 prefill 有望从 114 秒压到 **12–25 秒**。

⚠️ 但要注意：18K 的固定开销对小模型是**沉重包袱**——小模型的上下文窗口通常 32K，
18K 固定开销 + 对话历史很快就顶到边，且小模型在长上下文里更容易退化（刚踩过）。
**所以路径 1 最好和路径 2 一起上。**

验证方法：
```bash
node _bench_tmp/bench-llamacpp.mjs --base http://<LAPTOP_IP>:8080/v1 \
  --model <新模型> --image sent-image.png --text "图里是什么？" --max-tokens 300
```
先看干净上下文的 TTFT 有没有到 1 秒级，再看 DSH 里 18K 时是多少。

### 路径 2 · 砍掉 10K 记忆注入（最省事，立刻可试）

`meow-memory` 有总开关，关掉后**注入、反思、记忆工具全部停用**：

```yaml
# ~/.dsh/profiles/desktop/cordis.patch.yml
- id: meow-memory
  name: dsh-maid-preset/meow
  config:
    enabled: false      # ← 加这一行
    # 其余保持原样
```

**或者走 GUI（推荐，官方路径）**：设置 → 插件 → meow-memory → 关掉总开关
（英文界面提示原文：*When off, injection, reflection and the memory tools are all disabled*）

预期：18,213 → 约 **8,200 tokens**（砍掉 10,032 字符 ≈ 5K tokens，加上关键词命中不再注入）。
按 160 tok/s 算，prefill 从 114 秒降到 **约 51 秒**。**仍然不算快**，但少一半。

⚠️ 代价：记忆功能全停，包括这次刚写的那些记忆条目不再自动注入。
测完记得开回来。

### 路径 3 · 绕开 agent 循环（真要用它看图，这条最舒服）

不经过 DSH 的 18K 铺垫，直接打 OpenAI 兼容接口：

```bash
cd B:\dsh-workspace\_bench_tmp
node bench-llamacpp.mjs --base http://<LAPTOP_IP>:8080/v1 \
  --model qwen3.6-vision --image <图片路径> --text "图里是什么？" --max-tokens 300
```

实测 **TTFT 1.2 秒、40 tok/s**，比 DSH 里快 95 倍。
缺点：只能一问一答，没有工具、没有记忆、没有多轮。

---

## 七、附带产出

### 7.1 TTFT / 速度提取脚本

`B:\dsh-workspace\_bench_tmp\dsh-ttft.py`

原理：`step/start` 事件时间 = 请求发出时刻；`assistant/message` 的 `data.stream[].time` = 分块到达时刻；
首块 − step/start = **TTFT**；`usage.outputTokens ÷ (末块 − 首块)` = **tok/s**。

```bash
python dsh-ttft.py --provider laptop-vision --last 5
python dsh-ttft.py --session <sessionId>
```

坑：会话目录是**两级**（`<工作区>/<会话id>/`），glob 必须写 `*/*/session.v4.jsonl.zstd`。

### 7.2 直连测速脚本

`B:\dsh-workspace\_bench_tmp\bench-llamacpp.mjs` —— 支持 `--text` / `--text-file` / `--image`，
输出 TTFT / prefill / decode / tok-s 全套指标。

### 7.3 llama.cpp 实时状态（比看终端日志还直接）

```bash
curl http://<LAPTOP_IP>:8080/slots
```
`n_prompt_tokens_processed` 就是 prefill 走到哪了，`is_processing` 判断是否在处理中。

---

## 八、排查方法论（下次可复用）

1. **先分清「慢」和「挂」**：`/health` + `/slots` 判断是在干活还是卡死。
2. **不要相信界面上的耗时数字去反推原因**，去会话日志抠 `inputTokens` / `stopReason`。
3. **`stopReason` 是关键分叉**：`length` = 截断，`stop` = 模型自己收尾（问题在模型，不在参数）。
4. **做对照实验，一次只改一个变量**：干净提示词 vs 加人设 vs 加全部工具，差距立刻显形。
5. **拆解固定开销，别猜**：把 system prompt / 记忆注入 / 工具定义 / AGENTS.md 各自的字符数量出来。
6. **认错要快**：本次先后误判过「ping 不通=没连通」「18K 是会话历史」两次，都是靠现场数据推翻的。

---

*报告生成：2026-11-11 · 愛奈*
