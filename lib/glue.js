/**
 * DSH ⇄ soulcore 的纯函数胶水（不依赖 ctx，方便单测）。
 */
import crypto from 'node:crypto'

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

/** 会话的工作目录 → 记忆分支 project/<目录名> */
export function channelOf(agent) {
  const cwd = agent?.session?.header?.cwd
  if (typeof cwd === 'string' && cwd) {
    const base = cwd.replace(/\\/g, '/').replace(/\/+$/, '').split('/').pop()
    if (base && !/^[A-Za-z]:$/.test(base)) return base
  }
  const id = sessionIdOf(agent)
  return id ? `session-${String(id).slice(0, 24)}` : 'default'
}

export function isExcluded(cfg, agent) {
  const id = String(sessionIdOf(agent) ?? '')
  const list = cfg?.dsh?.excludeSessions ?? []
  return list.some((x) => x && (id === x || id.startsWith(x)))
}

export function toEvent(agent, text, cfg) {
  return {
    platform: 'local',
    scene: 'project',
    channel_id: channelOf(agent),
    user_id: 'owner',
    user_name: cfg?.dsh?.ownerName || '主人',
    text: String(text ?? '').slice(0, 4000),
    mentions_self: true,
    ts: Date.now() / 1000,
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
 * 压缩请求里的消息 → 只保留人和她说的话（去掉 system、压缩指令、各种运行时快照、工具调用）
 * @returns {{role:string, content:string}[]}
 */
export function compactionMessages(messages) {
  const out = []
  for (const m of messages ?? []) {
    if (m?.role === 'user' && m?.source?.kind !== 'user') continue
    if (m?.role !== 'user' && m?.role !== 'assistant') continue
    const t = textOf(m)
    if (t) out.push({ role: m.role, content: t.slice(0, 4000) })
  }
  return out
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
  return items.map((it, i) => `${i + 1}. [${it.path} · ${it.kind}] ${it.content}`).join('\n')
}
