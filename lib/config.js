/**
 * 配置与路径。
 *
 * 一份配置文件，两边共用：
 *   ~/.dsh/soulcore/config.json   （Python 后端读整份；插件读其中的 `dsh` 段）
 *   ~/.dsh/soulcore/persona.md    人设（可在 DSH「设置 → 人格与记忆」里直接改）
 *   ~/.dsh/soulcore/data/         记忆库 memory.db + Markdown 镜像 + 情绪状态
 * 可用环境变量 SOULCORE_HOME 改位置。
 */
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

export const DSH_HOME = process.env.DSH_HOME || path.join(os.homedir(), '.dsh')
export const HERE = path.dirname(fileURLToPath(import.meta.url))
export const PLUGIN_ROOT = path.resolve(HERE, '..')
export const CORE_DIR = path.join(PLUGIN_ROOT, 'core')

export function soulHome() {
  return process.env.SOULCORE_HOME || path.join(DSH_HOME, 'soulcore')
}

export const DEFAULT_DSH = {
  enabled: true,
  injectPersona: true,      // 人设写进 system prompt
  injectContext: true,      // 每轮注入心情 + 相关记忆
  recordReplies: true,      // 每轮结束把她的回复写回记忆
  compactionHook: true,     // 上下文压缩前把要被压掉的内容提炼落盘
  tools: true,              // 注册 memory_search / remember / mood_status 工具
  ownerName: '主人',        // 她怎么称呼 DSH 的使用者
  excludeSessions: ['bili-whale'], // 这些会话不注入（例如别的插件自己的人设会话）
  contextTimeoutMs: 4000,
  pythonPath: '',
}

export function defaultConfig(home) {
  return {
    persona: { name: '小汐', card_file: path.join(home, 'persona.md') },
    data_dir: path.join(home, 'data'),
    llm: { provider: 'deepseek', base_url: 'https://api.deepseek.com', api_key: '', model: 'deepseek-chat', extract_model: 'deepseek-chat' },
    emotion: { baseline: 0.62, sensitivity: 1.0 },
    memory: { extract_every: 6, top_k: 6, inject_max_chars: 1600, share_projects_in_chat: false,
      embedding: { provider: '', base_url: 'https://api.siliconflow.cn/v1', api_key: '', model: 'BAAI/bge-m3' } },
    server: { host: '127.0.0.1', port: 0, tick_every_min: 15 },
    qq: { enabled: false },
    bilibili: { enabled: false },
    dsh: { ...DEFAULT_DSH },
  }
}

const isObj = (v) => v && typeof v === 'object' && !Array.isArray(v)
export function merge(base, over) {
  const out = { ...base }
  for (const [k, v] of Object.entries(over || {})) out[k] = isObj(v) && isObj(base?.[k]) ? merge(base[k], v) : v
  return out
}

/** 首次运行：建目录、写默认配置、拷贝人设模板 */
export function ensureHome(home = soulHome()) {
  fs.mkdirSync(home, { recursive: true })
  const cfgPath = path.join(home, 'config.json')
  if (!fs.existsSync(cfgPath)) fs.writeFileSync(cfgPath, JSON.stringify(defaultConfig(home), null, 2) + '\n')
  const persona = path.join(home, 'persona.md')
  if (!fs.existsSync(persona)) {
    try { fs.copyFileSync(path.join(CORE_DIR, 'persona.example.md'), persona) } catch { fs.writeFileSync(persona, '') }
  }
  return { home, cfgPath, persona }
}

export function readConfig(home = soulHome()) {
  const { cfgPath } = ensureHome(home)
  let user = {}
  try { user = JSON.parse(fs.readFileSync(cfgPath, 'utf8')) } catch (e) { console.warn('[soulcore] config.json 解析失败，用默认值：' + e.message) }
  return merge(defaultConfig(home), user)
}

export function writeConfig(cfg, home = soulHome()) {
  const { cfgPath } = ensureHome(home)
  const tmp = cfgPath + '.tmp'
  fs.writeFileSync(tmp, JSON.stringify(cfg, null, 2) + '\n')
  fs.renameSync(tmp, cfgPath)
  try { fs.chmodSync(cfgPath, 0o600) } catch {}
}

const SECRET_PATHS = [['llm', 'api_key'], ['memory', 'embedding', 'api_key'], ['qq', 'access_token'], ['bilibili', 'credential', 'sessdata'],
  ['bilibili', 'credential', 'bili_jct'], ['bilibili', 'credential', 'buvid3'], ['server', 'token']]

/** 给设置页看的版本：密钥只回"有没有"，永不回显 */
export function publicConfig(cfg) {
  const out = JSON.parse(JSON.stringify(cfg))
  for (const p of SECRET_PATHS) {
    let o = out
    for (const k of p.slice(0, -1)) o = o?.[k]
    if (o && p.at(-1) in o) o[p.at(-1)] = o[p.at(-1)] ? '••••••' : ''
  }
  return out
}

/** 设置页提交的修改：只接受白名单段；密钥字段为空或是掩码时保留原值 */
export function applySettings(cur, incoming) {
  const KNOWN = new Set(['persona', 'llm', 'emotion', 'memory', 'dsh', 'qq', 'bilibili', 'appraisal'])
  const filtered = {}
  for (const [k, v] of Object.entries(incoming || {})) if (KNOWN.has(k) && isObj(v)) filtered[k] = v
  const next = merge(cur, filtered)
  for (const p of SECRET_PATHS) {
    let o = next, c = cur
    for (const k of p.slice(0, -1)) { o = o?.[k]; c = c?.[k] }
    if (!o) continue
    const v = o[p.at(-1)]
    if (v === '••••••' || v === undefined) o[p.at(-1)] = c?.[p.at(-1)] ?? ''
  }
  // 数值夹紧
  const e = next.emotion ?? {}
  if (e.baseline !== undefined) e.baseline = Math.min(0.9, Math.max(0.1, Number(e.baseline) || 0.62))
  if (e.sensitivity !== undefined) e.sensitivity = Math.min(3, Math.max(0.1, Number(e.sensitivity) || 1))
  if (!Array.isArray(next.dsh?.excludeSessions)) next.dsh.excludeSessions = String(next.dsh?.excludeSessions ?? '').split(/[,，\s]+/).filter(Boolean)
  // 人设文件、数据目录、端口不允许从网页改（防止被引到任意路径）
  next.persona.card_file = cur.persona.card_file
  next.data_dir = cur.data_dir
  next.server = cur.server
  return next
}
