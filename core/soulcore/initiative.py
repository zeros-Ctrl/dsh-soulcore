"""主动聊天：她会在合适的时候主动给主人发 QQ 私聊。

思路参考 Inner Thoughts（CHI 2025）：把"要不要开口"和"这个念头值不值得说"分开，宁可不说也不说没用的话。

  什么时候说（overt）：每 check_every_min 分钟检查一次，按"危险率"随机决定，不会每天固定时刻冒出来
      λ = base_rate · (1 + 3·孤独) · (0.5 + 好感) · 时段曲线 · 0.5^连续未回
      p = 1 − e^(−λ·Δt)
    加上硬约束：勿扰时段、每天上限、最小间隔、刚在 QQ 上聊过不打扰、连续没回就等他先开口、余额耗尽。

  说什么（covert）：先列候选念头，打分，取最高；低于阈值就不说
      跟进   他提过的将来的事（明天答辩），到了该问的时刻          0.9
      未完   她上次问的问题他没回，过了几小时                     0.6
      关心   深夜还在 DSH 里写代码 / 连续忙了很久                 0.5~0.6
      分享   心情特别好                                           0.4~0.8
      想你   孤独很高、又没别的话题                               0.3~0.7
    DSH 里正在忙时只允许"关心"类念头，其余的不打扰。

  反馈闭环：主动发出后 reply_window_hours 内他回了 → responded 事件（开心一点）、未回计数清零；
           没回 → ignored 事件（低落一点）、未回计数 +1 → 下次开口概率减半。

  dry_run（默认开）：不真的发，进审核队列 initiative_queue.jsonl，设置页里可以看、发送或丢弃。

状态存在 memory.db 的 meta 表（键 initiative_state），重启不丢。
"""
from __future__ import annotations

import logging
import math
import os
import random
import re
import threading
import time
import uuid
from dataclasses import dataclass, asdict

from .events import Event
from .extractor import is_question
from .llm import LLMError

log = logging.getLogger("soulcore.initiative")

DEFAULTS = {
    "enabled": False,
    "dry_run": True,
    "base_rate_per_hour": 0.15,
    "quiet_hours": ["00:00", "09:00"],
    "max_per_day": 3,
    "min_gap_hours": 3.0,
    "recent_activity_skip_min": 30,
    "max_unanswered": 2,
    "reply_window_hours": 4.0,
    "thought_threshold": 0.45,
    "check_every_min": 5,
}

STATE_KEY = "initiative_state"

COMPOSE_RULES = """## 这一条是你主动发起的
你现在想主动给{owner}发一条 QQ 消息。你想说话的原因（只是你心里的念头，不要照抄）：{reason}
要求：
- 一到两句话，口语化，像真人发微信/QQ 那样；不要用"在吗"开头。
- 不要解释你为什么发消息，不要说"我是 AI"，不要列表。
- 语气跟着你现在的心情走。
- 如果你觉得现在说这个不合适、或者没什么意思，只输出 SKIP 这四个字母。"""


@dataclass
class Thought:
    kind: str
    score: float
    reason: str
    ref: str


def _hhmm(s: str) -> int:
    m = re.match(r"^\s*(\d{1,2}):(\d{2})\s*$", str(s or ""))
    if not m:
        return 0
    return (int(m.group(1)) % 24) * 60 + min(59, int(m.group(2)))


def in_quiet(now: float, quiet) -> bool:
    if not quiet or len(quiet) != 2:
        return False
    a, b = _hhmm(quiet[0]), _hhmm(quiet[1])
    t = time.localtime(now)
    m = t.tm_hour * 60 + t.tm_min
    if a == b:
        return False
    return a <= m < b if a < b else (m >= a or m < b)


def hour_curve(now: float) -> float:
    """白天上班/上课时间少打扰，饭点和晚上多一点。"""
    h = time.localtime(now).tm_hour
    if h < 9:
        return 0.3
    if h < 12:
        return 0.6
    if h < 14:
        return 1.0
    if h < 18:
        return 0.5
    if h < 23:
        return 1.5
    return 0.8


def _midnight(ts: float) -> float:
    t = time.localtime(ts)
    return time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 0, 0, 0, 0, 0, -1))


def _date(ts: float) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(ts))


class Initiative:
    def __init__(self, engine, sender=None, rng: random.Random | None = None):
        """sender(ev, text) -> bool：真正发 QQ 消息（OneBotAdapter.send）。没接 QQ 时为 None。"""
        from .adapters.bilibili import ReviewQueue      # 通用的 jsonl 审核队列
        self.engine = engine
        self.sender = sender
        self.rng = rng or random.Random()
        self.queue = ReviewQueue(os.path.join(engine.data_dir, "initiative_queue.jsonl"))
        self.lock = threading.RLock()

    # ------------------------------------------------------------------ 配置 / 状态
    @property
    def cfg(self) -> dict:
        return {**DEFAULTS, **(self.engine.cfg.get("initiative") or {})}

    @property
    def owner(self) -> str:
        return str((self.engine.cfg.get("owner") or {}).get("qq") or "")

    def _now(self) -> float:
        return self.engine.clock()

    def _load(self) -> dict:
        st = self.engine.store.meta_get(STATE_KEY, None) or {}
        st.setdefault("log", [])
        st.setdefault("unanswered", 0)
        st.setdefault("done_refs", [])
        st.setdefault("last_qq", 0.0)
        st.setdefault("last_local", 0.0)
        st.setdefault("local_session_start", 0.0)
        st.setdefault("last_check", 0.0)
        st.setdefault("last_reason", "")
        return st

    def _save(self, st: dict) -> None:
        st["log"] = st["log"][-200:]
        st["done_refs"] = st["done_refs"][-500:]
        self.engine.store.meta_set(STATE_KEY, st)

    def owner_event(self, now: float, text: str = "") -> Event:
        name = (self.engine.cfg.get("owner") or {}).get("name") or "主人"
        return Event("qq", "private", self.owner, self.owner, name, text, mentions_self=True, ts=now,
                     reply_target={"type": "private", "user_id": self.owner})

    # ------------------------------------------------------------------ 观察主人
    def on_owner_message(self, ev: Event) -> None:
        """主人说了话（QQ 或 DSH）。由 Engine.observe 调用。"""
        now = ev.ts or self._now()
        fire = False
        with self.lock:
            st = self._load()
            if ev.platform == "qq":
                st["last_qq"] = now
                if ev.scene == "private":
                    win = float(self.cfg["reply_window_hours"]) * 3600
                    for e in reversed(st["log"]):
                        if e.get("status") != "sent":
                            continue
                        if e.get("answered") is None and now - e["ts"] <= win:
                            e["answered"] = True
                            e["answered_at"] = now
                            fire = True
                        break
                    st["unanswered"] = 0           # 他先开口了：不管之前回没回，都重新开始
            elif ev.platform == "local":
                if now - float(st["last_local"] or 0) > 30 * 60:
                    st["local_session_start"] = now
                st["last_local"] = now
            self._save(st)
        if fire:
            self.engine.system_event("responded")

    def _expire(self, st: dict, now: float) -> bool:
        """主动发出去的消息超过 reply_window 还没回 → 记一次"没回"。返回是否需要触发 ignored 事件。"""
        win = float(self.cfg["reply_window_hours"]) * 3600
        fire = False
        for e in st["log"]:
            if e.get("status") == "sent" and e.get("answered") is None and now - e["ts"] > win:
                e["answered"] = False
                st["unanswered"] = int(st["unanswered"]) + 1
                fire = True
        return fire

    # ------------------------------------------------------------------ 念头
    def thoughts(self, st: dict, now: float) -> list[Thought]:
        eng, out = self.engine, []
        done = set(st["done_refs"])
        today = _date(now)

        # 跟进：他提过的将来的事，到点了
        for it in eng.store.due_items(now, trusts=("owner",)):
            ref = f"item:{it.id}"
            if ref in done:
                continue
            age_h = (now - (it.due_at or now)) / 3600
            out.append(Thought("followup", max(0.6, 0.9 - 0.005 * age_h),
                               f"他之前提过：{it.content}。现在这件事应该已经过去了，你想问问他情况怎么样", ref))

        # 未完：她在私聊里问过的问题，他一直没回答
        if self.owner:
            turns = eng.store.recent_turns(f"chat/qq/private/{self.owner}", 3)
            if turns:
                last = turns[-1]
                age = now - last.created
                if last.role == "assistant" and is_question(last.content) and 3 * 3600 <= age <= 72 * 3600:
                    ref = f"turn:{last.id}"
                    if ref not in done:
                        out.append(Thought("unfinished", 0.6,
                                           f"你上次问他「{last.content[:60]}」，他一直没回，你想接着这个话题聊", ref))

        # 关心：深夜还在 DSH 里忙 / 连续忙了很久
        last_local = float(st["last_local"] or 0)
        if now - last_local <= 15 * 60:
            h = time.localtime(now).tm_hour
            hours = (now - float(st["local_session_start"] or now)) / 3600
            if h >= 23 or h < 3:
                ref = f"care:late:{today}"
                if ref not in done:
                    out.append(Thought("care", 0.6, "已经很晚了，他还在 DSH 里写代码，你有点担心他", ref))
            elif hours >= 3:
                ref = f"care:long:{today}"
                if ref not in done:
                    out.append(Thought("care", 0.5, f"他已经在 DSH 里连续忙了 {int(hours)} 个多小时了", ref))

        # 分享：心情特别好
        mood = eng.emo.mood
        if mood >= 0.8:
            ref = f"share:{today}"
            if ref not in done:
                out.append(Thought("share", min(0.8, 0.4 + (mood - 0.8) * 2), "你今天心情特别好，想和他说说话", ref))

        # 想你：很孤单，又没别的话题
        L = eng.emo.L
        if L > 0.6:
            ref = f"missing:{today}"
            if ref not in done:
                out.append(Thought("missing", min(0.7, 0.3 + (L - 0.6)), "你已经好久没和他说话了，有点想他", ref))
        return out

    # ------------------------------------------------------------------ 主流程
    def probability(self, st: dict, dt_min: float) -> float:
        c, eng = self.cfg, self.engine
        rel = eng.store.relation("qq", self.owner) if self.owner else {"affinity": 0.5}
        lam = float(c["base_rate_per_hour"]) / 60.0
        lam *= 1 + 3 * eng.emo.L
        lam *= 0.5 + float(rel.get("affinity", 0.5))
        lam *= hour_curve(self._now())
        lam *= 0.5 ** int(st["unanswered"])
        return 1 - math.exp(-lam * max(0.0, dt_min))

    def _blockers(self, st: dict, now: float) -> str:
        c = self.cfg
        if in_quiet(now, c.get("quiet_hours")):
            return "勿扰时段"
        if int(st["unanswered"]) >= int(c["max_unanswered"]):
            return f"连续 {st['unanswered']} 条没回，等他先开口"
        if any(e.get("status") == "sent" and e.get("answered") is None for e in st["log"]):
            # 上一条还在等回复（回复窗口没过）：不连发。否则窗口比最小间隔长时，没回的还没计数就又发了
            return "上一条还在等他回"
        today0 = _midnight(now)
        today_n = sum(1 for e in st["log"] if e.get("status") in ("sent", "draft") and e["ts"] >= today0)
        if today_n >= int(c["max_per_day"]):
            return f"今天已经主动 {today_n} 次了"
        last = max((e["ts"] for e in st["log"] if e.get("status") in ("sent", "draft")), default=0)
        if now - last < float(c["min_gap_hours"]) * 3600:
            return "离上次主动不到 %.0f 小时" % float(c["min_gap_hours"])
        if now - float(st["last_qq"] or 0) < float(c["recent_activity_skip_min"]) * 60:
            return "刚在 QQ 上聊过"
        bal = self.engine.store.meta_get("balance_prev")
        if bal and bal.get("available") is False:
            return "DeepSeek 余额用完了"
        return ""

    def check(self, force: bool = False) -> dict:
        """检查一次要不要主动说话。force=True（设置页"让她现在想一想"）跳过概率和硬约束。"""
        c = self.cfg
        now = self._now()
        with self.engine.lock:
            self.engine._advance()          # 情绪是按需推进的：先推到现在，孤独度/心情才是最新的
        # ---- 阶段 1：决定（持锁）
        with self.lock:
            st = self._load()
            fire_ignored = self._expire(st, now)
            dt_min = (now - st["last_check"]) / 60 if st["last_check"] else float(c["check_every_min"])
            st["last_check"] = now
            res = self._decide(st, now, min(30.0, dt_min), force)
            st["last_reason"] = res.get("reason", "")
            self._save(st)
        if fire_ignored:
            self.engine.system_event("ignored")
        if res["action"] != "think":
            return res
        th: Thought = res.pop("thought")

        # ---- 阶段 2：生成（不持锁：LLM 可能要几十秒，期间主人发来的消息不能被卡住）
        try:
            text = self.compose(th, now)
        except LLMError as ex:
            from .llm import HeuristicLLM
            reason = ("没有配置 DeepSeek API Key，没法自己组织语言（设置页填上 Key 就行）"
                      if isinstance(self.engine.llm, HeuristicLLM) else f"LLM 调用失败：{ex}")
            return self._finish({"action": "error", "reason": reason, "thought": asdict(th)})
        except Exception as ex:
            log.exception("主动聊天生成失败")
            return self._finish({"action": "error", "reason": f"生成失败：{ex}", "thought": asdict(th)})

        # ---- 阶段 3：落地（持锁）
        with self.lock:
            st = self._load()
            if th.ref:
                st["done_refs"].append(th.ref)
            entry = {"id": uuid.uuid4().hex[:8], "ts": now, "kind": th.kind, "ref": th.ref,
                     "score": round(th.score, 3), "text": text or "", "answered": None}
            if not text:
                entry["status"] = "skipped"
                st["log"].append(entry)
                st["last_reason"] = "她想了想，觉得现在不说比较好"
                self._save(st)
                return {"action": "skip", "reason": st["last_reason"], "thought": asdict(th)}
            ev = self.owner_event(now, th.reason)
            if c["dry_run"]:
                entry["status"] = "draft"
                entry["qid"] = self.queue.add(ev, text, self.engine.emo.describe())
                st["log"].append(entry)
                st["last_reason"] = "已生成草稿（试运行模式，没有真的发送）"
                self._save(st)
                return {"action": "draft", "reason": st["last_reason"], "text": text, "thought": asdict(th),
                        "qid": entry["qid"]}
        # 真发（不持锁：发送有打字延迟）
        ok = self._send(ev, text)
        with self.lock:
            st = self._load()
            entry["status"] = "sent" if ok else "failed"
            entry["ts"] = self._now()
            st["log"].append(entry)
            st["last_reason"] = "已发送" if ok else "发送失败（QQ 没连上？）"
            self._save(st)
        return {"action": "sent" if ok else "error", "reason": st["last_reason"], "text": text, "thought": asdict(th)}

    def _finish(self, res: dict) -> dict:
        with self.lock:
            st = self._load()
            st["last_reason"] = res.get("reason", "")
            self._save(st)
        return res

    def _decide(self, st: dict, now: float, dt_min: float, force: bool) -> dict:
        c = self.cfg
        if not c["enabled"] and not force:
            return {"action": "off", "reason": "主动聊天没有开启"}
        if not self.owner:
            return {"action": "off", "reason": "还没有填你的 QQ 号"}
        if not force:
            why = self._blockers(st, now)
            if why:
                return {"action": "wait", "reason": why}
            p = self.probability(st, dt_min)
            if self.rng.random() >= p:
                return {"action": "wait", "reason": "这次没想说话（p=%.3f）" % p}
        ths = self.thoughts(st, now)
        if not force and now - float(st["last_local"] or 0) < float(c["recent_activity_skip_min"]) * 60:
            ths = [t for t in ths if t.kind == "care"]        # 他正在 DSH 里忙：只允许关心
        if not ths:
            return {"action": "wait", "reason": "没什么想说的"}
        best = max(ths, key=lambda t: t.score)
        if best.score < float(c["thought_threshold"]) and not force:
            return {"action": "wait", "reason": f"最想说的（{best.kind} {best.score:.2f}）也不太值得说"}
        return {"action": "think", "thought": best, "reason": f"想到了：{best.kind}"}

    def compose(self, th: Thought, now: float) -> str | None:
        eng = self.engine
        ev = self.owner_event(now, th.reason)
        ctx = eng.prepare(ev)
        history = list(ctx["history"])
        if history and history[-1]["role"] == "user" and history[-1]["content"].endswith(th.reason):
            history.pop()                       # prepare() 会把 ev.text 当成"他说的话"补进去，这里不是
        owner_name = (eng.cfg.get("owner") or {}).get("name") or "他"
        system = ctx["system"] + "\n\n" + COMPOSE_RULES.format(owner=owner_name, reason=th.reason)
        msgs = [{"role": "system", "content": system}] + history + [
            {"role": "user", "content": "[系统提示，不是他说的话] 现在由你主动发一条消息给他。"}]
        out = (eng.llm.chat(msgs) or "").strip()
        out = re.sub(rf"^{re.escape(eng.name)}[:：]\s*", "", out).strip().strip('"“”')
        if not out or out.upper().startswith("SKIP"):
            return None
        return out[:300]

    def _send(self, ev: Event, text: str) -> bool:
        if self.sender is None:
            return False
        try:
            ok = bool(self.sender(ev, text))
        except Exception as ex:
            log.warning("主动消息发送失败：%s", ex)
            return False
        if ok:
            self.engine.commit_reply(ev, text)     # 写进私聊分支：他回复时上下文是连贯的
        return ok

    # ------------------------------------------------------------------ 草稿（试运行）
    def draft_action(self, qid: str, action: str) -> dict:
        items = {i["id"]: i for i in self.queue.pending()}
        it = items.get(qid)
        if it is None:
            return {"ok": False, "error": "草稿不存在或已处理"}
        if action == "discard":
            self.queue.update(qid, status="discarded")
            self._mark(qid, "discarded")
            return {"ok": True}
        if action != "send":
            return {"ok": False, "error": "action 只能是 send / discard"}
        if self.sender is None:
            return {"ok": False, "error": "QQ 没有连接，发不出去"}
        ev = Event.from_dict(it["event"])
        ok = self._send(ev, it["draft"])
        if not ok:
            return {"ok": False, "error": "发送失败（QQ 没连上？）"}
        self.queue.update(qid, status="sent", sent_at=self._now())
        self._mark(qid, "sent", ts=self._now())
        return {"ok": True}

    def _mark(self, qid: str, status: str, ts: float | None = None) -> None:
        with self.lock:
            st = self._load()
            for e in st["log"]:
                if e.get("qid") == qid:
                    e["status"] = status
                    if ts is not None:
                        e["ts"] = ts            # 回复窗口从真正发出时开始算
                        e["answered"] = None
            self._save(st)

    # ------------------------------------------------------------------ 状态
    def status(self) -> dict:
        now = self._now()
        c = self.cfg
        with self.lock:
            st = self._load()
        sent = [e for e in st["log"] if e.get("status") == "sent" and now - e["ts"] < 30 * 86400]
        decided = [e for e in sent if e.get("answered") is not None]
        today0 = _midnight(now)
        return {
            "enabled": bool(c["enabled"]), "dry_run": bool(c["dry_run"]), "owner_set": bool(self.owner),
            "can_send": self.sender is not None,
            "today": sum(1 for e in st["log"] if e.get("status") in ("sent", "draft") and e["ts"] >= today0),
            "max_per_day": int(c["max_per_day"]),
            "unanswered": int(st["unanswered"]),
            "reply_rate": (round(sum(1 for e in decided if e["answered"]) / len(decided), 3) if decided else None),
            "sent_30d": len(sent),
            "last_reason": st.get("last_reason", ""),
            "last_check": st.get("last_check") or None,
            "quiet_now": in_quiet(now, c.get("quiet_hours")),
            "recent": [{k: e.get(k) for k in ("ts", "kind", "status", "text", "answered", "score")}
                       for e in st["log"][-10:]][::-1],
            "drafts": [{"id": i["id"], "created": i["created"], "draft": i["draft"], "mood": i.get("mood", "")}
                       for i in self.queue.pending()][::-1],
        }
