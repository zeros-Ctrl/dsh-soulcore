"""HTTP boundary tests invoke handlers directly: no socket or model calls."""
import io
import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from urllib.parse import urlencode

from soulcore.events import Event
from soulcore.memory import MemoryStore
from soulcore.server import make_handler
from soulcore.usage import UsageLedger


class UsageHttpTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ledger = UsageLedger(self.tmp.name, clock=lambda: 100)
        self.store = MemoryStore(self.tmp.name, clock=lambda: 100)
        self.engine = SimpleNamespace(usage=self.ledger, store=self.store, mcfg={"project_identity": "registry"})
        self.handler = make_handler(self.engine, token="test-token")

    def tearDown(self):
        self.ledger.close()
        self.store.close()
        self.tmp.cleanup()

    def request(self, method, path, body=None, authorized=True):
        payload = json.dumps(body).encode() if body is not None else b""
        handler = self.handler.__new__(self.handler)
        handler.path = path
        handler.headers = {"Content-Length": str(len(payload))}
        if authorized:
            handler.headers["Authorization"] = "Bearer test-token"
        handler.rfile, handler.wfile = io.BytesIO(payload), io.BytesIO()
        result = {}
        handler.send_response = lambda code: result.update(status=code)
        handler.send_header = lambda *args: None
        handler.end_headers = lambda: None
        getattr(handler, "do_" + method)()
        return result["status"], json.loads(handler.wfile.getvalue())

    def test_host_usage_and_snapshot_leave_private_content_out(self):
        status, result = self.request("POST", "/v1/usage", {"provider": "dsh", "model": "actual",
            "purpose": "host_online", "success": True, "elapsed_ms": 12,
            "messages": ["private prompt"], "reply": "private reply",
            "usage": {"prompt_tokens": 12, "completion_tokens": 7, "content": "private content",
                      "completion_tokens_details": {"reasoning_tokens": 3, "text": "private reasoning"}}})
        self.assertEqual(status, 200)
        self.assertTrue(result["recorded"])
        status, snapshot = self.request("GET", "/v1/usage")
        self.assertEqual(status, 200)
        self.assertEqual(snapshot["totals"]["input_tokens"], 12)
        self.assertEqual(snapshot["totals"]["output_tokens"], 7)
        self.assertEqual(snapshot["totals"]["billable_output_tokens"], 7)
        self.assertFalse(snapshot["cost_complete"])
        self.assertNotIn("private", "\n".join(self.ledger.db.iterdump()))
        self.assertEqual(self.request("GET", "/v1/usage?since=101")[1]["totals"]["calls"], 0)

    def test_unknown_host_usage_and_failure_are_explicit(self):
        self.request("POST", "/v1/usage", {"purpose": "host_compaction", "usage": None, "success": False,
                                          "error_type": "private error text is ignored"})
        row = self.ledger.db.execute("SELECT * FROM requests").fetchone()
        self.assertEqual(row["provider"], "dsh")
        self.assertEqual(row["model"], "unknown")
        self.assertEqual(row["usage_status"], "unavailable")
        self.assertIsNone(row["input_tokens"])
        self.assertEqual(row["error_type"], "Error")
        self.assertEqual(row["success"], 0)

    def test_usage_authorization_and_validation(self):
        self.assertEqual(self.request("POST", "/v1/usage", {}, authorized=False)[0], 401)
        self.assertEqual(self.request("GET", "/v1/usage", authorized=False)[0], 401)
        for body in ({"purpose": "arbitrary"}, {"elapsed_ms": -1}, {"success": "yes"}, []):
            self.assertEqual(self.request("POST", "/v1/usage", body)[0], 400)
        self.assertEqual(self.request("GET", "/v1/usage?since=nan")[0], 400)
        self.assertEqual(self.ledger.snapshot()["totals"]["calls"], 0)

    def test_disabled_usage_endpoint_does_not_record(self):
        self.ledger.close()
        self.ledger = UsageLedger(self.tmp.name, {"enabled": False})
        self.engine.usage = self.ledger
        status, result = self.request("POST", "/v1/usage", {"usage": {"prompt_tokens": 12}})
        self.assertEqual(status, 200)
        self.assertFalse(result["recorded"])
        self.assertFalse(result["enabled"])

    def project(self, side):
        cwd = os.path.join(self.tmp.name, side, "shared")
        os.makedirs(os.path.join(cwd, ".git"))
        event = {"platform": "local", "scene": "project", "channel_id": "shared", "user_id": "owner",
                 "session_id": side, "cwd": cwd}
        status, identity = self.request("POST", "/v1/projects/resolve", {"event": event})
        self.assertEqual(status, 200)
        return event, identity

    def test_project_identity_is_persistent_and_search_is_scoped(self):
        event_a, a = self.project("A")
        _, b = self.project("B")
        self.assertNotEqual(a["project_id"], b["project_id"])
        again = self.request("POST", "/v1/projects/resolve", {"event": event_a})[1]
        self.assertEqual(again["project_id"], a["project_id"])
        self.assertTrue(again["collision"])
        source = self.store.write(a["path"], "turn", "项目A决定使用SQLite", .8,
                                  role="user", audience="owner", project_id=a["project_id"],
                                  session_id="A", source_event_id="event-A")
        self.store.write(a["path"], "decision", "项目A使用SQLite，因为部署轻", .9, audience="owner",
                         project_id=a["project_id"], session_id="A", source_event_id="event-A",
                         source_ids=[source], epistemic_state="accepted", evidence={"reason": "部署轻"})
        self.store.write(b["path"], "decision", "项目B使用SQLite，因为运行快", .9, audience="owner",
                         project_id=b["project_id"], session_id="B", epistemic_state="suggested")
        query = urlencode({"q": "SQLite", "path": a["path"], "project_id": a["project_id"]})
        status, items = self.request("GET", "/v1/memory/search?" + query)
        self.assertEqual(status, 200)
        self.assertTrue(items)
        self.assertTrue(all(item["project_id"] == a["project_id"] for item in items))
        decision = next(item for item in items if item["kind"] == "decision")
        self.assertEqual(decision["source_ids"], [source])
        self.assertEqual(decision["epistemic_state"], "accepted")
        self.assertEqual(decision["evidence"], {"reason": "部署轻"})
        _, timeline = self.request("GET", "/v1/memory/timeline?" + urlencode({"path": a["path"], "project_id": a["project_id"]}))
        self.assertTrue(all(item["project_id"] == a["project_id"] for item in timeline["recent"]))

    def test_invalid_project_id_returns_client_error(self):
        self.assertEqual(self.request("POST", "/v1/projects/resolve", {"event": {
            "platform": "local", "scene": "project", "channel_id": "p", "user_id": "owner",
            "project_id": "not-a-uuid"}})[0], 400)


if __name__ == "__main__":
    unittest.main()
