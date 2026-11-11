"""配置：JSON 文件 + 默认值深合并"""
from __future__ import annotations
import json, os, copy

DEFAULTS = {
    "persona": {"name": "小汐", "card_file": "persona.md"},
    "data_dir": "./data",
    "llm": {"provider": "deepseek", "base_url": "https://api.deepseek.com", "api_key": "",
            "api_key_env": "DEEPSEEK_API_KEY", "model": "deepseek-chat", "extract_model": "deepseek-chat",
            "temperature": 1.0, "max_tokens": 400, "timeout": 60},
    "emotion": {},
    "appraisal": {"mode": "heuristic"},
    "owner": {"qq": "", "name": "主人"},      # 主人的 QQ 号：用来把 QQ 上的你认成 owner
    "memory": {"extract_every": 6, "recent_turns": 12, "top_k": 8, "cold_threshold": 0.04,
               "consolidate_min": 30, "beta": 0.3, "tau_hours": {}, "touch_mode": "recency"},
    "balance": {"check_every_min": 60, "low_threshold": 5.0, "recharge_min": 5.0},
    "server": {"host": "127.0.0.1", "port": 8765, "token": "", "tick_every_min": 15},
    "service": {"mode": "follow_dsh"},        # follow_dsh | resident
    "qq": {"enabled": False, "transport": "ws", "ws_url": "ws://127.0.0.1:3001",
           "api_base": "http://127.0.0.1:3000", "access_token": "", "self_id": "",
           "group_whitelist": [], "private_whitelist": [], "allow_all_private": False,
           "group_known": [],
           "min_interval_s": 3, "max_per_min": 10, "interject_prob": 0.0, "typing_delay": True},
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
