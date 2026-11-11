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

/** 读锁文件（后端 `serve` 写的 {pid, port, token, started, version}） */
export function readLockInfo(lockPath) {
  try { return JSON.parse(fs.readFileSync(lockPath, 'utf8')) } catch { return null }
}

/** pid 是否还活着；EPERM 说明进程存在但不属于当前用户 */
export function pidAlive(pid) {
  if (!pid) return false
  try { process.kill(pid, 0); return true } catch (e) { return e.code === 'EPERM' }
}

/** 连接断了（进程没了 / 端口不通）或 token 不对了（常驻进程重启后换了 token） */
function isConnLost(e) {
  if (e?.status === 401) return true
  if (e?.name === 'AbortError' || e?.name === 'TimeoutError') return false   // 只是慢，不是没了
  const code = e?.cause?.code ?? e?.code
  return e?.name === 'TypeError' || ['ECONNREFUSED', 'ECONNRESET', 'EPIPE', 'UND_ERR_SOCKET'].includes(code)
}

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
  constructor({ python, coreDir, configPath, log, spawnImpl, fetchImpl, lockPath }) {
    this.python = python
    this.coreDir = coreDir
    this.configPath = configPath
    this.lockPath = lockPath ?? null
    this.log = log ?? ((lv, m) => { if (lv !== 'debug') console.error(`[soulcore] ${m}`) })
    this.spawnImpl = spawnImpl ?? spawn
    this.fetchImpl = fetchImpl ?? globalThis.fetch
    this.child = null
    this.port = null
    this.token = null
    this.pid = null
    this.attached = false       // true：复用了已经常驻运行的后端，DSH 退出时不该杀它
    this.switching = false      // true：正在切换运行模式，禁止自动重连 / 自己拉子进程
    this.stopping = false
    this.restarts = 0
    this.lastError = null
    this.startedAt = null
    this._ready = null
    this._restartTimer = null
    this.stderrTail = []
  }

  get ready() { return this.port !== null && (this.child !== null || this.attached) }

  status() {
    return {
      ready: this.ready, attached: this.attached, port: this.port, pid: this.pid ?? this.child?.pid ?? null,
      python: this.python, restarts: this.restarts, lastError: this.lastError, startedAt: this.startedAt,
      stderrTail: this.stderrTail.slice(-12),
    }
  }

  /** 启动：先看有没有常驻实例可接入，没有再自己拉子进程 */
  start() {
    if (this._ready) return this._ready
    this.stopping = false
    this._ready = this._start()
    this._ready.catch(() => {})
    return this._ready
  }

  async _start() {
    if (this.lockPath && await this.attach()) return { port: this.port, pid: this.pid, attached: true }
    // 正在切换常驻 / 跟随模式：这时自己拉子进程会抢走锁，让刚起来的常驻进程退出
    if (this.switching) throw new Error('后端正在切换运行模式')
    return this._spawn()
  }

  /** 反复尝试接入常驻进程（它刚被计划任务拉起，可能还没写好锁文件）。成功返回 true */
  async waitAttach(timeoutMs = 25_000) {
    const end = Date.now() + timeoutMs
    while (Date.now() < end) {
      if (await this.attach()) {
        this.stopping = false
        this._ready = Promise.resolve({ port: this.port, pid: this.pid, attached: true })
        return true
      }
      await new Promise((r) => setTimeout(r, 500))
    }
    return false
  }

  /** 让后端退出并等它真正退出（释放锁）：接入的常驻进程走 /v1/shutdown，自己的子进程走 stop() */
  async shutdownBackend(timeoutMs = 15_000) {
    if (!this.attached) { await this.stop(); return true }
    const pid = this.pid
    try { await this._request('POST', '/v1/shutdown', {}, { timeoutMs: 3000 }) } catch {}
    const end = Date.now() + timeoutMs
    while (pid && pidAlive(pid) && Date.now() < end) await new Promise((r) => setTimeout(r, 200))
    await this.stop()                    // 断开（attached 模式下 stop 不会杀进程）
    return !pid || !pidAlive(pid)
  }

  /** 读锁文件 → 确认进程活着 → 用锁里的 token 探一次健康检查 */
  async attach() {
    const info = readLockInfo(this.lockPath)
    if (!info || !info.port || !info.token) return false
    if (info.pid && !pidAlive(info.pid)) return false
    try {
      const res = await this.fetchImpl(`http://127.0.0.1:${info.port}/v1/health`, {
        headers: { Authorization: `Bearer ${info.token}` },
        signal: AbortSignal.timeout(2000),
      })
      if (!res.ok) return false
    } catch { return false }
    this.port = info.port
    this.token = info.token
    this.pid = info.pid ?? null
    this.attached = true
    this.startedAt = new Date().toISOString()
    this.lastError = null
    this.log('info', `接入已运行的后端 port=${info.port} pid=${info.pid} version=${info.version ?? '?'}（不再启动子进程）`)
    return true
  }

  _spawn() {
    this.attached = false
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
    return new Promise((resolve, reject) => {
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

  /** 发请求；后端没就绪时抛错（调用方决定降级）。
   *  接入的是常驻进程时：连不上 / token 失效（常驻进程重启后端口和 token 都会变），
   *  先重读锁文件重新接入（必要时自己拉子进程），再重试一次。 */
  async request(method, pathname, body, opts = {}) {
    if (!this.ready && this._reattaching) await this._reattaching.catch(() => {})
    try {
      return await this._request(method, pathname, body, opts)
    } catch (e) {
      if (!this.attached || this.switching || opts.signal?.aborted || !isConnLost(e)) throw e
      await this._reattach(e)
      return this._request(method, pathname, body, opts)
    }
  }

  _reattach(cause) {
    if (!this._reattaching) {
      this.log('info', `常驻后端连不上（${String(cause?.message ?? cause).slice(0, 120)}），重新读锁文件接入`)
      this.attached = false
      this.port = null; this.token = null; this.pid = null; this._ready = null
      this.stopping = false
      this._reattaching = this.start().finally(() => { this._reattaching = null })
    }
    return this._reattaching
  }

  async _request(method, pathname, body, { timeoutMs = 4000, signal } = {}) {
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
    if (!res.ok) {
      const err = new Error(`soulcore ${pathname} HTTP ${res.status}: ${text.slice(0, 200)}`)
      err.status = res.status
      throw err
    }
    return data
  }

  async restart() {
    if (this.attached) {
      // 常驻进程不归 DSH 管：重启它会把"她一直在"这件事打断，留着下次手动重启
      this.log('info', '常驻模式：后端由计划任务管理，配置改动会在常驻进程重启后生效')
      return this.status()
    }
    await this.stop()
    this.restarts = 0
    this.stopping = false
    return this.start()
  }

  stop() {
    this.stopping = true
    if (this._restartTimer) { clearTimeout(this._restartTimer); this._restartTimer = null }
    if (this.attached) {
      // 只是断开，不杀常驻进程——DSH 关了，她也应该继续在线
      this.attached = false
      this.port = null; this.token = null; this.pid = null; this._ready = null
      return Promise.resolve()
    }
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
