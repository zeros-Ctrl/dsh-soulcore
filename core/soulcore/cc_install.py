"""cc-install：把 soulcore 的 hooks 合并进 Claude Code 的 settings.json。

默认**只打印**要粘贴的片段；加 `--apply` 才写文件（先备份），`--uninstall` 只移除 soulcore 的项。
不覆盖用户已有的 hooks：同一事件是追加一项，已经存在的 soulcore 项会被替换（所以连续 apply 幂等）。
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import time

from .client import RUN_PY, user_home

MARK = "soulcore"

# (Claude Code 事件, cc-hook 子命令, hooks.json 里的 timeout 秒, matcher)
EVENTS = [
    ("SessionStart", "session-start", 10, "startup|resume|clear|compact|fork"),
    ("UserPromptSubmit", "user-prompt", 5, None),
    ("Stop", "stop", 5, None),
    ("PreCompact", "pre-compact", 60, "manual|auto"),
    ("SessionEnd", "session-end", 10, None),
]


def settings_path(scope: str = "user", cwd: str | None = None, home: str | None = None) -> str:
    if scope == "project":
        return os.path.join(cwd or os.getcwd(), ".claude", "settings.json")
    return os.path.join(home or user_home(), ".claude", "settings.json")


def hook_command(event: str, python: str | None = None, run_py: str | None = None) -> str:
    """手动安装用绝对路径最稳：Windows 上 hook 由 Git Bash 执行，PATH 里的 python 未必对。"""
    py = python or sys.executable or "python"
    return f'"{py}" -I "{run_py or RUN_PY}" cc-hook {event}'


def hook_groups(python: str | None = None, run_py: str | None = None) -> dict:
    groups = {}
    for event, sub, timeout, matcher in EVENTS:
        group: dict = {"hooks": [{"type": "command",
                                  "command": hook_command(sub, python, run_py),
                                  "timeout": timeout}]}
        if matcher:
            group["matcher"] = matcher
        groups[event] = group
    return groups


def mcp_command(python: str | None = None, run_py: str | None = None) -> str:
    py = python or sys.executable or "python"
    return f'claude mcp add soulcore -- "{py}" -I "{run_py or RUN_PY}" mcp'


def _is_ours(group) -> bool:
    if not isinstance(group, dict):
        return False
    hooks = group.get("hooks")
    if not isinstance(hooks, list):
        return False
    return any(isinstance(h, dict) and MARK in str(h.get("command", "")).lower() for h in hooks)


def merge_settings(data: dict | None, groups: dict) -> dict:
    """把我们的 hook 组合并进去：同一事件追加一项；已有的 soulcore 项替换掉（幂等）"""
    data = dict(data or {})
    all_hooks = {k: list(v) for k, v in (data.get("hooks") or {}).items()}
    for event, group in groups.items():
        cur = [g for g in (all_hooks.get(event) or []) if not _is_ours(g)]
        cur.append(group)
        all_hooks[event] = cur
    data["hooks"] = all_hooks
    return data


def remove_settings(data: dict | None) -> dict:
    """移除所有 command 里带 soulcore 的 hook 项，别的一律不动"""
    data = dict(data or {})
    hooks = {k: list(v) for k, v in (data.get("hooks") or {}).items()}
    for event in list(hooks):
        cur = [g for g in hooks[event] if not _is_ours(g)]
        if cur:
            hooks[event] = cur
        else:
            hooks.pop(event, None)
    if hooks:
        data["hooks"] = hooks
    else:
        data.pop("hooks", None)
    return data


def _load(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as ex:
        raise ValueError(f"{path} 不是合法 JSON，先手动修好它：{ex}") from ex


def _write(path: str, data: dict) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, path)


def install(scope: str = "user", apply: bool = False, uninstall: bool = False,
            cwd: str | None = None, home: str | None = None,
            python: str | None = None, run_py: str | None = None) -> tuple[int, str]:
    """返回 (退出码, 要打印的文本)"""
    path = settings_path(scope, cwd, home)
    groups = hook_groups(python, run_py)
    lines: list[str] = []
    if not apply and not uninstall:
        snippet = {"hooks": groups}
        lines.append(f"# 目标文件：{path}")
        lines.append("# 把下面这段合并进 settings.json 的顶层（已有 hooks 就按事件追加）：")
        lines.append(json.dumps(snippet, ensure_ascii=False, indent=2))
        lines.append("")
        lines.append("# MCP（只想要工具、不要自动注入时用这条）：")
        lines.append(mcp_command(python, run_py))
        lines.append("")
        lines.append(f"# 想让我直接改文件：python -I {run_py or RUN_PY} cc-install --scope {scope} --apply")
        return 0, "\n".join(lines)

    try:
        data = _load(path)
    except ValueError as ex:
        return 2, f"✗ {ex}"
    if uninstall and not os.path.exists(path):
        return 0, f"✓ 没有需要移除的（{path} 不存在）"
    new = remove_settings(data) if uninstall else merge_settings(data, groups)
    if new == data and os.path.exists(path):
        return 0, f"✓ 没有变化：{path}"
    if os.path.exists(path) and not uninstall:
        backup = f"{path}.bak-{time.strftime('%Y%m%d-%H%M%S')}"
        shutil.copyfile(path, backup)
        lines.append(f"已备份：{backup}")
    _write(path, new)
    if uninstall:
        lines.append(f"✓ 已从 {path} 移除 soulcore 的 hooks")
    else:
        lines.append(f"✓ 已写入 {path}")
        lines.append("  MCP 如果还没加过，再执行：")
        lines.append("  " + mcp_command(python, run_py))
    return 0, "\n".join(lines)
