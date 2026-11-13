"""
HTTP 服务：给任意 harness（JS/TS/Python 都行）调用的钩子接口 + OneBot 上报入口。

  GET  /v1/state                      当前情绪、记忆统计
  GET  /v1/memory/search?q=..&path=.. 手动查记忆
  POST /v1/chat        {event}        全托管：返回 {reply, mood, label}
  POST /v1/hooks/before {event}       钩子：返回 {system, history, memories, mood, label}
  POST /v1/hooks/after  {event, reply}
  POST /v1/hooks/pre_compact {event, messages:[{role,content}]}
  POST /v1/event       {name}         手动触发情绪事件（调试用）
  POST /onebot                        OneBot v11 HTTP 上报地址
"""
from __future__ import annotations
import json, logging, os, secrets, sys, threading, time
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

from . import __version__
from .events import Event
from .emotion import EVENTS

log = logging.getLogger("soulcore.server")


def make_handler(engine, onebot=None, token: str = "", ctl: dict | None = None):
    """ctl：运行时控制回调，由 serve() 在建好 httpd 之后填入
         qq_status() -> dict   当前 QQ 连接状态（设置页显示）
         shutdown()            优雅退出（设置页切换常驻 / 重启常驻进程用）"""
    ctl = ctl if ctl is not None else {}
    class H(BaseHTTPRequestHandler):
        server_version = "soulcore/0.1"

        def log_message(self, fmt, *args):
            log.debug(fmt, *args)

        def _send(self, code: int, obj=None):
            body = b"" if obj is None else json.dumps(obj, ensure_ascii=False).encode()
            self.send_response(code)
            if obj is not None:
                self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _auth(self, qs) -> bool:
            if not token:
                return True
            h = self.headers.get("Authorization", "")
            return h == f"Bearer {token}" or qs.get("token", [""])[0] == token

        def _body(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n).decode() or "{}") if n else {}

        def do_GET(self):
            u = urlparse(self.path); qs = parse_qs(u.query)
            if not self._auth(qs):
                return self._send(401, {"error": "unauthorized"})
            if u.path == "/v1/health":
                return self._send(200, {"ok": True, "name": engine.name})
            if u.path == "/v1/state":
                return self._send(200, engine.state())
            if u.path == "/v1/persona":
                return self._send(200, {"name": engine.name, "text": engine.card_text()})
            if u.path == "/v1/persona/stable":
                # 稳定块：锚点 / 她对自己的认识 / 主人画像。宿主缓存起来放进 system prompt，
                # 只在 version 变化时替换（这份内容一天才变一次，每轮重复注入会破坏提示缓存）。
                return self._send(200, engine.stable_block())
            if u.path == "/v1/qq/status":
                fn = ctl.get("qq_status")
                return self._send(200, fn() if fn else {"enabled": False})
            if u.path == "/v1/initiative/status":
                it = ctl.get("initiative")
                return self._send(200, it.status() if it else {"enabled": False, "available": False})
            if u.path == "/v1/memory/search":
                extra = [x for x in qs.get("extra", [""])[0].split(",") if x]
                its = engine.store.search(qs.get("q", [""])[0], qs.get("path", ["core"])[0],
                                          k=int(qs.get("k", ["10"])[0]),
                                          touch=qs.get("touch", ["0"])[0].lower() in ("1", "true", "yes"),
                                          extra_paths=extra)
                return self._send(200, [{"id": i.id, "path": i.path, "kind": i.kind, "content": i.content,
                                         "trust": i.trust, "pinned": bool(i.pinned),
                                         "score": round(i.score, 4), "weight": round(i.weight_now, 4)}
                                        for i in its])
            return self._send(404, {"error": "not found"})

        def do_POST(self):
            u = urlparse(self.path); qs = parse_qs(u.query)
            if not self._auth(qs):
                # 先把请求体读掉再回 401：否则 Windows 上客户端看到的是"连接被重置"而不是 401
                try:
                    self.rfile.read(min(int(self.headers.get("Content-Length") or 0), 1 << 20))
                except (OSError, ValueError):
                    pass
                return self._send(401, {"error": "unauthorized"})
            try:
                b = self._body()
            except json.JSONDecodeError:
                return self._send(400, {"error": "bad json"})
            try:
                if u.path == "/v1/qq/test":
                    from .adapters.onebot_ws import probe
                    return self._send(200, probe(str(b.get("ws_url") or ""), str(b.get("access_token") or ""),
                                                 timeout=float(b.get("timeout") or 8)))
                if u.path in ("/v1/initiative/run", "/v1/initiative/draft"):
                    it = ctl.get("initiative")
                    if it is None:
                        return self._send(503, {"error": "主动聊天不可用"})
                    if u.path == "/v1/initiative/run":
                        return self._send(200, it.check(force=bool(b.get("force", True))))
                    return self._send(200, it.draft_action(str(b.get("id") or ""), str(b.get("action") or "")))
                if u.path == "/v1/shutdown":
                    fn = ctl.get("shutdown")
                    if fn is None:
                        return self._send(503, {"error": "shutdown not available"})
                    self._send(200, {"ok": True, "pid": os.getpid()})
                    fn()
                    return None
                if u.path == "/onebot":
                    if onebot is None:
                        return self._send(503, {"error": "qq adapter disabled"})
                    onebot.on_post(b)
                    return self._send(204)
                if u.path == "/v1/event":
                    if b.get("name") not in EVENTS:
                        return self._send(400, {"error": f"unknown event, choose from {list(EVENTS)}"})
                    amp = engine.system_event(b["name"])
                    return self._send(200, {"amp": amp, **engine.state()})
                if u.path == "/v1/memory/sync":
                    return self._send(200, engine.store.sync_markdown(force=bool(b.get("force"))))
                ev = Event.from_dict(b.get("event", {}))
                if u.path == "/v1/chat":
                    reply = engine.handle(ev, reply=b.get("reply", True))
                    st = engine.state()
                    return self._send(200, {"reply": reply, "mood": st["mood"], "label": st["label"]})
                if u.path == "/v1/hooks/before":
                    return self._send(200, {k: v for k, v in engine.before(ev).items() if not k.startswith("_")})
                if u.path == "/v1/hooks/context":
                    # include_stable=False：宿主已经把稳定块放进 system prompt，这里别重复
                    return self._send(200, engine.context_block(
                        ev, include_stable=bool(b.get("include_stable", True)),
                        style=str(b.get("style") or "companion")))
                if u.path == "/v1/memory/remember":
                    iid = engine.remember(ev, str(b.get("content", ""))[:1000], b.get("kind", "fact"),
                                          float(b.get("importance", 0.85)))
                    return self._send(200, {"ok": iid is not None, "id": iid})
                if u.path == "/v1/hooks/after":
                    engine.after(ev, b.get("reply", ""))
                    return self._send(200, {"ok": True})
                if u.path == "/v1/hooks/pre_compact":
                    n = engine.pre_compact(ev, b.get("messages", []))
                    return self._send(200, {"ok": True, "extracted": n})
            except TypeError as ex:
                return self._send(400, {"error": f"bad event: {ex}"})
            except Exception as ex:
                log.exception("处理请求失败")
                return self._send(500, {"error": str(ex)})
            return self._send(404, {"error": "not found"})

    return H


def bind_http_server(host: str, port: int, handler, attempts: int = 10) -> ThreadingHTTPServer:
    """绑定 HTTP 服务。port=0（随机端口，DSH 拉起子进程时就是这样）遇到 WinError 10013 时重试。

    Windows 上 Hyper-V / WinNAT 会在动态端口范围里保留大段端口（`netsh int ipv4 show
    excludedportrange protocol=tcp` 可以看到），系统偶尔把随机端口分到保留段里，bind 就报 10013。
    再要一次端口通常就好了；指定了固定端口时不重试，直接报错让用户换端口。
    实测这种拒绝有时会持续几秒（期间所有回环 bind 都失败），所以用指数退避，总共约 10 秒，
    仍在 DSH 等待握手的 20 秒以内。"""
    for i in range(attempts):
        try:
            return ThreadingHTTPServer((host, port), handler)
        except OSError as ex:
            if port != 0 or getattr(ex, "winerror", None) != 10013 or i == attempts - 1:
                raise
            delay = min(2.0, 0.05 * (2 ** i))
            log.info("绑定端口被系统拒绝（WinError 10013），%.2f 秒后重试 %d/%d", delay, i + 1, attempts)
            time.sleep(delay)
    raise RuntimeError("unreachable")


def serve(engine, cfg: dict, block: bool = True, lock_path: str | None = None,
          version: str = __version__, lock=None):
    """启动 HTTP 服务。

    lock_path：单实例锁文件。抢不到锁说明已经有实例在跑，直接打印信息后退出（返回 None）。
    version：写进锁文件，DSH 接入时用来判断"锁里是不是旧版本的后端"。
    """
    scfg = cfg.get("server", {})
    onebot = None
    ws = None
    stop = threading.Event()
    threads = []
    if lock is not None:
        # 调用方（cli）已经在创建 Engine 之前抢到锁了：避免第二个实例先跑迁移再发现自己是多余的
        lock_path = lock_path or lock.path
    elif lock_path:
        from .lockfile import InstanceLock
        lock = InstanceLock(lock_path)
        if not lock.acquire():
            info = InstanceLock.read(lock_path) or {}
            log.warning("已有 soulcore 在运行（pid=%s port=%s），本进程退出",
                        info.get("pid"), info.get("port"))
            print("SOULCORE_ALREADY_RUNNING " + json.dumps(info, ensure_ascii=False), flush=True)
            return None

    qq = cfg.get("qq", {})
    if qq.get("enabled"):
        if qq.get("transport") == "ws":
            from .adapters.onebot_ws import OneBotWSAdapter
            ws = OneBotWSAdapter(engine, qq).start()
            log.info("QQ 使用 WebSocket（%s），不需要再配 HTTP 上报", qq.get("ws_url"))
        else:
            from .adapters.onebot import OneBotAdapter
            onebot = OneBotAdapter(engine, qq)
            log.info("QQ(OneBot v11 HTTP) 已启用，上报地址 http://%s:%s/onebot",
                     scfg.get("host"), scfg.get("port"))
    if cfg.get("bilibili", {}).get("enabled"):
        from .adapters.bilibili import BiliAdapter
        bili = BiliAdapter(engine, cfg["bilibili"])
        threads.append(threading.Thread(target=bili.run_forever, args=(stop,), daemon=True, name="bili"))
        log.info("B站已启用（auto_send=%s）", cfg["bilibili"].get("auto_send"))

    def ticker():
        while not stop.wait(scfg.get("tick_every_min", 15) * 60):
            try:
                log.info("tick %s", engine.tick())
            except Exception:
                log.exception("tick 失败")
    threads.append(threading.Thread(target=ticker, daemon=True, name="ticker"))

    # 独立运行时也要有 token：否则本机任何程序都能调 /v1/chat。
    # 由 DSH 拉起的子进程会用环境变量传 token；只有自己常驻启动（有锁）时才随机生成。
    token = os.environ.get("SOULCORE_TOKEN") or scfg.get("token") or ""
    if not token and lock_path:
        token = secrets.token_hex(24)
    ctl: dict = {}
    httpd = bind_http_server(scfg.get("host", "127.0.0.1"), int(scfg.get("port", 8765)),
                             make_handler(engine, onebot, token, ctl))

    def qq_status() -> dict:
        st = {"enabled": bool(qq.get("enabled")), "transport": qq.get("transport", "http")}
        if ws is not None:
            st.update(connected=ws.connected, self_id=ws.onebot.self_id, ws_url=qq.get("ws_url"))
        elif onebot is not None:
            st.update(connected=None, self_id=onebot.self_id)
        return st
    ctl["qq_status"] = qq_status

    # 主动聊天：发送走 QQ 私聊（没开 QQ 时只能生成草稿）
    from .initiative import Initiative
    qq_adapter = ws.onebot if ws is not None else onebot
    initiative = Initiative(engine, sender=qq_adapter.send if qq_adapter is not None else None)
    engine.initiative = initiative
    engine.qq_sender = qq_adapter.send if qq_adapter is not None else None   # 看完视频后主动发消息
    ctl["initiative"] = initiative

    def initiative_loop():
        every = max(1.0, float(initiative.cfg.get("check_every_min", 5))) * 60
        while not stop.wait(every):
            try:
                r = initiative.check()
                if r.get("action") in ("sent", "draft", "error"):
                    log.info("主动聊天：%s %s", r.get("action"), r.get("reason"))
            except Exception:
                log.exception("主动聊天检查失败")
    threads.append(threading.Thread(target=initiative_loop, daemon=True, name="initiative"))
    # 在另一个线程里调 shutdown()：serve_forever 所在线程返回后走下面 finally 的清理（释放锁、关库）
    ctl["shutdown"] = lambda: threading.Thread(target=httpd.shutdown, daemon=True, name="shutdown").start()
    port = httpd.server_address[1]
    if lock is not None:
        lock.write({"pid": os.getpid(), "port": port, "token": token,
                    "started": time.time(), "version": version,
                    "name": engine.name, "data_dir": engine.data_dir})
    # 握手：宿主（DSH 插件）从 stdout 读这一行拿到实际端口
    print("SOULCORE_READY " + json.dumps({"port": port, "pid": os.getpid(),
                                           "name": engine.name, "version": version},
                                          ensure_ascii=False), flush=True)
    if os.environ.get("SOULCORE_STDIN_WATCHDOG") == "1":
        # 宿主进程退出（哪怕是被强杀）时 stdin 管道会关闭 → 跟着退出，避免留下孤儿进程
        def watchdog():
            try:
                while sys.stdin.buffer.read(1024):
                    pass
            except Exception:
                pass
            log.info("宿主已退出，soulcore 跟随退出")
            try:
                engine.close()
            finally:
                os._exit(0)
        threads.append(threading.Thread(target=watchdog, daemon=True, name="watchdog"))
    for t in threads:
        t.start()
    log.info("soulcore 服务已启动 http://%s:%s", *httpd.server_address[:2])
    if not block:
        th = threading.Thread(target=httpd.serve_forever, daemon=True)
        th.start()
        httpd.soulcore_lock = lock
        httpd.soulcore_ws = ws
        return httpd, onebot or ws, stop
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        httpd.server_close()
        if onebot:
            onebot.stop()
        if ws:
            ws.stop()
        if lock is not None:
            lock.release()
        engine.close()
