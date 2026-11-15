// v0.9 R3：稳定块（人设锚点 / 画像）缓存进 system prompt，pre-step 不再重复注入。
// 这里用假后端（deps.core），不拉真 Python 进程；真实后端的挂点由 plugin.test.mjs 覆盖。
import { test, before } from 'node:test'
import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import vm from 'node:vm'

const TMP = fs.mkdtempSync(path.join(os.tmpdir(), 'soulcore-stable-'))
process.env.DSH_HOME = path.join(TMP, 'dsh')
process.env.SOULCORE_HOME = path.join(TMP, 'soul')

const { apply } = await import('../lib/index.js')
const glue = await import('../lib/glue.js')

for (const present of [false, true]) test(`设置页渲染：${present ? '显示用量计数' : '兼容缺少用量'}且 QQ 卡片作用域有效`, () => {
  const cfg = { persona: { name: '小汐' }, qq: { enabled: false, transport: 'ws' } }
  const state = { core: { ready: true }, state: { mood: 0.62, target: 0.62, label: '平静', memory: {},
    usage: present ? { enabled: true, totals: { calls: 3, input_tokens: 1234, output_tokens: 56,
      missing_usage_calls: 2 }, cost_complete: false } : undefined } }
  let root = true, index = 0, client
  const React = { Fragment: Symbol('fragment'),
    createElement: (type, props, ...children) => ({ type, props: props || {}, children }),
    useState: (initial) => {
      const slot = root ? index++ : -1
      return [slot === 0 ? state : slot === 1 ? cfg : initial, () => {}]
    },
    useCallback: (fn) => fn, useEffect: () => {} }
  vm.runInNewContext(fs.readFileSync(new URL('../lib/client.js', import.meta.url), 'utf8'), {
    window: { __ModuleLoader__: { load: (definition) => {
      client = definition.factory((name) => { assert.equal(name, 'react'); return React })
    } } },
  })
  const tree = client.SoulcoreSection()
  const text = JSON.stringify(tree)
  if (present) {
    assert.match(text, /1,234/)
    assert.match(text, /56/)
    assert.match(text, /2 次缺 usage/)
  } else assert.doesNotMatch(text, /实际用量/)
  const nodes = []
  function collect(node) {
    if (!node || typeof node !== 'object') return
    nodes.push(node)
    for (const child of node.children || []) collect(child)
  }
  collect(tree)
  const qq = nodes.find((node) => typeof node.type === 'function' && node.type.name === 'QQCard')
  assert.ok(qq)
  root = false
  assert.doesNotThrow(() => qq.type(qq.props))
})

const STABLE = {
  version: 'abc123def456',
  anchor: '好奇、嘴上偶尔不饶人但心软',
  profile_owner: '主人叫丁真，喜欢技术和视频话题',
  profile_self: '小汐清楚自己是角色，不是鱼',
  text: '## 保持人设\n好奇、嘴上偶尔不饶人但心软\n\n## 你对自己的认识\n小汐清楚自己是角色，不是鱼\n\n## 关于主人（整体印象）\n主人叫丁真，喜欢技术和视频话题',
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

test('llm/stream：普通模型调用直接迭代并逐块转发', async () => {
  const [{ fn }] = ctx.listeners['llm/stream']
  const chunks = [{ type: 'text', text: '回复' }, { type: 'finish', reason: 'stop' }]
  let nextCalls = 0
  const stream = fn({ purpose: 'main', messages: [] }, () => {
    nextCalls += 1
    return (async function* () { yield* chunks })()
  })
  const received = []
  for await (const chunk of stream) received.push(chunk)
  assert.deepEqual(received, chunks)
  assert.equal(received[0], chunks[0], '保持原始流块')
  assert.equal(nextCalls, 1)
})

test('llm/stream：模型流错误传给调用方', async () => {
  const [{ fn }] = ctx.listeners['llm/stream']
  const expected = new Error('模型流读取失败')
  const stream = fn({ purpose: 'main', messages: [] }, () => (async function* () { throw expected })())
  await assert.rejects(async () => {
    for await (const chunk of stream) assert.fail('失败的流不应输出内容')
  }, (error) => error === expected)
})

test('llm/stream：提前停止读取时关闭模型流', async () => {
  const [{ fn }] = ctx.listeners['llm/stream']
  let closed = false
  const stream = fn({ purpose: 'main', messages: [] }, () => (async function* () {
    try { yield { type: 'text', text: '第一块' }; yield { type: 'text', text: '第二块' } }
    finally { closed = true }
  })())
  for await (const chunk of stream) {
    assert.equal(chunk.text, '第一块')
    break
  }
  assert.equal(closed, true)
})

test('llm/stream：压缩前提炼失败仍转发模型流', async () => {
  const [{ fn }] = ctx.listeners['llm/stream']
  const originalRequest = plugin.core.request
  const failures = plugin.stats.failures
  const chunks = [{ type: 'text', text: '压缩摘要' }]
  let preCompactCalls = 0
  plugin.core.request = async function (method, requestPath, ...args) {
    if (requestPath === '/v1/hooks/pre_compact') {
      preCompactCalls += 1
      throw new Error('提炼后端暂时不可用')
    }
    return originalRequest.call(this, method, requestPath, ...args)
  }
  try {
    const stream = fn({ purpose: 'compaction', sessionId: 'stream-failure', messages: [userMsg('保留这条记忆')] },
      () => (async function* () { yield* chunks })())
    const received = []
    for await (const chunk of stream) received.push(chunk)
    assert.deepEqual(received, chunks)
    assert.equal(preCompactCalls, 1)
    assert.equal(plugin.stats.failures, failures + 1)
  } finally {
    plugin.core.request = originalRequest
  }
})

async function associateSession(id, messages = [userMsg('当前任务')]) {
  const [{ fn }] = ctx.listeners['agent/pre-step']
  await fn({ agent: agent(id), step: 1, signal: new AbortController().signal },
    async () => ({ kind: 'enter', messages }))
}

const usageCalls = () => calls.filter((call) => call.path === '/v1/usage')

test('宿主用量：累计块只记一次且不传消息或思考文本', async () => {
  await associateSession('usage-complete')
  const [{ fn }] = ctx.listeners['llm/stream']
  const before = usageCalls().length
  const chunks = [
    { type: 'text', text: 'private response', usage: { prompt_tokens: 100, prompt_cache_hit_tokens: 30, content: 'private prompt' } },
    { type: 'finish', model: 'actual-model', provider: 'actual-provider', usage: {
      prompt_tokens: 100, prompt_cache_hit_tokens: 30, completion_tokens: 40,
      completion_tokens_details: { reasoning_tokens: 20, reasoning_content: 'private reasoning' },
    } },
  ]
  const received = []
  for await (const chunk of fn({ sessionId: 'usage-complete', model: 'requested-model', provider: 'requested-provider', messages: [userMsg('private prompt')] },
    () => (async function* () { yield* chunks })())) received.push(chunk)
  assert.deepEqual(received, chunks)
  assert.equal(received[0], chunks[0])
  const added = usageCalls().slice(before)
  assert.equal(added.length, 1)
  assert.equal(added[0].body.provider, 'actual-provider')
  assert.equal(added[0].body.model, 'actual-model')
  assert.equal(added[0].body.purpose, 'host_online')
  assert.equal(added[0].body.success, true)
  assert.deepEqual(added[0].body.usage, { prompt_tokens: 100, prompt_cache_hit_tokens: 30,
    completion_tokens: 40, completion_tokens_details: { reasoning_tokens: 20 } })
  assert.doesNotMatch(JSON.stringify(added[0].body), /private prompt|private response|private reasoning|messages/)
})

test('宿主用量：显式 usage.data 可读取，缺 usage 明确未知', async () => {
  await associateSession('usage-data')
  const [{ fn }] = ctx.listeners['llm/stream']
  const chunk = { type: 'usage', data: { input_tokens: 8, output_tokens: 3 } }
  for await (const received of fn({ sessionId: 'usage-data' }, () => (async function* () { yield chunk })())) {
    assert.equal(received, chunk)
  }
  assert.deepEqual(usageCalls().at(-1).body.usage, { input_tokens: 8, output_tokens: 3 })
  for await (const received of fn({ sessionId: 'usage-data' }, () => (async function* () { yield { type: 'text', text: '很多字也不估算 token' } })())) assert.ok(received)
  const row = usageCalls().at(-1).body
  assert.equal(row.usage, null)
  assert.equal(row.model, 'unknown')
  assert.equal(row.provider, 'dsh')
  assert.equal(row.success, true)
})

test('宿主用量：失败流保留原错误并记录已报告的消耗', async () => {
  await associateSession('usage-error')
  const [{ fn }] = ctx.listeners['llm/stream']
  const expected = new Error('private error detail')
  const stream = fn({ sessionId: 'usage-error' }, () => (async function* () {
    yield { type: 'usage', data: { prompt_tokens: 9 } }
    throw expected
  })())
  await assert.rejects(async () => { for await (const chunk of stream) assert.ok(chunk) }, (error) => error === expected)
  const row = usageCalls().at(-1).body
  assert.equal(row.success, false)
  assert.equal(row.error_type, 'Error')
  assert.deepEqual(row.usage, { prompt_tokens: 9 })
  assert.doesNotMatch(JSON.stringify(row), /private error/)
})

test('宿主用量：提前停止关闭原流，标记未完整读取', async () => {
  await associateSession('usage-close')
  const [{ fn }] = ctx.listeners['llm/stream']
  let closed = false
  for await (const chunk of fn({ sessionId: 'usage-close' }, () => (async function* () {
    try { yield { type: 'text', text: 'first' }; yield { type: 'text', text: 'second' } }
    finally { closed = true }
  })())) { assert.equal(chunk.text, 'first'); break }
  assert.equal(closed, true)
  assert.equal(usageCalls().at(-1).body.success, false)
  assert.equal(usageCalls().at(-1).body.error_type, 'StreamClosed')
})

test('宿主用量：next 值与 throw 仍委托给模型迭代器', async () => {
  await associateSession('usage-delegation')
  const [{ fn }] = ctx.listeners['llm/stream']
  let nextValue
  const expected = new Error('injected')
  const stream = fn({ sessionId: 'usage-delegation' }, () => (async function* () {
    nextValue = yield { type: 'text', text: 'first' }
    try { yield { type: 'text', text: 'second' } }
    catch (error) { assert.equal(error, expected); yield { type: 'text', text: 'recovered' } }
  })())
  assert.equal((await stream.next()).value.text, 'first')
  assert.equal((await stream.next('continue')).value.text, 'second')
  assert.equal(nextValue, 'continue')
  assert.equal((await stream.throw(expected)).value.text, 'recovered')
  assert.equal((await stream.next()).done, true)
  assert.equal(usageCalls().at(-1).body.success, true)
})

test('宿主用量：账本失败不吞掉模型错误或流块', async () => {
  await associateSession('usage-backend-error')
  const [{ fn }] = ctx.listeners['llm/stream']
  const original = plugin.core.request
  const failures = plugin.stats.failures
  plugin.core.request = async function(method, p, ...args) {
    if (p === '/v1/usage') throw new Error('ledger offline')
    return original.call(this, method, p, ...args)
  }
  try {
    const chunk = { type: 'text', text: 'still delivered' }
    for await (const received of fn({ sessionId: 'usage-backend-error' }, () => (async function* () { yield chunk })())) assert.equal(received, chunk)
    await new Promise((resolve) => setTimeout(resolve, 0))
    assert.equal(plugin.stats.failures, failures + 1)
  } finally { plugin.core.request = original }
})

test('宿主用量：未关联会话或关闭统计时不入账', async () => {
  const [{ fn }] = ctx.listeners['llm/stream']
  const before = usageCalls().length
  for await (const chunk of fn({ sessionId: 'unassociated-usage' }, () => (async function* () { yield { usage: { prompt_tokens: 99 } } })())) assert.ok(chunk)
  await associateSession('usage-disabled')
  const cfg = plugin.getConfig()
  const enabled = cfg.usage.enabled
  cfg.usage.enabled = false
  try {
    for await (const chunk of fn({ sessionId: 'usage-disabled' }, () => (async function* () { yield { usage: { prompt_tokens: 99 } } })())) assert.ok(chunk)
  } finally { cfg.usage.enabled = enabled }
  assert.equal(usageCalls().length, before)
})

test('宿主用量：压缩调用单独归类', async () => {
  await associateSession('usage-compaction')
  const [{ fn }] = ctx.listeners['llm/stream']
  const before = usageCalls().length
  for await (const chunk of fn({ sessionId: 'usage-compaction', purpose: 'compaction', messages: [] },
    () => (async function* () { yield { usage: { prompt_tokens: 10, completion_tokens: 2 } } })())) assert.ok(chunk)
  assert.equal(usageCalls().length, before + 1)
  assert.equal(usageCalls().at(-1).body.purpose, 'host_compaction')
})

test('来源与正文：只记录最新人类消息，长回复完整保存', async () => {
  const earlier = { ...userMsg('旧轮次内容'), id: 'human-old' }
  const latestText = '新轮次内容'.repeat(1200)
  const latest = { ...userMsg(latestText), id: 'human-latest' }
  await associateSession('source-identity', [earlier, latest])
  const context = calls.filter((call) => call.path === '/v1/hooks/context').at(-1)
  assert.equal(context.body.event.text, latestText)
  assert.equal(context.body.event.event_id, 'human-latest')
  const [{ fn }] = ctx.listeners['session/event']
  const reply = '完整回复'.repeat(1300)
  fn({ id: 'source-identity' }, { type: 'assistant/message', data: { message: { role: 'assistant', content: reply } } })
  fn({ id: 'source-identity' }, { type: 'turn/end' })
  assert.equal(calls.filter((call) => call.path === '/v1/hooks/after').at(-1).body.reply, reply)
})

test('项目检索：先解析稳定 ID 再传给搜索端点', async () => {
  const registered = {}
  const localCtx = fakeCtx()
  localCtx.tools.register = (definition) => { registered[definition.name] = definition; return () => {} }
  const localCore = fakeCore()
  const original = localCore.core.request
  localCore.core.request = async function(method, p, body, ...args) {
    if (p === '/v1/projects/resolve') {
      localCore.calls.push({ method, path: p, body })
      return { project_id: 'project-stable-id', path: 'project/uuid-path' }
    }
    return original.call(this, method, p, body, ...args)
  }
  apply(localCtx, {}, { core: localCore.core, log: () => {} })
  await localCore.core.start()
  await registered.memory_search.execute({ query: '查询' }, { agent: agent('identity-search') })
  const relevant = localCore.calls.filter((call) => call.path === '/v1/projects/resolve' || call.path.startsWith('/v1/memory/search?'))
  assert.equal(relevant[0].path, '/v1/projects/resolve')
  const query = new URL(relevant[1].path, 'http://localhost').searchParams
  assert.equal(query.get('path'), 'project/uuid-path')
  assert.equal(query.get('project_id'), 'project-stable-id')
})
