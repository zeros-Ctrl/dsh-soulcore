"""配置：JSON 文件 + 默认值深合并"""
from __future__ import annotations
import json, os, copy

DEFAULTS = {
    # anchor：手动指定的人格锚点（留空则从人设卡里自动摘）；slip_ttl_h：助手腔提醒保留多久
    "persona": {"name": "小汐", "card_file": "persona.md", "anchor": "", "anchor_enabled": True,
                "slip_ttl_h": 72},
    "data_dir": "./data",
    "llm": {"provider": "deepseek", "base_url": "https://api.deepseek.com", "api_key": "",
            "api_key_env": "DEEPSEEK_API_KEY", "model": "deepseek-chat", "extract_model": "deepseek-chat",
            "temperature": 1.0, "max_tokens": 400, "timeout": 60},
    "emotion": {},
    "appraisal": {"mode": "heuristic"},
    "owner": {"qq": "", "name": "主人"},      # 主人的 QQ 号：用来把 QQ 上的你认成 owner
    "memory": {"extract_every": 6, "recent_turns": 12, "top_k": 8, "cold_threshold": 0.04,
               "consolidate_min": 30, "beta": 0.3, "tau_hours": {}, "touch_mode": "recency",
               # v0.8：知识更新 / 时间感知检索 / 冷存储回想 / 检索门控 / 提炼降本
               "auto_supersede": True, "time_boost": 2.0, "cold_recall": True, "cold_min_rel": 0.45,
               "gate_retrieval": True, "extract_assistant_chars": 300, "extract_user_chars": 800,
               "segment_gap_min": 30, "reflect_every_h": 20},
    "balance": {"check_every_min": 60, "low_threshold": 5.0, "recharge_min": 5.0},
    "server": {"host": "127.0.0.1", "port": 8765, "token": "", "tick_every_min": 15},
    "service": {"mode": "follow_dsh"},
    # 主动聊天（见 initiative.py）：默认关闭；打开后默认试运行，只生成草稿不真的发
    "initiative": {"enabled": False, "dry_run": True, "base_rate_per_hour": 0.15,
                   "quiet_hours": ["00:00", "09:00"], "max_per_day": 3, "min_gap_hours": 3,
                   "recent_activity_skip_min": 30, "max_unanswered": 2, "reply_window_hours": 4,
                   "thought_threshold": 0.45, "check_every_min": 5},        # follow_dsh | resident
    "qq": {"enabled": False, "transport": "ws", "ws_url": "ws://127.0.0.1:3001",
           "api_base": "http://127.0.0.1:3000", "access_token": "", "self_id": "",
           "group_whitelist": [], "private_whitelist": [], "allow_all_private": False,
           "group_known": [],
           "min_interval_s": 3, "max_per_min": 10, "interject_prob": 0.0, "typing_delay": True,
           "sticker": {"enabled": True, "dir": "", "max_images": 1},
           "bili": {"enabled": False, "tools_path": "", "node": "", "every_min": 180,
                    "max_candidates": 3, "timeout_s": 45, "chat_trigger": True,
                    "chat_min_gap_s": 600,
                    # 聊天里搜视频 / 读字幕：每小时上限（防止账号被风控）和超时
                    "search_per_hour": 10, "video_per_hour": 5,
                    "search_timeout_s": 20, "subtitle_timeout_s": 45}},
    "bilibili": {"enabled": False, "credential": {"sessdata": "", "bili_jct": "", "buvid3": "", "dedeuserid": ""},
                 "poll_interval_s": 90, "reply_at": True, "reply_replies": True, "dm_whitelist": [],
                 "auto_send": False, "comment_min_interval_s": 15, "comment_max_per_hour": 20,
                 "dm_min_interval_s": 10, "dm_max_per_hour": 20},
}


def deep_merge(a: dict, b: dict) -> dict:
    out = copy.deepcopy(a)
    for k, v in (b or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(path: str | None) -> tuple[dict, str]:
    if path and os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            user = json.load(f)
        return deep_merge(DEFAULTS, user), os.path.dirname(os.path.abspath(path))
    return copy.deepcopy(DEFAULTS), os.getcwd()
