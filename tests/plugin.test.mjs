// node --test tests/   —— 用一个模拟的 Cordis ctx 跑真实的 Python 后端，验证所有 DSH 挂点。
import { test, before, after } from 'node:test'
import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { Readable } from 'node:stream'

const TMP = fs.mkdtempSync(path.join(os.tmpdir(), 'soulcore-test-'))
process.env.DSH_HOME = path.join(TMP, 'dsh')          // 没有 DSH 运行时 → 回退到 PATH 上的 python
process.env.SOULCORE_HOME = path.join(TMP, 'soul')
if (process.platform !== 'win32') process.env.SOULCORE_PYTHON = process.env.SOULCORE_PYTHON || '/usr/bin/python3'

const { apply } = await import('../lib/index.js')
const glue = await import('../lib/glue.js')
const config = await import('../lib/config.js')

function fakeCtx() {
  const listeners = {}, sections = [], registered = {}, routes = {}, effects = []
  const ctx = {
    listeners, sections, registered, routes, effects,
    on(name, fn, opts) { (listeners[name] ??= []).push({ fn, opts }); return () => {} },
    effect(fn) { const d = fn(); if (typeof d === 'function') effects.push(d); return d },
    get(name) { return ctx[name] },
    systemPrompt: {
      getSectionOrder: (n) => ({ DEPLOYMENT_PERSONA_SUFFIX: 10200 })[n],
      section(s) { sections.push(s); return () => {} },
    },
    tools: { register(def) {
      assert.equal(typeof def.output?.render, 'function', 'tool must declare output.render')
      registered[def.name] = def; return () => {}
    } },
    webServer: { register({ path: p, handler }) { routes[p] = handler; return () => {} } },
    connection: { requestRejection: (req) => (req.headers['x-reject'] ? 401 : undefined) },
  }
  return ctx
}

const agent = (id = 's1', cwd = '/home/u/projects/毕设') => ({ id, session: { id, header: { cwd } } })
const userMsg = (text) => ({ role: 'user', content: [{ type: 'text', text }], source: { kind: 'user', rpcId: 'r' } })

async function callRoute(ctx, p, { method = 'GET', body, headers = {} } = {}) {
  const req = Readable.from(body === undefined ? [] : [Buffer.from(JSON.stringify(body))])
  Object.assign(req, { method, url: p, headers: { host: '127.0.0.1:5555', ...headers } })
  return new Promise((resolve) => {
    const res = { statusCode: 200, headers: {}, writeHead(c, h) { this.statusCode = c; this.headers = h }, end(b) { resolve({ status: this.statusCode, body: b ? JSON.parse(b) : null }) } }
    const handler = ctx.routes[p.split('?')[0]]
    handler(req, res)
  })
}

let ctx, plugin
before(async () => {
  ctx = fakeCtx()
  plugin = apply(ctx, {}, { log: () => {} })
  await plugin.core.start()
})
after(async () => {
  for (const d of ctx.effects) await d()
  fs.rmSync(TMP, { recursive: true, force: true })
})

test('首次运行生成配置与人设模板', () => {
  const home = process.env.SOULCORE_HOME
  assert.ok(fs.existsSync(path.join(home, 'config.json')))
  assert.ok(fs.readFileSync(path.join(home, 'persona.md'), 'utf8').includes('{name}'))
})

test('人设写入 system prompt；被排除的会话不写', () => {
  const s = ctx.sections.find((x) => x.name === 'soulcore:persona')
  assert.equal(s.order, 10150)
  assert.match(s.text({ agent: agent() }), /你叫小汐/)
  assert.equal(s.text({ agent: agent('bili-whale') }), '')
  assert.equal(s.text({}), '')
})

test('pre-step：第一步注入心情+记忆快照，第二步不注入', async () => {
  const [{ fn, opts }] = ctx.listeners['agent/pre-step']
  assert.equal(opts.prepend, true)
  const base = { kind: 'enter', messages: [userMsg('记住，数据库我们决定用 SQLite，因为部署轻')] }
  const d1 = await fn({ agent: agent(), step: 1, turn: 1, signal: new AbortController().signal }, async () => base)
  assert.equal(d1.messages.length, 2)
  const inj = d1.messages[1]
  assert.equal(inj.role, 'user')
  assert.equal(inj.source.kind, glue.SOURCE_KIND)
  assert.equal(inj.source.form, 'snapshot')
  assert.match(glue.textOf(inj), /小汐·内心状态/)
  assert.ok(Object.isFrozen(inj))
  const d2 = await fn({ agent: agent(), step: 2, turn: 1, signal: new AbortController().signal }, async () => base)
  assert.equal(d2.messages.length, 1)
  const rej = await fn({ agent: agent(), step: 1, turn: 1, signal: new AbortController().signal }, async () => ({ kind: 'reject' }))
  assert.equal(rej.kind, 'reject')
  // 只有运行时消息、没有人话 → 不注入
  const sys = { kind: 'enter', messages: [{ role: 'user', content: [{ type: 'text', text: 'x' }], source: { kind: 'time-context' } }] }
  assert.equal((await fn({ agent: agent(), step: 1, signal: new AbortController().signal }, async () => sys)).messages.length, 1)
})

test('turn/end 时把最终回复写回记忆', async () => {
  const [{ fn }] = ctx.listeners['session/event']
  const session = { id: 's1' }
  fn(session, { type: 'assistant/message', data: { message: { role: 'assistant', content: [{ type: 'text', text: '先查一下文件' }, { type: 'tool-call', name: 'x' }] } } })
  fn(session, { type: 'assistant/message', data: { message: { role: 'assistant', content: [{ type: 'text', text: '好，SQLite 方案我记下了，嘿嘿' }] } } })
  fn(session, { type: 'turn/end', data: { turn: 1 } })
  await new Promise((r) => setTimeout(r, 400))
  assert.equal(plugin.stats.recorded, 1)
  const items = await plugin.core.request('GET', '/v1/memory/search?q=' + encodeURIComponent('SQLite 方案 记下') + '&path=project/毕设')
  assert.ok(items.some((i) => i.content.includes('嘿嘿')))
})

test('压缩前抢救：compaction 请求里的原文被提炼成长期记忆', async () => {
  const [{ fn, opts }] = ctx.listeners['llm/stream']
  assert.equal(opts.global, true)
  let passed = false
  fn({ purpose: 'compaction', sessionId: 's1', messages: [
    { role: 'system', content: [{ type: 'text', text: 'sys' }] },
    userMsg('我们决定日志改用 JSONL，不用 CSV 了，因为要追加写'),
    { role: 'assistant', content: [{ type: 'text', text: '好的' }], source: { kind: 'model' } },
    { role: 'user', content: [{ type: 'text', text: 'Summarize the conversation' }], source: { kind: 'compaction' } },
  ] }, () => { passed = true })
  assert.ok(passed, '必须继续调用 next()')
  fn({ purpose: 'main', messages: [] }, () => {})
  for (let i = 0; i < 30 && plugin.stats.compactions === 0; i++) await new Promise((r) => setTimeout(r, 100))
  assert.equal(plugin.stats.compactions, 1)
  const r = await ctx.registered.memory_search.execute({ query: '日志 JSONL' }, { agent: agent() })
  assert.match(r.text, /JSONL/)
  assert.doesNotMatch(r.text, /Summarize/)
})

test('工具：remember / memory_search / mood_status', async () => {
  const exec = { agent: agent() }
  const w = await ctx.registered.remember.execute({ content: '主人喜欢深色主题', kind: 'preference' }, exec)
  assert.match(w.text, /已记住/)
  const s = await ctx.registered.memory_search.execute({ query: '深色主题' }, exec)
  assert.match(s.text, /深色主题/)
  const m = await ctx.registered.mood_status.execute({}, exec)
  assert.match(m.text, /mood=/)
  await assert.rejects(ctx.registered.memory_search.execute({ query: '  ' }, exec))
  for (const t of Object.values(ctx.registered)) assert.equal(t.parameters.type, 'object')
})

test('设置 API：密钥不回显、掩码提交不覆盖、信任栅栏 fail-closed', async () => {
  const cfg0 = config.readConfig()
  cfg0.llm.api_key = 'sk-secret'
  config.writeConfig(cfg0)
  const g = await callRoute(ctx, '/dsh-soulcore/api/settings')
  assert.equal(g.body.config.llm.api_key, '••••••')
  const p = await callRoute(ctx, '/dsh-soulcore/api/settings', { method: 'POST', body: { config: { ...g.body.config, emotion: { baseline: 5 }, data_dir: '/evil' } } })
  assert.equal(p.status, 200)
  const now = config.readConfig()
  assert.equal(now.llm.api_key, 'sk-secret')
  assert.equal(now.emotion.baseline, 0.9)
  assert.notEqual(now.data_dir, '/evil')
  assert.equal((await callRoute(ctx, '/dsh-soulcore/api/state', { headers: { host: 'evil.com' } })).status, 403)
  assert.equal((await callRoute(ctx, '/dsh-soulcore/api/state', { headers: { 'x-reject': '1' } })).status, 401)
  for (let i = 0; i < 50 && !plugin.core.ready; i++) await new Promise((r) => setTimeout(r, 100))   // 保存后后端重启
  const st = await callRoute(ctx, '/dsh-soulcore/api/state')
  assert.equal(st.body.core.ready, true)
  assert.ok(st.body.state.mood > 0 && st.body.state.mood < 1)
})

test('后端挂了不影响 DSH：pre-step 原样放行', async () => {
  await plugin.core.stop()
  const [{ fn }] = ctx.listeners['agent/pre-step']
  const base = { kind: 'enter', messages: [userMsg('你好')] }
  const d = await fn({ agent: agent(), step: 1, signal: new AbortController().signal }, async () => base)
  assert.equal(d, base)
  await plugin.core.start()
})

test('glue：压缩消息过滤、会话目录名', () => {
  assert.equal(glue.channelOf(agent('a', 'C:\\Users\\me\\proj-x\\')), 'proj-x')
  assert.equal(glue.channelOf({ session: { id: 'abc' } }), 'session-abc')
  assert.ok(glue.isExcluded({ dsh: { excludeSessions: ['bili-whale'] } }, agent('bili-whale-2')))
})
