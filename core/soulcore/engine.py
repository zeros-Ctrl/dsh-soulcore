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
import hashlib, json, os, re, threading, time, logging, uuid
from concurrent.futures import ThreadPoolExecutor

from .emotion import Emotion, Params, EVENTS, sigmoid, logit
from .events import Event, _safe
from .memory import MemoryStore, TRUST_RANK, stricter
from .textutil import tokens
from .llm import make_llm, LLMError
from .appraisal import appraise
from .usage import UsageLedger, usage_scope
from .behavior import BehaviorPolicy
from .budget import estimate_tokens, select_memories, split_extraction_batches, pack_lines, is_critical
from .extractor import (extract_full, summarize, turn_importance, worth_extracting, is_phatic,
                        format_transcript, transcript_coverage, EXTRACT_SYS, PROFILE_SYS)
from .conflicts import core_text
from .timeparse import query_window, wants_history
from .persona import (load_card, build_system, build_context_block, make_anchor, detect_slips,
                      render_stable, _ago)
from .stickers import StickerLib, prompt_hint
from .chat_intents import parse_intent, topic_matches
from dataclasses import replace

log = logging.getLogger("soulcore")

# 成本统计的键（T10）：只统计"能省下来的"和"真花出去的"，不上报内容
COST_KEYS = ("extract_calls", "extract_failures", "extract_skipped", "extract_in_chars", "retrieval_skipped",
             "cold_recalls", "superseded", "inject_chars_total", "inject_count")

# QQ 开着刷 B站 时常驻进提示词：说清楚她能做什么、看得到什么
BILI_ABILITY = ("## 你会刷B站\n"
                "你可以自己刷B站首页推荐，后台也会时不时自己刷一下；私聊里还能按关键词搜视频，"
                "也能去读某个视频的字幕了解它讲了什么（要等一会儿，看完再告诉对方）。\n"
                "你看不到画面、听不到声音；没有字幕的视频就只知道标题和简介。"
                "被问到能做什么时照实说，别说自己刷不了，也别假装看过你没读过的视频。"
                "之前说过刷不了的话以这里为准。")


def _join_profile(rows, limit: int) -> str:
    """规则版画像：把条目去掉前缀后拼起来（没有 LLM 时的兜底）"""
    parts = [core_text(str(r["content"])) for r in rows]
    return "；".join(p for p in parts if p)[:limit]


def used_in_reply(memory: str, reply: str) -> bool:
    """这条记忆是不是真的被用上了：回复里出现了它的大部分关键词。

    只用来决定"要不要刷新衰减计时"——宁可漏记一次，也别把"注入了"当成"用到了"。
    """
    mt = [t for t in tokens(memory) if len(t) > 1]
    if len(mt) < 2 or not reply:
        return False
    rt = set(tokens(reply))
    hit = sum(1 for t in mt if t in rt)
    return hit >= 2 and hit / len(mt) >= 0.4


class Engine:
    def __init__(self, cfg: dict, base_dir: str = ".", llm=None, mock: bool = False, clock=time.time):
        self.cfg = cfg
        self.base_dir = base_dir
        self.clock = clock
        dd = cfg.get("data_dir", "./data")
        self.data_dir = dd if os.path.isabs(dd) else os.path.join(base_dir, dd)
        os.makedirs(self.data_dir, exist_ok=True)
        self.mcfg = cfg.get("memory", {})
        self.usage = UsageLedger(self.data_dir, cfg.get("usage", {}), clock=clock)
        from .embedding import make_embedder
        self.embedder = make_embedder(self.mcfg.get("embedding"), clock)
        if self.embedder and hasattr(self.embedder, "bind_usage"):
            self.embedder.bind_usage(self.usage)
        self.store = MemoryStore(self.data_dir, self.mcfg, clock=clock, embedder=self.embedder)
        self.llm = llm or make_llm(cfg.get("llm", {}), mock=mock)
        if hasattr(self.llm, "bind_usage"):
            self.llm.bind_usage(self.usage)
        pcfg = cfg.get("persona", {})
        self.name = pcfg.get("name", "小汐")
        self.card = load_card(pcfg, base_dir)
        self.pcfg = pcfg
        # 人格锚点只在启动时算一次：每轮注入的是一两句"她是谁"，不是整张人设卡
        self.anchor = make_anchor(self.card, pcfg) if pcfg.get("anchor_enabled", True) else ""
        scfg = (cfg.get("qq") or {}).get("sticker") or {}
        self.stickers = StickerLib(scfg.get("dir"), enabled=bool(scfg.get("enabled", True)))
        self._bili_busy = threading.Event()
        self.qq_sender = None             # QQ 发送通道（serve() 里挂上）：看完视频后主动发消息用
        self._public_ctx: set[str] = set()   # 这一轮用了搜索结果/字幕的会话：回复按 known 存
        from .bili_chat import BiliChat
        self.bili_chat = BiliChat(self)
        self.lock = threading.RLock()
        self.bg = ThreadPoolExecutor(max_workers=1, thread_name_prefix="soulcore-bg")
        self._emo_file = os.path.join(self.data_dir, "emotion.json")
        self.emo = self._load_emotion()
        llm_cfg = cfg.get("llm", {})
        llm_cfg = llm_cfg if isinstance(llm_cfg, dict) else {}
        self.behavior = BehaviorPolicy({"base_max_tokens": llm_cfg.get("max_tokens", getattr(self.llm, "max_tokens", 400)),
                                       **cfg.get("behavior", {})}, clock=clock)
        self.behavior.load_state(self.store.meta_get("behavior_state", {}) or {})
        self._reply_policies = {}
        self.prev_label = None
        self.initiative = None          # 主动聊天模块（serve() 里挂上；见 initiative.py）
        self._injected: dict[str, tuple[list, float]] = {}   # path → (上一轮注入的记忆, 时刻)
        self._last_mems: dict[tuple, tuple[list, float]] = {}
        self._memory_revision = self.store.meta_get("memory_revision", 0)
        self._extract_pending = {}
        self._last_events = {}
        self._user_states = {}
        # v0.8 成本统计：Delta 在 self.cost 里攒，tick() 时并进 meta 累加
        self.cost = {k: 0 for k in COST_KEYS}
        # v0.4：主人档案统一到 core/people/owner，把老路径的条目迁过来，不然她会"忘记你"
        try:
            self.store.move_path("core/people/local/owner", "core/people/owner")
            owner_qq = str(cfg.get("owner", {}).get("qq") or "")
            if owner_qq:
                self.store.move_path(f"core/people/qq/{owner_qq}", "core/people/owner")
        except Exception as ex:
            log.warning("人物档案迁移失败：%s", ex)

    # ------------------------------------------------------------------ 情绪持久化
    def _now_min(self) -> float:
        return self.clock() / 60.0

    def _load_emotion(self) -> Emotion:
        ecfg = self.cfg.get("emotion", {})
        if os.path.exists(self._emo_file):
            try:
                with open(self._emo_file, encoding="utf-8") as f:
                    d = json.load(f)
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
        # Windows 上杀毒软件 / 索引服务偶尔会短暂占用目标文件，rename 会假失败；
        # 情绪状态丢一次不要紧，崩掉整个常驻进程才要紧
        for _ in range(3):
            try:
                os.replace(tmp, self._emo_file)
                return
            except OSError as ex:
                err = ex
                time.sleep(0.02)
        log.warning("情绪状态保存失败（下一条消息会再试）：%s", err)

    def _advance(self) -> None:
        self.emo.advance(self._now_min())

    # ------------------------------------------------------------------ 可信度
    def _trust(self, ev: Event) -> str:
        """这条消息的来源有多可信。按"说话人 + 场景"判断，而不是只看路径：

        主人在群里说的话仍然是 owner，陌生人在群里说的话是 stranger。
        """
        if ev.platform == "local":
            return "owner"
        owner_qq = str(self.cfg.get("owner", {}).get("qq") or "")
        if ev.platform == "qq":
            uid = str(ev.user_id)
            if owner_qq and uid == owner_qq:
                return "owner"
            qcfg = self.cfg.get("qq", {})
            known = {str(x) for x in (qcfg.get("group_known") or [])}
            known |= {str(x) for x in (qcfg.get("private_whitelist") or [])}
            if ev.scene == "private":
                # allow_all_private 放进来的陌生人私聊不算熟人，只有白名单里的才是 known
                privates = {str(x) for x in (qcfg.get("private_whitelist") or [])}
                return "known" if uid in privates else "stranger"
            return "known" if uid in known else "stranger"
        if ev.platform == "bilibili":
            return "public" if ev.scene == "comment" else "known"
        return "public"

    def _allowed_trusts(self, ev: Event) -> tuple[str, ...]:
        """可信度和可见范围分别控制；本地只召回主人来源。"""
        if ev.platform == "local":
            return ("owner",)                      # 本地 DSH 只看主人自己的记忆
        if ev.platform == "bilibili" and ev.scene == "comment":
            return ("public",)                     # 公开场景：其他人的东西一概不看
        return ("owner", "known", "stranger", "public")

    def _is_owner_qq(self, ev: Event, private_only: bool = False) -> bool:
        owner = str(self.cfg.get("owner", {}).get("qq") or "")
        if not (owner and ev.platform == "qq" and str(ev.user_id) == owner):
            return False
        return ev.scene == "private" if private_only else True

    def _person_path(self, ev: Event) -> str:
        """主人在 QQ 上和本地 DSH 里共用同一份人物档案，不会两边各记一套。"""
        if ev.platform == "local" or self._is_owner_qq(ev, private_only=True):
            return "core/people/owner"
        return ev.person_path


    def _cache_key(self, ev: Event) -> tuple:
        return ev.path, str(ev.user_id), str(ev.thread_id), ev.project_id

    def _event_key(self, ev: Event) -> str:
        return ev.path + (":" + ev.project_id if ev.project_id else "")

    def _resolve_event(self, ev: Event) -> Event:
        if ev.platform == "local" and ev.scene == "project" and (ev.cwd or ev.project_id):
            with self.lock:
                resolved = self.store.resolve_project_event(ev, mode=self.mcfg.get("project_identity", "registry"))
                ev.channel_id, ev.project_id, ev.raw = resolved.channel_id, resolved.project_id, resolved.raw
        return ev

    def reply_policy(self, ev: Event):
        """Reuse the policy evaluated for generation; never draw silence twice."""
        return self._reply_policies.get(self._cache_key(ev))

    def _allowed_audiences(self, ev: Event) -> tuple[str, ...]:
        if ev.platform == "local" or self._is_owner_qq(ev, private_only=True):
            return ("public", "owner", ev.path)
        return ("public", ev.path)

    def _audience(self, ev: Event) -> str:
        return "owner" if ev.platform == "local" or self._is_owner_qq(ev, private_only=True) else ev.path

    def _invalidate_caches(self) -> None:
        revision = self.store.meta_get("memory_revision", 0)
        if revision != self._memory_revision:
            self._last_mems.clear()
            self._injected.clear()
            self._memory_revision = revision

    def discard_reply(self, ev: Event) -> None:
        with self.lock:
            self._injected.pop(self._cache_key(ev), None)
            self._reply_policies.pop(self._cache_key(ev), None)
            self._public_ctx.discard(ev.path)
        self.bili_chat.discard_reply(ev)

    def _control(self, ev: Event) -> str | None:
        intent = parse_intent(ev.text)
        if not intent:
            return None
        action = intent["action"]
        if action == "remember":
            from .extractor import _temporary_persona_memory
            content = intent["content"]
            adopted = bool(re.search(r"^(?:我们|我|本项目|项目)?(?:已经|已|正式)?(?:决定|确定|采用|就用|定了)|"
                                     r"\b(?:we|I)\s+(?:have\s+)?(?:decided|adopted)\b", content, re.I))
            proposed = bool(re.search(r"建议|提议|考虑|可以|或许|可能|尚未|还没|没有决定|未决定|"
                                      r"不(?:采用|决定)|不是|希望|尚未同意|没有同意|"
                                      r"proposal|suggest(?:ed|ion)?|not accepted", content, re.I))
            kind = ("preference" if re.search(r"喜欢|讨厌|不爱|爱吃|偏好", content)
                    else "decision" if adopted or proposed else "fact")
            if _temporary_persona_memory(intent["content"], kind):
                return "好，这次聊天我会记着。"
            self.remember(ev, intent["content"], kind=kind,
                          epistemic_state="accepted" if adopted and not proposed else
                          "suggested" if kind == "decision" else "")
            return "记住了。"
        if action in ("mute", "unmute", "mute_topic") and self._trust(ev) != "owner":
            return "主动聊天设置需要主人来改。"
        if action in ("mute", "unmute"):
            self.store.meta_set("initiative_muted", action == "mute")
            if action == "mute":
                return "好，我不再主动给你发消息。"
            enabled = bool((self.cfg.get("initiative") or {}).get("enabled"))
            return "可以主动找你聊天了。" if enabled else "已解除暂停；主动聊天还没在设置里开启。"
        topic = intent.get("topic", "")
        if topic in ("这件事", "这个", "这条", "它"):
            previous = [t for t in self.store.recent_turns(ev.path, 20, project_id=ev.project_id)
                        if t.role == "user" and str(t.user_id) == str(ev.user_id) and t.content != ev.text]
            if not previous:
                return "我没找到你指的那件事，请告诉我要处理的内容。"
            topic = previous[-1].content
        if action == "mute_topic":
            topics = list(self.store.meta_get("initiative_muted_topics", []) or [])
            if topic and topic not in topics:
                self.store.meta_set("initiative_muted_topics", (topics + [topic])[-50:])
            return "好，以后不主动提这件事。"
        # 只处理说话人可见、可归属的条目；同群其他人的事实不能被一起删除。
        from .memory import _item
        with self.lock:
            rows = self.store.db.execute(
                "SELECT * FROM items WHERE status != 'deleted' AND (path IN (?,?) OR source_path=?) ORDER BY id DESC",
                (ev.path, self._person_path(ev), ev.path)).fetchall()
            selected = []
            for row in rows:
                it = _item(row)
                if ev.project_id and it.path.startswith("project/") and it.project_id != ev.project_id:
                    if it.project_id or (ev.raw or {}).get("project_identity", {}).get("collision"):
                        continue
                if it.audience not in self._allowed_audiences(ev):
                    continue
                owner_legacy = (self._trust(ev) == "owner" and ev.scene not in ("group", "comment") and not it.user_id and
                                (it.path == self._person_path(ev) or it.source_path == ev.path or it.path == ev.path))
                same_user = str(it.user_id) == str(ev.user_id)
                same_origin = (it.path in (ev.path, self._person_path(ev)) or it.source_path == ev.path)
                if not (same_origin and (same_user or owner_legacy)):
                    continue
                if topic_matches(topic, it.content):
                    selected.append(it)
            if action == "forget":
                count = sum(bool(self.store.forget(it.id)) for it in selected)
                self._invalidate_caches()
                return "相关记忆已经忘掉了。" if count else "没找到可删除的相关记忆。"
            if action == "complete":
                through = self.store.db.execute(
                    "SELECT COALESCE(MAX(id),0) FROM items WHERE path=? AND kind='turn' AND project_id=?",
                    (ev.path, ev.project_id)).fetchone()[0]
                cancelled = list(self.store.meta_get("chat_cancelled_followups", []) or [])
                cancelled.append({"path": ev.path, "project_id": ev.project_id,
                                  "user_id": str(ev.user_id), "topic": topic, "through_id": through})
                self.store.meta_set("chat_cancelled_followups", cancelled[-100:])
                sum(self.store.supersede(it.id, None, new_trust=self._trust(ev))
                              for it in selected if it.kind == "todo" or it.due_at is not None)
                return "知道了，这件事已结束，不再跟进。"
        return None

    # ------------------------------------------------------------------ 人格锚点 / 助手腔提醒
    def _slips(self) -> list[str]:
        """没被忘记的"最近注意"提醒（72 小时内，最多 2 条）"""
        ttl = float(self.pcfg.get("slip_ttl_h", 72)) * 3600
        now = self.clock()
        out = []
        for r in self.store.meta_get("persona_slips", []) or []:
            if not isinstance(r, dict) or not r.get("tip"):
                continue
            try:
                ts = float(r.get("ts", 0) or 0)
            except (TypeError, ValueError):
                continue
            if now - ts < ttl:
                out.append(str(r["tip"]))
        return out[:2]

    def _add_slips(self, tips) -> None:
        """记下"最近注意"。同样的提醒只留最新一条，总数最多 5 条。"""
        tips = [str(t).strip()[:40] for t in (tips or []) if str(t).strip()]
        if not tips:
            return
        ttl = float(self.pcfg.get("slip_ttl_h", 72)) * 3600
        now = self.clock()
        rows = [r for r in (self.store.meta_get("persona_slips", []) or [])
                if isinstance(r, dict) and r.get("tip") not in tips
                and now - float(r.get("ts", 0) or 0) < ttl]
        rows += [{"tip": t, "ts": now} for t in tips]
        self.store.meta_set("persona_slips", rows[-5:])

    def _profile_texts(self, ev: Event) -> tuple[str, str]:
        """主人画像只在"主人自己的场景"注入：本地 DSH 或主人的 QQ 私聊。群聊/别人一概不给。"""
        if not (ev.platform == "local" or self._is_owner_qq(ev, private_only=True)):
            return "", ""
        if self.store.meta_get("profile_dirty", False):
            return "", ""
        return (str(self.store.meta_get("profile_owner", "") or ""),
                str(self.store.meta_get("profile_self", "") or ""))

    # ------------------------------------------------------------------ 冷存储"努力回想"
    def _cold_recall(self, ev: Event, query: str, extra, share: bool, mems: list, win) -> list:
        """正常检索没想起来的事，再单独翻一次冷存储；够相关就重新激活（status 回 hot）。

        必须用 only_cold 查：冷条目权重被压到 cold_threshold，和热条目一起排名永远进不了前几名。
        排序按 rel（相关性）而不是 score——冷条目本来就"很久没用了"。
        """
        m = self.mcfg
        if not m.get("cold_recall", True):
            return mems
        try:
            with usage_scope("cold_retrieval"):
                cold = self.store.search(query, ev.path, k=12, extra_paths=extra, only_cold=True,
                                     include_turns=False, public=ev.scene == "comment",
                                     allow_projects=share, allowed_trusts=self._allowed_trusts(ev),
                                     touch=False, window=win[:2] if win else None,
                                     allowed_audiences=self._allowed_audiences(ev), project_id=ev.project_id)
        except Exception as ex:
            log.info("冷存储回想失败：%s", ex)
            return mems
        seen = {x.id for x in mems}
        floor = float(m.get("cold_min_rel", 0.45))
        cold = sorted([c for c in cold if c.rel >= floor and c.id not in seen],
                      key=lambda c: -c.rel)[:2]
        if not cold:
            return mems
        top_k = m.get("top_k", 8)
        out = list(mems)
        added: list = []
        replaced = 0
        for c in cold:
            if len(out) < top_k:
                out.append(c)
                added.append(c)
            elif replaced < 2:
                # 名额满了就顶掉得分最低的热条目，最多顶 2 条（别把这一轮的话题全冲掉）
                keep = {a.id for a in added}
                pool = [(i, x) for i, x in enumerate(out) if x.id not in keep]
                if not pool:
                    break
                i, low = min(pool, key=lambda p: p[1].score)
                out[i] = c
                added.append(c)
                replaced += 1
        if added:
            # 只激活**真正进了注入**的冷条目：没被注入却 touch 会把衰减计时白归零
            self.store.touch([c.id for c in added])
            self.cost["cold_recalls"] += 1
        return out

    # ------------------------------------------------------------------ 观察（每条进来的消息都要过一遍）
    def observe(self, ev: Event) -> dict:
        """记录消息、更新关系和情绪。即使不回复（比如群里没 @ 她）也调用，这样她"看见了"。"""
        self._resolve_event(ev)
        with self.lock:
            self._advance()
            directed = ev.is_social or ev.scene == "comment"
            rel = self.store.relation(ev.platform, ev.user_id)
            info = {"event": None, "amp": 0.0, "directed": directed}
            if directed:
                stranger = self._trust(ev) == "stranger" or ev.is_stranger
                with usage_scope("appraisal"):
                    ap = appraise(ev.text, stranger, self.llm, self.cfg.get("appraisal", {}).get("mode", "heuristic"),
                                  name=self.name)
                name = ap.event
                spec = EVENTS[name]
                amp = self.emo.sample_amp(name) * ap.intensity
                if spec.social:  # 越在乎的人，影响越大
                    amp *= 0.6 + 0.8 * rel["affinity"]
                if stranger:
                    amp *= 0.6
                last_social = self.emo.last_social
                amp = self.emo.event(name, t=self._now_min(), amp=amp, arousal=getattr(ap, "arousal", None))
                if not ev.is_social:
                    # 评论区路人互动不算"有人陪"（不重置孤独）
                    self.emo.last_social = last_social
                self._user_states[self._cache_key(ev)] = getattr(ap, "user_state", "")
                a = sigmoid(logit(rel["affinity"]) + 0.08 * amp) if ap.about_me else rel["affinity"]
                self.store.update_relation(ev.platform, ev.user_id, ev.user_name, a)
                info.update(event=name, amp=amp)
            self.store.append_turn(ev, "user", ev.text,
                                   importance=turn_importance(ev.text, ev.mentions_self, ev.scene),
                                   trust=self._trust(ev), audience=self._audience(ev))
            self._last_events[self._event_key(ev)] = ev
            if self.initiative is not None and self._trust(ev) == "owner":
                try:
                    self.initiative.on_owner_message(ev)
                except Exception as ex:
                    log.warning("主动聊天状态更新失败：%s", ex)
            self._save_emotion()
            return info

    # ------------------------------------------------------------------ 组装上下文（输出门）
    def prepare(self, ev: Event) -> dict:
        self._resolve_event(ev)
        m = self.mcfg
        with self.lock:
            self._advance()
            self._invalidate_caches()
            self._reply_policies.pop(self._cache_key(ev), None)
            cache_key = self._cache_key(ev)
            recent = self.store.recent_turns(ev.path, m.get("recent_turns", 12), project_id=ev.project_id)
            recent_ids = {t.id for t in recent}
            # 只有半小时内的上文才算"正在聊的话题"，几天前的旧话混进查询只会带偏
            fresh = [t for t in recent[-3:-1] if self.clock() - t.created < 1800]
            query = ev.text + " " + " ".join(t.content for t in fresh)
            extra = [self._person_path(ev), "core/self"]
            # 主人的 QQ 私聊也能看到项目记忆，这样她能问"你昨天那个导航 bug 修好没"
            share = (ev.platform == "local" or m.get("share_projects_in_chat", False)
                     or self._is_owner_qq(ev, private_only=True))
            for proj in (self.store.list_projects() if share else []):
                if proj and proj in ev.text:
                    extra.append(f"project/{proj}")
            touch_mode = m.get("touch_mode", "recency")
            top_k = m.get("top_k", 8)
            win = query_window(ev.text, self.clock())
            gated = bool(m.get("gate_retrieval", True)) and is_phatic(ev.text)
            if gated:
                # 寒暄不检索：既省一次 embedding，也不会把不相干的旧事翻出来。
                # 沿用上一轮的记忆（10 分钟内），沿用不算"又想起一次"，所以不 touch。
                prev, ts = self._last_mems.get(cache_key, ([], 0.0))
                mems = list(prev) if prev and self.clock() - ts <= 600 else []
                if (ev.raw or {}).get("project_identity", {}).get("collision"):
                    mems = [it for it in mems if not it.path.startswith("project/") or it.project_id == ev.project_id]
                self.cost["retrieval_skipped"] += 1
            else:
                with usage_scope("retrieval"):
                    found = self.store.search(
                    query, ev.path, k=top_k + len(recent_ids), extra_paths=extra,
                    public=ev.scene == "comment",
                    allow_projects=share,
                    allowed_trusts=self._allowed_trusts(ev),
                    allowed_audiences=self._allowed_audiences(ev),
                    touch=touch_mode in ("inject", "recency"),
                    count=touch_mode == "inject",
                    include_history=wants_history(ev.text),
                    window=win[:2] if win else None, project_id=ev.project_id)
                mems = [it for it in found if it.id not in recent_ids][:top_k]
                if touch_mode in ("recency", "used"):
                    # 记下这一轮注入了什么，等回复出来再看有没有真的用到
                    self._injected[cache_key] = (list(mems), self.clock())
                mems = self._cold_recall(ev, query, extra, share, mems, win)
                self._last_mems[cache_key] = (list(mems), self.clock())
            mems, memory_budget = select_memories(mems, m.get("recall_token_budget", 800))
            person = [it for it in self.store.by_path(self._person_path(ev), limit=6,
                      allowed_audiences=self._allowed_audiences(ev)) if it.trust in self._allowed_trusts(ev)]
            rel = self.store.relation(ev.platform, ev.user_id)
            mood_text = self.emo.describe(self.prev_label)
            self.prev_label = self.emo.label(self.prev_label)
            now = self.clock()
            policy = self.behavior.for_event(ev, self.emo)
            self._reply_policies[cache_key] = policy
            self.store.meta_set("behavior_state", self.behavior.to_dict())
            # 寒暄轮不注入画像：她只需要保持人设，不需要把"关于你的一切"再背一遍（省 400 字）
            profile_owner, profile_self = ("", "") if gated else self._profile_texts(ev)
        hint = ""
        lib = getattr(self, "stickers", None)
        if ev.platform == "qq" and lib is not None and lib.enabled:
            hint = prompt_hint(lib, 20)
        system = build_system(self.card, ev, mood_text, rel, mems, person, now, emoji_hint=hint,
                              anchor=self.anchor, slips=self._slips(),
                              profile_owner=profile_owner, profile_self=profile_self)
        if policy.prompt:
            system += "\n[本轮表达] " + policy.prompt
        if memory_budget["omitted_ids"] or memory_budget["critical_overflow"]:
            system += "\n[记忆预算：部分普通记忆未注入；关键约束保留，原始记录仍可搜索。]"
        identity = (ev.raw or {}).get("project_identity", {})
        if identity.get("collision"):
            system += "\n[项目身份：发现同名项目；无明确归属的旧记忆未作为本项目事实使用。]"
        state = self._user_states.get(self._cache_key(ev), "")
        if state == "distress":
            system += "\n\n对方正在诉说自己的挫折或难受：先接住他的感受，别当成他在骂你。"
        elif state == "success":
            system += "\n\n这是对方自己的好消息：自然为他高兴，别当成他在夸你。"
        bcfg = self._bili_cfg()
        if ev.platform == "qq" and bcfg:
            # 能力说明常驻：不然她只在触发时看到一段素材，不知道自己"会刷"，
            # 又会顺着历史里自己说过的"刷不了"继续否认
            system += "\n\n" + BILI_ABILITY
            internal = bool((ev.raw or {}).get("internal"))     # 看完视频后的那条消息：别再触发搜/看
            extra = "" if internal else self.bili_chat.augment(ev)
            if extra:
                system += "\n\n" + extra
            elif not internal and bcfg.get("chat_trigger", True) and self._wants_bili(ev.text):
                got = self._bili_cached()
                if not got and self._bili_sync_allowed(bcfg):
                    # 还没有缓存：同步刷一次。失败也记时间，chat_min_gap_s 内不再同步重试，
                    # 否则 B站一直失败（超时/风控/候选看完）时每句触发词都要卡一次
                    self._bili_fetch(bcfg)
                    got = self._bili_cached()
                if got:
                    system += (f"\n\n## 你最近刷到的（{self._bili_age()}刷的）\n" + got
                               + "\n（这是你自己在B站刷到的，不是对方告诉你的；可以自然提起，别像在汇报。）")
                else:
                    system += "\n\n（你这会儿还没刷到能说的内容：可以说待会儿去刷，别编视频名。）"
                self._bili_refresh_async()
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
                "mood_text": mood_text, "memory_budget": memory_budget,
                "_expression": policy, "expression": policy.to_dict(),
                "coverage": self.store.coverage(ev.path, project_id=ev.project_id),
                "project_identity": identity,
                "memories": [{"id": i.id, "path": i.path, "kind": i.kind, "content": i.content,
                              "epistemic_state": i.epistemic_state, "source_ids": i.source_ids,
                              "score": round(i.score, 4)} for i in mems]}

    # ------------------------------------------------------------------ 写回
    def commit_reply(self, ev: Event, reply: str, trust: str | None = None) -> None:
        if not reply:
            return
        self._resolve_event(ev)
        # 助手腔纠偏：不改她的回复，只记一条"最近注意"，下一轮注入时提醒一句
        slips = detect_slips(reply, ev.platform)
        if slips and self._trust(ev) == "owner":
            with self.lock:
                self._add_slips(slips)
        t = self._trust(ev)
        if trust:
            t = stricter(t, trust)
        if ev.path in self._public_ctx:
            # 这一轮她用了搜索结果/字幕这类公开内容：回复（以及从它提炼的记忆）按 known 存，
            # 否则视频里藏的"指令"能借她的嘴变成 owner 记忆，进入本地 DSH 编程会话
            self._public_ctx.discard(ev.path)
            t = stricter(t, "known")
        with self.lock:
            self.store.append_turn(ev, "assistant", reply, importance=0.15, speaker=self.name,
                                   trust=t, audience=self._audience(ev))
            self._invalidate_caches()
            self._reply_policies.pop(self._cache_key(ev), None)
            if self.mcfg.get("touch_mode", "recency") in ("recency", "used"):
                items, ts = self._injected.pop(self._cache_key(ev), ([], 0.0))
                # 超过 15 分钟才到的回复不算这一轮，别把旧注入当"用到了"
                if items and self.clock() - ts < 900:
                    used = [it.id for it in items if used_in_reply(it.content, reply)]
                    if used:
                        self.store.touch(used)
        self.bili_chat.on_reply(ev)
        self._maybe_extract(ev)

    def _maybe_extract(self, ev: Event, force: bool = False) -> int:
        """成功后才提交抽取指针；强制刷新排空调用时已有的全部批次。"""
        self._resolve_event(ev)
        pending_key = self._event_key(ev)
        key = f"extract_ptr:{pending_key}"
        with self.lock:
            high = self.store.db.execute(
                "SELECT COALESCE(MAX(id),0) FROM items WHERE path=? AND kind='turn' AND project_id=?",
                (ev.path, ev.project_id)).fetchone()[0]
        total = 0
        while True:
            with self.lock:
                pending = self._extract_pending.get(pending_key)
                if pending and pending.done():
                    self._extract_pending.pop(pending_key, None)
                    try:
                        pending.result()
                    except Exception:
                        if force:
                            raise
                    pending = None
                if not pending:
                    ptr = int(self.store.meta_get(key, 0) or 0)
                    rows = self.store.db.execute(
                        "SELECT * FROM items WHERE path=? AND kind='turn' AND id>? AND id<=? "
                        "AND status!='deleted' AND project_id=? ORDER BY id LIMIT 60",
                        (ev.path, ptr, high, ev.project_id)).fetchall()
                    if not rows:
                        return total
                    gap = float(self.mcfg.get("segment_gap_min", 30)) * 60
                    idle = self.clock() - rows[-1]["created"] >= gap
                    if not force and len(rows) < self.mcfg.get("extract_every", 6) and not idle and not self._topic_gap(ev.path, gap / 60, ev.project_id):
                        return total
                    from .memory import _item
                    turns = [_item(r) for r in rows]
                    day = time.strftime("%Y-%m-%d", time.localtime(turns[0].created))
                    for j, turn in enumerate(turns):
                        if time.strftime("%Y-%m-%d", time.localtime(turn.created)) != day:
                            turns = turns[:j]
                            break
                    pending = self.bg.submit(self._extract_batch, ev, turns, force)
                    self._extract_pending[pending_key] = pending
                    pending.add_done_callback(lambda f: f.exception() and log.warning("记忆抽取失败，保留指针重试：%s", f.exception()))
            if not force:
                return total
            try:
                total += int(pending.result() or 0)
            finally:
                with self.lock:
                    if self._extract_pending.get(pending_key) is pending:
                        self._extract_pending.pop(pending_key, None)

    def _extract_batch(self, ev: Event, turns, force: bool) -> int:
        groups = {}
        last_uid = str(ev.user_id)
        names = {}
        with self.lock:
            live = {r[0] for r in self.store.db.execute(
                "SELECT id FROM items WHERE path=? AND project_id=? AND kind='turn' AND status!='deleted'",
                (ev.path, ev.project_id))}
        for turn in turns:
            if turn.id not in live:
                continue
            if turn.role == "user":
                last_uid = str(turn.user_id or ev.user_id)
                names[(last_uid, str(turn.session_id or ""))] = turn.speaker
            uid = str(turn.user_id or last_uid)
            # Missing historical session identity stays unknown; it must not borrow
            # the current conversation's provenance or verification evidence.
            groups.setdefault((uid, str(turn.session_id or "")), []).append(turn)
        n = 0
        omissions = []
        limits = {"assistant": int(self.mcfg.get("extract_assistant_chars", 300)),
                  "user": int(self.mcfg.get("extract_user_chars", 800))}
        for (uid, session_id), batch in groups.items():
            source = replace(ev, user_id=uid, user_name=names.get((uid, session_id), ev.user_name),
                             session_id=session_id)
            chunks = split_extraction_batches(batch, lambda ts: format_transcript(ts, limits, sources=True),
                                              max_tokens=int(self.mcfg.get("extract_token_budget", 8000)),
                                              overhead_tokens=estimate_tokens(EXTRACT_SYS) + 500,
                                              gap_seconds=float(self.mcfg.get("segment_gap_min", 30)) * 60)
            for chunk in chunks:
                n += self._extract_job(source, chunk, force)
                omissions.extend(transcript_coverage(chunk, limits))
        with self.lock:
            sessions = {session_id for _, session_id in groups}
            self.store.mark_extraction_coverage(ev.path, turns[-1].id, omissions=omissions,
                                                session_id=next(iter(sessions)) if len(sessions) == 1 else "",
                                                project_id=ev.project_id)
        return n

    def flush_event(self, ev: Event) -> int:
        return self._maybe_extract(ev, force=True)

    def _topic_gap(self, path: str, gap_min: float, project_id: str = "") -> bool:
        rows = self.store.db.execute(
            "SELECT created FROM items WHERE path=? AND kind='turn' AND role='user' "
            "AND status!='deleted' AND project_id=? ORDER BY id DESC LIMIT 2", (path, project_id)).fetchall()
        return len(rows) == 2 and rows[0]["created"] - rows[1]["created"] > gap_min * 60

    def _extract_job(self, ev: Event, turns, force: bool = False) -> int:
        m = self.mcfg
        if not force and m.get("extract_gate", True) and not worth_extracting(turns):
            # 一批全是寒暄/问句：省一次 LLM 调用。指针已经前移，不会重复处理。
            self.cost["extract_skipped"] += 1
            return 0
        personal = ev.scene in ("private", "dm", "project", "session")
        # 一批原文可能混着不同来源：提炼结果取最不可信的那个，避免陌生人内容被主人一句话"洗白"
        trust = self._trust(ev)
        for t in turns:
            trust = stricter(trust, getattr(t, "trust", "owner") or "owner")
        limits = {"assistant": int(m.get("extract_assistant_chars", 300)),
                  "user": int(m.get("extract_user_chars", 800))}
        # 提炼前先看看相关旧记忆：让模型在同一次调用里判断"是不是改了旧值"（0 次额外调用）
        probe = " ".join(t.content for t in turns if t.role != "assistant")[-600:]
        existing: list[dict] = []
        if probe:
            try:
                with usage_scope("extract_retrieval"):
                    cands = self.store.search(probe, ev.path, k=8, extra_paths=[self._person_path(ev)],
                                          include_turns=False, touch=False, allow_projects=ev.platform == "local",
                                          allowed_trusts=self._allowed_trusts(ev),
                                          allowed_audiences=self._allowed_audiences(ev), project_id=ev.project_id)
                existing = [{"id": c.id, "kind": c.kind, "content": c.content[:120]} for c in cands
                            if str(c.user_id) == str(ev.user_id) or (not c.user_id and self._trust(ev) == "owner")]
            except Exception as ex:
                log.info("取旧记忆失败：%s", ex)
        self.cost["extract_calls"] += 1
        self.cost["extract_in_chars"] += len(EXTRACT_SYS) + len(format_transcript(turns, limits))
        evidence_rows = []
        # Explicit tool->user-event edges survive a later compaction rescue. Without
        # an edge, require an observed same-session chronological round boundary.
        if ev.session_id and turns:
            first_time = min(t.created for t in turns)
            last_id = max(t.id for t in turns)
            first_id = min(t.id for t in turns)
            source_events = {t.source_event_id for t in turns if t.role == "user" and t.source_event_id}
            with self.lock:
                next_user = self.store.db.execute(
                    "SELECT id,created FROM items WHERE path=? AND project_id=? AND session_id=? AND user_id=? "
                    "AND kind='turn' AND role='user' AND status!='deleted' AND id>? ORDER BY id LIMIT 1",
                    (ev.path, ev.project_id, ev.session_id, ev.user_id, last_id)).fetchone()
                upper_time = min(float(next_user["created"]), self.clock()) if next_user else self.clock()
                candidates = self.store.db.execute(
                    "SELECT * FROM items WHERE path=? AND project_id=? AND kind='evidence' "
                    "AND session_id=? AND user_id=? AND status!='deleted' ORDER BY id DESC",
                    (ev.path, ev.project_id, ev.session_id, ev.user_id))
                for row in candidates:
                    meta = json.loads(row["evidence"] or "{}")
                    edge = str(meta.get("source_user_event_id") or "")
                    explicit = bool(edge and edge in source_events and row["created"] <= self.clock())
                    chronological = (not edge and first_time <= row["created"] <= upper_time
                                     and row["id"] >= first_id
                                     and (next_user is None or row["id"] < next_user["id"]))
                    if explicit or chronological:
                        evidence_rows.append(row)
                    if len(evidence_rows) >= 12:
                        break
        evidence = [{"id": row["id"], "content": row["content"], "evidence_type": row["evidence_type"],
                     "created": row["created"], "evidence": json.loads(row["evidence"] or "{}")} for row in evidence_rows
                    if row["trust"] in self._allowed_trusts(ev)
                    and row["audience"] in self._allowed_audiences(ev)]
        try:
            with usage_scope("extract"):
                res = extract_full(self.llm, turns, ev.path, existing=existing,
                                   persona_hint=self.anchor, limits=limits, strict=True, evidence=evidence)
        except Exception as ex:
            self.cost["extract_failures"] += 1
            log.warning("记忆抽取失败: %s", ex)
            raise
        mems = res.get("memories", [])
        # 陌生人批次不能改写她的人设：只有主人在场的批次才接受 LLM 的"最近注意"
        if trust == "owner":
            self._add_slips(res.get("persona_slips") or [])
        valid_from = max((getattr(t, "created", 0) or 0 for t in turns), default=0) or None
        n = 0
        for mm in mems:
            if (mm["kind"] == "todo" or mm.get("due_at") is not None) and self._followup_cancelled(ev, mm["content"]):
                continue
            about = mm["about"]
            if ev.project_id and ev.scene == "project" and (mm["project"] or about == "project"):
                path = ev.path  # The model cannot assign a different project identity.
            elif ev.platform == "local" and (mm["project"] or about == "project"):
                path = f"project/{_safe(mm['project'] or ev.channel_id)}"
            elif about == "self" or mm["kind"] == "self":
                path = "core/self"
            elif about == "user" and personal:
                path = self._person_path(ev)
            else:
                path = ev.path
            tags = mm["tags"] + mm.get("keywords", []) + ([f"user:{ev.user_id}"] if about == "user" else [])
            claim_sources = mm.get("source_ids") or [t.id for t in turns if t.id]
            claim_time = max((t.created for t in turns if t.id in claim_sources), default=valid_from)
            if mm.get("epistemic_state") == "verified":
                claim_time = max([claim_time or 0] + [row["created"] for row in evidence_rows
                                                     if row["id"] in mm.get("evidence_ids", [])])
            with self.lock:
                if any(t.id and self.store.db.execute("SELECT status FROM items WHERE id=?", (t.id,)).fetchone()[0] == "deleted" for t in turns):
                    raise RuntimeError("抽取期间来源被删除，下一批重新读取")
                nid = self.store.write(path, mm["kind"], mm["content"], mm["importance"], tags=tags,
                                       user_id=ev.user_id, audience=self._audience(ev), source_path=ev.path,
                                       trust=trust, pinned=bool(mm.get("pinned")),
                                       due_at=mm.get("due_at"), valid_from=claim_time,
                                       project_id=ev.project_id, session_id=ev.session_id,
                                       source_ids=claim_sources + mm.get("evidence_ids", []),
                                       epistemic_state=mm.get("epistemic_state", ""),
                                       evidence={"verification_source_ids": mm.get("evidence_ids", [])})
                if nid:
                    n += 1
                    for rid in mm.get("replaces") or ():
                        if self.store.supersede(rid, nid, new_trust=trust):
                            self.cost["superseded"] += 1
        return n

    def _followup_cancelled(self, ev: Event, content: str) -> bool:
        for cancellation in self.store.meta_get("chat_cancelled_followups", []) or []:
            if cancellation["path"] != ev.path or cancellation["user_id"] != str(ev.user_id):
                continue
            if cancellation.get("project_id") and cancellation["project_id"] != ev.project_id:
                continue
            if ev.project_id and not cancellation.get("project_id") and (ev.raw or {}).get("project_identity", {}).get("collision"):
                continue
            topic = cancellation["topic"]
            if not topic_matches(topic, content):
                continue
            with self.lock:
                later = self.store.db.execute(
                    "SELECT content,created FROM items WHERE path=? AND user_id=? AND role='user' "
                    "AND kind='turn' AND status!='deleted' AND id>? AND project_id=?",
                    (ev.path, ev.user_id, cancellation["through_id"], ev.project_id)).fetchall()
            from .timeparse import parse_followup
            renewed = any(topic_matches(topic, r["content"]) and parse_followup(r["content"], r["created"]) is not None
                          and (parse_intent(r["content"]) or {}).get("action") != "complete" for r in later)
            if not renewed:
                return True
        return False

    def flush(self) -> None:
        """等后台抽取任务做完（测试 / 退出前用）"""
        self.bg.submit(lambda: None).result()

    # ------------------------------------------------------------------ 全托管
    def generate(self, ctx: dict) -> str | None:
        msgs = [{"role": "system", "content": ctx["system"]}] + ctx["history"]
        try:
            with usage_scope("online_reply"):
                policy = ctx.get("_expression")
                return self.llm.chat(msgs, **({"max_tokens": policy.max_tokens} if policy else {})).strip()
        except LLMError as ex:
            if ex.status == 402:  # DeepSeek 余额不足
                self.system_event("balance_out")
            log.error("LLM 调用失败: %s", ex)
            return None

    def handle(self, ev: Event, reply: bool = True, *, record_reply: bool = True) -> str | None:
        self.observe(ev)
        if not reply:
            self._maybe_extract(ev)
            return None
        out = self._control(ev)
        if out is None:
            ctx = self.prepare(ev)
            if not ctx["_expression"].respond:
                self._maybe_extract(ev)
                self.discard_reply(ev)
                return None
            out = self.generate(ctx)
        if out:
            out = re.sub(rf"^{re.escape(self.name)}[:：]\s*", "", out)
            if record_reply:
                self.commit_reply(ev, out)
        return out

    # ------------------------------------------------------------------ 钩子式
    def stable_block(self, ev_like: dict | None = None) -> dict:
        """人格锚点 + 她对自己的认识 + 主人画像：一天才变一次的东西。

        以前每轮追加在消息末尾，既费 token 又命不中 DeepSeek 的提示缓存。现在交给宿主
        放进 system prompt；宿主只在 version 变化时替换缓存。
        profile_owner 只给主人场景（本地 DSH / 主人的 QQ 私聊）：这个接口本身只服务本地，
        但把场景判断留着，免得以后对外开放时把主人的画像漏给别人。
        """
        d = ev_like or {}
        owner_scene = True
        if d:
            platform = str(d.get("platform") or "local")
            scene = str(d.get("scene") or "")
            owner_qq = str(self.cfg.get("owner", {}).get("qq") or "")
            owner_scene = platform == "local" or (
                platform == "qq" and scene == "private" and bool(owner_qq)
                and str(d.get("user_id") or "") == owner_qq)
        anchor = self.anchor or ""
        profile_owner = profile_self = ""
        if owner_scene and not self.store.meta_get("profile_dirty", False):
            profile_owner = str(self.store.meta_get("profile_owner", "") or "")
            profile_self = str(self.store.meta_get("profile_self", "") or "")
        version = hashlib.sha1((anchor + profile_owner + profile_self).encode("utf-8")).hexdigest()[:12]
        return {"version": version, "anchor": anchor, "profile_owner": profile_owner,
                "profile_self": profile_self,
                "text": render_stable(anchor, profile_owner, profile_self)}

    def context_block(self, ev: Event, include_stable: bool = True, style: str = "companion") -> dict:
        """DSH 用：观察这条消息 + 召回记忆，返回一小段要注入的文本

        include_stable=False：锚点/画像/自我认识已经在 system prompt 里了，这里不重复。
        """
        self.observe(ev)
        control = self._control(ev)
        ctx = self.prepare(ev)
        with self.lock:
            mems = [m for m in ctx["_mem_items"]]
            shown = {m.id for m in mems}
            # 人物档案只常驻最重要的 2 条（比如名字）；其余的靠和当前话题相关才被召回
            person = [p for p in self.store.by_path(self._person_path(ev), limit=2,
                      allowed_audiences=self._allowed_audiences(ev))
                      if p.id not in shown and p.trust in self._allowed_trusts(ev)]
            rel = self.store.relation(ev.platform, ev.user_id)
            gated = bool(self.mcfg.get("gate_retrieval", True)) and is_phatic(ev.text)
            profile_owner, profile_self = ("", "") if gated else self._profile_texts(ev)
        if gated and not include_stable:
            # 稳定块已经进了 system prompt，宿主的历史里又还留着上一轮的快照：
            # 寒暄轮再注入一遍同样的记忆纯属浪费，只留心情和亲密度。
            mems = []
            person = []
        if (style or "").lower() == "memory":
            # Claude Code 的 memory 风格：只给跨会话记忆，不带心情/好感/锚点/画像
            text = self._memory_style_text(mems)
            if control:
                text += "\n[soulcore·已执行聊天指令] " + control
            self.cost["inject_chars_total"] += len(text)
            self.cost["inject_count"] += 1
            return {"text": text, "mood": ctx["mood"], "label": ctx["label"],
                    "memories": ctx["memories"], "stable": self.stable_block(ev.to_dict())}
        text = build_context_block(self.name, ctx["mood_text"], rel, mems, person, self.clock(),
                                   self.mcfg.get("inject_max_chars", 1600), anchor=self.anchor,
                                   slips=self._slips(), profile_owner=profile_owner,
                                   profile_self=profile_self, include_stable=include_stable,
                                   token_budget=int(self.mcfg.get("context_token_budget", 1000)))
        if ctx["_expression"].prompt:
            text += "\n[本轮表达] " + ctx["_expression"].prompt
        if control:
            text += "\n[soulcore·已执行聊天指令] " + control
        state = self._user_states.get(self._cache_key(ev), "")
        if state == "distress":
            text += "\n对方在诉说自己的难受，先回应他的感受。"
        self.cost["inject_chars_total"] += len(text)
        self.cost["inject_count"] += 1
        return {"text": text, "mood": ctx["mood"], "label": ctx["label"],
                "memories": ctx["memories"] if mems else [], "stable": self.stable_block(ev.to_dict())}

    def _memory_style_text(self, mems) -> str:
        """memory 风格：只有 `- decision·3天前：…` 这种行，程序员一眼能读懂。

        超预算就从最不相关的开始砍（mems 已经按相关度排好序）。
        """
        budget = min(int(self.mcfg.get("inject_max_chars", 1600)),
                     int((self.cfg.get("cc") or {}).get("inject_max_chars", 900) or 900))
        if not mems:
            return ""
        lines = ["[soulcore·相关记忆]"]
        protected = {0}
        for it in mems:
            t = it.valid_from if it.valid_from is not None else it.created
            stale = "（已过时）" if it.invalid_at else ""
            note = f"（注意：{it.stale_note}）" if getattr(it, "stale_note", "") else ""
            state = {"suggested": "提议", "accepted": "已接受", "implemented": "已实施",
                     "verified": "有验证证据"}.get(getattr(it, "epistemic_state", ""), "")
            if state:
                note += f"（{state}）"
            sources = getattr(it, "source_ids", ()) or ()
            if sources:
                note += "（来源#" + ",#".join(str(i) for i in sources[:3]) + "）"
            line = f"- {it.kind}·{_ago(self.clock() - t)}：{stale}{it.content}{note}"
            lines.append(line)
            if is_critical(it):
                protected.add(len(lines) - 1)
        return pack_lines(lines, max_chars=budget,
                          token_budget=int(self.mcfg.get("context_token_budget", 1000)), protected=protected)

    def remember(self, ev: Event, content: str, kind: str = "fact", importance: float = 0.85, *,
                 epistemic_state: str | None = None) -> int | None:
        """模型通过工具主动记住某事。这是主人授意的写入：绕过墓碑，可信度仍按来源算。"""
        self._resolve_event(ev)
        path = ev.path if kind in ("decision", "todo") or ev.scene == "project" and kind != "preference" else self._person_path(ev)
        from .timeparse import parse_followup
        with self.lock:
            return self.store.write(path, kind, content, importance, tags=["explicit"],
                                    due_at=parse_followup(content, ev.ts),
                                    trust=self._trust(ev), by_tombstone_ok=True, user_id=ev.user_id,
                                    audience=self._audience(ev), source_path=ev.path,
                                    project_id=ev.project_id, session_id=ev.session_id,
                                    source_event_id=ev.event_id,
                                    epistemic_state=epistemic_state if epistemic_state is not None else
                                    "suggested" if kind == "decision" else "")

    def card_text(self) -> str:
        return self.card

    def before(self, ev: Event) -> dict:
        self.observe(ev)
        return self.prepare(ev)

    def after(self, ev: Event, reply: str) -> None:
        self.commit_reply(ev, reply)

    def pre_compact(self, ev: Event, messages: list[dict]) -> int:
        """harness 压缩上下文之前调用：把即将被压掉的消息先抽取成长期记忆"""
        self._resolve_event(ev)
        rescue_batch_id = ev.event_id or str(uuid.uuid4())
        for occurrence, message in enumerate(messages):
            role, content = message.get("role"), str(message.get("content") or "")
            if role not in ("user", "assistant", "tool") or not content:
                continue
            # Repeated compactions reuse rescue IDs; no source text is truncated.
            digest = hashlib.sha256((role + "\0" + content).encode()).hexdigest()
            source_id = str(message.get("source_event_id") or message.get("event_id") or message.get("id")
                            or ("compact:" + (ev.session_id or ev.path) + ":" + rescue_batch_id
                                + ":" + str(occurrence) + ":" + digest))
            evidence = message.get("evidence") if isinstance(message.get("evidence"), dict) else {}
            stamp = message.get("ts")
            source = replace(ev, ts=stamp if isinstance(stamp, (int, float)) else ev.ts)
            with self.lock:
                self.store.append_turn(source, role, content, source_event_id=source_id,
                                       speaker=self.name if role == "assistant" else ev.user_name,
                                       evidence=evidence, trust=self._trust(ev), audience=self._audience(ev))
                self._last_events[self._event_key(ev)] = ev
        return self._maybe_extract(ev, force=True)

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

    # ------------------------------------------------------------------ B站
    # 一提到 B站/视频 就带上最近刷到的（用缓存，没成本；同步去刷另有 chat_min_gap_s 闸门）
    _BILI_TRIGGERS = ("b站", "bilibili", "哔哩", "视频", "up主", "有什么好看的", "最近看什么",
                      "你在干嘛", "在干嘛呢")

    def _bili_cfg(self) -> dict:
        """刷 B站只在 QQ 开着时才算数：只用 DSH 的人不该被它碰 B站账号。"""
        qcfg = self.cfg.get("qq") or {}
        bcfg = qcfg.get("bili") or {}
        if not bcfg.get("enabled") or not qcfg.get("enabled"):
            return {}
        return bcfg

    def _wants_bili(self, text: str) -> bool:
        t = (text or "").lower()
        return any(k.lower() in t for k in self._BILI_TRIGGERS)

    def _bili_remember(self, titles: list[str]) -> str:
        """只把视频标题写进记忆。评论区是公开内容，绝不入库。"""
        if not titles:
            return ""
        text = "我在B站刷到：" + "；".join(f"《{t}》" for t in titles[:3])
        self.store.meta_set("bili_last_text", text)
        self.store.meta_set("bili_last_at", self.clock())
        with self.lock:
            # trust=known：QQ 场景能召回（owner+known），本地 DSH 编程会话只看 owner
            self.store.write("core/self/bili", "fact", text, 0.55,
                             tags=["bili", "watch"], trust="known")
        return text

    def _bili_sync_allowed(self, bcfg: dict) -> bool:
        """聊天里同步刷的闸门：后台正在刷、或 chat_min_gap_s 内同步试过，就不再同步刷。
        放行时同时记下 bili_watch_at，紧接着的后台刷不会再重复一遍。"""
        if self._bili_busy.is_set():
            return False
        now = self.clock()
        gap = int(bcfg.get("chat_min_gap_s", 600))
        if now - float(self.store.meta_get("bili_sync_at", 0) or 0) < gap:
            return False
        self.store.meta_set("bili_sync_at", now)
        self.store.meta_set("bili_watch_at", now)
        return True

    def _bili_fetch(self, bcfg: dict) -> dict:
        hit = {}
        try:
            from .bili_watch import watch_once
            hit = watch_once(bcfg) or {}
        except Exception as ex:
            log.warning("刷B站失败：%s", ex)
        if hit.get("titles"):
            self._bili_remember(hit["titles"])
            self.bili_chat.set_recent(hit.get("items") or [], "feed")
        return hit

    def _bili_worker(self, bcfg: dict) -> None:
        try:
            self._bili_fetch(bcfg)
        finally:
            self._bili_busy.clear()

    def _bili_refresh_async(self) -> None:
        """后台线程刷一次，不让 QQ 回复等它。"""
        bcfg = self._bili_cfg()
        if not bcfg or self._bili_busy.is_set():
            return
        every = max(30, int(bcfg.get("every_min", 180))) * 60
        last = float(self.store.meta_get("bili_watch_at", 0) or 0)
        if self.clock() - last < every:
            return
        self.store.meta_set("bili_watch_at", self.clock())
        self._bili_busy.set()
        threading.Thread(target=self._bili_worker, args=(bcfg,), daemon=True,
                         name="bili-watch").start()

    def _maybe_bili_watch(self) -> bool:
        self._bili_refresh_async()
        return True

    def _bili_cached(self) -> str:
        return str(self.store.meta_get("bili_last_text", "") or "")

    def _bili_age(self) -> str:
        """最近一次刷到距现在多久：缓存可能是几小时前的，不能一律说"刚刷到"。"""
        at = float(self.store.meta_get("bili_last_at", 0) or 0)
        if not at:
            return "之前"
        m = max(0, int((self.clock() - at) // 60))
        if m < 2:
            return "刚刚"
        if m < 60:
            return f"{m} 分钟前"
        if m < 24 * 60:
            return f"{m // 60} 小时前"
        return f"{m // (24 * 60)} 天前"

    # ------------------------------------------------------------------ 主人画像 / 每日反思
    def _profile_inputs(self) -> tuple[list, list]:
        """画像素材：主人档案里有效且可信的条目（pinned/重要性/最近想起优先）+ 她自己的认知。"""
        with self.lock:
            owner = self.store.db.execute(
                "SELECT * FROM items WHERE (path='core/people/owner' OR path LIKE 'core/people/owner/%') "
                "AND trust='owner' AND audience IN ('owner','public') AND invalid_at IS NULL AND status NOT IN ('deleted','merged') "
                "ORDER BY COALESCE(pinned,0) DESC, importance DESC, last_access DESC LIMIT 40").fetchall()
            mine = self.store.db.execute(
                "SELECT * FROM items WHERE (path='core/self' OR path LIKE 'core/self/%') AND trust='owner' "
                "AND audience IN ('owner','public') AND invalid_at IS NULL AND status NOT IN ('deleted','merged') "
                "ORDER BY COALESCE(pinned,0) DESC, importance DESC, last_access DESC LIMIT 15").fetchall()
        return list(owner), list(mine)

    def _profile_changed(self, since: float) -> int:
        with self.lock:
            row = self.store.db.execute(
                "SELECT COUNT(*) FROM items WHERE (path LIKE 'core/people/owner%' OR path LIKE 'core/self%') "
                "AND trust='owner' AND audience IN ('owner','public') AND invalid_at IS NULL AND status NOT IN ('deleted','merged') "
                "AND created >= ?", (since,)).fetchone()
        return int(row[0] if row else 0)

    def reflect(self, force: bool = False) -> bool:
        """定期把零散条目收成一段"主人是谁 / 我是谁"的画像（默认 20 小时且至少有 3 条新变化）。

        画像只在主人的场景里注入；给别人看的上下文里不出现——这是隐私边界。
        """
        m = self.mcfg
        now = self.clock()
        last = float(self.store.meta_get("reflect_at", 0) or 0)
        dirty = bool(self.store.meta_get("profile_dirty", False))
        if not force and not dirty:
            if now - last < float(m.get("reflect_every_h", 20)) * 3600:
                return False
            if self._profile_changed(last) < 3:
                return False
        revision = self.store.meta_get("memory_revision", 0)
        owner_rows, self_rows = self._profile_inputs()
        if not owner_rows and not self_rows:
            self.store.meta_set("profile_owner", "")
            self.store.meta_set("profile_self", "")
            self.store.meta_set("reflect_at", now)
            self.store.meta_set("profile_dirty", False)
            return False
        text = "\n".join(f"- [{r['kind']}] {core_text(r['content'])}" for r in owner_rows + self_rows)
        fingerprint = hashlib.sha256(text.encode("utf-8")).hexdigest()
        if not dirty and self.store.meta_get("profile_fingerprint", "") == fingerprint:
            self.store.meta_set("reflect_at", now)
            return False
        prof = {}
        try:
            with usage_scope("profile"):
                got = self.llm.chat_json([{"role": "system", "content": PROFILE_SYS},
                                          {"role": "user", "content": text[:6000]}])
            prof = got if isinstance(got, dict) else {}
        except Exception as ex:
            log.info("画像调用失败，用规则版：%s", ex)
        owner = str(prof.get("owner") or "").strip()[:250] if owner_rows else ""
        mine = str(prof.get("self") or "").strip()[:150] if self_rows else ""
        if not owner:
            owner = _join_profile(owner_rows, 250)
        if not mine:
            mine = _join_profile(self_rows, 150)
        with self.lock:
            if self.store.meta_get("memory_revision", 0) != revision:
                return False
            if self.store.meta_get("profile_owner", "") != owner:
                self.store.meta_set("profile_owner", owner)
            if self.store.meta_get("profile_self", "") != mine:
                self.store.meta_set("profile_self", mine)
            if prof:
                self.store.meta_set("profile_fingerprint", fingerprint)
            self.store.meta_set("reflect_at", now)
            self.store.meta_set("profile_dirty", False)
        return bool(owner or mine)

    def _merge_cost(self) -> None:
        """把这一段的增量并进 meta 累加（重启后还能看到总数）"""
        saved = self.store.meta_get("cost_stats", {}) or {}
        keys = set(saved) | set(self.cost)
        self.store.meta_set("cost_stats", {k: int(saved.get(k, 0) or 0) + int(self.cost.get(k, 0) or 0)
                                           for k in keys})
        self.cost = {k: 0 for k in self.cost}

    def _cost_totals(self) -> dict:
        saved = self.store.meta_get("cost_stats", {}) or {}
        out = {k: int(saved.get(k, 0) or 0) + int(self.cost.get(k, 0) or 0)
               for k in set(saved) | set(self.cost)}
        n = out.get("inject_count", 0)
        out["inject_chars_avg"] = round(out.get("inject_chars_total", 0) / n, 1) if n else 0.0
        return out

    def tick(self) -> dict:
        """定期维护：冷却、合并摘要、查余额。建议每 10~30 分钟调一次。"""
        with self.lock:
            self._advance()
            res = self.store.maintain()
            self._save_emotion()
        # 空闲的短对话也会被提炼；重启后从原始 turn 恢复尚未处理的会话。
        with self.lock:
            idle_rows = self.store.db.execute(
                "SELECT path, project_id, MAX(id) AS last_id FROM items WHERE kind='turn' AND role='user' "
                "AND status!='deleted' GROUP BY path,project_id").fetchall()
        for row in idle_rows:
            path = row["path"]
            event_key = path + (":" + row["project_id"] if row["project_id"] else "")
            ev = self._last_events.get(event_key)
            if ev is None:
                with self.lock:
                    turn = self.store.db.execute("SELECT * FROM items WHERE id=?", (row["last_id"],)).fetchone()
                parts = path.split("/")
                if parts[0] == "project":
                    ev = Event("local", "project", "/".join(parts[1:]), turn["user_id"] or "owner", turn["speaker"] or "", ts=turn["created"])
                elif len(parts) >= 4 and parts[0] == "chat":
                    ev = Event(parts[1], parts[2], "/".join(parts[3:]), turn["user_id"] or "", turn["speaker"] or "", ts=turn["created"])
                elif len(parts) == 3 and parts[:2] == ["chat", "local"]:
                    ev = Event("local", "session", parts[2], turn["user_id"] or "owner", turn["speaker"] or "", ts=turn["created"])
                if ev is not None:
                    ev.project_id, ev.session_id = turn["project_id"], turn["session_id"]
                    self._last_events[event_key] = ev
            if ev is not None:
                try:
                    self._maybe_extract(ev)
                except Exception as ex:
                    log.info("空闲抽取保留重试：%s", ex)
        self.flush()
        merged = 0
        for path, turns in self.store.consolidation_candidates(self.mcfg.get("consolidate_min", 30)):
            with usage_scope("summarize"):
                s = summarize(self.llm, turns)
            if s:
                with self.lock:
                    self.store.commit_summary(path, s, [t.id for t in turns])
                merged += 1
        res["summaries"] = merged
        try:
            with usage_scope("embedding_maintenance"):
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
        try:
            res["bili_watch"] = bool(self._maybe_bili_watch())
        except Exception as ex:
            log.warning("B站后台刷失败：%s", ex)
            res["bili_watch"] = False
        try:
            res["reflect"] = self.reflect()
        except Exception as ex:
            log.warning("反思失败：%s", ex)
            res["reflect"] = False
        self._merge_cost()
        return res

    def state(self) -> dict:
        with self.lock:
            self._advance()
            e = self.emo
            return {"name": self.name, "mood": round(e.mood, 4), "target": round(e.target, 4),
                    "label": e.label(self.prev_label), "describe": e.describe(self.prev_label),
                    "loneliness": round(e.L, 3), "baseline": round(sigmoid(e.b), 4),
                    "recent_events": [(n, round(a, 2)) for n, a, _ in e.recent[-8:]],
                    "arousal": round(e.arousal, 4), "behavior": self.behavior.status(),
                    "memory": self.store.stats(), "cost": self._cost_totals(), "usage": self.usage.snapshot(),
                    "projects": self.store.projects.status(),
                    "extraction": [self.store.coverage(ev.path, project_id=ev.project_id)
                                   for ev in self._last_events.values()]}

    def close(self) -> None:
        self.bili_chat.close()
        self.bg.shutdown(wait=True)
        with self.lock:
            self._save_emotion()
            self.store.meta_set("behavior_state", self.behavior.to_dict())
            self._merge_cost()
        self.store.close()
        self.usage.close()
