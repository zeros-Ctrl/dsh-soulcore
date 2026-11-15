"""Claude Code hook 适配器：`python -m soulcore cc-hook <event>`。

Claude Code 在生命周期事件上调用这个短命进程，从 stdin 拿到事件 JSON，转成 soulcore 的
Event 交给本地常驻后端，必要时往 stdout 输出 `hookSpecificOutput.additionalContext`。

**失败即放行**：任何异常（后端没起、超时、JSON 坏了）都只写日志、以退出码 0 退出、不输出
内容——hook 是旁路增强，绝不能挡住用户的 Claude Code。
stdout 只放约定格式的 JSON，日志一律写文件。
"""
from __future__ import annotations

import json
import os
import sys
import time
from urllib.parse import quote

from .client import Backend, BackendUnavailable, resolve_home
from .config import load_config

# 脚本自己控制的时间预算（比 hooks.json 里的 timeout 更短），超时就放弃，不拖累 Claude Code
BUDGET = {"user-prompt": 2.5, "session-start": 3.5, "stop": 2.0,
          "pre-compact": 30.0, "session-end": 5.0}
EVENT_NAME = {"user-prompt": "UserPromptSubmit", "session-start": "SessionStart", "stop": "Stop",
              "pre-compact": "PreCompact", "session-end": "SessionEnd"}
LOG_MAX = 1024 * 1024
MEMORY_KINDS = ("decision", "todo")


# ------------------------------------------------------------------ 日志 / 路径
def log_line(msg: str) -> None:
    """写 <home>/logs/cc-hook.log；超过 1MB 轮转一次。写日志本身出任何错都吞掉。"""
    try:
        home = resolve_home(create=True)
        d = os.path.join(home, "logs")
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, "cc-hook.log")
        try:
            if os.path.getsize(path) > LOG_MAX:
                os.replace(path, path + ".1")
        except OSError:
            pass
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
    except Exception:                       # noqa: BLE001 —— 日志失败不能影响主流程
        pass


def git_root(cwd: str) -> str | None:
    """向上找 .git（文件或目录都算，worktree/submodule 的 .git 是文件）"""
    d = os.path.abspath(cwd)
    for _ in range(40):
        try:
            if os.path.exists(os.path.join(d, ".git")):
                return d
        except OSError:
            return None
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent
    return None


def project_name(cwd: str | None, session_id: str = "") -> str:
    """cwd → 记忆分支名。规则和 DSH 的 lib/glue.js:channelOf 完全一致。

    1. 有 .git 就用仓库根目录名（cwd 在仓库子目录里也归到同一个项目）；
    2. 否则用最后一级目录名，盘符根目录不算；
    3. 都没有就用 session_id。
    """
    cwd = (cwd or "").strip()
    if cwd:
        root = git_root(cwd) if os.path.isdir(cwd) else None
        base = os.path.basename((root or cwd).rstrip("\\/"))
        if base and not (len(base) == 2 and base[1] == ":"):
            return base
    sid = str(session_id or "").strip()
    if not sid:
        return "default"
    return (sid if sid.startswith("session-") else f"session-{sid}")[:32]


def make_event(payload: dict, cfg: dict, text: str = "") -> dict:
    """Claude Code 本地会话一律视为 owner；scene=project，和 DSH 共用 project/<名> 记忆。"""
    owner = str((cfg.get("owner") or {}).get("name") or "主人")
    return {"platform": "local", "scene": "project",
            "channel_id": project_name(payload.get("cwd"), payload.get("session_id", "")),
            "user_id": "owner", "user_name": owner,
            "text": text or "", "mentions_self": True, "ts": time.time(),
            "cwd": str(payload.get("cwd") or ""),
            "session_id": str(payload.get("session_id") or ""),
            "event_id": str(payload.get("uuid") or payload.get("event_id") or ""),
            "raw": {"client": "claude-code", "session_id": str(payload.get("session_id") or "")}}


def resolve_project_event(backend, event: dict, timeout: float = 2.0, *, strict: bool = False) -> dict:
    """Resolve routing through the shared backend; older backends remain usable."""
    if not (event.get("cwd") or event.get("project_id") or event.get("raw", {}).get("resolve_project_name")):
        return event
    try:
        info = backend.request("POST", "/v1/projects/resolve", {"event": event}, timeout=timeout)
        if isinstance(info, dict) and info.get("project_id") and info.get("channel_id"):
            return {**event, "project_id": info["project_id"], "channel_id": info["channel_id"],
                    "raw": {**event.get("raw", {}), "project_identity": info}}
        if strict:
            raise ValueError("项目尚未绑定稳定身份，请在目标项目目录中接入，或指定已注册的 project UUID")
    except BackendUnavailable:
        if strict:
            raise
        pass
    return event


# ------------------------------------------------------------------ transcript 解析
def _tail_text(path: str, nbytes: int = 2 * 1024 * 1024) -> str:
    """从文件尾部倒着读最多 2MB（transcript 可能很大），跳过被截断的第一行。"""
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        start = max(0, size - nbytes)
        f.seek(start)
        data = f.read()
    if start > 0:
        nl = data.find(b"\n")
        if nl >= 0:
            data = data[nl + 1:]
    return data.decode("utf-8", errors="replace")


def _is_real_user(obj: dict) -> bool:
    """真正的用户输入：content 是字符串，或含 text 块；只有 tool_result 的不算。"""
    msg = obj.get("message")
    content = msg.get("content") if isinstance(msg, dict) else None
    if isinstance(content, str):
        return bool(content.strip())
    if isinstance(content, list):
        for b in content:
            if isinstance(b, dict) and b.get("type") == "text" and str(b.get("text") or "").strip():
                return True
    return False


def transcript_reply(path: str | None, limit: int | None = 4000) -> str:
    """从 transcript JSONL 里取"本轮最终的 assistant 文本"。

    - 逐行容错解析，坏行跳过；
    - 从最后一条真正的用户输入之后，收集所有 assistant 文本块按顺序拼接；
    - 跳过子 agent（isSidechain）和压缩摘要（isCompactSummary）条目；
    - 不取 tool_result：工具输出里可能夹着网页/文件内容，不能当成她自己的话。
    """
    if not path or not os.path.isfile(path):
        return ""
    entries = []
    for line in _tail_text(path).splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if not isinstance(obj, dict):
            continue
        if obj.get("isSidechain") or obj.get("isCompactSummary"):
            continue
        entries.append(obj)
    start = -1
    for i, obj in enumerate(entries):
        if obj.get("type") == "user" and _is_real_user(obj):
            start = i
    if start < 0:
        return ""
    parts: list[str] = []
    for obj in entries[start + 1:]:
        if obj.get("type") != "assistant":
            continue
        msg = obj.get("message")
        content = msg.get("content") if isinstance(msg, dict) else None
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            for b in content:
                if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str):
                    parts.append(b["text"])
    text = "\n".join(p.strip() for p in parts if p.strip())
    return text[:limit] if limit is not None else text


def last_assistant_text(payload: dict) -> str:
    """Stop 事件的最终回复：优先用官方提供的 last_assistant_message，没有才翻 transcript。"""
    raw = payload.get("last_assistant_message")
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    if isinstance(raw, list):
        parts = [b.get("text", "") for b in raw if isinstance(b, dict) and b.get("type") == "text"]
        text = "\n".join(p for p in parts if p).strip()
        if text:
            return text
    return transcript_reply(payload.get("transcript_path"), limit=None)


# ------------------------------------------------------------------ 各事件
def _remaining(deadline: float, cap: float = 10.0) -> float:
    return max(0.2, min(cap, deadline - time.monotonic()))


def _hook_json(event: str, context: str) -> str:
    return json.dumps({"hookSpecificOutput": {"hookEventName": EVENT_NAME[event],
                                              "additionalContext": context}},
                      ensure_ascii=False)


def _fmt_memory(items) -> str:
    out = []
    for r in items:
        content = str(r.get("content") or "").strip().replace("\n", " ")
        if content:
            state = {"suggested": "提议", "accepted": "已接受", "implemented": "已实施",
                     "verified": "有验证证据"}.get(r.get("epistemic_state"), "")
            label = str(r.get("kind", "fact")) + ("·" + state if state else "")
            out.append(f"- [{label}] {content[:200]}")
    return "\n".join(out)


def handle_session_start(payload, backend, cfg, style, deadline):
    ev = make_event(payload, cfg)
    if backend.ensure(start=True, wait_s=min(3.0, _remaining(deadline, 3.0))) is None:
        return None
    ev = resolve_project_event(backend, ev, timeout=_remaining(deadline, 1.0))
    parts: list[str] = []
    if style == "companion":
        try:
            stable = backend.request("GET", "/v1/persona/stable", timeout=_remaining(deadline))
            if stable and stable.get("text"):
                parts.append(str(stable["text"]))
        except BackendUnavailable:
            pass
    try:
        path = quote("project/" + ev["channel_id"], safe="")
        pid = quote(ev.get("project_id", ""), safe="")
        data = backend.request("GET", f"/v1/memory/timeline?path={path}&project_id={pid}&limit=20&top=1",
                               timeout=_remaining(deadline)) or {}
        rows = [r for r in (data.get("recent") or []) if r.get("kind") in MEMORY_KINDS][:5]
        body = _fmt_memory(rows)
        if body:
            title = "压缩前的要点" if str(payload.get("source") or "") == "compact" else "本项目长期记忆"
            parts.append(f"[soulcore·{title}]\n{body}")
    except BackendUnavailable as ex:
        log_line(f"session-start: 取项目记忆失败：{ex}")
    text = "\n\n".join(p for p in parts if p).strip()
    return _hook_json("session-start", text) if text else None


def handle_user_prompt(payload, backend, cfg, style, deadline):
    prompt = str(payload.get("prompt") or "")
    ev = make_event(payload, cfg, prompt)
    if not ev["text"].strip():
        return None
    # 这里**不冷启动**后端：UserPromptSubmit 卡住会拖慢用户输入
    if backend.ensure(start=False) is None:
        return None
    r = backend.request("POST", "/v1/hooks/context",
                        {"event": ev, "include_stable": False, "style": style},
                        timeout=_remaining(deadline, 2.0)) or {}
    text = str(r.get("text") or "").strip()
    return _hook_json("user-prompt", text) if text else None


def handle_stop(payload, backend, cfg, style, deadline):
    if payload.get("stop_hook_active"):
        return None                                  # 已经在 stop hook 里了，别再套一层
    if (cfg.get("cc") or {}).get("record_replies", True) is False:
        return None
    reply = last_assistant_text(payload)
    if not reply:
        return None
    ev = make_event(payload, cfg)
    if backend.ensure(start=False) is None:
        return None
    backend.request("POST", "/v1/hooks/after", {"event": ev, "reply": reply},
                    timeout=_remaining(deadline, 1.8))
    return None


def handle_flush(payload, backend, cfg, style, deadline):
    """PreCompact / SessionEnd：轮次已经实时写进后端，这里只需要强制提炼。"""
    ev = make_event(payload, cfg)
    if backend.ensure(start=True, wait_s=min(8.0, _remaining(deadline, 8.0))) is None:
        return None
    backend.request("POST", "/v1/hooks/flush", {"event": ev}, timeout=_remaining(deadline, 25.0))
    return None


HANDLERS = {
    "session-start": handle_session_start,
    "user-prompt": handle_user_prompt,
    "stop": handle_stop,
    "pre-compact": handle_flush,
    "session-end": handle_flush,
}


# ------------------------------------------------------------------ 入口
def main(event: str, stdin_text: str | None = None) -> int:
    """跑一个 hook 事件。永远返回 0：失败即放行。"""
    event = event.strip().lower()
    budget = BUDGET.get(event, 2.0)
    t0 = time.monotonic()
    deadline = t0 + budget
    out = None
    try:
        # Claude Code 用 UTF-8 传 JSON；Windows 上 Python 默认按 cp936 解管道会乱码，
        # 所以显式按 UTF-8 读字节
        if stdin_text is not None:
            raw = stdin_text
        else:
            try:
                raw = sys.stdin.buffer.read().decode("utf-8", errors="replace")
            except AttributeError:
                raw = sys.stdin.read()
        payload = json.loads(raw or "{}")
        if not isinstance(payload, dict):
            raise ValueError("hook 输入不是 JSON 对象")
        home = os.environ.get("SOULCORE_HOME") or resolve_home()
        cfg, _base = load_config(os.path.join(home, "config.json"))
        ccfg = cfg.get("cc") or {}
        if ccfg.get("enabled", True) is False:
            return 0
        style = str(ccfg.get("style") or "memory").lower()
        if style not in ("memory", "companion"):
            style = "memory"
        handler = HANDLERS.get(event)
        if handler is None:
            log_line(f"未知事件：{event}")
            return 0
        out = handler(payload, Backend(home=home), cfg, style, deadline)
    except Exception as ex:               # noqa: BLE001 —— 任何异常都不能挡住 Claude Code
        log_line(f"{event}: {ex!r}")
        return 0
    cost = time.monotonic() - t0
    if cost > budget:
        log_line(f"{event}: 超预算 {cost:.2f}s > {budget}s，输出已放弃")
        return 0
    if out:
        try:
            sys.stdout.buffer.write(out.encode("utf-8"))
            sys.stdout.buffer.flush()
        except AttributeError:
            sys.stdout.write(out)
            sys.stdout.flush()
        except Exception as ex:           # noqa: BLE001
            log_line(f"{event}: 写 stdout 失败：{ex!r}")
    return 0
