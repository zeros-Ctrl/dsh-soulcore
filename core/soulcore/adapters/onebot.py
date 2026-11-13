"""
QQ 接入：OneBot v11（LLBot / NapCat 等协议端通用）

协议端配置（以 LLBot / NapCat 的 WebUI 为准）：
  - 开启 HTTP 服务（给本插件调用发消息），例如 http://127.0.0.1:3000，记下 access_token
  - 开启 HTTP 上报（HTTP POST），上报地址填 http://127.0.0.1:8765/onebot
  - 消息上报格式选 array（数组），字符串格式也能识别 CQ 码
"""
from __future__ import annotations
import json, logging, queue, random, re, threading, time, urllib.request

from ..events import Event
from ..ratelimit import RateLimiter
from ..stickers import parse_reply, strip_tags

log = logging.getLogger("soulcore.qq")
CQ = re.compile(r"\[CQ:(\w+)((?:,[^\]]*)?)\]")


def parse_segments(message) -> list[dict]:
    if isinstance(message, list):
        return message
    segs, pos = [], 0
    for m in CQ.finditer(message or ""):
        if m.start() > pos:
            segs.append({"type": "text", "data": {"text": _unescape(message[pos:m.start()])}})
        data = dict(kv.split("=", 1) for kv in m.group(2).strip(",").split(",") if "=" in kv)
        segs.append({"type": m.group(1), "data": data})
        pos = m.end()
    if pos < len(message or ""):
        segs.append({"type": "text", "data": {"text": _unescape(message[pos:])}})
    return segs


def _unescape(s: str) -> str:
    return s.replace("&#91;", "[").replace("&#93;", "]").replace("&#44;", ",").replace("&amp;", "&")


class OneBotAdapter:
    def __init__(self, engine, cfg: dict, api_fn=None):
        """api_fn：发送消息的通道。None 时走 HTTP（api_base），传函数时走别的传输（例如 WebSocket）。"""
        self.engine, self.cfg = engine, cfg
        self.api_fn = api_fn
        self.self_id = str(cfg.get("self_id") or "")
        self.groups = {str(g) for g in cfg.get("group_whitelist", [])}
        self.privates = {str(u) for u in cfg.get("private_whitelist", [])}
        self.limiter = RateLimiter(cfg.get("min_interval_s", 3), cfg.get("max_per_min", 10), 60)
        self.q: queue.Queue = queue.Queue(maxsize=200)
        self.sent_log: list[dict] = []   # 测试用
        self._stop = False
        self.worker = threading.Thread(target=self._loop, daemon=True, name="onebot-worker")
        self.worker.start()

    # ---------- 上报入口 ----------
    def on_post(self, payload: dict) -> None:
        ev, reply = self.to_event(payload)
        if ev is None:
            return
        try:
            self.q.put_nowait((ev, reply))
        except queue.Full:
            log.warning("消息队列已满，丢弃一条")

    def to_event(self, p: dict):
        if p.get("post_type") != "message":
            return None, False
        self_id = str(p.get("self_id") or self.self_id)
        uid = str(p.get("user_id", ""))
        if uid == self_id:
            return None, False
        mtype = p.get("message_type")
        segs = parse_segments(p.get("message", p.get("raw_message", "")))
        texts, mention = [], False
        for s in segs:
            t, d = s.get("type"), s.get("data", {})
            if t == "text":
                texts.append(d.get("text", ""))
            elif t == "at":
                if str(d.get("qq")) == self_id:
                    mention = True
                else:
                    texts.append(f"@{d.get('name') or d.get('qq')} ")
            elif t == "image":
                texts.append("[图片]")
            elif t == "face":
                texts.append("[表情]")
        text = "".join(texts).strip()
        if not text:
            return None, False
        name = self.engine.name
        if name and name in text:
            mention = True
        sender = p.get("sender", {}) or {}
        uname = sender.get("card") or sender.get("nickname") or uid
        if mtype == "private":
            # 主人（owner.qq）的私聊总是放行：设置页填了"你的 QQ 号"就够了，不用再加一遍白名单
            owner = str((getattr(self.engine, "cfg", None) or {}).get("owner", {}).get("qq") or "")
            if not (self.cfg.get("allow_all_private") or uid in self.privates or (owner and uid == owner)):
                return None, False
            ev = Event("qq", "private", uid, uid, uname, text, mentions_self=True, ts=p.get("time") or time.time(),
                       event_id=f"qq:{uid}:{p.get('message_id')}",
                       reply_target={"type": "private", "user_id": uid}, raw=p)
            ev.is_stranger = self.engine._trust(ev) == "stranger"
            return ev, True
        if mtype == "group":
            gid = str(p.get("group_id", ""))
            if gid not in self.groups:
                return None, False   # 非白名单群：不看、不存
            ev = Event("qq", "group", gid, uid, uname, text, mentions_self=mention, ts=p.get("time") or time.time(),
                       event_id=f"qq:{gid}:{p.get('message_id')}",
                       reply_target={"type": "group", "group_id": gid, "message_id": p.get("message_id")}, raw=p)
            ev.is_stranger = self.engine._trust(ev) == "stranger"
            reply = mention or random.random() < self.cfg.get("interject_prob", 0.0)
            return ev, reply
        return None, False

    # ---------- 处理与发送 ----------
    def _loop(self):
        while not self._stop:
            try:
                ev, reply = self.q.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                out = self.engine.handle(ev, reply=reply, record_reply=False)
                if out:
                    if self.send(ev, out):
                        self.engine.commit_reply(ev, out)
                    else:
                        self.engine.discard_reply(ev)
            except Exception:
                log.exception("处理 QQ 消息失败")
                try:
                    self.engine.discard_reply(ev)
                except Exception:
                    log.exception("清理未发送的 QQ 回复失败")
            finally:
                self.q.task_done()

    def send(self, ev: Event, text: str) -> bool:
        rt = ev.reply_target
        key = f"{rt['type']}:{rt.get('group_id') or rt.get('user_id')}"
        if not self.limiter.acquire(key):
            log.warning("触发限流，放弃回复 %s", key)
            return False
        if self.cfg.get("typing_delay", True):
            time.sleep(min(6.0, 0.8 + len(text) / 10))
        segs = self._segments(text)
        if not segs:
            return False
        guard = getattr(ev, "_soulcore_delivery_guard", None)
        if callable(guard):
            try:
                if not guard():
                    log.info("取消已过时的主动消息")
                    return False
            except Exception:
                log.exception("主动消息投递前检查失败")
                return False
        if rt["type"] == "group":
            msg = []
            if rt.get("message_id") is not None:
                msg.append({"type": "reply", "data": {"id": str(rt["message_id"])}})
            msg.extend(segs)
            return self._api("send_group_msg", {"group_id": int(rt["group_id"]), "message": msg})
        return self._api("send_private_msg", {"user_id": int(rt["user_id"]), "message": segs})

    def _segments(self, text: str) -> list[dict]:
        """把文字里的 [表情:x] / [图:x] 切成消息段；认不出的标记删掉，别漏成原文。"""
        lib = getattr(self.engine, "stickers", None)
        max_images = int((self.cfg.get("sticker") or {}).get("max_images", 1))
        try:
            segs = parse_reply(text, lib, max_images=max_images)
        except Exception as ex:
            log.warning("表情解析失败，退回纯文字：%s", ex)
            segs = []
        if segs:
            return segs
        plain = strip_tags(text)
        if plain.strip():
            return [{"type": "text", "data": {"text": plain}}]
        log.info("回复只有认不出的表情标记，不发送：%r", (text or "")[:60])
        return []

    def _api(self, action: str, body: dict) -> bool:
        if self.api_fn is not None:
            try:
                res = self.api_fn(action, body) or {}
            except Exception as ex:
                log.error("调用 OneBot action 失败 %s: %s", action, ex)
                self.sent_log.append({"action": action, "body": body, "ok": False})
                return False
            ok = res.get("status") == "ok" or res.get("retcode") == 0
            self.sent_log.append({"action": action, "body": body, "ok": ok})
            if not ok:
                log.warning("OneBot 返回失败: %s", res)
            return ok
        url = self.cfg.get("api_base", "http://127.0.0.1:3000").rstrip("/") + "/" + action
        headers = {"Content-Type": "application/json"}
        if self.cfg.get("access_token"):
            headers["Authorization"] = "Bearer " + self.cfg["access_token"]
        req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                res = json.loads(r.read().decode() or "{}")
            ok = res.get("status") == "ok" or res.get("retcode") == 0
            self.sent_log.append({"action": action, "body": body, "ok": ok})
            if not ok:
                log.warning("OneBot 返回失败: %s", res)
            return ok
        except Exception as ex:
            log.error("调用 OneBot API 失败 %s: %s", action, ex)
            return False

    def stop(self):
        self._stop = True
