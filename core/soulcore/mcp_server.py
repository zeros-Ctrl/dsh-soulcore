"""MCP server：`python -m soulcore mcp`（stdio 传输，按行分隔的 JSON-RPC 2.0）。

不只给 Claude Code 用：Cursor、Claude Desktop、Cherry Studio 等任何 MCP 客户端都可以接。
后端还是本地常驻的那个（和 DSH 共享同一份记忆），这里只做协议转换。

约定：
  - stdout 只能出现 JSON-RPC 消息；
  - 日志写 stderr；
  - 后端没起时工具返回 isError: true 和一句可读的说明，不抛协议错误。
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
import uuid
from urllib.parse import quote

from . import __version__
from .cc_hook import project_name, resolve_project_event
from .client import Backend, BackendUnavailable, resolve_home
from .persona import _ago

log = logging.getLogger("soulcore.mcp")

# 支持的 MCP 协议版本；客户端请求的版本认识就回显，不认识就回最新的
SUPPORTED_VERSIONS = ("2024-11-05", "2025-03-26", "2025-06-18")
LATEST_VERSION = SUPPORTED_VERSIONS[-1]

TOOLS = [
    {
        "name": "memory_search",
        "description": "在长期记忆里搜索：以前聊过的事、项目里做过的决定和理由、主人的偏好。"
                       "上下文里找不到、或用户问\"之前/上次/还记得吗\"时使用。",
        "inputSchema": {
            "type": "object", "additionalProperties": False,
            "properties": {
                "query": {"type": "string", "description": "要找什么，用关键词或一句话"},
                "k": {"type": "integer", "minimum": 1, "maximum": 50, "default": 8,
                      "description": "返回条数"},
                "include_history": {"type": "boolean", "default": False,
                                    "description": "是否连已过时的旧说法一起搜（问\"以前\"时用）"},
                "project": {"type": "string",
                            "description": "项目名或已注册 UUID；同名时使用当前项目身份或指定 UUID，默认当前工作目录"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "remember",
        "description": "把一件值得长期记住的事写进记忆：用户明确让你记住的事、项目的关键决定"
                       "（写清决定+理由+否掉的方案）、用户偏好、待办。不要记寒暄。",
        "inputSchema": {
            "type": "object", "additionalProperties": False,
            "properties": {
                "content": {"type": "string", "description": "一句完整、脱离上下文也能看懂的话，写明主语"},
                "kind": {"type": "string", "enum": ["fact", "preference", "decision", "todo"],
                         "default": "fact", "description": "类别"},
            },
            "required": ["content"],
        },
    },
    {
        "name": "forget",
        "description": "忘掉一条记忆（按 id）：标为已删除并加墓碑，之后自动提炼不会把它复活。"
                       "只对主人/熟人来源的条目有效。",
        "inputSchema": {
            "type": "object", "additionalProperties": False,
            "properties": {"id": {"type": "integer", "description": "记忆 id（memory_search 返回的 #id）"}},
            "required": ["id"],
        },
    },
    {
        "name": "restore",
        "description": "恢复一条被忘掉或被取代的记忆：删掉的恢复成有效记忆，被旧说法取代的清掉失效标记。",
        "inputSchema": {
            "type": "object", "additionalProperties": False,
            "properties": {"id": {"type": "integer", "description": "记忆 id"}},
            "required": ["id"],
        },
    },
    {
        "name": "memory_timeline",
        "description": "列出最近新增的记忆和最近被取代的记忆（旧说法 → 新说法），用来回答"
                       "\"最近记了什么\"\"哪些说法过时了\"。",
        "inputSchema": {
            "type": "object", "additionalProperties": False,
            "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 10,
                                     "description": "每类返回条数"}},
            "required": [],
        },
    },
    {
        "name": "mood_status",
        "description": "查看角色此刻的心情、孤独度和一句状态描述。用户问\"你心情怎么样\"时使用。",
        "inputSchema": {"type": "object", "additionalProperties": False, "properties": {}, "required": []},
    },
]


class McpServer:
    def __init__(self, home: str | None = None, out=None, project: str | None = None):
        self.home = home or resolve_home()
        self.out = out or sys.stdout
        self.project = project or project_name(os.getcwd())
        self.cwd = os.getcwd() if project is None else ""
        self.session_id = "mcp-" + str(uuid.uuid4())
        self._project_info = {}
        self.backend = Backend(home=self.home)
        self._ready = False

    # ------------------------------------------------------------------ 输出
    def _write(self, obj: dict) -> None:
        data = (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")
        buf = getattr(self.out, "buffer", None)
        if buf is not None:
            buf.write(data)
            buf.flush()
        else:                                 # 测试里可以直接塞 StringIO
            self.out.write(data.decode("utf-8"))
            self.out.flush()

    def _result(self, rid, result: dict) -> None:
        self._write({"jsonrpc": "2.0", "id": rid, "result": result})

    def _error(self, rid, code: int, message: str) -> None:
        self._write({"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}})

    # ------------------------------------------------------------------ 工具
    def _ensure(self) -> tuple[bool, str]:
        if self._ready and self.backend.health():
            return True, ""
        if self.backend.ensure(start=True, wait_s=5.0) is not None:
            self._ready = True
            return True, ""
        log_path = os.path.join(self.home, "logs", "serve.log")
        return False, (f"soulcore 后端没有启动，查看 {log_path}；"
                       f"也可以手动运行 `python -m soulcore --config {os.path.join(self.home, 'config.json')} serve`。")

    def _event(self, text: str = "") -> dict:
        return {"platform": "local", "scene": "project", "channel_id": self.project,
                "user_id": "owner", "user_name": "主人", "text": text,
                "mentions_self": True, "ts": time.time(),
                "cwd": self.cwd, "project_id": self._project_info.get("project_id", ""),
                "session_id": self.session_id, "event_id": str(uuid.uuid4()),
                "raw": {"client": "mcp", "session_id": self.session_id}}

    def _resolve_project(self) -> dict:
        # Model tools must not silently fall back to a shared basename if the
        # identity endpoint is unavailable; hooks retain their optional fallback.
        event = resolve_project_event(self.backend, self._event(), timeout=2.0, strict=True)
        if event.get("project_id"):
            self._project_info = event.get("raw", {}).get("project_identity", {})
        return event

    def _search_project(self, requested: str = "") -> dict:
        current = self._resolve_project()
        if not requested:
            return current
        requested = str(requested).strip()
        info = current.get("raw", {}).get("project_identity", {})
        if requested in (self.project, current.get("channel_id"), current.get("project_id"), info.get("name")):
            if not current.get("project_id"):
                raise ValueError("无法确认当前项目身份，请省略 project 或在目标目录接入后重试")
            return current
        try:
            project_id = str(uuid.UUID(requested))
        except (ValueError, TypeError, AttributeError):
            project_id = ""
        target = {**self._event(), "cwd": "", "channel_id": requested, "project_id": project_id,
                  "raw": {"client": "mcp", "resolve_project_name": True}}
        return resolve_project_event(self.backend, target, timeout=2.0, strict=True)

    def _fmt_items(self, items) -> str:
        if not items:
            return "没有找到相关记忆。"
        now = time.time()
        lines = []
        for it in items:
            t = it.get("valid_from") or it.get("created") or now
            try:
                ago = _ago(now - float(t))
            except (TypeError, ValueError):
                ago = "以前"
            stale = "（已过时）" if it.get("invalid_at") else ""
            note = f"（注意：{it['stale_note']}）" if it.get("stale_note") else ""
            state = {"suggested": "提议", "accepted": "已接受", "implemented": "已实施",
                     "verified": "有验证证据"}.get(it.get("epistemic_state"), "")
            label = f"{it.get('kind', 'fact')}·{ago}" + ("·" + state if state else "")
            lines.append(f"#{it.get('id')} [{label}] {stale}{it.get('content', '')}{note}")
        return "\n".join(lines)

    def call_tool(self, name: str, args: dict) -> dict:
        ok, why = self._ensure()
        if not ok:
            return {"content": [{"type": "text", "text": why}], "isError": True}
        try:
            if name == "memory_search":
                q = str(args.get("query") or "").strip()
                if not q:
                    raise ValueError("query 不能为空")
                k = max(1, min(int(args.get("k") or 8), 50))
                event = self._search_project(args.get("project") or "")
                project = str(event["channel_id"])
                pid = str(event.get("project_id") or "")
                extra = quote("core/people/owner,core/self", safe="")
                hist = "1" if args.get("include_history") else "0"
                items = self.backend.request(
                    "GET", f"/v1/memory/search?q={quote(q)}&path={quote('project/' + project)}"
                           f"&project_id={quote(pid)}&extra={extra}&k={k}&history={hist}&touch=1") or []
                return {"content": [{"type": "text", "text": self._fmt_items(items)}]}
            if name == "remember":
                content = str(args.get("content") or "").strip()
                if not content:
                    raise ValueError("content 不能为空")
                kind = args.get("kind") if args.get("kind") in ("fact", "preference", "decision", "todo") else "fact"
                r = self.backend.request("POST", "/v1/memory/remember",
                                         {"event": self._event(content), "content": content, "kind": kind}) or {}
                if r.get("ok") and r.get("id"):
                    return {"content": [{"type": "text", "text": f"已记住（{kind}），id={r['id']}：{content}"}]}
                return {"content": [{"type": "text",
                                     "text": "没有写入（可能与已有记忆重复，已合并）。"}]}
            if name in ("forget", "restore"):
                iid = args.get("id")
                r = self.backend.request("POST", f"/v1/memory/{name}", {"id": iid}) or {}
                if name == "forget":
                    return {"content": [{"type": "text", "text": f"已忘掉 #{iid}。"}]}
                return {"content": [{"type": "text", "text": f"已恢复 #{iid}：{r.get('item', {}).get('content', '')}"}]}
            if name == "memory_timeline":
                limit = max(1, min(int(args.get("limit") or 10), 100))
                t = self.backend.request("GET", f"/v1/memory/timeline?limit={limit}") or {}
                lines = ["最近记的："]
                recent = t.get("recent") or []
                lines += self._fmt_items(recent).splitlines() if recent else ["（还没有）"]
                sup = t.get("superseded") or []
                if sup:
                    lines.append("")
                    lines.append("最近被取代的：")
                    for x in sup:
                        old = x.get("old") or {}
                        new = (x.get("new") or {}).get("content", "")
                        lines.append(f"- #{old.get('id')} {old.get('content', '')} → {new or '（作废）'}")
                return {"content": [{"type": "text", "text": "\n".join(lines)}]}
            if name == "mood_status":
                st = self.backend.request("GET", "/v1/state") or {}
                tag = st.get("label") or ""
                return {"content": [{"type": "text", "text": (
                    f"{st.get('describe', '')}（心情 {st.get('mood')}，标签「{tag}」，"
                    f"孤独度 {st.get('loneliness')}，基线 {st.get('baseline')}）")}]}
        except BackendUnavailable as ex:
            self._ready = False
            return {"content": [{"type": "text", "text": f"soulcore 后端连不上：{ex}"}], "isError": True}
        except ValueError as ex:
            return {"content": [{"type": "text", "text": str(ex)}], "isError": True}
        return {"content": [{"type": "text", "text": f"未知工具：{name}"}], "isError": True}

    # ------------------------------------------------------------------ 协议
    def handle(self, msg: dict) -> None:
        rid = msg.get("id")
        method = str(msg.get("method") or "")
        params = msg.get("params") or {}
        if rid is None:                        # 通知：没有 id，不回复
            return
        if method == "initialize":
            want = str(params.get("protocolVersion") or "")
            version = want if want in SUPPORTED_VERSIONS else LATEST_VERSION
            self._result(rid, {
                "protocolVersion": version,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "soulcore", "version": __version__},
            })
            return
        if method == "ping":
            self._result(rid, {})
            return
        if method == "tools/list":
            self._result(rid, {"tools": TOOLS})
            return
        if method == "tools/call":
            name = str(params.get("name") or "")
            args = params.get("arguments") or {}
            if not isinstance(args, dict):
                self._error(rid, -32602, "arguments 必须是对象")
                return
            self._result(rid, self.call_tool(name, args))
            return
        self._error(rid, -32601, f"Method not found: {method}")

    def run(self, stream=None) -> None:
        stream = stream or sys.stdin
        buf = getattr(stream, "buffer", None)
        while True:
            if buf is not None:
                line = buf.readline()
                if not line:
                    break
                raw = line.decode("utf-8", errors="replace")
            else:
                raw = stream.readline()
                if not raw:
                    break
            raw = raw.strip()
            if not raw:
                continue
            try:
                msg = json.loads(raw)
            except ValueError:
                self._error(None, -32700, "Parse error")
                continue
            if not isinstance(msg, dict):
                self._error(None, -32600, "Invalid Request")
                continue
            try:
                self.handle(msg)
            except Exception as ex:            # noqa: BLE001 —— 单个请求失败不能拖垮整个 server
                log.exception("处理 MCP 请求失败")
                self._error(msg.get("id"), -32603, f"Internal error: {ex}")


def main() -> int:
    McpServer().run()
    return 0


if __name__ == "__main__":                 # pragma: no cover
    sys.exit(main())
