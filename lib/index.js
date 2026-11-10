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
import fs from 'node:fs'
import { CoreProcess, resolvePython } from './core-process.js'
import { CORE_DIR, DSH_HOME, applySettings, ensureHome, publicConfig, readConfig, soulHome, writeConfig } from './config.js'
import {
  SOURCE_KIND, TOOL_OUTPUT, compactionMessages, formatSearch, isExcluded, isHumanUserMessage,
  sessionIdOf, snapshotMessage, textOf, toEvent,
} from './glue.js'

export const name = 'dsh-soulcore'
export const inject = ['systemPrompt', 'tools', 'webServer', 'connection']

export function apply(ctx, _config, deps = {}) {
  const home = soulHome()
  const paths = ensureHome(home)
  let cfg = readConfig(home)
  let personaText = readPersona()
  const log = deps.log ?? ((lv, m) => { try { (lv === 'error' ? console.error : console.log)(`[soulcore] ${m}`) } catch {} })

  function readPersona() {
    try {
      const raw = fs.readFileSync(cfg?.persona?.card_file ?? paths.persona, 'utf8')
      return raw.replaceAll('{name}', cfg?.persona?.name || '小汐').trim()
    } catch { return '' }
  }

  // ───────────────────────── 后端子进程 ─────────────────────────
  const py = resolvePython(DSH_HOME, cfg.dsh?.pythonPath)
  const core = deps.core ?? new CoreProcess({ python: py.file, coreDir: CORE_DIR, configPath: paths.cfgPath, log })
  if (cfg.dsh?.enabled !== false) core.start().catch((e) => log('error', '后端启动失败：' + (e?.message ?? e)))
  ctx.effect(() => () => { core.stop() }, 'soulcore: core process')

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
    text: (c) => (cfg.dsh?.injectPersona !== false && personaText && enabledFor(c?.agent ?? c?.scope)) ? personaText : '',
  }), 'soulcore: persona section')

  // ───────────────────────── 2. 每轮注入心情 + 记忆 ─────────────────────────
  ctx.on('agent/pre-step', async (payload, next) => {
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
      const r = await core.request('POST', '/v1/hooks/context', { event: ev },
        { timeoutMs: cfg.dsh?.contextTimeoutMs ?? 4000, signal })
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
  ctx.on('llm/stream', (options, next) => {
    try {
      if (options?.purpose === 'compaction' && cfg.dsh?.compactionHook !== false && core.ready) {
        const sid = options.sessionId
        const ev = lastEvent.get(sid) ?? { ...toEvent({ session: { id: sid } }, '', cfg) }
        const msgs = compactionMessages(options.messages)
        if (msgs.length) {
          core.request('POST', '/v1/hooks/pre_compact', { event: ev, messages: msgs }, { timeoutMs: 180_000 })
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
        const extra = encodeURIComponent('core/people/local/owner,core/self')
        const items = await core.request('GET', `/v1/memory/search?q=${encodeURIComponent(q)}&path=${encodeURIComponent('project/' + ev.channel_id)}&extra=${extra}&k=8`, undefined, { signal: exec?.signal })
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
  function rejected(req, res) {
    if (!isLoopback(req)) { send(res, 403, { ok: false, error: '只接受本机回环访问' }); return true }
    try {
      const conn = ctx.get?.('connection') ?? ctx.connection
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
  const disposers = []
  const route = (pathname, handler) => disposers.push(ctx.webServer.register({
    kind: 'exact', path: pathname,
    handler: async (req, res) => {
      if (rejected(req, res)) return
      try { await handler(req, res) } catch (e) { try { send(res, 500, { ok: false, error: String(e?.message ?? e) }) } catch {} }
    },
  }))
  const API = '/dsh-soulcore/api'

  route(`${API}/state`, async (_req, res) => {
    let state = null
    if (core.ready) { try { state = await core.request('GET', '/v1/state') } catch (e) { stats.lastError = String(e.message) } }
    send(res, 200, { ok: true, core: core.status(), python: py, state, stats, home })
  })
  route(`${API}/settings`, async (req, res) => {
    if (req.method === 'GET') { cfg = readConfig(home); send(res, 200, { ok: true, config: publicConfig(cfg), configPath: paths.cfgPath }); return }
    if (req.method !== 'POST') { send(res, 405, { ok: false }); return }
    const b = await body(req)
    cfg = applySettings(readConfig(home), b.config ?? b)
    writeConfig(cfg, home)
    personaText = readPersona()
    let restarted = false
    if (cfg.dsh?.enabled === false) await core.stop()
    else { core.restart().catch((e) => fail('restart', e)); restarted = true }
    send(res, 200, { ok: true, config: publicConfig(cfg), restarted })
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
    const extra = encodeURIComponent('core/people/local/owner,core/self')
    const items = q ? await core.request('GET', `/v1/memory/search?q=${encodeURIComponent(q)}&path=${encodeURIComponent(u.searchParams.get('path') || 'project')}&extra=${extra}&k=20`) : []
    send(res, 200, { ok: true, items })
  })
  route(`${API}/event`, async (req, res) => {
    if (req.method !== 'POST') { send(res, 405, { ok: false }); return }
    const b = await body(req)
    send(res, 200, { ok: true, ...(await core.request('POST', '/v1/event', { name: String(b.name ?? '') })) })
  })
  route(`${API}/restart`, async (req, res) => {
    if (req.method !== 'POST') { send(res, 405, { ok: false }); return }
    try { await core.restart(); send(res, 200, { ok: true, core: core.status() }) } catch (e) { send(res, 200, { ok: false, error: String(e.message), core: core.status() }) }
  })
  ctx.effect(() => () => { for (const d of disposers) { try { d() } catch {} } }, 'soulcore: routes')

  log('info', `已挂载。数据目录 ${home}；Python ${py.file}${py.fromPath ? '（来自 PATH）' : ''}`)
  return { core, stats, getConfig: () => cfg, SOURCE_KIND }
}

export default { name, inject, apply }
