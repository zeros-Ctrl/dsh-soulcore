"""
B站接入（评论区 @/回复 + 私信），基于 bilibili-api-python==17.4.2（原仓库已归档，务必锁版本）。

安全设计：
  - 首次运行只记录游标，不回复历史积压
  - 默认 auto_send=false：生成的回复进入人工审核队列（python -m soulcore review 审核后才发）
  - 评论 / 私信分别限流；遇到 -352 / -412 / 验证码等风控码立即暂停，需手动 resume
  - 私信只处理白名单 UID；评论只处理 @她 和回复她的
  - 回复在真正发出后才写入记忆（被你否掉的草稿不会变成"她说过的话"）
"""
from __future__ import annotations
import json, logging, os, threading, time, uuid

from ..events import Event
from ..ratelimit import RateLimiter

log = logging.getLogger("soulcore.bili")
RISK_CODES = {-352, -412, -509, 12015, 12051, 21109, -101, -111}


class ReviewQueue:
    def __init__(self, path: str):
        self.path = path
        self.lock = threading.Lock()

    def _load(self) -> list[dict]:
        if not os.path.exists(self.path):
            return []
        with open(self.path, encoding="utf-8") as f:
            return [json.loads(l) for l in f if l.strip()]

    def _save(self, items):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            for it in items:
                f.write(json.dumps(it, ensure_ascii=False) + "\n")
        os.replace(tmp, self.path)

    def add(self, ev: Event, draft: str, ctx_mood: str = "") -> str:
        with self.lock:
            items = self._load()
            it = {"id": uuid.uuid4().hex[:8], "status": "pending", "created": time.time(),
                  "event": ev.to_dict(), "draft": draft, "mood": ctx_mood}
            it["event"].pop("raw", None)
            items.append(it)
            self._save(items)
            return it["id"]

    def pending(self) -> list[dict]:
        with self.lock:
            return [i for i in self._load() if i["status"] == "pending"]

    def update(self, iid: str, **kw):
        with self.lock:
            items = self._load()
            for it in items:
                if it["id"] == iid:
                    it.update(kw)
            self._save(items)


class BiliPaused(Exception):
    pass


class BiliAdapter:
    def __init__(self, engine, cfg: dict, api=None):
        """api: 便于测试注入假实现；默认使用 bilibili_api"""
        self.engine, self.cfg = engine, cfg
        self.store = engine.store
        self.queue = ReviewQueue(os.path.join(engine.data_dir, "review_queue.jsonl"))
        self.c_lim = RateLimiter(cfg.get("comment_min_interval_s", 15), cfg.get("comment_max_per_hour", 20), 3600)
        self.d_lim = RateLimiter(cfg.get("dm_min_interval_s", 10), cfg.get("dm_max_per_hour", 20), 3600)
        self.dm_white = {str(u) for u in cfg.get("dm_whitelist", [])}
        self.api = api or self._real_api()
        self.cred = self.api.make_credential(cfg.get("credential", {}))
        self.self_uid = str(cfg.get("credential", {}).get("dedeuserid", ""))

    @staticmethod
    def _real_api():
        try:
            return _RealApi()
        except ImportError as ex:
            raise SystemExit("缺少依赖：pip install bilibili-api-python==17.4.2  (%s)" % ex)

    # ---------- 风控暂停 ----------
    @property
    def paused(self):
        return self.store.meta_get("bili_paused")

    def resume(self):
        self.store.meta_set("bili_paused", None)

    def _guard(self, fn, *a, **kw):
        if self.paused:
            raise BiliPaused(self.paused)
        try:
            return fn(*a, **kw)
        except Exception as ex:
            code = getattr(ex, "code", None)
            if code in RISK_CODES:
                reason = {"code": code, "msg": str(ex)[:200], "at": time.strftime("%Y-%m-%d %H:%M:%S")}
                self.store.meta_set("bili_paused", reason)
                log.error("触发 B站风控 %s，已暂停。确认账号正常后运行 `python -m soulcore bili --resume`", reason)
                raise BiliPaused(reason)
            raise

    # ---------- 轮询 ----------
    def poll_once(self) -> list[Event]:
        evs: list[Event] = []
        if self.cfg.get("reply_at", True):
            evs += self._poll_feed("at", self.api.get_at, "at_time")
        if self.cfg.get("reply_replies", True):
            evs += self._poll_feed("reply", self.api.get_replies, "reply_time")
        if self.dm_white:
            evs += self._poll_dm()
        for ev in evs:
            self._process(ev)
        return evs

    def _poll_feed(self, kind: str, fn, tkey: str) -> list[Event]:
        data = self._guard(fn, self.cred) or {}
        items = data.get("items") or []
        key = f"bili_cursor:{kind}"
        last = self.store.meta_get(key)
        if not items:
            if last is None:
                self.store.meta_set(key, 0)
            return []
        max_id = max(int(i.get("id", 0)) for i in items)
        if last is None:  # 第一次：只记游标，不回历史
            self.store.meta_set(key, max_id)
            return []
        out = []
        for it in sorted(items, key=lambda x: int(x.get("id", 0))):
            if int(it.get("id", 0)) <= last:
                continue
            body = it.get("item", {}) or {}
            if body.get("type") not in ("reply", None) or int(body.get("business_id", 1)) != 1:
                continue  # 只处理视频评论区
            user = it.get("user", {}) or {}
            oid = int(body.get("subject_id", 0))
            src = int(body.get("source_id", 0))
            root = int(body.get("root_id", 0)) or src
            uid = str(user.get("mid", ""))
            if not oid or not src or uid == self.self_uid:
                continue
            rel = self.store.relation("bilibili", uid)
            out.append(Event(
                "bilibili", "comment", self.api.aid2bvid(oid), uid, user.get("nickname", uid),
                body.get("source_content", ""), mentions_self=True,
                ts=float(it.get(tkey) or time.time()), event_id=f"bili:{kind}:{it.get('id')}",
                thread_id=str(root), is_stranger=rel["interactions"] < 3,
                reply_target={"kind": "comment", "oid": oid, "type": 1, "root": root, "parent": src}))
        self.store.meta_set(key, max(max_id, last))
        return out

    def _poll_dm(self) -> list[Event]:
        data = self._guard(self.api.get_sessions, self.cred) or {}
        out = []
        for s in data.get("session_list") or []:
            tid = str(s.get("talker_id"))
            if s.get("session_type") != 1 or tid not in self.dm_white or not s.get("unread_count"):
                continue
            key = f"bili_dm_seq:{tid}"
            last = self.store.meta_get(key, 0)
            msgs = (self._guard(self.api.fetch_session_msgs, int(tid), self.cred) or {}).get("messages") or []
            mx = last
            for m in sorted(msgs, key=lambda x: int(x.get("msg_seqno", 0))):
                seq = int(m.get("msg_seqno", 0))
                mx = max(mx, seq)
                if seq <= last or str(m.get("sender_uid")) != tid or m.get("msg_type") != 1:
                    continue
                try:
                    text = json.loads(m.get("content", "{}")).get("content", "")
                except json.JSONDecodeError:
                    text = str(m.get("content", ""))
                if last == 0:
                    continue  # 首次只记游标
                out.append(Event("bilibili", "dm", tid, tid, tid, text, mentions_self=True,
                                 ts=float(m.get("timestamp") or time.time()), event_id=f"bili:dm:{tid}:{seq}",
                                 reply_target={"kind": "dm", "receiver_id": int(tid)}))
            self.store.meta_set(key, mx)
        return out

    # ---------- 生成 → 审核 / 发送 ----------
    def _process(self, ev: Event):
        if not ev.text.strip():
            return
        self.engine.observe(ev)
        ctx = self.engine.prepare(ev)
        draft = self.engine.generate(ctx)
        if not draft:
            return
        if ev.scene == "comment":
            draft = draft[:300]
        if self.cfg.get("auto_send", False):
            if self.send(ev, draft):
                self.engine.commit_reply(ev, draft)
        else:
            qid = self.queue.add(ev, draft, ctx.get("mood_text", ""))
            log.info("新草稿待审核 %s: %s", qid, draft[:40])

    def send(self, ev: Event, text: str) -> bool:
        rt = ev.reply_target
        lim = self.c_lim if rt["kind"] == "comment" else self.d_lim
        if not lim.acquire(rt["kind"], max_wait=600):
            log.warning("B站限流：本小时额度已满，跳过")
            return False
        if rt["kind"] == "comment":
            self._guard(self.api.send_comment, text, rt["oid"], rt["type"], rt["root"], rt["parent"], self.cred)
        else:
            self._guard(self.api.send_msg, self.cred, rt["receiver_id"], text)
        return True

    def approve(self, item: dict, text: str | None = None) -> bool:
        ev = Event.from_dict(item["event"])
        text = text or item["draft"]
        ok = self.send(ev, text)
        if ok:
            self.engine.commit_reply(ev, text)
            self.queue.update(item["id"], status="sent", final=text, sent_at=time.time())
        return ok

    def run_forever(self, stop: threading.Event | None = None):
        stop = stop or threading.Event()
        while not stop.is_set():
            try:
                if not self.paused:
                    self.poll_once()
            except BiliPaused:
                pass
            except Exception:
                log.exception("B站轮询出错")
            stop.wait(self.cfg.get("poll_interval_s", 90))


class _RealApi:
    """对 bilibili_api 的薄封装（同步调用）"""
    def __init__(self):
        import bilibili_api as b
        from bilibili_api import session, comment, sync
        self.b, self.session, self.comment, self.sync = b, session, comment, sync

    def make_credential(self, c: dict):
        return self.b.Credential(sessdata=c.get("sessdata") or None, bili_jct=c.get("bili_jct") or None,
                                 buvid3=c.get("buvid3") or None, dedeuserid=c.get("dedeuserid") or None)

    def get_at(self, cred):
        return self.sync(self.session.get_at(cred))

    def get_replies(self, cred):
        return self.sync(self.session.get_replies(cred))

    def get_sessions(self, cred):
        return self.sync(self.session.get_sessions(cred))

    def fetch_session_msgs(self, talker_id, cred):
        return self.sync(self.session.fetch_session_msgs(talker_id, cred))

    def send_comment(self, text, oid, type_, root, parent, cred):
        rtype = self.comment.CommentResourceType(type_)
        return self.sync(self.comment.send_comment(text, oid, rtype, root=root, parent=parent if parent != root else None,
                                                   credential=cred))

    def send_msg(self, cred, receiver_id, text):
        return self.sync(self.session.send_msg(cred, receiver_id, self.session.EventType.TEXT, text))

    def aid2bvid(self, aid: int) -> str:
        return self.b.aid2bvid(aid)
