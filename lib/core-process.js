/**
 * soulcore 后端子进程管理。
 *
 * 后端是纯标准库 Python（core/），用 DSH 自带的 Python 运行时启动：
 *   ~/.dsh/dsh-runtimes/<runtime>/dependencies/python/python.exe
 * 所以装了 DSH 的人不需要另装 Python。
 *
 * 握手：子进程绑定随机端口后在 stdout 打一行 `SOULCORE_READY {"port":...}`。
 * 鉴权：每次启动生成随机 token，经环境变量交给子进程；所有请求带 Bearer token。
 * 防孤儿：子进程监听 stdin，DSH 退出（哪怕被强杀）管道关闭 → 子进程自动退出。
 */
import { spawn } from 'node:child_process'
import crypto from 'node:crypto'
import fs from 'node:fs'
import path from 'node:path'

/** 找 Python：设置里指定的 → 环境变量 → DSH 自带运行时 → PATH 上的 python */
export function resolvePython(dshHome, override) {
  const tried = []
  const ok = (p) => { tried.push(p); try { return fs.statSync(p).isFile() } catch { return false } }
  if (override && ok(override)) return { file: override, tried }
  if (process.env.SOULCORE_PYTHON && ok(process.env.SOULCORE_PYTHON)) return { file: process.env.SOULCORE_PYTHON, tried }
  const rtRoot = path.join(dshHome, 'dsh-runtimes')
  let runtimes = []
  try { runtimes = fs.readdirSync(rtRoot) } catch { /* 没有 DSH 运行时目录 */ }
  // primary 优先
  runtimes.sort((a, b) => (b.includes('primary') ? 1 : 0) - (a.includes('primary') ? 1 : 0))
  for (const rt of runtimes) {
    const base = path.join(rtRoot, rt, 'dependencies', 'python')
    for (const rel of process.platform === 'win32' ? ['python.exe'] : ['bin/python3', 'bin/python']) {
      const p = path.join(base, rel)
      if (ok(p)) return { file: p, tried }
    }
  }
  // 最后交给 PATH（spawn 失败时会报出来）
  return { file: process.platform === 'win32' ? 'python' : 'python3', tried, fromPath: true }
}

export class CoreProcess {
  /**
   * @param {object} o
   * @param {string} o.python      Python 可执行文件
   * @param {string} o.coreDir     core/ 目录（含 run.py）
   * @param {string} o.configPath  soulcore 配置文件
   * @param {(level:string, msg:string)=>void} [o.log]
   * @param {Function} [o.spawnImpl] 测试注入
   */
  constructor({ python, coreDir, configPath, log, spawnImpl, fetchImpl }) {
    this.python = python
    this.coreDir = coreDir
    this.configPath = configPath
    this.log = log ?? ((lv, m) => console[lv === 'error' ? 'error' : 'log'](`[soulcore] ${m}`))
    this.spawnImpl = spawnImpl ?? spawn
    this.fetchImpl = fetchImpl ?? globalThis.fetch
    this.child = null
    this.port = null
    this.token = null
    this.stopping = false
    this.restarts = 0
    this.lastError = null
    this.startedAt = null
    this._ready = null
    this._restartTimer = null
    this.stderrTail = []
  }

  get ready() { return this.port !== null && this.child !== null }

  status() {
    return {
      ready: this.ready, port: this.port, pid: this.child?.pid ?? null, python: this.python,
      restarts: this.restarts, lastError: this.lastError, startedAt: this.startedAt,
      stderrTail: this.stderrTail.slice(-12),
    }
  }

  /** 启动并等待握手；重复调用返回同一个 Promise */
  start() {
    if (this._ready) return this._ready
    this.stopping = false
    this.token = crypto.randomBytes(24).toString('hex')
    const args = ['-I', path.join(this.coreDir, 'run.py'), '--config', this.configPath, 'serve', '--port', '0']
    const env = {
      ...process.env,
      SOULCORE_TOKEN: this.token,
      SOULCORE_STDIN_WATCHDOG: '1',
      PYTHONIOENCODING: 'utf-8',
      PYTHONUTF8: '1',
      PYTHONDONTWRITEBYTECODE: '1',
    }
    delete env.ELECTRON_RUN_AS_NODE
    this._ready = new Promise((resolve, reject) => {
      let child
      try {
        child = this.spawnImpl(this.python, args, { cwd: this.coreDir, env, stdio: ['pipe', 'pipe', 'pipe'], windowsHide: true })
      } catch (e) {
        this._fail(e); reject(e); return
      }
      this.child = child
      const timer = setTimeout(() => {
        const e = new Error('soulcore 后端 20 秒内没有就绪：' + this.stderrTail.slice(-3).join(' | '))
        this._fail(e); reject(e); try { child.kill() } catch {}
      }, 20_000)
      let buf = ''
      child.stdout.setEncoding?.('utf8')
      child.stdout.on('data', (d) => {
        buf += d
        let i
        while ((i = buf.indexOf('\n')) >= 0) {
          const line = buf.slice(0, i).trim(); buf = buf.slice(i + 1)
          if (line.startsWith('SOULCORE_READY ')) {
            try {
              const info = JSON.parse(line.slice('SOULCORE_READY '.length))
              this.port = info.port
              this.startedAt = new Date().toISOString()
              this.lastError = null
              clearTimeout(timer)
              this.log('info', `后端就绪 port=${info.port} pid=${info.pid}`)
              resolve(info)
            } catch (e) { this.log('error', '握手解析失败：' + line) }
          } else if (line) this.log('debug', line)
        }
      })
      child.stderr.setEncoding?.('utf8')
      child.stderr.on('data', (d) => {
        for (const line of String(d).split(/\r?\n/)) {
          if (!line.trim()) continue
          this.stderrTail.push(line.slice(0, 300))
          if (this.stderrTail.length > 50) this.stderrTail.shift()
          if (/ERROR|Traceback/.test(line)) this.log('error', line)
        }
      })
      child.on('error', (e) => {
        clearTimeout(timer)
        this._fail(e)
        reject(e)
      })
      child.on('exit', (code, sig) => {
        clearTimeout(timer)
        const was = this.child === child
        if (was) { this.child = null; this.port = null; this._ready = null }
        if (!this.stopping && was) {
          this.lastError = `后端退出 code=${code} signal=${sig}`
          this.log('error', this.lastError + '：' + this.stderrTail.slice(-3).join(' | '))
          this._scheduleRestart()
        }
        reject(new Error(`soulcore 后端退出 code=${code}`))
      })
    })
    this._ready.catch(() => {})
    return this._ready
  }

  _fail(e) {
    this.lastError = String(e?.message ?? e)
    this.child = null; this.port = null; this._ready = null
    if (!this.stopping) this._scheduleRestart()
  }

  _scheduleRestart() {
    if (this._restartTimer || this.stopping) return
    const delay = Math.min(60_000, [2000, 5000, 15000, 30000][this.restarts] ?? 60_000)
    this.restarts += 1
    this._restartTimer = setTimeout(() => {
      this._restartTimer = null
      this.start().catch(() => {})
    }, delay)
    this._restartTimer.unref?.()
  }

  /** 发请求；后端没就绪时抛错（调用方决定降级） */
  async request(method, pathname, body, { timeoutMs = 4000, signal } = {}) {
    if (!this.ready) throw new Error('soulcore 后端未就绪')
    const signals = [AbortSignal.timeout(timeoutMs)]
    if (signal) signals.push(signal)
    const res = await this.fetchImpl(`http://127.0.0.1:${this.port}${pathname}`, {
      method,
      headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${this.token}` },
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: AbortSignal.any ? AbortSignal.any(signals) : signals[0],
    })
    const text = await res.text()
    let data = null
    try { data = text ? JSON.parse(text) : null } catch { data = { raw: text } }
    if (!res.ok) throw new Error(`soulcore ${pathname} HTTP ${res.status}: ${text.slice(0, 200)}`)
    return data
  }

  async restart() {
    await this.stop()
    this.restarts = 0
    this.stopping = false
    return this.start()
  }

  stop() {
    this.stopping = true
    if (this._restartTimer) { clearTimeout(this._restartTimer); this._restartTimer = null }
    const child = this.child
    this.child = null; this.port = null; this._ready = null
    if (!child) return Promise.resolve()
    return new Promise((resolve) => {
      const done = () => resolve()
      child.once?.('exit', done)
      try { child.stdin?.end() } catch {}
      const t = setTimeout(() => { try { child.kill() } catch {} ; resolve() }, 3000)
      t.unref?.()
    })
  }
}
