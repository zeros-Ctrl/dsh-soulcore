// v0.9 R3：稳定块（人设锚点 / 画像）缓存进 system prompt，pre-step 不再重复注入。
// 这里用假后端（deps.core），不拉真 Python 进程；真实后端的挂点由 plugin.test.mjs 覆盖。
import { test, before } from 'node:test'
import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

const TMP = fs.mkdtempSync(path.join(os.tmpdir(), 'soulcore-stable-'))
process.env.DSH_HOME = path.join(TMP, 'dsh')
process.env.SOULCORE_HOME = path.join(TMP, 'soul')

const { apply } = await import('../lib/index.js')
const glue = await import('../lib/glue.js')

const STABLE = {
  version: 'abc123def456',
  anchor: '好奇、嘴上偶尔不饶人但心软',
  profile_owner: '主人叫丁真，喜欢技术和视频话题',
  profile_self: '小汐清楚自己是角色，不是鱼',
  text: '## 保持人设\n好奇、嘴上偶尔不饶人但心软\n\n## 你对自己的认识\n小汐清楚自己是角色，不是鱼',
}

function fakeCore() {
  const calls = []
  let ready = false
  const core = {
    attached: false,
    switching: false,
    get ready() { return ready },
    async start() { ready = true },
    async stop() { ready = false },
    async restart() {},
    async shutdownBackend() {},
    async waitAttach() { return true },
    status() { return { ready, port: 0, pid: process.pid } },
    async request(method, p, body) {
      calls.push({ method, path: p, body })
      if (p === '/v1/persona/stable') return STABLE
      if (p === '/v1/hooks/context') return { text: '注入的易变上下文', mood: 0.6, label: '平', memories: [] }
      return { ok: true }
    },
  }
  return { core, calls }
}

function fakeCtx() {
  const sections = []
  const listeners = {}
  const ctx = {
    sections, listeners,
    on(name, fn, opts) { (listeners[name] ??= []).push({ fn, opts }); return () => {} },
    effect(fn) { const d = fn(); return d },
    get(name) { return ctx[name] },
    inject() { return () => {} },
    systemPrompt: {
      getSectionOrder: (n) => ({ DEPLOYMENT_PERSONA_SUFFIX: 10200 })[n],
      section(s) { sections.push(s); return () => {} },
    },
    tools: { register(def) { return () => {} } },
  }
  return ctx
}

const agent = (id = 's1', cwd = '/home/u/projects/毕设') => ({ id, session: { id, header: { cwd } } })
const userMsg = (text) => ({ role: 'user', content: [{ type: 'text', text }], source: { kind: 'user', rpcId: 'r' } })

let ctx, plugin, fake, calls
before(async () => {
  ctx = fakeCtx()
  fake = fakeCore()
  calls = fake.calls
  plugin = apply(ctx, {}, { core: fake.core, log: () => {} })
  await plugin.core.start()
  // 启动时的那次刷新是异步的，等一轮微任务 + 定时器
  await new Promise((r) => setTimeout(r, 30))
})

test('稳定块拉取一次并拼进 system prompt', () => {
  const s = ctx.sections.find((x) => x.name === 'soulcore:persona')
  assert.ok(s, '要有 soulcore:persona section')
  const text = s.text({ agent: agent() })
  assert.match(text, /你叫小汐/)
  assert.match(text, /好奇、嘴上偶尔不饶人但心软/)          // 锚点进了 system prompt
  assert.match(text, /主人叫丁真/)                          // 画像也进了
  assert.equal(s.text({ agent: agent('bili-whale') }), '')  // 被排除的会话照旧不注入
  // 启动时只拉一次，之后靠 turn/end 的 10 分钟节流
  assert.equal(calls.filter((c) => c.path === '/v1/persona/stable').length, 1)
})

test('pre-step 请求带上 include_stable=false', async () => {
  const [{ fn }] = ctx.listeners['agent/pre-step']
  const base = { kind: 'enter', messages: [userMsg('今天继续看项目')] }
  const d = await fn({ agent: agent(), step: 1, turn: 1, signal: new AbortController().signal }, async () => base)
  assert.equal(d.messages.length, 2)
  const hit = calls.filter((c) => c.path === '/v1/hooks/context').at(-1)
  assert.ok(hit, 'pre-step 要调用 /v1/hooks/context')
  assert.equal(hit.body.include_stable, false)
  assert.equal(hit.body.event.channel_id, '毕设')
})

test('版本没变不替换缓存：turn/end 不额外拉取', async () => {
  const before = calls.filter((c) => c.path === '/v1/persona/stable').length
  const [{ fn }] = ctx.listeners['session/event']
  fn({ id: 's1' }, { type: 'assistant/message', data: { message: { role: 'assistant', content: [{ type: 'text', text: '嗯' }] } } })
  fn({ id: 's1' }, { type: 'turn/end', data: { turn: 1 } })
  await new Promise((r) => setTimeout(r, 20))
  // 距上次拉取还不到 10 分钟，turn/end 不再发请求
  assert.equal(calls.filter((c) => c.path === '/v1/persona/stable').length, before)
})

test('channelOf：在 git 仓库子目录里用仓库根目录名', () => {
  const repo = path.join(TMP, 'work', 'myrobot')
  fs.mkdirSync(path.join(repo, 'src', 'nav'), { recursive: true })
  fs.mkdirSync(path.join(repo, '.git'), { recursive: true })
  assert.equal(glue.channelOf({ session: { id: 'x', header: { cwd: path.join(repo, 'src', 'nav') } } }), 'myrobot')
  // 不是 git 仓库：用最后一级目录名
  const plain = path.join(TMP, 'work', 'scratch')
  fs.mkdirSync(plain, { recursive: true })
  assert.equal(glue.channelOf({ session: { id: 'x', header: { cwd: plain } } }), 'scratch')
})
