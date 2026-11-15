"""统一事件格式：QQ / B站 / 本地 harness 的消息都转成 Event，记忆按 path 路由。"""
from __future__ import annotations
import time
from dataclasses import dataclass, field, asdict
from typing import Optional


@dataclass
class Event:
    platform: str                 # qq | bilibili | local
    scene: str                    # private | group | dm | comment | project | session
    channel_id: str               # QQ号 / 群号 / 对方UID / av号 / 项目名 / 会话名
    user_id: str
    user_name: str = ""
    text: str = ""
    mentions_self: bool = False
    ts: float = field(default_factory=time.time)
    event_id: str = ""
    thread_id: str = ""           # B站评论楼层 root rpid
    reply_target: dict = field(default_factory=dict)  # 回发时需要的平台参数
    is_stranger: bool = False     # B站评论区路人等
    raw: dict = field(default_factory=dict)
    cwd: str = ""                # 项目身份由后端注册表解析，不依赖 basename 唯一性
    project_id: str = ""
    session_id: str = ""

    # ---------- 记忆路由（确定性，按元数据分流，不靠 LLM 猜） ----------
    @property
    def path(self) -> str:
        p, s, c = self.platform, self.scene, _safe(self.channel_id)
        if p == "local" and s == "project":
            return f"project/{c}"
        if p == "local":
            return f"chat/local/{c}"
        if p == "bilibili" and s == "comment":
            return f"chat/bilibili/comment/{c}"
        return f"chat/{p}/{s}/{c}"

    @property
    def person_path(self) -> str:
        return f"core/people/{self.platform}/{_safe(self.user_id)}"

    @property
    def is_social(self) -> bool:
        """这条消息是否算"有人在陪她"（重置孤独计时）"""
        return self.scene in ("private", "dm", "project", "session") or self.mentions_self

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Event":
        keys = cls.__dataclass_fields__.keys()
        return cls(**{k: v for k, v in d.items() if k in keys})


def _safe(s: str) -> str:
    s = str(s).strip() or "default"
    for ch in '\\/:*?"<>|':
        s = s.replace(ch, "_")
    return s[:80]
