/**
 * maid-local-lite — 给本地小模型用的「工具瘦身」bundle。
 *
 * 两件事：
 *   1. cordis.patch.yml 注册一个独立 preset `maid-local-lite`（不碰正式 maid）。
 *   2. 本文件把「跑在这个 preset 上的 agent」的工具集合裁到 KEEP —— 用
 *      agent.ctx.tools.restrict({ deny })。
 *
 * 为什么要 restrict 而不是只靠 preset：
 *   preset 只能增删 **preset 级** 工具；host 插件（dsh-ops / billion-context /
 *   meow-memory / dsh-image-gen / dsh-free-search …）注册的工具不受 preset 管辖。
 *   restrict 是唯一能裁掉它们的机制（它过滤 scope 继承来的工具）。
 *
 * 为什么用 deny 白名单反写、而不是 allow 白名单：
 *   agent/created 触发时 preset 的插件树还没把 read/write/edit 注册上来，
 *   此刻用 allow 定死白名单会把「尚未注册的必需工具」永久挡在外面（实测踩过）。
 *   deny = 当前已知名字 − KEEP，对后注册的工具天然安全；每次 step 前重算一次，
 *   晚注册的工具下一轮就会被 deny 掉。多层 deny 只取并集，不会越裁越少，
 *   因此这里不做 dispose/重建，只在集合扩张时补一层。
 *
 * 为什么还要「自愈」（2026-11-01 补）：
 *   GUI 新建会话走的是两步——先 session/create（拿到默认 preset maid），
 *   再 agentPresets/select('maid-local-lite')。recompose 只 rebind 了 preset 的
 *   generation，不会重发 agent/created，于是 agent/created 那一刻
 *   presetOf() 还是 maid、sync 早退，deny 层压根没建 → tools 停在 45 个。
 *   所以判据放宽到 composedPreset 拿不到时回退 session 投影，并把
 *   tools/change 的遍历集合从 tracked 换成 live（created 时早退也不丢注册）。
 *
 * 为什么要显式撤销（2026-11-01 补）：
 *   tools.restrict 注册的 effect 归属 agent scope 的 fiber，而 agent scope 的
 *   key 是 sessionId、永不重建；rebind 只改 parent 链，collect() 也只 dispose
 *   旧 preset generation 的 scope。所以 deny 层跨 preset 切换存活 ——
 *   切回 maid 后必须逐个调用 restrict() 返回的精确 disposer 撤销，
 *   否则真 maid 会被残留的 deny 裁成 lite。
 *
 * 为什么状态拆成三张表 + 重入锁（2026-11-01 第二版，踩坑后重写）：
 *   上一版把「已处理」状态和「待撤销句柄」塞进同一个 record，切 preset 时
 *   release() 连带 state.delete(agent)，seen 一起没了 —— 下一轮 fresh 又算
 *   出同样的 35 个，同一层 deny 反复建了拆 420 次。
 *   而且上一版把 pre-step 的节流整个拆掉，pre-step 无条件调 sync；sync 里
 *   restrict() 会同步 emit tools/change（ScopedLayers.effect 的 generator 是
 *   同步执行的），tools/change handler 又回头调 sync —— 同步递归，直接
 *   Maximum call stack size exceeded。原来那个节流虽然制造死锁，同时也挡着
 *   这条回环。
 *   所以这一版：
 *     - seen / handles / applied 三张表分开，release 只碰 handles 和 applied；
 *     - sync 用 per-agent 的 syncingAgents WeakSet 做重入锁（不用全局标志，
 *       否则多 agent 并发时会把别人的 sync 吃掉）；
 *     - 判据只用 composedPreset，拿不到（undefined）才回退投影，避免过渡态震荡。
 */

const PRESET_ID = 'maid-local-lite'

/**
 * 保留集（取舍依据见报告「保留/移除清单」）：
 *   read / write / edit        文件读写改
 *   glob / grep                文件搜索
 *   read_image                 视觉输入（任务书要求保留实际可用的视觉工具）
 *   working_directory          目录定位
 *   ops_bash                   命令执行（Windows 上 tool-pwsh 被 dsh-ops 的
 *                              deny-host-shell 挡住，实际可用的是 host 的 ops_bash）
 *   compress                   maid 里 compaction-basic 是 disabled，32K 窗口下
 *                              它是唯一能腾地方的手段，删了会把「窗口不够」变死局
 */
const KEEP = Object.freeze([
  'read',
  'write',
  'edit',
  'glob',
  'grep',
  'read_image',
  'working_directory',
  'ops_bash',
  'compress',
])

const KEEP_SET = new Set(KEEP)

/** 当前可被 restrict 的名字集合；拿不到返回 undefined。 */
function restrictableNames(agentCtx) {
  try {
    const view = agentCtx.tools.view(agentCtx)
    if (view && view.restrictableNames instanceof Set) return view.restrictableNames
  } catch {
    /* view() 的实现细节可能变；拿不到就跳过这一轮 */
  }
  return undefined
}

function presetOf(ctx, agentCtx) {
  try {
    return ctx.get('agentPresets')?.composedPreset?.(agentCtx)
  } catch {
    return undefined
  }
}

/**
 * Session 投影记录的 preset。GUI 的「先 create 再 select」路径下，
 * composedPreset() 与这个投影会短暂不一致，两者取其一为 lite 即认。
 */
function chosenPreset(ctx, agent) {
  try {
    return ctx.get('sessionProjections')?.stateOf?.(agent?.session, 'agentPreset')
  } catch {
    return undefined
  }
}

/**
 * 这个 agent 现在该是什么状态：'lite' 还是 'maid'。
 *
 * composedPreset 是唯一权威。只有它彻底拿不到（undefined，比如启动早期
 * preset registry 还没装好）才退回 session 投影。
 *
 * 不能写成「两者任一为 lite 即认」：agentPresets.select() 的顺序是
 *   await recompose()  → 里面先 emit('tools/change')
 *   session.append('agent-preset/selected')  → 投影在这一步才更新
 * 两个判据在过渡瞬间会互相矛盾（实测同一个 agent 一分钟内
 * composed=lite/projected=maid 出现 423 次、反过来 421 次），
 * 于是 release 和 restrict 来回抖，seen 被反复清空，deny 层建了拆 420 次。
 */
function wantState(ctx, agentCtx, agent) {
  const composed = presetOf(ctx, agentCtx)
  if (composed !== undefined) return composed === PRESET_ID ? 'lite' : 'maid'
  return chosenPreset(ctx, agent) === PRESET_ID ? 'lite' : 'maid'
}

export function apply(ctx) {
  /**
   * 三张表分开，各管一件事：
   *   seen     agent -> Set<name>      已经 deny 过的名字。own layer 注册的工具
   *                                    restrict 永远裁不掉（view() 文档原话：
   *                                    a restriction never filters what its OWN
   *                                    layer registers），所以「可见 − KEEP」可能
   *                                    恒非空；不记就会每轮重复 append 一层 deny。
   *   handles  agent -> Array<dispose> restrict() 返回的精确撤销句柄。
   *   applied  agent -> 'lite' | 'maid' 上次 sync 结束时认定的状态，用来判断
   *                                    「是否真的发生了 lite → maid 的跃迁」。
   *
   * release() 只碰 handles 和 applied，**绝对不碰 seen** —— 上一版就是因为
   * release 顺手 state.delete 把 seen 抹了，切回 lite 时 fresh 恒空。
   */
  const seenMap = new WeakMap()
  const handlesMap = new WeakMap()
  const appliedMap = new WeakMap()
  /** 所有活着的 agent：tools/change 是全局限广播，得自己筛 */
  const live = new Set()
  /** 正在 sync 的 agent：restrict() 会同步 emit tools/change，靠它挡递归 */
  const syncingAgents = new WeakSet()

  const release = (agent) => {
    const handles = handlesMap.get(agent)
    appliedMap.delete(agent)
    if (handles === undefined) return
    const lifted = handles.length
    for (const dispose of handles.splice(0).reverse()) {
      try {
        dispose()
      } catch (error) {
        ctx.logger?.warn?.(`maid-local-lite: lifting a restriction failed — ${error?.message ?? error}`)
      }
    }
    handlesMap.delete(agent)
    if (lifted > 0) {
      ctx.logger?.info?.(
        `maid-local-lite: ${agent.session?.id ?? 'agent'} lifted ${lifted} restriction layer(s)`,
      )
    }
  }

  /** 廉价兜底：agent/disposed 万一漏发，按 session 存在性回收 live 条目。 */
  const alive = (agent) => {
    try {
      const sessions = ctx.get('sessions')
      const id = agent?.session?.id
      if (sessions?.get === undefined || id === undefined) return true
      return sessions.get(id) !== undefined
    } catch {
      return true
    }
  }

  const syncBody = (agent) => {
    const agentCtx = agent?.ctx
    if (!agentCtx?.tools?.restrict) return

    const sid = agent.session?.id ?? 'agent'
    const target = wantState(ctx, agentCtx, agent)

    // 离开 lite：只在「确实建过层」时撤销，不在过渡态里无脑 release
    if (target === 'maid') {
      if (appliedMap.get(agent) === 'lite') {
        release(agent)
      }
      appliedMap.set(agent, 'maid')
      return
    }

    // 进入 lite
    const handles = handlesMap.get(agent) ?? []
    handlesMap.set(agent, handles)

    // 上一轮已被撤销干净（handles 为空）→ seen 是过期的，必须重置。
    // 重置放在这里而不是 release 里：release 永远不碰 seen。
    if (handles.length === 0 && seenMap.has(agent)) {
      seenMap.set(agent, new Set())
    }
    const seen = seenMap.get(agent) ?? new Set()
    seenMap.set(agent, seen)

    const known = restrictableNames(agentCtx)
    if (known === undefined) {
      appliedMap.set(agent, 'lite')
      return
    }

    const fresh = [...known].filter((name) => !KEEP_SET.has(name) && !seen.has(name))
    if (fresh.length === 0) {
      appliedMap.set(agent, 'lite')
      return
    }

    try {
      handles.push(agentCtx.tools.restrict({ deny: fresh }))
      for (const name of fresh) seen.add(name)
      ctx.logger?.info?.(
        `maid-local-lite: ${sid} denied ${fresh.length} (${fresh.join(', ')})`,
      )
    } catch (error) {
      ctx.logger?.warn?.(`maid-local-lite: tools.restrict failed — ${error?.message ?? error}`)
    }
    appliedMap.set(agent, 'lite')
  }

  const guarded = (label, agent) => {
    // per-agent 重入锁：restrict() 会同步 emit tools/change（ScopedLayers.effect
    // 的 generator 由 Cordis Fiber 同步执行完），不挡住就是同步递归爆栈。
    // 用 WeakSet 而不是全局布尔，否则多 agent 并发时会把别人的 sync 吃掉。
    if (syncingAgents.has(agent)) return
    syncingAgents.add(agent)
    try {
      syncBody(agent)
    } catch (error) {
      ctx.logger?.warn?.(`maid-local-lite: ${label} hook threw — ${error?.message ?? error}`)
    } finally {
      syncingAgents.delete(agent)
    }
  }

  ctx.on('agent/created', ({ agent }) => {
    live.add(agent)
    guarded('created', agent)
  })

  // preset 的插件树在 created 之后才把工具注册上来；GUI 的 preset 切换又是
  // 在 created 之后才下发 agent-preset/selected —— 两条路都靠这里补刀。
  ctx.on('tools/change', () => {
    for (const agent of live) {
      if (!alive(agent)) {
        release(agent)
        seenMap.delete(agent)
        live.delete(agent)
        continue
      }
      guarded('tools/change', agent)
    }
  })

  ctx.on('agent/pre-step', async ({ agent }, next) => {
    // 无条件调用。原先这里是 `if (!synced.has(agent) || dirty.has(agent))`，
    // 那套节流在「切回 maid」时没人清 synced、也没人置 dirty，条件恒假 →
    // release 永不执行。现在重入由 syncingAgents 挡，不需要入口节流。
    guarded('pre-step', agent)
    return next()
  })

  ctx.on('agent/disposed', ({ agent }) => {
    release(agent)
    seenMap.delete(agent)
    live.delete(agent)
  })
}

export const name = 'maid-local-lite'
export { KEEP as KEEP_TOOLS, PRESET_ID }
