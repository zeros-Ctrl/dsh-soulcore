"""v0.9 K3：MCP server（stdio JSON-RPC）。"""
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from soulcore.client import Backend, BackendUnavailable, resolve_home
from soulcore.mcp_server import McpServer

CORE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUN_PY = os.path.join(CORE, "run.py")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


def rpc(rid, method, params=None):
    msg = {"jsonrpc": "2.0", "id": rid, "method": method}
    if params is not None:
        msg["params"] = params
    return msg


class McpProtocolTest(Base):
    def setUp(self):
        super().setUp()
        self.home = os.path.join(self.tmp, "home")
        os.makedirs(self.home)
        with patch.dict(os.environ, {"SOULCORE_HOME": self.home}):
            resolve_home()
        self.backend = Backend(home=self.home, mock=True)
        self.backend.ensure(start=True, wait_s=20)
        self.addCleanup(self.backend.shutdown)
        self.cwd = os.path.join(self.tmp, "mcpshop")
        os.makedirs(self.cwd)

    def run_mcp(self, messages, home=None, cwd=None, timeout=90):
        data = "\n".join(json.dumps(m, ensure_ascii=False) for m in messages) + "\n"
        env = {**os.environ, "SOULCORE_HOME": home or self.home,
               "PYTHONUTF8": "1", "PYTHONDONTWRITEBYTECODE": "1"}
        p = subprocess.run([sys.executable, "-I", RUN_PY, "mcp"], input=data, text=True,
                           capture_output=True, encoding="utf-8", timeout=timeout,
                           env=env, cwd=cwd or self.cwd)
        lines = [ln for ln in p.stdout.splitlines() if ln.strip()]
        parsed = [json.loads(ln) for ln in lines]      # 任何非 JSON 行都会在这里炸
        return p, parsed

    def test_handshake_tools_remember_search(self):
        msgs = [
            rpc(1, "initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                                  "clientInfo": {"name": "test", "version": "0"}}),
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            rpc(2, "tools/list"),
            rpc(3, "tools/call", {"name": "remember",
                                  "arguments": {"content": "项目的检索层决定用 SQLite+FTS5，不上向量库",
                                                "kind": "decision"}}),
            rpc(4, "tools/call", {"name": "memory_search", "arguments": {"query": "检索层 向量库"}}),
            rpc(5, "ping"),
            rpc(6, "tools/call", {"name": "mood_status", "arguments": {}}),
            rpc(7, "no/such/method"),
        ]
        p, out = self.run_mcp(msgs)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(len(out), 7, f"通知不该有回复：{out}")   # 7 个带 id 的请求
        by_id = {x["id"]: x for x in out}

        init = by_id[1]["result"]
        self.assertEqual(init["protocolVersion"], "2024-11-05")
        self.assertEqual(init["capabilities"], {"tools": {}})
        self.assertEqual(init["serverInfo"]["name"], "soulcore")

        names = {t["name"] for t in by_id[2]["result"]["tools"]}
        self.assertEqual(names, {"memory_search", "remember", "forget", "restore",
                                 "memory_timeline", "mood_status"})
        for t in by_id[2]["result"]["tools"]:
            self.assertIn("inputSchema", t)
            self.assertTrue(t["description"])

        remembered = by_id[3]["result"]
        self.assertFalse(remembered.get("isError"), remembered)
        self.assertIn("已记住", remembered["content"][0]["text"])

        searched = by_id[4]["result"]
        text = searched["content"][0]["text"]
        self.assertIn("SQLite", text)
        self.assertRegex(text, r"#\d+ \[decision·")

        self.assertEqual(by_id[5]["result"], {})
        self.assertIn("心情", by_id[6]["result"]["content"][0]["text"])
        self.assertEqual(by_id[7]["error"]["code"], -32601)

    def test_unknown_protocol_version_gets_latest(self):
        _, out = self.run_mcp([rpc(1, "initialize", {"protocolVersion": "9999-01-01"})])
        self.assertEqual(out[0]["result"]["protocolVersion"], "2025-06-18")

    def test_timeline_forget_restore(self):
        msgs = [
            rpc(1, "tools/call", {"name": "remember", "arguments": {"content": "项目用 MySQL 做数据库", "kind": "fact"}}),
            rpc(2, "tools/call", {"name": "memory_timeline", "arguments": {"limit": 5}}),
        ]
        _, out = self.run_mcp(msgs)
        body = out[0]["result"]["content"][0]["text"]
        iid = int(body.split("id=")[1].split("：")[0])
        timeline = out[1]["result"]["content"][0]["text"]
        self.assertIn("MySQL", timeline)

        _, out = self.run_mcp([rpc(3, "tools/call", {"name": "forget", "arguments": {"id": iid}})])
        self.assertIn("已忘掉", out[0]["result"]["content"][0]["text"])
        _, out = self.run_mcp([rpc(4, "tools/call", {"name": "memory_search", "arguments": {"query": "MySQL 数据库"}})])
        self.assertNotIn("项目用 MySQL 做数据库", out[0]["result"]["content"][0]["text"])
        _, out = self.run_mcp([rpc(5, "tools/call", {"name": "restore", "arguments": {"id": iid}})])
        self.assertIn("已恢复", out[0]["result"]["content"][0]["text"])

    def test_project_uses_cwd_name(self):
        _, out = self.run_mcp([
            rpc(1, "tools/call", {"name": "remember", "arguments": {"content": "这个项目用 SQLite", "kind": "fact"}}),
            rpc(2, "tools/call", {"name": "memory_search", "arguments": {"query": "SQLite"}}),
        ])
        self.assertIn("SQLite", out[1]["result"]["content"][0]["text"])
        items = self.backend.request("GET", "/v1/memory/search?q=SQLite&path=project/mcpshop&touch=0")
        self.assertTrue(any("SQLite" in x["content"] for x in items), items)


class FakeDownBackend:
    def ensure(self, start=True, wait_s=0):
        return None

    def health(self):
        return None

    def request(self, *a, **kw):
        raise BackendUnavailable("down")


class FakeOkBackend(FakeDownBackend):
    def __init__(self):
        self.calls = []

    def ensure(self, start=True, wait_s=0):
        return ("http://127.0.0.1:1", "t")

    def health(self):
        return {"ok": True}

    def request(self, method, path, body=None, timeout=5.0):
        self.calls.append((method, path, body))
        return []


class McpUnitTest(Base):
    def server(self):
        return McpServer(home=self.tmp, out=io.StringIO(), project="demo")

    def test_backend_down_returns_is_error(self):
        srv = self.server()
        srv.backend = FakeDownBackend()
        r = srv.call_tool("memory_search", {"query": "x"})
        self.assertTrue(r["isError"])
        self.assertIn("serve.log", r["content"][0]["text"])

    def test_empty_query_is_tool_error(self):
        srv = self.server()
        srv.backend = FakeOkBackend()
        r = srv.call_tool("memory_search", {"query": "   "})
        self.assertTrue(r["isError"])
        self.assertIn("query 不能为空", r["content"][0]["text"])

    def test_parse_error(self):
        srv = self.server()
        srv.run(io.StringIO("{这不是 JSON\n"))
        out = json.loads(srv.out.getvalue().strip().splitlines()[0])
        self.assertEqual(out["error"]["code"], -32700)

    def test_notification_no_reply(self):
        srv = self.server()
        srv.run(io.StringIO(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n"))
        self.assertEqual(srv.out.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
