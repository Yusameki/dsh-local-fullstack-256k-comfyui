<div align="center">

# 64 GB + 256K + ComfyUI · 一台消费级机器的终极本地工作站

[English](64g-256k-comfyui-workstation.en.md) | **简体中文**

**同一台机器上：25 万 token 上下文的大模型 + 8 GB 显存的生图后端 + 显存分时复用**

`Strata 0.1.58` · `Qwen3.8-Flash-Next-Gyro-M` · `ComfyUI 0.30.2` · `RTX 3080 Ti 12 GB`

</div>

---

> [!NOTE]
> 本文所有数字均来自**现场实测**（引擎日志 / 代理 SSE / nvidia-smi），不是估算。
> 凡是同一份日志里混了多次启动的，数值一律按 `expert cache auto` / `serve: N MiB of VRAM free` / `verify` 分段归位，不跨次混用 —— 这是本文最重要的一条方法论。

---

## 一、64 GB 内存适配：为什么必须改 pagefile

### 症状

256K 上下文下，模型加载到一半直接崩：

```
Windows commit: 4.2 GiB left of a 35.3 GiB limit
resident complement 20.88 GiB exceeds available commit (9.22 GiB) minus the 4 GiB safety headroom
```

进程退出码 `0xC0000409`（STATUS_STACK_BUFFER_OVERRUN）—— **看起来像栈溢出，实际是 commit 用尽**。

### 根因

Windows 的物理内存之外还有一个 **commit limit**：`物理内存 + pagefile`。

| 配置 | 物理内存 | pagefile | commit limit |
|---|---|---|---|
| 默认（系统托管） | 63.91 GiB | ≈4 GiB | **67.91 GiB** |
| 固定 32768 MB | 63.91 GiB | 32 GiB | **95.91 GiB** |

256K 需要把 3.09 GiB 的 KV 常驻 pinned RAM，加上 92 GB 权重里的 29.88 GiB 常驻，再加专家池 worker —— 4 GiB 的 pagefile 根本兜不住。

### 做法

**系统属性 → 高级 → 性能设置 → 高级 → 虚拟内存 → 更改**：

1. 取消「自动管理所有驱动器的分页文件大小」
2. 选**有 SSD 且空间足够的盘**（本机选系统盘 C，NVMe PCIe 4.0 3985 MB/s）
3. 勾「自定义大小」，初始和最大值都填 **32768 MB**
4. 确定 → **重启**

> [!IMPORTANT]
> 必须「自定义大小」而不是「系统管理」——系统管理的值是动态的，Windows 可能只给 4 GB 且不会为你的进程临时扩容。固定值才会把 commit limit 抬到 95.91 GiB。

### 验证

```powershell
# 看 pagefile 实际大小（应是 34359738368 = 32 GiB）
Get-Item C:\pagefile.sys | Select-Object Length

# 看 commit limit
(Get-CimInstance Win32_OperatingSystem).TotalVirtualMemorySize / 1024
```

启动引擎后引擎日志会打印：

```
strata serve: Windows commit: 35.4 GiB left of a 95.9 GiB limit (RAM + page file)
```

95.9 GiB 就是它。改之前这里是 35.3 GiB。

---

## 二、256K 上下文升级

### 2.1 模型加载实测

| 项 | 值 |
|---|---|
| 模型 | `Qwen3.8-Flash-Next-Gyro-M-TQ2_0.gguf` |
| 字节数 | **91,965,774,560**（≈91.97 GB） |
| `loaded ... GiB at ... GiB/s` | **29.88 GiB at 5.27~6.07 GiB/s** |
| 冷启动到 ready | **29~42 秒**（不是固定值） |

**「冷启动 42 秒」这个说法是错的。** 按 `launch json` 与 `engine.log` 的 mtime 复算，三次分别是约 20 / 18 / 29 秒。正确表述是 **29~42 秒区间**。

关键认知：**冷启动时间与上下文大小几乎无关**。瓶颈是读 92 GB 权重（日志里 29.88 GiB 常驻部分以 5~6 GiB/s 读入，约 5 秒），剩下的主要是 host 启动开销。

另外，`engine_ready_s: 1076` 这个配置项**不是实测耗时**，而是公式算出来的等待预算：

```python
# strata_cli/launch/runner.py:1058
start_wait() = min(runtime_max_wait(), max(600, 600 + (download_gb - 40.0) * 8.0))
# runner.py:1450
cfg["engine_ready_s"] = wait + 60
```

Gyro-M 92 GB → `600 + (92-40)*8 = 1016` → `+60` = **1076**。把它当实测值会把排错方向带偏。

### 2.2 `--vram-reserve-mib` 与 expert cache 是反比关系

这条关系很容易记反。

```
reserve 越大  →  expert cache 拿到的 VRAM 越少
```

引擎源码 `serve/server.py:8237` 的 `desktop_vram_note()` 原话：`the expert cache gets {...} GB less, a few % of speed`。

| `--vram-reserve-mib` | expert cache | 加载后剩余 VRAM | verify window | 告警 |
|---|---|---|---|---|
| **1728（推荐）** | **1394 slots / 1.70 GiB** | 1217 MiB | **1123~1213 MiB** | 无 |
| 700（默认） | 2220 slots / 2.70 GiB | 387 MiB | 346 MiB | `WARNING: only 400 MiB of VRAM free (#2065)` |

**2220 slots 属于 reserve=700，1728 是 1394 slots。** 把这两个数张冠李戴是很常见的误读——因为同一份 engine.log 里两个跑次挨着。

1728 用 −1.0 GiB 的 expert cache 换来了 3 倍以上的 verify 余量，**在 12 GB 卡上是正确取舍**。

### 2.3 分层压测数据

直连代理 `/v1/chat/completions`，`stream: true` + `stream_options.include_usage`，`temperature: 0`。

| 级别 | 实测 prompt tok | TTFT | 总耗时 | finish_reason | 引擎 prefill | decode |
|---|---|---|---|---|---|---|
| 32k | **32,272** | 20.3 s | 35.6 s | **stop** | 20,299 ms → **1589.8 tok/s** | 68.2 tok/s |
| 128k | **129,850** | 85.9 s | 109.3 s | **stop** | 85,849 ms → **1512.5 tok/s** | 66.9 tok/s |
| 256k | **251,809** | 177.8 s | 200.1 s | **stop** | 177,656 ms → **1417.4 tok/s** | 70.2 tok/s |

- 全部 `cached_tokens = 0` → **彻底冷 prefill，无任何前缀缓存红利**，属最坏情况。
- prefill 吞吐随长度衰减 -10.8%（1589.8 → 1417.4），符合长序列注意力开销，无断崖。
- decode 全程稳在 66.9~70.2 tok/s。

### 2.4 探针命中率：远端检索没有衰减

三根暗号埋在正文 **2% / 50% / 97%** 处，问句在文末。

| 级别 | 探针所在行（共 N 行） | 首端 | 中段 | **远端 97%** |
|---|---|---|---|---|
| 32k | 25 / 616 / 1195（共 1232 行） | ✅ | ✅ | ✅ |
| 128k | 97 / 2423 / 4701（共 4846 行） | ✅ | ✅ | ✅ |
| 256k | 187 / 4682 / **9082**（共 9363 行） | ✅ | ✅ | ✅ |

256k 的实际回答（全文 20 字符）：

```
LAPIS-37, AMBER-91, INDIGO-52
```

> [!IMPORTANT]
> **这一节第一次是错的，必须记下来。**
> 初版造探针脚本时，插入位置是用「预算估算行数」算的，但写正文的循环有 `total >= limit` 的提前 break，
> 实际行数永远少于估算值 → 97% 那根针的行号越界，**从来没有真正写进 prompt**。
> 表现是「模型答不出第三根针」，看起来像远端遗忘，其实是自己没把问题问出去。
> 修法：先空跑一遍量出实际写入行数，再按实际行数算位置，并在发请求前断言「所有针都在 prompt 里」。
> 同样的问题也会出现在「用无关样本校准字符/token 比例」上——本机用中文样本×60 得 2.021，用真实模板得 **1.397**，按前者生成会直接超标 45%。

### 2.5 资源占用（256k 跑完）

| 指标 | 32k 前 | 256k 后 |
|---|---|---|
| `commit_used` | 65,822 MiB | **68,452 MiB** |
| `commit_limit` | 98,214 MiB | **98,214 MiB（恒定）** |
| 物理内存剩余 | 14,619 MiB | **12,008 MiB** |
| 引擎进程 WS | 39,042 MiB | 41,782 MiB |
| KV streaming 峰值 | — | **98.57% 命中，仅 636.9 MiB 走 RAM** |

**无 OOM，pagefile 没有被动态扩容**——这正是「固定 32768 MB」的价值所在。

---

## 三、ComfyUI 盘位迁移：robocopy + mklink /J

### 3.1 为什么不用 `extra_model_paths.yaml`

ComfyUI 支持在 `extra_model_paths.yaml` 里加新的模型搜索路径，看起来是更"官方"的做法。但它有致命短板：

`utils/extra_config.py:34` 只调用 `folder_paths.add_model_folder_path()` 改**注册表**，覆盖不了那些直接字符串拼接 `folder_paths.models_dir` 的节点代码（RMBG / LLM / LTXVideo / KJNodes / IPAdapter 等 6 处）。结果是**半迁移**：主流 loader 走新路径，这几个插件还在读旧目录，D 盘清不干净。

而且 `add_model_folder_path()` 是 `append` 语义，不写 `is_default: true` 时原目录仍排第一，天然会回落。

**junction（`mklink /J`）是文件系统层重定向，对上层 100% 透明**，所有拼接、所有插件、所有 `os.walk` 一视同仁。所以选它。

### 3.2 操作序列

```bat
REM 1. 先复制，源目录不动
robocopy "D:\ComfyUI\models" "B:\ComfyUI-models" /E /R:2 /W:5 /MT:4 /NP /TEE /LOG:"robocopy.log"
REM 退出码 0-7 都算成功（1 = 有文件被复制），8+ 才是失败

REM 2. 逐目录校验两边件数/字节/文件名集合全等，再往下走

REM 3. 改名保留回滚点
move "D:\ComfyUI\models" "D:\ComfyUI\models.old"

REM 4. 原位建 junction
mklink /J "D:\ComfyUI\models" "B:\ComfyUI-models"
```

实测（65.22 GiB / 51 文件）：

| 步骤 | 耗时 |
|---|---|
| robocopy | **2 分 17 秒**，690 MB/s |
| 改名 | 瞬时（同盘，无数据搬迁） |
| 建 junction | 瞬时 |
| 经 junction 真读大 safetensors | 头部合法，链路通 |

### 3.3 两条绝对红线

> [!CAUTION]
> **红线 1：不能 `rm -rf` / `shutil.rmtree` 删 junction。**
> 那会顺着链接把**目标盘的真身数据全删掉**。删 junction 只能用 `rmdir`（不带 `/s`）或 `Remove-Item`（不带 `-Recurse`）。

> [!CAUTION]
> **红线 2：不能对 junction 源路径跑 `robocopy /MIR`。**
> `/MIR` 是镜像语义，会**反向**把内容写回目标——如果源是"空目录"，等于清空目标盘。

### 3.4 验收

```bash
curl -s http://127.0.0.1:8188/object_info \
  | python -c "import sys,json; d=json.load(sys.stdin); print(len(d['CheckpointLoaderSimple']['input']['required']['ckpt_name'][0]))"
```

比对 ComfyUI 的 loader 下拉列表与磁盘上的文件清单，**双向都要查**：

- 「磁盘有但列表没有」→ junction 或路径没生效
- 「列表有但磁盘没有」→ 缓存残留或路径错位

两个坑：①`/object_info` 的分隔符可能是 `/` 也可能是 `\`，比对前必须统一；②新版 ComfyUI 把下拉结构从 `[["a","b"], {...}]` 改成了 `["COMBO", {"options":["a","b"]}]`，只判断 `entry[0]` 是不是 list 会**静默跳过新版节点**，误报"节点不存在"。另外 `VAELoader` 列表里的 `pixel_space` 是内置虚拟 VAE（无对应文件），必须加白名单，否则必报一条假 EXTRA。

---

## 四、显存分时复用红线

### 4.1 一句话红线

**日常轻量共存，硬核长文切独占。**

| 场景 | 做法 |
|---|---|
| 查文档、写提示词、联合调试（短问短答） | ✅ **直接共存**，54 tok/s 够用，不折腾 |
| 超长代码库 / 几十万字文档（> 1 万 token） | ❌ **必须先手动关掉 ComfyUI 进程** |
| 连续抽卡出图 | ComfyUI 优先，要腾显存就停引擎 |

### 4.2 实测数据

场景：ComfyUI 保持在线（`POST /free` 后仍占约 1.8 GB），直接拉起 Gyro-M。

| 项 | 独占 | 共存 | 变化 |
|---|---|---|---|
| 冷启动到 ready | 42 秒 | **51 秒** | +9 秒 |
| `expert cache slots` | **1394** | **894** | **−35.9%** |
| prompt path 借用 slots | 993（1.21 GiB） | 676（0.82 GiB） | −32% |
| prefill chunk | 2048 tokens | **1024 tokens** | 减半 |
| prefill 耗时（≈3.2 万 token） | 20,299 ms | **37,780 ms** | **+86%** |
| prefill 速度 | 1589.8 tok/s | **863.6 tok/s** | **−45.7%** |
| decode 速度 | 68.2 tok/s | **54.3 tok/s** | −20.4% |
| cache 命中 | 68.2% | 55.2% | −13 pt |
| OOM / `#2065` 告警 | — | **零** | — |

引擎自己发现了余量不足并主动缩容：

```
expert cache auto: 3.91 GiB free, 1728 MiB reserved (...) -> 1394 slots
only 1169 MiB free once the slots are written (reserve 1728 MiB); shrinking the expert cache
expert cache 894 slots, 1.09 GiB of VRAM
```

这是保护机制，不是故障。但它如实说明：**ComfyUI 那 1.8 GB 确实挤到了它。**

### 4.3 关键认知：`/free` 清不掉 CUDA context

```bash
curl -X POST http://127.0.0.1:8188/free \
  -H "Content-Type: application/json" \
  -d '{"unload_models": true, "free_memory": true}'
```

实测：8435 MiB → 1811 MiB，回落 6624 MiB，ComfyUI 仍在线。

**但那剩下的 1.8 GB 是 CUDA context，只要进程活着就释放不掉。** 所以「点一下 `/free` 就够了」这个假设**不成立**。

还有一个 API 细节：`/free` **不是同步释放**。它只在 `prompt_queue` 上 `set_flag`，由主循环下一轮才执行 `unload_all_models()` + `executor.reset()`：

```python
# main.py:401-408
flags = q.get_flags()
free_memory = flags.get("free_memory", False)
if flags.get("unload_models", free_memory):
    comfy.model_management.unload_all_models()
    need_gc = True
```

所以脚本里必须**轮询 `nvidia-smi` 等待**，不能发完请求就宣布"已释放"。

### 4.4 不要动的启动参数

| 参数 | 结论 |
|---|---|
| `--lowvram` | **空操作**。`cli_args.py:169` 原文："Doesn't do anything if dynamic vram is enabled"，本机 dynamic vram 确实开着 |
| `--normalvram` | **不存在**。0.30.2 的 vram 互斥组只有 `--gpu-only` / `--highvram` / `--lowvram` / `--novram` / `--cpu`，全仓 grep 零命中 |
| `--vram-headroom` / `--reserve-vram` | 会让出图更容易 offload 到内存、掉速。与「保出图速度优先」冲突 |

现有 `--use-sage-attention --cuda-malloc` 保留即可。

---

## 五、这套栈的整体形态

```
┌────────────────────────────────────────────────────────────────────┐
│  DSH Desktop GUI                                                   │
│    ├─ Agent preset layer: maid-local-memory (13 tools / 2.9K)      │
│    └─ bili (billion-context): 压缩层，context 262144, output 16384 │
└────────────────────────────┬───────────────────────────────────────┘
                             │ OpenAI-compatible /v1
                             ▼
┌────────────────────────────────────────────────────────────────────┐
│  Lazy-loading proxy  :8090                                         │
│    · 首个请求 → 拉起引擎 → ready 后转发                            │
│    · 等待期发 SSE 注释行保活                                       │
│    · 闲置 900s → strata launch --stop                              │
└────────────────────────────┬───────────────────────────────────────┘
                             │ :8081
                             ▼
┌────────────────────────────────────────────────────────────────────┐
│  Strata Engine  gyro-m-256k                                        │
│    --max-context 262144 --kv int8 --kv-resident 32768              │
│    --vram-reserve-mib 1728  →  expert cache 1394 slots             │
│    模型 92 GB，常驻 29.88 GiB，KV pinned RAM 3.09 GiB              │
└────────────────────────────────────────────────────────────────────┘

┌────────────────────────────────────────────────────────────────────┐
│  ComfyUI  :8188  (并行运行，显存分时复用)                          │
│    · 模型库经 junction 指向 PCIe 4.0 盘                            │
│    · 8 GB 显存占用；出图前停引擎 / 聊天前 POST /free               │
└────────────────────────────────────────────────────────────────────┘
```

---

## 六、盘位分配建议

| 盘 | 类型 | 实测顺序读 | 放什么 |
|---|---|---|---|
| 系统盘（NVMe PCIe 4.0） | 3985 MB/s | 3985 | **大模型权重 + pack + pagefile** —— prefill 要反复读盘，盘速直接决定 TTFT |
| 第二 NVMe（PCIe 4.0） | 2205 MB/s | 2205 | ComfyUI 模型库（冷数据，读一次驻留显存） |
| SATA SSD | 478 MB/s | 478 | 备份、归档 |
| USB 外置 | 慢 | — | 冷归档 |

**判断依据**：256K 冷 prefill 时引擎日志会出现 `file set 18.1 GiB does not fit the page cache 3.1 GiB -> about 116.5 GB read from the drive per 32768-token prompt`。按每 32K prompt 读 116 GB 算：

- 3985 MB/s → 约 29 秒
- 2205 MB/s → 约 53 秒
- 478 MB/s → 约 4 分钟

所以**大模型必须在最快的盘上**，ComfyUI 模型库可以次一等。

---

## 七、可以搬走的经验

1. **commit limit 不是物理内存。** Windows 下跑大模型，先看 `Get-CimInstance Win32_OperatingSystem` 的 `TotalVirtualMemorySize`，别只看 RAM。
2. **同一份引擎日志里混着多次启动。** 读性能数字前先按 `expert cache auto` / `serve: N MiB of VRAM free` / `verify` 分段归位，否则必然张冠李戴。
3. **造长上下文探针时，插入位置要按「实际写入行数」算**，而且发请求前先断言所有探针都在 prompt 里。
4. **压测数据要从能拿到真值的那一层取。** 只存响应头的日志永远给不了 `finish_reason`；引擎日志和代理 SSE 才是真值来源。
5. **迁移大目录：先复制、再改名、最后建链接。** 永远给自己留一个 `.old` 回滚点，等端到端跑通了再删。

---

<div align="center">

*25 万 token 进去，3 分钟不到开始说话；旁边还跑着一个 8 GB 的生图后端。中间没有一次 OOM。*

</div>
