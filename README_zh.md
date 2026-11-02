<div align="center">

# DSH Local FullStack · 128K

[English](README.md) | **简体中文**

**把一台 32GB 内存 + 12GB 显存的消费级机器，调成「128K 上下文 + 2.1K 工具开销 + 长效记忆 + 引擎按需起停」的本地 Agent 工作站**

`DeepSeek Harness` · `Strata` · `billion-context` · `Qwen3.8-Flash-Next`

</div>

---

> [!WARNING]
> **本项目包含本地绝对路径、端口与模型名配置。部署前请根据实际环境修改。**
> 归档里的 `C:\Users\YOUR_USERNAME`、`STRATA_HOME`、`YOUR_HOSTNAME` 都是占位符，不是可直接运行的值。
> 所有配置片段都是**摘录**，请合并进你自己的配置文件，不要整文件覆盖。

---

## 这个项目解决什么问题

本地跑大模型做 Agent，通常卡在四个地方：

| 症状 | 真实原因 |
|---|---|
| 模型说记不住三分钟前的话 | 上下文窗口只有 32K，长文一进去就撞墙 |
| 每次请求光工具定义就吃掉 11K tokens | 默认预设挂了 65 个工具，光 schema 就 15.2K |
| 换个会话就忘了你是谁 | 没有跨会话的持久记忆 |
| 显存被模型常驻吃满，想干别的都不行 | 引擎起来就不走了 |

这个仓库是这四件事的一套完整解法，外加一个**不碰引擎**的守护脚本。

---

## 全景链路

```
┌──────────────────────────────────────────────────────────────────────┐
│  DSH Desktop (GUI)                                                    │
│  http://127.0.0.1:RANDOM_PORT   每次重启都会变，不是固定值             │
│                                                                       │
│  ┌────────────────────────────────────────────────────────────────┐  │
│  │  Agent Preset 层                                                 │  │
│  │                                                                  │  │
│  │   maid（默认，65 工具 / 15.2K tokens）                            │  │
│  │   maid-local-lite    （10 工具 / 2.1K）  ← 纯瘦身                 │  │
│  │   maid-local-memory  （13 工具 / 2.9K）  ← 瘦身 + 长效记忆 ★      │  │
│  │                                                                  │  │
│  │   瘦身靠 tools.restrict({deny}) + 自愈状态机，不靠切 preset 树     │  │
│  └────────────────────────────────────────────────────────────────┘  │
│                              │                                        │
│                              │ OpenAI-compatible /v1                  │
│                              ▼                                        │
│  ┌────────────────────────────────────────────────────────────────┐  │
│  │  bili (billion-context)   管理端点 127.0.0.1:18788              │  │
│  │  · 上下文压缩：maxContextLimit = 90%（131072 × 0.9 ≈ 118K 才动手）│  │
│  │  · 模型窗口：models.MODEL_ID.context = 131072                   │  │
│  │  · 上下文观测 / 折叠 / 注入块管理                                 │  │
│  └────────────────────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────────────────────┘
                               │
                               │ baseURL http://127.0.0.1:8090/v1
                               ▼
┌──────────────────────────────────────────────────────────────────────┐
│  懒加载代理  proxy/strata-on-demand-proxy.py  :8090                   │
│  · 收到第一个请求 → detached 拉起引擎 → 就绪后原样转发                │
│  · 等待期间向客户端吐 SSE 注释行保活（客户端不会超时断开）             │
│  · 闲置 900s（15 分钟）→ `strata launch --stop` 卸载引擎             │
└──────────────────────────────────────────────────────────────────────┘
                               │
                               │ http://127.0.0.1:8081
                               ▼
┌──────────────────────────────────────────────────────────────────────┐
│  Strata Engine  gyro-s-128k                                          │
│  args: --max-context 131072 --kv int8 --kv-resident 32768            │
│        --resident-experts --vision --vram-reserve-mib 1728           │
│                                                                       │
│  模型：Qwen3.8-Flash-Next-Gyro-S  Q4 量化  58 GB                      │
│  显存：12GB 卡加载后仅剩约 1GB                                        │
│  内存：专家常驻受 Windows commit limit（35.3 GiB）压制                │
└──────────────────────────────────────────────────────────────────────┘
```

**关键点：控制面与数据面彻底解耦。**
守护脚本只管 8090（代理进程），永远不碰 8081（引擎）。引擎该起来时由代理按请求拉起，该走时由代理按闲置计时卸载。谁都不能为了「保持在线」把引擎钉在显存里。

---

## 四根支柱

### 一、128K 上下文

| 层 | 位置 | 值 |
|---|---|---|
| 引擎 | `proxy/strata-on-demand-proxy.py` 的 `STRATA_LAUNCH_ARGS` | `"--context", "128k"` |
| 引擎 profile | `%LOCALAPPDATA%\Strata\launch\gyro-s-128k.json` | `--max-context 131072` |
| DSH | `cordis.patch.yml` 的 `llm-pi-ai` | `defaultContextWindow: 131072` / `contextWindow: 131072` |
| bili | `billion-context.json` | `context: 131072`, `output: 8192` |
| bili 压缩阈值 | 同上 | `maxContextLimit: "90%"` |

- `maxTokens` / `output` 全程保持 **8192**，没跟着放大 —— 输出侧不需要那么多。
- 改 `--context` 会让 Strata 新建一个 profile 文件（`gyro-s-32k` → `gyro-s-128k`），旧文件留着可回滚。
- **改参数的顺序有坑**：必须先停引擎，再停代理进程，再改文件。代理探活到旧实例会直接复用，光改文件不生效。

### 二、工具瘦身 2.1K

默认预设是 65 个工具 / 15.2K tokens。瘦身版只留 10 个 / 2.1K。

```
maid-local-lite  KEEP（10）：
  compress  edit  glob  grep  ops_bash  ops_discover_tools
  read  read_image  working_directory  write

maid-local-memory  KEEP（12 → 运行时 13）：
  上面 10 个 + memory_remember  memory_search  memory_read
```

**为什么不用「切 preset 树」而用 `tools.restrict({deny})`：**
GUI 新建会话是两步 —— 先 `session/create`（默认 preset 是 maid），再 `agentPresets/select`。`recompose()` 只 rebind generation，不重发 `agent/created`，插件在 created 时看到的还是 maid，会直接早退，deny 层压根没建。结果就是「以为裁了，其实 45 个工具一个没少」。

自愈状态机的四件套（两个预设共用同一份实现）：

| 组件 | 作用 |
|---|---|
| `wantState()` 双判据 | `composedPreset` 优先，undefined 才回退 session 投影 |
| `syncingAgents` (WeakSet) | per-agent 重入锁，防递归风暴；不用全局锁，多 agent 不互相饿死 |
| 三表 `seenMap`/`handlesMap`/`appliedMap` | `seen`（已 deny 名单）与 `handles`（待撤销句柄）分离，撤销不清 seen |
| `live` Set + `alive()` | agent scope key 永不重建，deny 层跨 preset 存活，必须显式 dispose |

`agent/pre-step` **无条件**调 `guarded()`（不用 `synced`/`dirty` 节流）—— 节流会让「切回默认预设」那次 release 永远等不到。

### 三、长效记忆

底层是 `dsh-maid-preset` 内置的 meow 子模块（不是独立包），挂在 host 全局层，所以能被 `restrict` 裁。

五条链路：

```
① 首轮注入     agent/pre-step 里 priorUser===0
              → buildInjection → 快照消息插在首条 user 之前
              （soul + user + 全局 rules + project 名单）

② 显式读写     memory_remember / memory_search / memory_read
              （完整版还有 update / find_similar / project / home / dream）

③ 自动反思     agent/turn-stopping：连续工具步 ≥ reflectTurns 且上个工具不是 memory_*
              → 发 [meow-memory-reflect] 独立 turn，跑 prompts/zh/reflect.md

④ dream       scheduleDream：idle 180 分钟触发，峰时抑制

⑤ 压缩后重注入  compaction/end → 标记，下轮 pre-step 重新注入
```

存储：SQLite `.dsh-meow/memory.db`（七层表 soul / user / project / fact / lesson / topic / rules），不是向量库。

**省 token 的取舍**：只装三个记忆工具（remember 428 + search 307 + read 69 = **804 tokens**）。刻意省掉了 `memory_update`(222) / `memory_find_similar`(131) / `memory_project`(122) / `memory_home`(131) / `memory_dream`(85)，共 691 tokens。

代价是反思轮没法用 `memory_update` 做「同一结论改写」。补偿方案：`AGENTS.md` 里写五条规则 —— 先 search 查重 → 有近似就把新信息写进 content 标 `★更新` 再 remember（靠 0.8 相似度自动合并，等价于软 update）→ 没有近似才新建 → 不硬凑 → 要看全景就说明「需要 memory_project」。

> 60 tokens 的规则补丁换 222 tokens 的工具预算，划算。

### 四、按需拉起：代理与引擎

```
DSH 发请求
   ↓
8090 代理：引擎没跑？
   ├─ 是 → detached 拉起 strata launch（profile gyro-s-128k）
   │       → 等就绪（上限 240s），期间向客户端吐 SSE 注释保活
   └─ 否 → 直接转发
   ↓
8081 引擎处理
   ↓
请求结束，重置 last_activity
   ↓
idle_watcher 每 30s 检查：闲置 ≥ 900s？
   ├─ 是 → 抢占卸载权 → strata launch --stop → 显存释放
   └─ 否 → 继续睡
```

守护脚本 `proxy/guard-proxy.vbs` 只探 8090，断了就静默跑 `start-proxy.vbs`；它对 8081 与引擎一无所知，也不会去「顺便检查一下引擎还活着吗」。

实测数据：

| 场景 | 结果 |
|---|---|
| 杀掉 8090 代理进程 | **24 秒**内守护拉起新实例 |
| 自愈那一刻的 8081 | **仍为空** —— 证明没碰引擎 |
| 发消息后 | 引擎按需冷启动，13 工具 / 2982 tokens 正常 |
| 闲置后 | 约 16 分钟（900s 阈值 + 30s 检查间隔）卸载 |
| 卸载后 | `strata status` → `{"running": []}`，显存回到 **997 MiB / 12288 MiB** |

---

## 已知的坑

### 坑 1：DSH 的 token 估算低 1.9~2.4 倍

| 同一请求 | DSH `contextTimeline` 报 | bili 实测 `[acp-usage] input=` |
|---|---|---|
| 12.4 万字 | 37727 | 89323 |
| 20.9 万字 | 76343 | 145986 |

DSH 按字符估，中文切词更碎。**判断会不会撞窗，看 bili 日志，别看 DSH 报数。** 按 DSH 报数大约 64000 就是真实撞窗点。

### 坑 2：超长输入不是 413，是 400

```
request exceeds the context window: prompt (145986 tokens)
leaves no room to answer in the context (131072); requests are never truncated
```

而且 bili 的预压缩很慢（实测 187 秒），会被客户端 300s 超时先打断，结果是「压缩没做完 → 请求被拒」。所以阈值从 65% 推到了 90%，把触发点从 8 万 token 推到约 11.8 万，让常规长文走极速路径。

### 坑 3：压缩摘要会抄错原文数字

实测把「青金石-三七」记成「绯红-17」。所以**超长上下文不能拿来做精确检索型任务**，压缩一旦发生，细节就不可信了。

### 坑 4：显存余量告警

```
only 865 MiB of VRAM free at the start of this request (below 1024 MiB) ...
decode falls to a few tok/s and a window may never finish
```

`vram-reserve-mib` 已从 1536 提到 **1728**。这是「不卡死」的底线，不是速度保证。

### 坑 5：Windows commit limit 才是真天花板

```
resident complement 20.88 GiB exceeds available commit (9.22 GiB) minus the 4 GiB safety headroom
Windows commit: 4.2 GiB left of a 35.3 GiB limit
about 121.3 GB read from the drive per 32768-token prompt
conversation cache off: not enough free RAM
```

32GB 物理内存 + 默认页面文件 → commit limit 只有 35.3 GiB。想跑得更从容要加内存或加大页面文件，不是改代码。

### 坑 6：`engine_usable: false` 是假警报

`strata launch --plan` 在某些 shell 宿主里拿不到 GPU 信息，会报 "did not pass Strata's check on this computer"。引擎实际跑得好好的，忽略。

### 坑 7：DSH 重启后 8090 代理不会自己回来

症状：发消息后模型转两分钟一个字不出。
修法：跑一次 `start-proxy.vbs`。这正是 `guard-proxy.vbs` 要解决的问题。

### 坑 8：bili 管理端点不在 DSH 端口上

`curl http://127.0.0.1:<DSH端口>/__bili/config` → `{"error":"admin relay disabled"}`（`security.adminRelay` 默认关）。
正确地址是 **18788**：`curl http://127.0.0.1:18788/__bili/config`。

### 坑 9：VBS 里用 `findstr` 判端口退出码不可信

`cmd /c "... | findstr ... >nul && exit 0 || exit 1"` 的嵌套引号会在 `cmd /c` 层被吞掉，端口明明空着也可能返回 0。可靠做法是把 `netstat` 输出落成临时文件，再用 `InStr` 逐行判断。

---

## 案例：退役旧的 Ollama 链路

在这套栈搭起来之前，机器上跑着一个 Ollama 9B 当应急兜底。128K 的 Strata 链路接管之后，它每一字节都是纯浪费。下面是完整拆除过程——「删掉旧东西」从来不像听起来那么简单。

**回收 19.48 GiB。** 这些资产并不都在你以为的地方：

| 位置 | 大小 | 是什么 |
|---|---|---|
| `~/.ollama` | 7.44 GiB | 3 个 tag，共用同一份 7.36 GB 权重 blob + 624 MB mmproj |
| `<DSH_HOME>/our-free-model/_deploy` | 8.98 GiB | 同一份 GGUF 的**第二份拷贝**（不是硬链接）+ 1.6 GB 安装包 |
| `%LOCALAPPDATA%/Programs/Ollama` | 3.06 GiB | 程序本体 |

`_deploy` 那份最容易漏。它是手工 `ollama create` 时留下的，因为只是普通拷贝而非硬链接，光删 Ollama 模型会留下 7.5 GiB 的孤儿文件。

**先查依赖，再动手。** 删之前先确认没人还在指着那个端口：

```bash
grep -rn "11434" ~/.dsh/profiles/*/cordis.patch.yml
curl -s http://127.0.0.1:18788/__bili/config | grep 11434
```

这次 DSH profile 是干净的，但还有两层在引用：`our-free-model` 的 `settings.json`（一个 `aggregate.providers.ollama` 条目，外加一个把 9B 拉进模型目录的兜底 combo），以及 bili 配置（`localhost`、`127.0.0.1`、`mitm://` 三条路由）。**只清文件不清路由的话，模型会继续在选单里挂着，bili 也会继续拿一个已经不存在的窗口去做压缩判断。**

**拆除顺序**——`tools/cleanup-ollama-9b.py` 把它自动化了：

```
--backup   →  每个要改的文件先存 *.bak-<时间戳>
--config   →  删 aggregate provider、兜底 combo、11434 三条路由
--stop     →  结束进程，静默跑卸载器
--path     →  清 PATH 条目、启动目录快捷方式、OLLAMA_* 环境变量
--archive  →  把一次性补丁目录和调试脚本挪出工作区
--purge    →  rmtree 缓存目录（需要手输 PURGE）
```

脚本里有两条值得抄走的规则：

1. **写回 bili 配置前，如果发现 8090 provider 不见了就直接中止。** 一个会悄悄删掉你生产路由的清理脚本，比没有脚本更糟。
2. **`--purge` 必须手输 `PURGE`。** 其它步骤都是命令行开关，不可逆的那一步不是。

**拆除后验证：**

```bash
curl -s http://127.0.0.1:18788/__bili/config    # 应该只剩 2 个 provider，8090 的 context 是 131072
ls ~/.ollama ~/.dsh/our-free-model/_deploy      # 两个都应该没了
```

注意 `our-free-model` 的 `settings.json` **不会热重载**——插件在 apply 时读一次。如果拆完 9B 还在模型目录里，重启一次 DSH。bili 是会热重载的：`POST http://127.0.0.1:18788/__bili/config/reload`。

**有一件事是故意不动的：** `ofm-effort-menu-patch` 目录归档了，但里面的补丁**没有回滚**。它早就应用进插件源码了，本身无害；回滚等于往一个正常工作的系统里塞一个新变量。归档工具，保留运行态。

---

## 真实用量红线

以 bili 实测 `[acp-usage] input=` 为准：

| 输入量 | 真实 token | 表现 |
|---|---|---|
| < 5 万汉字 | < 4 万 | 极速 |
| 5–8 万汉字 | 4–8 万 | 正常 |
| 8–11 万汉字 | 8–11 万 | 变慢 |
| **> 12 万 token（约 25 万汉字）** | **撞墙** | 触发压缩 → 超时 → 400 |

---

## 仓库结构

```
.
├── README.md
├── README_zh.md                        简体中文版
├── AGENTS.md                          工作区记忆写入规则（配合三件套记忆工具）
├── .gitignore
├── config/
│   ├── cordis.patch.example.yml       DSH profile 配置摘录（模型 + meow-memory）
│   └── billion-context.example.json   bili 压缩阈值与模型窗口
├── plugins/
│   ├── maid-local-lite/               瘦身预设（10 工具）
│   │   ├── package.json
│   │   ├── cordis.patch.yml
│   │   └── lib/index.js
│   └── maid-local-memory/             瘦身 + 记忆预设（13 工具）
│       ├── package.json
│       ├── cordis.patch.yml
│       └── lib/index.js
├── proxy/
│   ├── strata-on-demand-proxy.py      8090 懒加载代理（纯标准库）
│   ├── start-proxy.vbs                静默启动器（pythonw，无控制台）
│   └── guard-proxy.vbs                守护脚本（只探 8090）
└── tools/
    └── cleanup-ollama-9b.py           旧 Ollama 兜底链路的拆除脚本（默认 dry-run）
```

---

## 部署步骤

1. **装插件**：把 `plugins/maid-local-lite` 与 `plugins/maid-local-memory` 放进 DSH 工作区，在 profile 的 `package.json` 里用 `link:` 指向它们，并把两个包名加进 `dsh.bundle`。
2. **改配置**：把 `config/cordis.patch.example.yml` 里的 `llm-pi-ai` 与 `meow-memory` 两块合并进你自己的 `~/.dsh/profiles/PROFILE/cordis.patch.yml`；把 `config/billion-context.example.json` 合并进 `~/.config/billion-context/billion-context.json`。
3. **改路径**：`proxy/` 下三个文件里的 `STRATA_HOME` 换成你机器上的实际安装路径。
4. **起代理**：双击或 `wscript.exe proxy/start-proxy.vbs`，确认 8090 在听。
5. **装守护**：把 `guard-proxy.vbs` 的快捷方式丢进 `shell:startup`（目标 `wscript.exe`，参数是 vbs 全路径，工作目录设成 proxy 目录，窗口最小化）。
6. **重启 DSH**，新建会话选 `maid-local-memory`。

---

## 验收基线

| 项 | 值 |
|---|---|
| 默认 maid 预设 | 65 工具 / 15162 tokens |
| maid-local-lite（header 直设） | 10 / 2142 |
| maid-local-lite（GUI 两步） | 12 / 2860 |
| maid-local-memory（两条路径） | 13 / 2982 |
| 长文 N=2500 | bili input 89323，三暗号全对 |
| 长文 N=2300（90% 阈值后） | bili input 83163，零预压缩，三暗号全对 |
| 长文 N=4200 | bili input 145986，400 撞窗 |

---

## 许可

MIT。配置片段与脚本按原样提供，不保证适配任何特定机器。

---

<div align="center">

*控制面与数据面分离。代理管代理，引擎管引擎。显存该空着就空着。*

</div>
