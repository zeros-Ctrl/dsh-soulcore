/**
 * dsh-soulcore —— 给 DeepSeek Harness 的 agent 一个独立人格 + 会慢慢变化的情绪 + 分层长期记忆。
 *
 * 挂在 DSH 的这几个点上（都只是旁路增强，后端挂了 DSH 照常工作）：
 *   systemPrompt.section   人设卡写进 system prompt（稳定内容，不破坏提示缓存）
 *   agent/pre-step         每轮第一步：把"这句话"交给后端 → 观察情绪事件 + 召回记忆
 *                          → 追加一条 snapshot 消息（与 dsh-time-context 同一种做法）
 *   session/event          turn/end 时把她这一轮的最终回复写回记忆
 *   llm/stream(compaction) 上下文压缩前，把即将被压掉的那段原文交给后端提炼落盘（防失忆）
 *   tools                  memory_search / remember / mood_status 三个工具
 *   webServer              /dsh-soulcore/api/* 给设置页用（信任栅栏 fail-closed）
 */
import { execFile } from 'node:child_process'
import fs from 'node:fs'
import path from 'node:path'
import { CoreProcess, resolvePython } from './core-process.js'
import { CORE_DIR, DSH_HOME, applySettings, ensureHome, publicConfig, readConfig, soulHome, writeConfig } from './config.js'
import {
  SOURCE_KIND, TOOL_OUTPUT, compactionMessages, formatSearch, isExcluded, isHumanUserMessage,
  sessionIdOf, snapshotMessage, textOf, toEvent,
} from './glue.js'

export const name = 'dsh-soulcore'
// webServer / connection 不是硬依赖：headless、SDK 等没有 Web 端的 profile 里也要能工作（设置页路由按需挂载）
export const inject = ['systemPrompt', 'tools']

export function apply(ctx, _config, deps = {}) {
  const home = soulHome()
  const paths = ensureHome(home)
  let cfg = readConfig(home)
  let personaText = readPersona()
  // 一律写 stderr：headless --json 模式下 stdout 是事件流，不能被插件日志污染
  const log = deps.log ?? ((lv, m) => { try { if (lv !== 'debug') console.error(`[soulcore] ${m}`) } catch {} })

  function readPersona() {
    try {
      const raw = fs.readFileSync(cfg?.persona?.card_file ?? paths.persona, 'utf8')
      return raw.replaceAll('{name}', cfg?.persona?.name || '小汐').trim()
    } catch { return '' }
  }

  // ───────────────────────── 后端子进程 ─────────────────────────
  const py = resolvePython(DSH_HOME, cfg.dsh?.pythonPath)
  // 锁文件放在 soulcore home（和 config.json 同级）：常驻进程先起时，插件直接接入它
  const lockPath = path.join(home, 'soulcore.lock')
  const core = deps.core ?? new CoreProcess({ python: py.file, coreDir: CORE_DIR, configPath: paths.cfgPath, lockPath, log })

  /**
   * 稳定块（人格锚点 / 她对自己的认识 / 主人画像）：一天才变一次。
   * 放进 system prompt 才能命中提示缓存；后端不可用时用旧缓存，没有缓存就只放人设。
   */
  const stable = { version: null, text: '' }
  let stableAt = 0
  let stableBusy = null
  function refreshStable(force = false) {
    if (cfg.dsh?.enabled === false || !core.ready) return Promise.resolve()
    if (stableBusy) return stableBusy
    if (!force && Date.now() - stableAt < 10 * 60 * 1000) return Promise.resolve()
    stableAt = Date.now()
    stableBusy = core.request('GET', '/v1/persona/stable', undefined, { timeoutMs: 3000 })
      .then((r) => {
        if (!r || typeof r.version !== 'string' || typeof r.text !== 'string') return
        if (r.version === stable.version) return       // 版本没变就不换，提示缓存继续命中
        stable.version = r.version
        stable.text = r.text
      })
      .catch((e) => log('debug', '拉取稳定块失败：' + (e?.message ?? e)))
      .finally(() => { stableBusy = null })
    return stableBusy
  }

  if (cfg.dsh?.enabled !== false) {
    core.start()
      .then(() => refreshStable(true))
      .catch((e) => log('error', '后端启动失败：' + (e?.message ?? e)))
  }
  // 返回 stop() 的 Promise：让宿主等子进程真正退出（释放 memory.db / 锁文件）再继续卸载
  ctx.effect(() => () => core.stop(), 'soulcore: core process')

  const enabledFor = (agent) => cfg.dsh?.enabled !== false && !!agent && !isExcluded(cfg, agent)

  /** sessionId → 最近一次的事件模板 / 本轮最后一条回复 */
  const lastEvent = new Map()
  const lastReply = new Map()
  const stats = { injected: 0, recorded: 0, compactions: 0, failures: 0, lastError: null }
  const fail = (where, e) => {
    stats.failures += 1
    stats.lastError = `${where}: ${String(e?.message ?? e).slice(0, 200)}`
    log('error', stats.lastError)
  }

  // ───────────────────────── 1. 人设进 system prompt ─────────────────────────
  let order = 10150
  try {
    const suffix = ctx.systemPrompt.getSectionOrder?.('DEPLOYMENT_PERSONA_SUFFIX')
    if (Number.isFinite(suffix)) order = suffix - 50
  } catch {}
  ctx.effect(() => ctx.systemPrompt.section({
    name: 'soulcore:persona',
    order,
    // 人设 + 稳定块（锚点/画像）：都是"一天才变一次"的内容，放在 system prompt 里命中缓存
    text: (c) => {
      if (!(cfg.dsh?.injectPersona !== false && personaText && enabledFor(c?.agent ?? c?.scope))) return ''
      return stable.text ? `${personaText}\n\n${stable.text}` : personaText
    },
  }), 'soulcore: persona section')

  // ───────────────────────── 2. 每轮注入心情 + 记忆 ─────────────────────────
  ctx.on('agent/pre-step', async (payload, next) => {
    // 先记下"这个会话属于哪个项目"：自动压缩就发生在下面的 next() 里，压缩钩子需要它来定位记忆分支
    try {
      const sid0 = sessionIdOf(payload.agent)
      if (sid0 && !lastEvent.has(sid0) && enabledFor(payload.agent)) lastEvent.set(sid0, toEvent(payload.agent, '', cfg))
    } catch {}
    const decision = await next()
    try {
      const { agent, step, signal } = payload
      if (decision?.kind !== 'enter' || signal?.aborted || step !== 1) return decision
      if (!enabledFor(agent) || cfg.dsh?.injectContext === false || !core.ready) return decision
      const userText = decision.messages.filter(isHumanUserMessage).map(textOf).filter(Boolean).join('\n')
      if (!userText) return decision
      const ev = toEvent(agent, userText, cfg)
      const sid = sessionIdOf(agent)
      lastEvent.set(sid, ev)
      const r = await core.request('POST', '/v1/hooks/context', { event: ev, include_stable: false },
        { timeoutMs: cfg.dsh?.contextTimeoutMs ?? 4000, signal })
      if (r?.stable && typeof r.stable.version === 'string' && typeof r.stable.text === 'string') {
        stable.version = r.stable.version
        stable.text = r.stable.text
      }
      if (!r?.text) return decision
      stats.injected += 1
      return { ...decision, messages: [...decision.messages, snapshotMessage(r.text)] }
    } catch (e) {
      if (!payload.signal?.aborted) fail('pre-step', e)
      return decision
    }
  }, { prepend: true })

  // ───────────────────────── 3. 回复写回记忆 ─────────────────────────
  ctx.on('session/event', (session, event) => {
    try {
      const sid = session?.id
      if (event?.type === 'assistant/message') {
        const t = textOf(event.data?.message)
        if (t) lastReply.set(sid, t)
      } else if (event?.type === 'turn/end') {
        // 稳定块最多每 10 分钟后台刷一次：画像/锚点变了下一轮就生效，平时不额外花请求
        refreshStable()
        const reply = lastReply.get(sid)
        lastReply.delete(sid)
        const ev = lastEvent.get(sid)
        if (!reply || !ev || cfg.dsh?.recordReplies === false || !core.ready) return
        core.request('POST', '/v1/hooks/after', { event: ev, reply: reply.slice(0, 4000) }, { timeoutMs: 10_000 })
          .then(() => { stats.recorded += 1 })
          .catch((e) => fail('after', e))
      }
    } catch (e) { fail('session/event', e) }
  })
  ctx.on('session/disposed', (session) => {
    lastEvent.delete(session?.id); lastReply.delete(session?.id)
  })

  // ───────────────────────── 4. 压缩前抢救 ─────────────────────────
  ctx.on('llm/stream', async (options, next) => {
    try {
      if (options?.purpose === 'compaction' && cfg.dsh?.compactionHook !== false && core.ready) {
        const sid = options.sessionId
        const ev = lastEvent.get(sid) ?? { ...toEvent({ session: { id: sid } }, '', cfg) }
        const msgs = compactionMessages(options.messages)
        if (msgs.length) {
          await core.request('POST', '/v1/hooks/pre_compact', { event: ev, messages: msgs }, { timeoutMs: 180_000 })
            .then((r) => { stats.compactions += 1; log('info', `压缩前提炼 ${r?.extracted ?? 0} 条记忆（${msgs.length} 条消息）`) })
            .catch((e) => fail('pre_compact', e))
        }
      }
    } catch (e) { fail('compaction hook', e) }
    return next()
  }, { global: true, prepend: true })

  // ───────────────────────── 5. 工具 ─────────────────────────
  if (cfg.dsh?.tools !== false) {
    const evFor = (exec, text = '') => toEvent(exec?.agent, text, cfg)
    const str = (v, n) => (typeof v === 'string' ? v.trim().slice(0, n) : '')
    ctx.effect(() => ctx.tools.register({
      name: 'memory_search',
      description: '在你（角色）的长期记忆里搜索：以前和主人聊过的事、项目里做过的决定和理由、主人的偏好。上下文里找不到、或用户问"之前/上次/还记得吗"时使用。',
      parameters: {
        type: 'object', additionalProperties: false,
        properties: { query: { type: 'string', description: '要找什么，用关键词或一句话' } },
        required: ['query'],
      },
      output: TOOL_OUTPUT,
      isConcurrencySafe: () => true,
      async execute(args, exec) {
        const q = str(args?.query, 200)
        if (!q) throw new Error('query 不能为空')
        const ev = evFor(exec, q)
        const extra = encodeURIComponent('core/people/owner,core/self')
        // touch=1：模型主动检索到的记忆才算"用到了"，注入本身不刷新（见 memory.touch_mode）
        const items = await core.request('GET', `/v1/memory/search?q=${encodeURIComponent(q)}&path=${encodeURIComponent(ev.path ?? (ev.scene === 'project' ? 'project/' + ev.channel_id : 'chat/local/' + ev.channel_id))}&extra=${extra}&k=8&touch=1`, undefined, { signal: exec?.signal })
        return { text: formatSearch(items) }
      },
    }), 'soulcore: memory_search')
    ctx.effect(() => ctx.tools.register({
      name: 'remember',
      description: '把一件值得长期记住的事写进你的记忆：主人明确让你记住的事、项目的关键决定（写清决定+理由+否掉的方案）、主人的偏好、待办。不要记寒暄。',
      parameters: {
        type: 'object', additionalProperties: false,
        properties: {
          content: { type: 'string', description: '一句完整、脱离上下文也能看懂的话，写明主语' },
          kind: { type: 'string', enum: ['fact', 'preference', 'decision', 'todo'], description: '类别' },
        },
        required: ['content'],
      },
      output: TOOL_OUTPUT,
      async execute(args, exec) {
        const content = str(args?.content, 1000)
        if (!content) throw new Error('content 不能为空')
        const kind = ['fact', 'preference', 'decision', 'todo'].includes(args?.kind) ? args.kind : 'fact'
        const r = await core.request('POST', '/v1/memory/remember', { event: evFor(exec), content, kind, importance: kind === 'decision' ? 0.9 : 0.85 }, { signal: exec?.signal })
        return { text: r?.ok ? `已记住（${kind}）：${content}` : '没有写入（可能与已有记忆重复，已合并）。' }
      },
    }), 'soulcore: remember')
    ctx.effect(() => ctx.tools.register({
      name: 'mood_status',
      description: '查看你（角色）此刻的心情、孤独感和最近影响心情的事件。用户问你心情怎么样时使用。',
      parameters: { type: 'object', additionalProperties: false, properties: {} },
      output: TOOL_OUTPUT,
      isConcurrencySafe: () => true,
      async execute(_args, exec) {
        const s = await core.request('GET', '/v1/state', undefined, { signal: exec?.signal })
        return { text: `${s.describe}（内部数值 mood=${s.mood}，孤独=${s.loneliness}，性格基线=${s.baseline}）` }
      },
    }), 'soulcore: mood_status')
  }

  // ───────────────────────── 6. 设置页 API ─────────────────────────
  const isLoopback = (req) => {
    const h = String(req.headers?.host ?? '').toLowerCase().replace(/:\d+$/, '')
    return h === '127.0.0.1' || h === 'localhost' || h === '[::1]' || h === '::1'
  }
  function rejected(web, req, res) {
    if (!isLoopback(req)) { send(res, 403, { ok: false, error: '只接受本机回环访问' }); return true }
    try {
      const conn = web.connection
      if (!conn || typeof conn.requestRejection !== 'function') { send(res, 503, { ok: false, error: '信任栅栏不可用（fail-closed）' }); return true }
      const code = conn.requestRejection(req)
      if (code === undefined || code === null || code === false) return false
      res.statusCode = typeof code === 'number' ? code : 403
      res.end()
      return true
    } catch { send(res, 503, { ok: false, error: '信任栅栏异常（fail-closed）' }); return true }
  }
  function send(res, code, obj) {
    res.writeHead(code, { 'Content-Type': 'application/json; charset=utf-8', 'Cache-Control': 'no-store' })
    res.end(JSON.stringify(obj))
  }
  function body(req, limit = 64 * 1024) {
    return new Promise((resolve, reject) => {
      let size = 0; const chunks = []
      req.on('data', (c) => { size += c.length; if (size > limit) { reject(new Error('body too large')); req.destroy(); return } chunks.push(c) })
      req.on('end', () => { try { resolve(JSON.parse(Buffer.concat(chunks).toString('utf8') || '{}')) } catch (e) { reject(e) } })
      req.on('error', reject)
    })
  }
  const routes = []
  const route = (pathname, handler) => routes.push([pathname, handler])
  const mountRoutes = (web) => routes.map(([pathname, handler]) => web.webServer.register({
    kind: 'exact', path: pathname,
    handler: async (req, res) => {
      if (rejected(web, req, res)) return
      try { await handler(req, res) } catch (e) { try { send(res, 500, { ok: false, error: String(e?.message ?? e) }) } catch {} }
    },
  }))
  const API = '/dsh-soulcore/api'

  route(`${API}/state`, async (_req, res) => {
    let state = null
    let qq = null
    if (core.ready) {
      try { state = await core.request('GET', '/v1/state') } catch (e) { stats.lastError = String(e.message) }
      try { qq = await core.request('GET', '/v1/qq/status', undefined, { timeoutMs: 1500 }) } catch {}
    }
    send(res, 200, { ok: true, core: { ...core.status(), switching: core.switching }, python: py, state, qq, stats, home })
  })
  route(`${API}/settings`, async (req, res) => {
    if (req.method === 'GET') { cfg = readConfig(home); send(res, 200, { ok: true, config: publicConfig(cfg), configPath: paths.cfgPath }); return }
    if (req.method !== 'POST') { send(res, 405, { ok: false }); return }
    const b = await body(req)
    cfg = applySettings(readConfig(home), b.config ?? b)
    writeConfig(cfg, home)
    personaText = readPersona()
    let restarted = false
    let note
    if (cfg.dsh?.enabled === false && !core.attached) await core.stop()
    else if (core.attached) {
      // 常驻进程读的是启动时的配置：让它退出，再由计划任务拉起来，然后重新接入
      const r = await restartResident()
      restarted = r.ok
      note = r.ok ? '常驻进程已重启，新设置已生效' : r.error
    } else { core.restart().catch((e) => fail('restart', e)); restarted = true }
    send(res, 200, { ok: true, config: publicConfig(cfg), restarted, attached: core.attached, note })
  })
  route(`${API}/persona`, async (req, res) => {
    const file = cfg.persona.card_file
    if (req.method === 'GET') {
      let text = ''; try { text = fs.readFileSync(file, 'utf8') } catch {}
      send(res, 200, { ok: true, text, file }); return
    }
    if (req.method !== 'POST') { send(res, 405, { ok: false }); return }
    const b = await body(req)
    const text = String(b.text ?? '')
    if (text.length > 20000) { send(res, 400, { ok: false, error: '人设太长（上限 20000 字）' }); return }
    fs.writeFileSync(file, text, 'utf8')
    personaText = readPersona()
    core.restart().catch((e) => fail('restart', e))
    send(res, 200, { ok: true, bytes: Buffer.byteLength(text) })
  })
  route(`${API}/search`, async (req, res) => {
    const u = new URL(req.url, 'http://x')
    const q = u.searchParams.get('q') ?? ''
    const extra = encodeURIComponent('core/people/owner,core/self')
    const items = q ? await core.request('GET', `/v1/memory/search?q=${encodeURIComponent(q)}&path=${encodeURIComponent(u.searchParams.get('path') || 'project')}&extra=${extra}&k=20`) : []
    send(res, 200, { ok: true, items })
  })
  route(`${API}/event`, async (req, res) => {
    if (req.method !== 'POST') { send(res, 405, { ok: false }); return }
    const b = await body(req)
    send(res, 200, { ok: true, ...(await core.request('POST', '/v1/event', { name: String(b.name ?? '') })) })
  })
  route(`${API}/sync`, async (req, res) => {
    if (req.method !== 'POST') { send(res, 405, { ok: false }); return }
    send(res, 200, { ok: true, ...(await core.request('POST', '/v1/memory/sync', { force: true }, { timeoutMs: 60_000 })) })
  })
  route(`${API}/restart`, async (req, res) => {
    if (req.method !== 'POST') { send(res, 405, { ok: false }); return }
    try { await core.restart(); send(res, 200, { ok: true, core: core.status() }) } catch (e) { send(res, 200, { ok: false, error: String(e.message), core: core.status() }) }
  })
  // ───────────────────────── QQ 接入 / 常驻运行 ─────────────────────────
  const MASK = '••••••'
  route(`${API}/qq/test`, async (req, res) => {
    if (req.method !== 'POST') { send(res, 405, { ok: false }); return }
    const b = await body(req)
    const wsUrl = String(b.ws_url || cfg.qq?.ws_url || 'ws://127.0.0.1:3001')
    // 设置页里 token 是掩码：没改就用已保存的
    const token = !b.access_token || b.access_token === MASK ? (cfg.qq?.access_token || '') : String(b.access_token)
    if (!core.ready) { send(res, 200, { ok: false, error: '后端未就绪，稍后再试' }); return }
    const r = await core.request('POST', '/v1/qq/test', { ws_url: wsUrl, access_token: token }, { timeoutMs: 15_000 })
    send(res, 200, r)
  })

  /** 调 Python CLI（install-service 等），返回 { code, out } */
  function runCli(args, timeoutMs = 60_000) {
    return new Promise((resolve) => {
      execFile(py.file, ['-I', path.join(CORE_DIR, 'run.py'), '--config', paths.cfgPath, ...args],
        { cwd: CORE_DIR, timeout: timeoutMs, windowsHide: true,
          env: { ...process.env, PYTHONIOENCODING: 'utf-8', PYTHONUTF8: '1', PYTHONDONTWRITEBYTECODE: '1' } },
        (err, stdout, stderr) => resolve({ code: err ? (typeof err.code === 'number' ? err.code : 1) : 0,
          out: `${stdout ?? ''}${stderr ? '\n' + stderr : ''}`.trim().slice(0, 2000) }))
    })
  }
  const serviceDeps = deps.runCli ?? runCli
  const isWin = deps.platform ? deps.platform === 'win32' : process.platform === 'win32'

  async function serviceStatus() {
    if (!isWin) return { supported: false, installed: false, text: '常驻模式目前只支持 Windows' }
    const r = await serviceDeps(['service-status'], 20_000)
    return { supported: true, installed: r.code === 0, text: r.out }
  }

  /** 切换期间：禁止自动重连 / 自己拉子进程，避免抢走常驻进程的锁 */
  async function switching(fn) {
    core.switching = true
    try { return await fn() } finally { core.switching = false }
  }
  async function fallbackToChild() {
    core.stopping = false; core._ready = null
    try { await core.start() } catch (e) { fail('start', e) }
  }

  async function restartResident() {
    return switching(async () => {
      await core.shutdownBackend()
      const r = await serviceDeps(['service-start'], 30_000)
      if (r.code !== 0) {
        core.switching = false
        await fallbackToChild()
        return { ok: false, error: '常驻进程没有通过计划任务注册（可能是手动启动的），已改为由 DSH 启动后端：' + r.out }
      }
      if (await core.waitAttach()) return { ok: true }
      core.switching = false
      await fallbackToChild()
      return { ok: false, error: '计划任务已运行，但 25 秒内没等到常驻进程，已改为由 DSH 启动后端' }
    })
  }

  route(`${API}/service`, async (req, res) => {
    if (req.method === 'GET') { send(res, 200, { ok: true, ...(await serviceStatus()), attached: core.attached }); return }
    if (req.method !== 'POST') { send(res, 405, { ok: false }); return }
    if (!isWin) { send(res, 200, { ok: false, error: '常驻模式目前只支持 Windows' }); return }
    const { action } = await body(req)
    if (action === 'install') {
      const out = await switching(async () => {
        await core.shutdownBackend()                       // 先让出锁，常驻进程才能起来
        const r = await serviceDeps(['install-service'], 60_000)
        if (r.code !== 0) { core.switching = false; await fallbackToChild(); return { ok: false, error: r.out } }
        if (!(await core.waitAttach())) {
          core.switching = false; await fallbackToChild()
          return { ok: false, error: '计划任务已注册，但 25 秒内没等到常驻进程启动。DSH 先自己启动后端；下次登录 Windows 时常驻进程会自动运行。\n' + r.out }
        }
        cfg = applySettings(readConfig(home), { service: { mode: 'resident' } }); writeConfig(cfg, home)
        return { ok: true, text: r.out }
      })
      send(res, 200, { ...out, ...(await serviceStatus()), attached: core.attached }); return
    }
    if (action === 'uninstall') {
      const out = await switching(async () => {
        const r = await serviceDeps(['uninstall-service'], 30_000)
        if (core.attached) await core.shutdownBackend()   // 计划任务删了，但已经在跑的那个也要停
        core.switching = false
        await fallbackToChild()
        cfg = applySettings(readConfig(home), { service: { mode: 'follow_dsh' } }); writeConfig(cfg, home)
        return { ok: r.code === 0, text: r.out, error: r.code === 0 ? undefined : r.out }
      })
      send(res, 200, { ...out, ...(await serviceStatus()), attached: core.attached }); return
    }
    send(res, 400, { ok: false, error: 'action 只能是 install / uninstall' })
  })

  // ───────────────────────── 主动聊天 ─────────────────────────
  route(`${API}/initiative`, async (req, res) => {
    if (!core.ready) { send(res, 200, { ok: false, error: '后端未就绪' }); return }
    send(res, 200, { ok: true, ...(await core.request('GET', '/v1/initiative/status')) })
  })
  route(`${API}/initiative/run`, async (req, res) => {
    if (req.method !== 'POST') { send(res, 405, { ok: false }); return }
    if (!core.ready) { send(res, 200, { ok: false, error: '后端未就绪' }); return }
    // 生成要调 LLM，可能要几十秒
    send(res, 200, { ok: true, ...(await core.request('POST', '/v1/initiative/run', { force: true }, { timeoutMs: 120_000 })) })
  })
  route(`${API}/initiative/draft`, async (req, res) => {
    if (req.method !== 'POST') { send(res, 405, { ok: false }); return }
    const b = await body(req)
    const r = await core.request('POST', '/v1/initiative/draft', { id: String(b.id ?? ''), action: String(b.action ?? '') }, { timeoutMs: 30_000 })
    send(res, 200, r)
  })

  // 只有 profile 里有 Web 服务时才挂设置页路由；服务消失时 Cordis 会自动回收这个子上下文
  ctx.inject(['webServer', 'connection'], (web) => {
    const disposers = mountRoutes(web)
    web.effect(() => () => { for (const d of disposers) { try { d() } catch {} } }, 'soulcore: routes')
  })

  log('info', `已挂载。数据目录 ${home}；Python ${py.file}${py.fromPath ? '（来自 PATH）' : ''}`)
  return { core, stats, getConfig: () => cfg, SOURCE_KIND }
}

export default { name, inject, apply }
