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
import json, logging, os, sys, threading
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

from .events import Event
from .emotion import EVENTS

log = logging.getLogger("soulcore.server")


def make_handler(engine, onebot=None, token: str = ""):
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
            if u.path == "/v1/memory/search":
                extra = [x for x in qs.get("extra", [""])[0].split(",") if x]
                its = engine.store.search(qs.get("q", [""])[0], qs.get("path", ["core"])[0],
                                          k=int(qs.get("k", ["10"])[0]), touch=False, extra_paths=extra)
                return self._send(200, [{"id": i.id, "path": i.path, "kind": i.kind, "content": i.content,
                                         "score": round(i.score, 4), "weight": round(i.weight_now, 4)} for i in its])
            return self._send(404, {"error": "not found"})

        def do_POST(self):
            u = urlparse(self.path); qs = parse_qs(u.query)
            if not self._auth(qs):
                return self._send(401, {"error": "unauthorized"})
            try:
                b = self._body()
            except json.JSONDecodeError:
                return self._send(400, {"error": "bad json"})
            try:
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
                    return self._send(200, engine.context_block(ev))
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


def serve(engine, cfg: dict, block: bool = True):
    scfg = cfg.get("server", {})
    onebot = None
    stop = threading.Event()
    threads = []
    if cfg.get("qq", {}).get("enabled"):
        from .adapters.onebot import OneBotAdapter
        onebot = OneBotAdapter(engine, cfg["qq"])
        log.info("QQ(OneBot v11) 已启用，上报地址 http://%s:%s/onebot", scfg.get("host"), scfg.get("port"))
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

    token = os.environ.get("SOULCORE_TOKEN") or scfg.get("token", "")
    httpd = ThreadingHTTPServer((scfg.get("host", "127.0.0.1"), int(scfg.get("port", 8765))),
                                make_handler(engine, onebot, token))
    # 握手：宿主（DSH 插件）从 stdout 读这一行拿到实际端口
    print("SOULCORE_READY " + json.dumps({"port": httpd.server_address[1], "pid": os.getpid(),
                                           "name": engine.name}, ensure_ascii=False), flush=True)
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
        return httpd, onebot, stop
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        httpd.server_close()
        if onebot:
            onebot.stop()
        engine.close()
