/**
 * DSH ⇄ soulcore 的纯函数胶水（不依赖 ctx，方便单测）。
 */
import crypto from 'node:crypto'
import fs from 'node:fs'
import path from 'node:path'

export const SOURCE_KIND = 'dsh-soulcore'

/** 消息里的纯文本（只取 text 块） */
export function textOf(message) {
  const content = message?.content
  if (typeof content === 'string') return content
  if (!Array.isArray(content)) return ''
  return content.filter((b) => b && b.type === 'text' && typeof b.text === 'string').map((b) => b.text).join('\n').trim()
}

/** 真正由人输入的 user 消息（排除运行时上下文、压缩检查点、别的插件注入的快照） */
export function isHumanUserMessage(m) {
  return m?.role === 'user' && m?.source?.kind === 'user'
}

export function sessionIdOf(agent) {
  return agent?.session?.id ?? agent?.id ?? null
}

/** 向上找 .git：cwd 在仓库子目录里时，项目名要用仓库根目录的名字 */
export function gitRootOf(cwd) {
  if (typeof cwd !== 'string' || !cwd) return null
  let dir = path.resolve(cwd)
  for (let i = 0; i < 40; i++) {
    try {
      if (fs.existsSync(path.join(dir, '.git'))) return dir
    } catch { /* 权限问题就当没有 .git */ }
    const parent = path.dirname(dir)
    if (parent === dir) return null
    dir = parent
  }
  return null
}

/**
 * 会话的工作目录 → 记忆分支 project/<目录名>
 *
 * 规则必须和 Python 侧 soulcore.cc_hook.project_name 完全一致，否则同一个项目在 DSH
 * 和 Claude Code 里会各记一套：
 *   1. 有 .git 就往上找到仓库根目录，用根目录名；
 *   2. 否则用 cwd 的最后一级目录名，盘符根目录不算；
 *   3. 都没有就用会话 id。
 */
export function channelOf(agent) {
  const cwd = agent?.session?.header?.cwd
  if (typeof cwd === 'string' && cwd) {
    const root = gitRootOf(cwd) || cwd
    const base = root.replace(/\\/g, '/').replace(/\/+$/, '').split('/').pop()
    if (base && !/^[A-Za-z]:$/.test(base)) return base
  }
  const id = sessionIdOf(agent)
  if (!id) return 'default'
  const sid = String(id)
  return (sid.startsWith('session-') ? sid : `session-${sid}`).slice(0, 32)
}

export function isExcluded(cfg, agent) {
  const id = String(sessionIdOf(agent) ?? '')
  const list = cfg?.dsh?.excludeSessions ?? []
  return list.some((x) => x && (id === x || id.startsWith(x)))
}

export function toEvent(agent, text, cfg, metadata = {}) {
  const sessionId = String(sessionIdOf(agent) ?? '')
  return {
    platform: 'local',
    scene: 'project',
    channel_id: channelOf(agent),
    user_id: 'owner',
    user_name: cfg?.dsh?.ownerName || '主人',
    text: String(text ?? ''),
    mentions_self: true,
    ts: Date.now() / 1000,
    cwd: String(agent?.session?.header?.cwd ?? ''),
    session_id: sessionId,
    event_id: typeof metadata.event_id === 'string' && metadata.event_id ? metadata.event_id : crypto.randomUUID(),
    raw: { client: 'dsh', session_id: sessionId },
  }
}

/** 与 dsh-time-context 同构的快照消息：user 角色、source.kind=插件名、form=snapshot */
export function snapshotMessage(text) {
  const msg = {
    role: 'user',
    content: [{ type: 'text', text }],
    source: { kind: SOURCE_KIND, form: 'snapshot', sections: [{ name: SOURCE_KIND, text }] },
    id: crypto.randomUUID(),
  }
  return deepFreeze(msg)
}

function deepFreeze(o) {
  if (o && typeof o === 'object' && !Object.isFrozen(o)) {
    Object.freeze(o)
    for (const v of Object.values(o)) deepFreeze(v)
  }
  return o
}

/**
 * 压缩请求里的消息 → 人和她说的话，以及有来源的有界工具结果。
 * system、压缩指令、运行时快照和工具调用指令不作为对话事实。
 * @returns {{role:string, content:string}[]}
 */
export function compactionMessages(messages) {
  const out = []
  const calls = new Map()
  let sourceUserEventId = ''
  for (const m of messages ?? []) {
    if (isHumanUserMessage(m)) sourceUserEventId = typeof m.id === 'string' ? m.id : ''
    if (m?.role === 'assistant' && Array.isArray(m.content)) {
      for (const block of m.content) {
        if (!['tool-call', 'tool_use'].includes(block?.type)) continue
        const id = String(block.id ?? block.toolCallId ?? block.tool_call_id ?? '')
        let args = block.arguments ?? block.input ?? block.args ?? {}
        if (typeof args === 'string') { try { args = JSON.parse(args) } catch { args = {} } }
        if (id) calls.set(id, { tool_name: String(block.name ?? ''), command: String(args?.cmd ?? args?.command ?? '').slice(0, 1000),
          source_user_event_id: sourceUserEventId })
      }
    }
    if (m?.role === 'tool') {
      const item = toolEvidenceOf(m, calls)
      if (item) out.push(item)
      continue
    }
    if (m?.role === 'user' && m?.source?.kind !== 'user') continue
    if (m?.role !== 'user' && m?.role !== 'assistant') continue
    const t = textOf(m)
    if (t) {
      const item = { role: m.role, content: t }
      if (typeof m.id === 'string' && m.id) item.source_event_id = m.id
      out.push(item)
    }
  }
  return out
}

/** Only transport-level structured exit codes count; prose saying "passed" does not. */
export function toolEvidenceOf(message, calls = new Map()) {
  if (message?.role !== 'tool') return null
  const block = Array.isArray(message.content) ? message.content.find((b) => ['tool-result', 'tool_result'].includes(b?.type)) : null
  const callId = String(message.tool_call_id ?? message.toolCallId ?? block?.tool_use_id ?? block?.toolCallId ?? '')
  const result = block?.output ?? block?.result ?? message.result ?? message.output
  const known = calls.get(callId) ?? {}
  const evidence = {
    origin: 'tool_result', tool_call_id: callId,
    tool_name: String(message.name ?? known.tool_name ?? ''), command: known.command ?? '',
  }
  if (known.source_user_event_id) evidence.source_user_event_id = known.source_user_event_id
  const exit = message.exit_code ?? result?.exit_code ?? result?.exitCode
  if (Number.isInteger(exit)) evidence.exit_code = exit
  let text = textOf(message)
  if (!text && typeof result === 'string') text = result
  if (!text && result && typeof result === 'object') {
    text = String(result.output ?? result.stdout ?? result.text ?? '')
    if (result.stderr) text += `\n${String(result.stderr)}`
  }
  if (!text) return null
  if (text.length > 8000) {
    evidence.omitted_chars = text.length - 8000
    evidence.original_sha256 = crypto.createHash('sha256').update(text).digest('hex')
    text = `${text.slice(0, 4000)}\n…[工具原文中段省略]…\n${text.slice(-4000)}`
  }
  const item = { role: 'tool', content: text, evidence }
  if (typeof message.id === 'string' && message.id) item.source_event_id = message.id
  else if (callId) item.source_event_id = `tool:${callId}`
  return item
}

export const TOOL_OUTPUT = {
  schema: {
    type: 'object',
    additionalProperties: false,
    properties: { text: { type: 'string', description: '给模型看的结果' } },
    required: ['text'],
  },
  render: (_args, value) => [{ type: 'text', text: String(value?.text ?? '') }],
}

export function formatSearch(items) {
  if (!Array.isArray(items) || items.length === 0) return '没有找到相关记忆。'
  const states = { suggested: '提议', accepted: '已接受', implemented: '已实施', verified: '有验证证据' }
  return items.map((it, i) => `${i + 1}. [${it.path} · ${it.kind}${states[it.epistemic_state] ? ` · ${states[it.epistemic_state]}` : ''}] ${it.content}${it.stale_note ? `（注意：${it.stale_note}）` : ''}`).join('\n')
}
