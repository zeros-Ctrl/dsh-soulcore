"""Local expression policy: bounded behavior without extra model calls.

Quiet is an opt-in social behavior. Only closed light private smalltalk phrases
can be quiet or receive a smaller sad-state budget. Unfamiliar text, questions,
tasks, controls and help keep the complete response budget. Engine still records
and processes the message before consulting it.
"""
from __future__ import annotations

import hashlib
import math
import re
import secrets
import time
from dataclasses import asdict, dataclass

from .chat_intents import parse_intent
from .extractor import is_question


DEFAULTS = {
    "enabled": True,
    "base_max_tokens": 400,
    "max_messages": 3,
    "quiet_enabled": False,
    "quiet_threshold": 0.20,
    "quiet_arousal_max": 0.45,
    "quiet_max_probability": 0.35,
    "quiet_duration_min": 15.0,
    "quiet_cooldown_min": 30.0,
    "quiet_max_consecutive": 1,
}

_SMALLTALK = re.compile(
    r"(?:你好|嗨|哈喽|早安|晚安|午安|早上好|晚上好|路过|冒个泡|"
    r"[哈嘿呵]{2,}|嗯嗯?|哦哦?|好(?:的)?|"
    r"今天(?:天气(?:不错|很好|真好)|阳光(?:很好|真好)|好安静|好舒服)|"
    r"hello|hi|good night)[。.!！~～\s]*", re.I)
_NEEDS_RESPONSE = re.compile(
    r"帮|请|麻烦|需要|能不能|可不可以|怎么|为什么|什么|多少|哪里|哪儿|"
    r"救|难过|伤心|焦虑|害怕|疼|痛|不舒服|被拒|失恋|分手|失眠|疲惫|累|"
    r"记住|记一下|忘记|忘掉|删除|停止|暂停|恢复|取消|唤回|说句话|回来|"
    r"项目|代码|配置|方案|任务|执行|修改|测试|报错|失败|问题|错误|不对|"
    r"求助|help|please|fix|error|fail|stop|remember|forget", re.I)


def _unit(value, fallback=0.5) -> float:
    try:
        value = float(value)
        return max(0.0, min(1.0, value)) if math.isfinite(value) else fallback
    except (TypeError, ValueError):
        return fallback


@dataclass(frozen=True)
class ExpressionPolicy:
    respond: bool = True
    mode: str = "social"
    max_tokens: int = 400
    max_messages: int = 1
    sharing: float = 0.4
    initiative_multiplier: float = 1.0
    prompt: str = ""
    quiet_until: float = 0.0
    reason: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def expression_profile(mood: float, arousal: float, cfg: dict | None = None,
                       *, task: bool = False) -> ExpressionPolicy:
    c = {**DEFAULTS, **(cfg or {})}
    m, a = _unit(mood), _unit(arousal, 0.35)
    budget = max(1, int(c["base_max_tokens"]))
    if not c["enabled"]:
        return ExpressionPolicy(mode="task" if task else "social", max_tokens=budget)
    sharing = min(1.0, max(0.0, (0.15 + 0.75 * m) * (0.65 + 0.35 * a)))
    initiative = min(1.6, max(0.35, 0.35 + 0.95 * m + 0.5 * max(0.0, a - 0.35)))
    if task:
        return ExpressionPolicy(mode="task", max_tokens=budget, sharing=sharing,
                                initiative_multiplier=initiative,
                                prompt="完整回应当前内容，认真处理必要的问题或任务，保留必要信息；心情只影响语气与闲聊量。")
    messages = min(3, max(1, int(c["max_messages"]))) if m >= 0.74 and a >= 0.50 else 1
    if m < 0.35 and a < 0.45:
        budget = max(1, min(budget, int(budget * 0.65)))
        prompt = "这轮简短、温和，少延展闲聊；有真实原因时可说一次有些低落，不反复索取安慰。"
    elif m >= 0.74 and a >= 0.50:
        prompt = f"这轮活跃一些，可分享确实知道且相关的内容；最多{messages}条短消息，用空行分隔，不凭空编造经历。"
    elif m < 0.45 and a >= 0.50:
        prompt = "结合实际原因表达担忧或不满，保持清楚克制；不要把激动统一表达成伤心。"
    else:
        prompt = "自然交流，有相关内容时适度分享；表达跟随实际情绪原因。"
    return ExpressionPolicy(max_tokens=budget, max_messages=messages, sharing=sharing,
                            initiative_multiplier=initiative, prompt=prompt)


def _task_mode(ev) -> bool:
    text = (getattr(ev, "text", "") or "").strip()
    # A keyword blacklist cannot establish that an unfamiliar utterance is
    # harmless smalltalk. Keep its response budget intact by default.
    closed_smalltalk = bool(text and len(text) <= 40 and _SMALLTALK.fullmatch(text))
    return bool(getattr(ev, "platform", "") == "local" or
                getattr(ev, "scene", "") in ("project", "session") or
                parse_intent(text) or is_question(text) or "?" in text or "？" in text or
                _NEEDS_RESPONSE.search(text) or not closed_smalltalk)


def _light_social(ev) -> bool:
    text = (getattr(ev, "text", "") or "").strip()
    if getattr(ev, "platform", "") == "local" or getattr(ev, "scene", "") not in ("private", "dm"):
        return False
    if not text or len(text) > 40 or "?" in text or "？" in text:
        return False
    if _task_mode(ev):
        return False
    return bool(_SMALLTALK.fullmatch(text))


class BehaviorPolicy:
    def __init__(self, cfg: dict | None = None, clock=time.time, seed=None):
        self.cfg = {**DEFAULTS, **(cfg or {})}
        self.clock = clock
        self.seed = str(seed) if seed is not None else secrets.token_hex(16)
        self.channels: dict[str, dict] = {}

    def load_state(self, state: dict | None) -> None:
        state = state if isinstance(state, dict) else {}
        if isinstance(state.get("seed"), (str, int)):
            self.seed = str(state["seed"])
        channels = state.get("channels", {})
        self.channels = {str(k): dict(v) for k, v in channels.items() if isinstance(v, dict)} \
            if isinstance(channels, dict) else {}

    def to_dict(self) -> dict:
        # Quiet state is operational metadata, not remembered personal facts.
        return {"seed": self.seed, "channels": {k: dict(v) for k, v in self.channels.items()}}

    def _sample(self, key: str, ev, count: int) -> float:
        event = getattr(ev, "event_id", "") or f"{getattr(ev, 'ts', 0)}:{getattr(ev, 'text', '')}"
        material = f"{self.seed}:{key}:{event}:{count}".encode("utf-8")
        return int.from_bytes(hashlib.sha256(material).digest()[:8], "big") / (1 << 64)

    def for_event(self, ev, emotion) -> ExpressionPolicy:
        now = float(self.clock())
        m, a = _unit(emotion.mood), _unit(getattr(emotion, "arousal", 0.35), 0.35)
        light = _light_social(ev)
        profile = expression_profile(m, a, self.cfg, task=_task_mode(ev))
        key = f"{ev.path}|{getattr(ev, 'user_id', '')}"
        st = self.channels.setdefault(key, {"consecutive": 0, "quiet_until": 0.0,
                                           "cooldown_until": 0.0, "counter": 0, "updated": now})
        st["updated"] = now
        self._prune(now)
        threshold = max(0.01, _unit(self.cfg["quiet_threshold"], 0.20))
        if not self.cfg["enabled"] or not self.cfg["quiet_enabled"] or not light or m >= threshold \
                or a > _unit(self.cfg["quiet_arousal_max"], 0.45):
            if st.get("quiet_until", 0):
                st["cooldown_until"] = max(float(st.get("cooldown_until", 0)),
                                           now + max(0.0, float(self.cfg["quiet_cooldown_min"])) * 60)
            st["consecutive"], st["quiet_until"] = 0, 0.0
            self._prune(now)
            return profile
        expired = bool(st.get("quiet_until", 0)) and now >= float(st["quiet_until"])
        exhausted = int(st.get("consecutive", 0)) >= max(1, int(self.cfg["quiet_max_consecutive"]))
        if expired or exhausted:
            st["consecutive"], st["quiet_until"] = 0, 0.0
            st["cooldown_until"] = now + max(0.0, float(self.cfg["quiet_cooldown_min"])) * 60
            return profile
        if now < float(st.get("cooldown_until", 0)):
            return profile
        probability = _unit(self.cfg["quiet_max_probability"], 0.35) * (threshold - m) / threshold
        count = int(st.get("counter", 0))
        st["counter"] = count + 1
        if self._sample(key, ev, count) >= probability:
            st["consecutive"] = 0
            return profile
        if not st.get("quiet_until", 0):
            # A message cannot extend an already active quiet interval.
            st["quiet_until"] = now + max(0.01, float(self.cfg["quiet_duration_min"])) * 60
        st["consecutive"] = int(st.get("consecutive", 0)) + 1
        return ExpressionPolicy(**{**profile.to_dict(), "respond": False,
                                   "quiet_until": st["quiet_until"], "reason": "低落，暂时安静"})

    def _prune(self, now: float) -> None:
        if len(self.channels) > 100:
            active = sorted(self.channels, key=lambda k: float(self.channels[k].get("updated", 0)), reverse=True)
            self.channels = {k: self.channels[k] for k in active[:100]}

    def status(self) -> dict:
        now = float(self.clock())
        quiet = [{"channel": k, "until": st["quiet_until"], "consecutive": st.get("consecutive", 0)}
                 for k, st in self.channels.items() if float(st.get("quiet_until", 0)) > now]
        return {"enabled": bool(self.cfg["enabled"]), "quiet_enabled": bool(self.cfg["quiet_enabled"]),
                "quiet": quiet}
