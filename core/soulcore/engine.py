"""
engine.py —— 把 情绪 / 关系 / 记忆 / LLM 串起来。

两种用法：
  1) 全托管：engine.handle(event) → 直接返回回复文本（QQ / B站适配器、CLI 用这个）
  2) 钩子式：harness 自己调 LLM，只在前后调用
        before(event)            → 返回要注入的 system prompt、历史、记忆
        after(event, reply)      → 写回记忆
        pre_compact(event, msgs) → 上下文压缩前把即将丢掉的内容抽取落盘
"""
from __future__ import annotations
import json, os, re, threading, time, logging
from concurrent.futures import ThreadPoolExecutor

from .emotion import Emotion, Params, EVENTS, sigmoid, logit
from .events import Event, _safe
from .memory import MemoryStore
from .llm import make_llm, LLMError
from .appraisal import appraise
from .extractor import extract, summarize, turn_importance
from .persona import load_card, build_system, build_context_block

log = logging.getLogger("soulcore")


class Engine:
    def __init__(self, cfg: dict, base_dir: str = ".", llm=None, mock: bool = False, clock=time.time):
        self.cfg = cfg
        self.base_dir = base_dir
        self.clock = clock
        dd = cfg.get("data_dir", "./data")
        self.data_dir = dd if os.path.isabs(dd) else os.path.join(base_dir, dd)
        os.makedirs(self.data_dir, exist_ok=True)
        self.mcfg = cfg.get("memory", {})
        from .embedding import make_embedder
        self.embedder = make_embedder(self.mcfg.get("embedding"), clock)
        self.store = MemoryStore(self.data_dir, self.mcfg, clock=clock, embedder=self.embedder)
        self.llm = llm or make_llm(cfg.get("llm", {}), mock=mock)
        pcfg = cfg.get("persona", {})
        self.name = pcfg.get("name", "小汐")
        self.card = load_card(pcfg, base_dir)
        self.lock = threading.RLock()
        self.bg = ThreadPoolExecutor(max_workers=1, thread_name_prefix="soulcore-bg")
        self._emo_file = os.path.join(self.data_dir, "emotion.json")
        self.emo = self._load_emotion()
        self.prev_label = None

    # ------------------------------------------------------------------ 情绪持久化
    def _now_min(self) -> float:
        return self.clock() / 60.0

    def _load_emotion(self) -> Emotion:
        ecfg = self.cfg.get("emotion", {})
        if os.path.exists(self._emo_file):
            try:
                d = json.load(open(self._emo_file, encoding="utf-8"))
                d["params"] = {**d.get("params", {}), **ecfg}
                e = Emotion.from_dict(d)
                return e
            except Exception as ex:
                log.warning("情绪状态文件损坏，重新初始化: %s", ex)
        return Emotion(Params(**ecfg), t0=self._now_min())

    def _save_emotion(self) -> None:
        tmp = self._emo_file + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.emo.to_dict(), f, ensure_ascii=False)
        os.replace(tmp, self._emo_file)

    def _advance(self) -> None:
        self.emo.advance(self._now_min())

    # ------------------------------------------------------------------ 观察（每条进来的消息都要过一遍）
    def observe(self, ev: Event) -> dict:
        """记录消息、更新关系和情绪。即使不回复（比如群里没 @ 她）也调用，这样她"看见了"。"""
        with self.lock:
            self._advance()
            directed = ev.is_social or ev.scene == "comment"
            rel = self.store.relation(ev.platform, ev.user_id)
            info = {"event": None, "amp": 0.0, "directed": directed}
            if directed:
                ap = appraise(ev.text, ev.is_stranger, self.llm, self.cfg.get("appraisal", {}).get("mode", "heuristic"),
                              name=self.name)
                name = ap.event
                spec = EVENTS[name]
                amp = self.emo.sample_amp(name) * ap.intensity
                if spec.social:  # 越在乎的人，影响越大
                    amp *= 0.6 + 0.8 * rel["affinity"]
                if ev.is_stranger:
                    amp *= 0.6
                last_social = self.emo.last_social
                amp = self.emo.event(name, t=self._now_min(), amp=amp)
                if not ev.is_social:
                    # 评论区路人互动不算"有人陪"（不重置孤独）
                    self.emo.last_social = last_social
                a = sigmoid(logit(rel["affinity"]) + 0.08 * amp)
                self.store.update_relation(ev.platform, ev.user_id, ev.user_name, a)
                info.update(event=name, amp=amp)
            self.store.write(ev.path, "turn", ev.text, turn_importance(ev.text, ev.mentions_self, ev.scene),
                             role="user", speaker=ev.user_name or ev.user_id, user_id=ev.user_id, ts=ev.ts)
            self._save_emotion()
            return info

    # ------------------------------------------------------------------ 组装上下文（输出门）
    def prepare(self, ev: Event) -> dict:
        m = self.mcfg
        with self.lock:
            self._advance()
            recent = self.store.recent_turns(ev.path, m.get("recent_turns", 12))
            recent_ids = {t.id for t in recent}
            # 只有半小时内的上文才算"正在聊的话题"，几天前的旧话混进查询只会带偏
            fresh = [t for t in recent[-3:-1] if self.clock() - t.created < 1800]
            query = ev.text + " " + " ".join(t.content for t in fresh)
            extra = [ev.person_path, "core/self"]
            share = ev.platform == "local" or m.get("share_projects_in_chat", False)
            for proj in (self.store.list_projects() if share else []):
                if proj and proj in ev.text:
                    extra.append(f"project/{proj}")
            mems = [it for it in self.store.search(
                query, ev.path, k=m.get("top_k", 8) + len(recent_ids), extra_paths=extra,
                public=ev.scene == "comment",
                allow_projects=share)
                if it.id not in recent_ids][:m.get("top_k", 8)]
            person = self.store.by_path(ev.person_path, limit=6)
            rel = self.store.relation(ev.platform, ev.user_id)
            mood_text = self.emo.describe(self.prev_label)
            self.prev_label = self.emo.label(self.prev_label)
            now = self.clock()
        system = build_system(self.card, ev, mood_text, rel, mems, person, now)
        group_like = ev.scene in ("group", "comment")
        history = []
        for t in recent:
            if t.role == "assistant":
                history.append({"role": "assistant", "content": t.content})
            else:
                c = f"{t.speaker}: {t.content}" if group_like else t.content
                if history and history[-1]["role"] == "user":
                    history[-1]["content"] += "\n" + c
                else:
                    history.append({"role": "user", "content": c})
        if not history or history[-1]["role"] != "user":
            history.append({"role": "user", "content": ev.text})
        return {"system": system, "history": history, "mood": self.emo.mood, "label": self.prev_label, "_mem_items": mems,
                "mood_text": mood_text, "memories": [{"id": i.id, "path": i.path, "kind": i.kind, "content": i.content,
                                                      "score": round(i.score, 4)} for i in mems]}

    # ------------------------------------------------------------------ 写回
    def commit_reply(self, ev: Event, reply: str) -> None:
        if not reply:
            return
        with self.lock:
            self.store.write(ev.path, "turn", reply, 0.15, role="assistant", speaker=self.name)
        self._maybe_extract(ev)

    def _maybe_extract(self, ev: Event, force: bool = False) -> None:
        every = self.mcfg.get("extract_every", 6)
        key = f"extract_ptr:{ev.path}"
        with self.lock:
            ptr = self.store.meta_get(key, 0)
            n = self.store.db.execute("SELECT COUNT(*) FROM items WHERE path=? AND kind='turn' AND id>?",
                                      (ev.path, ptr)).fetchone()[0]
            if n == 0 or (n < every and not force):
                return
            rows = self.store.db.execute(
                "SELECT * FROM items WHERE path=? AND kind='turn' AND id>? ORDER BY id LIMIT 60",
                (ev.path, ptr)).fetchall()
            from .memory import _item
            turns = [_item(r) for r in rows]
            self.store.meta_set(key, turns[-1].id)
        fut = self.bg.submit(self._extract_job, ev, turns)
        if force:
            fut.result()

    def _extract_job(self, ev: Event, turns) -> int:
        try:
            mems = extract(self.llm, turns, ev.path)
        except Exception as ex:
            log.warning("记忆抽取失败: %s", ex)
            return 0
        n = 0
        personal = ev.scene in ("private", "dm", "project", "session")
        for mm in mems:
            about = mm["about"]
            if mm["project"] or about == "project":
                path = f"project/{_safe(mm['project'] or ev.channel_id)}"
            elif about == "self" or mm["kind"] == "self":
                path = "core/self"
            elif about == "user" and personal:
                path = ev.person_path
            else:
                path = ev.path
            tags = mm["tags"] + mm.get("keywords", []) + ([f"user:{ev.user_id}"] if about == "user" else [])
            with self.lock:
                if self.store.write(path, mm["kind"], mm["content"], mm["importance"], tags=tags,
                                    user_id=ev.user_id if about == "user" else ""):
                    n += 1
        return n

    def flush(self) -> None:
        """等后台抽取任务做完（测试 / 退出前用）"""
        self.bg.submit(lambda: None).result()

    # ------------------------------------------------------------------ 全托管
    def generate(self, ctx: dict) -> str | None:
        msgs = [{"role": "system", "content": ctx["system"]}] + ctx["history"]
        try:
            return self.llm.chat(msgs).strip()
        except LLMError as ex:
            if ex.status == 402:  # DeepSeek 余额不足
                self.system_event("balance_out")
            log.error("LLM 调用失败: %s", ex)
            return None

    def handle(self, ev: Event, reply: bool = True) -> str | None:
        self.observe(ev)
        if not reply:
            self._maybe_extract(ev)
            return None
        ctx = self.prepare(ev)
        out = self.generate(ctx)
        if out:
            out = re.sub(rf"^{re.escape(self.name)}[:：]\s*", "", out)
            self.commit_reply(ev, out)
        return out

    # ------------------------------------------------------------------ 钩子式
    def context_block(self, ev: Event) -> dict:
        """DSH 用：观察这条消息 + 召回记忆，返回一小段要注入的文本"""
        self.observe(ev)
        ctx = self.prepare(ev)
        with self.lock:
            mems = [m for m in ctx["_mem_items"]]
            shown = {m.id for m in mems}
            # 人物档案只常驻最重要的 2 条（比如名字）；其余的靠和当前话题相关才被召回
            person = [p for p in self.store.by_path(ev.person_path, limit=2) if p.id not in shown]
            rel = self.store.relation(ev.platform, ev.user_id)
        text = build_context_block(self.name, ctx["mood_text"], rel, mems, person, self.clock(),
                                   self.mcfg.get("inject_max_chars", 1600))
        return {"text": text, "mood": ctx["mood"], "label": ctx["label"], "memories": ctx["memories"]}

    def remember(self, ev: Event, content: str, kind: str = "fact", importance: float = 0.85) -> int | None:
        """模型通过工具主动记住某事"""
        path = ev.path if kind in ("decision", "todo") or ev.scene == "project" and kind != "preference" else ev.person_path
        with self.lock:
            return self.store.write(path, kind, content, importance, tags=["explicit"])

    def card_text(self) -> str:
        return self.card

    def before(self, ev: Event) -> dict:
        self.observe(ev)
        return self.prepare(ev)

    def after(self, ev: Event, reply: str) -> None:
        self.commit_reply(ev, reply)

    def pre_compact(self, ev: Event, messages: list[dict]) -> int:
        """harness 压缩上下文之前调用：把即将被压掉的消息先抽取成长期记忆"""
        from .memory import Item
        now = self.clock()
        turns = [Item(0, ev.path, "turn", m.get("role", "user"),
                      self.name if m.get("role") == "assistant" else (ev.user_name or "用户"),
                      str(m.get("content", ""))[:4000], 0, 0, now, now, 0, "hot", "", "", None)
                 for m in messages if m.get("role") in ("user", "assistant") and m.get("content")]
        self._maybe_extract(ev, force=True)  # 先把还没抽取的 turn 处理掉
        return self.bg.submit(self._extract_job, ev, turns).result()

    # ------------------------------------------------------------------ 系统事件 / 维护
    def system_event(self, name: str) -> float:
        with self.lock:
            amp = self.emo.event(name, t=self._now_min())
            self._save_emotion()
            return amp

    def check_balance(self) -> str | None:
        bcfg = self.cfg.get("balance", {})
        try:
            b = self.llm.balance()
        except Exception as ex:
            log.info("查询余额失败: %s", ex)
            return None
        if not b:
            return None
        prev = self.store.meta_get("balance_prev")
        self.store.meta_set("balance_prev", b)
        low = bcfg.get("low_threshold", 5.0)
        ev = None
        if not b["available"] or b["total"] <= 0:
            if not prev or prev.get("available", True):
                ev = "balance_out"
        elif b["total"] < low and (not prev or prev["total"] >= low):
            ev = "balance_low"
        elif prev and b["total"] > prev["total"] + bcfg.get("recharge_min", 5.0):
            ev = "recharge"
        if ev:
            self.system_event(ev)
        return ev

    def tick(self) -> dict:
        """定期维护：冷却、合并摘要、查余额。建议每 10~30 分钟调一次。"""
        with self.lock:
            self._advance()
            res = self.store.maintain()
            self._save_emotion()
        merged = 0
        for path, turns in self.store.consolidation_candidates(self.mcfg.get("consolidate_min", 30)):
            s = summarize(self.llm, turns)
            if s:
                with self.lock:
                    self.store.commit_summary(path, s, [t.id for t in turns])
                merged += 1
        res["summaries"] = merged
        try:
            res["embedded"] = self.store.backfill_embeddings()
        except Exception as ex:
            log.info("补向量失败: %s", ex)
        try:
            res["md_sync"] = self.store.sync_markdown()
        except Exception as ex:
            log.warning("Markdown 回写失败: %s", ex)
        last = self.store.meta_get("balance_checked", 0)
        every = self.cfg.get("balance", {}).get("check_every_min", 60) * 60
        if every > 0 and self.clock() - last >= every:
            self.store.meta_set("balance_checked", self.clock())
            res["balance_event"] = self.check_balance()
        return res

    def state(self) -> dict:
        with self.lock:
            self._advance()
            e = self.emo
            return {"name": self.name, "mood": round(e.mood, 4), "target": round(e.target, 4),
                    "label": e.label(self.prev_label), "describe": e.describe(self.prev_label),
                    "loneliness": round(e.L, 3), "baseline": round(sigmoid(e.b), 4),
                    "recent_events": [(n, round(a, 2)) for n, a, _ in e.recent[-8:]],
                    "memory": self.store.stats()}

    def close(self) -> None:
        self.bg.shutdown(wait=True)
        with self.lock:
            self._save_emotion()
        self.store.close()
