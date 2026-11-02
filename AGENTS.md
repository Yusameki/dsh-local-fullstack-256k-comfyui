# 工作区约定（AGENTS.md）

## 记忆写入规则（本地轻量版）

适用于 `maid-local-memory` 预设：只有 `memory_remember` / `memory_search` / `memory_read` 三个记忆工具，**没有 `memory_update`**。

1. **写之前先查重**：调用 `memory_search` 搜核心词。命中相似条目（同一条结论、只是措辞不同）就不要新建。
2. **有近似条目 → 软更新**：把新信息整段写进 `content`，并在开头标 `★更新`，再调用 `memory_remember`。系统在内容相似度 ≥0.8 时会自动合并到旧条目，等价于 update。
3. **没有近似条目 → 新建**：`level` 按层级选（fact/lesson/topic/rules/project/user/soul），`project` 填项目名或「全局」，`keywords` 给 8-13 个。
4. **不要为交差硬凑**：真没有值得记的就回「无需记忆」，别写噪音挤占首轮注入。
5. 需要读某条全文用 `memory_read`；想按项目看全景时说明「需要 memory_project」，由主人在 GUI 里临时切预设。

> 为什么这么省：这三个工具共 804 tokens，9 个基础工具 2142，合计 2946 —— 本地 128K 模型下每一分上下文都要花在正事上。`memory_update`（222）+ `memory_find_similar`（131）+ `memory_project`（122）+ `memory_home`（131）+ `memory_dream`（85）共 691 tokens 被刻意省掉了。
