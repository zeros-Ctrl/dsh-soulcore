"""Verification claims survive only valid evidence, edits and explicit identity."""
import io
import json
import os
import tempfile
import unittest
from urllib.parse import parse_qs, urlparse

from soulcore.cc_hook import _fmt_memory
from soulcore.events import Event
from soulcore.memory import MemoryStore
from soulcore.mcp_server import McpServer


class VerificationIntegrityTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.now = 1000.0
        self.store = MemoryStore(os.path.join(self.temp.name, "data"), clock=lambda: self.now)
        self.addCleanup(self.store.close)
        self.ev = Event("local", "project", "test", "owner", session_id="session-1", ts=self.now)

    def row(self, iid):
        return self.store.db.execute("SELECT * FROM items WHERE id=?", (iid,)).fetchone()

    def tool(self, command, output="12 tests passed", **overrides):
        evidence = {"origin": "tool_result", "tool_call_id": command,
                    "command": command, "exit_code": 0, **overrides}
        return self.store.append_turn(self.ev, "tool", output, evidence=evidence)

    def proved(self):
        tool = self.tool("python -m unittest discover -s tests")
        fact = self.store.write(self.ev.path, "decision", "当前十二个用例已经通过", .9,
                                source_ids=[tool], epistemic_state="verified",
                                evidence={"verification_source_ids": [tool]})
        self.assertEqual(self.row(fact)["epistemic_state"], "verified")
        return tool, fact

    def sync_decisions(self, replace_text=None):
        filename = os.path.join(self.store.md_root, "project", "test", "decisions.md")
        if replace_text:
            with open(filename, encoding="utf-8") as handle:
                content = handle.read()
            with open(filename, "w", encoding="utf-8") as handle:
                handle.write(content.replace(*replace_text))
        self.now += 60
        stats = {"updated": 0, "added": 0, "deleted": 0}
        self.store._sync_file(filename, self.ev.path, ("decision",), self.now, stats)
        return filename

    def test_real_supported_test_executions_have_verification_evidence(self):
        cases = (
            ("python -m unittest discover -s tests", "Ran 12 tests in 0.2s\n\nOK\n"),
            ('"C:\\Python\\python.exe" -m unittest discover', "Ran 1 test in 1s\nOK\n"),
            ("pytest tests/test_api.py -q", "12 passed in 0.1s"),
            ("py -3 -m pytest", "12 tests passed"),
            ("node --test tests/a.mjs", "# tests 2\n# pass 2\n# fail 0\n"),
            ("node --test tests/a.mjs", "ℹ tests 2\nℹ pass 2\nℹ fail 0\n"),
            ("npm run test", "Tests: 12 passed, 12 total"),
            ("ctest -V", "100% tests passed, 0 tests failed out of 4"),
        )
        for command, output in cases:
            with self.subTest(command=command):
                self.assertEqual(self.row(self.tool(command, output))["evidence_type"], "verification")

    def test_search_prose_collect_only_and_mixed_commands_never_verify(self):
        commands = ("rg pytest package.json", "echo pytest", "echo 12 tests passed", "I ran pytest",
                    "python -c print('pytest')", "pytest --collect-only", "pytest --version", "pytest -V",
                    "python -m unittest --help", "node --test --eval fake", "ctest -N",
                    "pytest tests; echo done", "pytest tests || true", "pytest tests && echo done",
                    "pytest tests\nGet-Content result.txt")
        for command in commands:
            with self.subTest(command=command):
                iid = self.tool(command)
                self.assertEqual(self.row(iid)["evidence_type"], "tool")
                claim = self.store.write(self.ev.path, "fact", "本次测试完成：" + command, .9,
                    source_ids=[iid], epistemic_state="verified", evidence={"verification_source_ids": [iid]}, dedup=False)
                self.assertEqual(self.row(claim)["epistemic_state"], "implemented")

    def test_zero_tests_failures_and_unsupported_summary_never_verify(self):
        for output in ("All tests passed", "Ran 0 tests in 0s\nOK\n", "12 passed, 1 failed",
                       "# tests 0\n# pass 0\n# fail 0\n", "12 tests passed\nFAILED (errors=1)"):
            with self.subTest(output=output):
                self.assertEqual(self.row(self.tool("pytest tests", output))["evidence_type"], "tool")
        self.assertEqual(self.row(self.tool("pytest tests", exit_code=1))["evidence_type"], "tool")

    def test_old_misclassified_tool_evidence_is_revalidated(self):
        iid = self.tool("rg pytest package.json")
        self.store.db.execute("UPDATE items SET evidence_type='verification' WHERE id=?", (iid,))
        metadata = json.loads(self.row(iid)["evidence"])
        metadata["outcome"] = "passed"
        self.store.db.execute("UPDATE items SET evidence=? WHERE id=?", (json.dumps(metadata), iid))
        self.store.db.commit()
        claim = self.store.write(self.ev.path, "fact", "不能因查到测试名称而声称通过", .9,
                    source_ids=[iid], epistemic_state="verified", evidence={"verification_source_ids": [iid]})
        self.assertEqual(self.row(claim)["epistemic_state"], "implemented")

    def test_manual_content_edit_demotes_claim_and_clears_old_provenance(self):
        _tool, fact = self.proved()
        derived = self.store.write(self.ev.path, "summary", "验收状态已经确定", .9, source_ids=[fact])
        self.sync_decisions(("当前十二个用例已经通过", "新方案尚未测试"))
        row = self.row(fact)
        self.assertEqual(row["epistemic_state"], "accepted")
        self.assertEqual(json.loads(row["evidence"]), {})
        self.assertEqual(json.loads(row["source_ids"]), [])
        self.assertEqual(self.row(derived)["status"], "deleted")

    def test_manual_kind_edit_requires_reverification(self):
        _tool, fact = self.proved()
        self.sync_decisions(("[decision]", "[fact]"))
        self.assertEqual(self.row(fact)["kind"], "fact")
        self.assertEqual(self.row(fact)["epistemic_state"], "accepted")

    def test_restore_rejects_deleted_sources(self):
        tool, fact = self.proved()
        self.store.forget(tool)
        self.assertIsNone(self.store.restore(fact))
        self.assertEqual(self.row(fact)["status"], "deleted")
        self.assertIsNotNone(self.store.restore(tool))
        self.assertEqual(self.store.restore(fact)["epistemic_state"], "verified")

    def test_restore_rechecks_selected_tool_result(self):
        tool, fact = self.proved()
        self.store.forget(fact)
        metadata = json.loads(self.row(tool)["evidence"])
        metadata["exit_code"] = 1
        self.store.db.execute("UPDATE items SET evidence=? WHERE id=?", (json.dumps(metadata), tool))
        self.store.db.commit()
        self.assertIsNone(self.store.restore(fact))
        self.assertEqual(self.row(fact)["status"], "deleted")

    def test_markdown_cannot_restore_unchanged_claim_with_deleted_source(self):
        tool, fact = self.proved()
        filename = os.path.join(self.store.md_root, "project", "test", "decisions.md")
        with open(filename, encoding="utf-8") as handle:
            original = handle.read()
        self.store.forget(tool)
        with open(filename, "w", encoding="utf-8") as handle:
            handle.write(original)
        self.sync_decisions()
        self.assertEqual(self.row(fact)["status"], "deleted")


class StoreBackend:
    def __init__(self, store):
        self.store = store
        self.calls = []

    def health(self):
        return {"ok": True}

    def ensure(self, **_kwargs):
        return ("http://local", "test")

    def request(self, method, url, body=None, **_kwargs):
        self.calls.append((method, url, body))
        if url == "/v1/projects/resolve":
            event = self.store.resolve_project_event(Event.from_dict(body["event"]))
            return {**event.raw.get("project_identity", {}), "project_id": event.project_id,
                    "channel_id": event.channel_id, "path": event.path}
        parsed = urlparse(url)
        if parsed.path == "/v1/memory/search":
            query = parse_qs(parsed.query)
            rows = self.store.search(query["q"][0], query["path"][0],
                                     project_id=query.get("project_id", [""])[0], touch=False)
            return [self.store.brief(self.store.db.execute("SELECT * FROM items WHERE id=?", (row.id,)).fetchone()) for row in rows]
        return {}


class ExplicitProjectIdentityTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = MemoryStore(os.path.join(self.temp.name, "data"))
        self.addCleanup(self.store.close)
        self.paths = [os.path.join(self.temp.name, letter, "same") for letter in ("a", "b")]
        for path in self.paths:
            os.makedirs(path)
        self.a, self.b = [self.store.resolve_project_event(Event("local", "project", "same", "owner", cwd=path)) for path in self.paths]
        self.store.write(self.a.path, "fact", "release token alpha", .9, project_id=self.a.project_id)
        self.store.write(self.b.path, "fact", "release token beta", .9, project_id=self.b.project_id)
        self.server = McpServer(home=self.temp.name, out=io.StringIO(), project="same")
        self.server.cwd = self.paths[0]
        self.server.backend = StoreBackend(self.store)

    def test_explicit_current_name_retains_current_uuid(self):
        result = self.server.call_tool("memory_search", {"query": "release token", "project": "same"})
        self.assertFalse(result.get("isError", False))
        text = result["content"][0]["text"]
        self.assertIn("alpha", text)
        self.assertNotIn("beta", text)
        url = [url for method, url, _body in self.server.backend.calls if method == "GET"][-1]
        self.assertEqual(parse_qs(urlparse(url).query)["project_id"], [self.a.project_id])

    def test_explicit_other_uuid_is_a_deliberate_target(self):
        result = self.server.call_tool("memory_search", {"query": "release token", "project": self.b.project_id})
        self.assertIn("beta", result["content"][0]["text"])
        self.assertNotIn("alpha", result["content"][0]["text"])

    def test_unique_registered_name_resolves_to_its_uuid(self):
        cwd = os.path.join(self.temp.name, "unique")
        os.makedirs(cwd)
        other = self.store.resolve_project_event(Event("local", "project", "unique", "owner", cwd=cwd))
        self.store.write(other.path, "fact", "release token gamma", .9, project_id=other.project_id)
        result = self.server.call_tool("memory_search", {"query": "release token", "project": "unique"})
        self.assertIn("gamma", result["content"][0]["text"])
        self.assertNotIn("alpha", result["content"][0]["text"])

    def test_identity_resolution_failure_never_falls_back_to_basename_search(self):
        original = self.server.backend.request
        def missing_identity(method, url, body=None, **kwargs):
            if url == "/v1/projects/resolve":
                return {}
            return original(method, url, body, **kwargs)
        self.server.backend.request = missing_identity
        result = self.server.call_tool("memory_search", {"query": "release token"})
        self.assertTrue(result.get("isError", False))
        self.assertFalse(any(method == "GET" for method, _url, _body in self.server.backend.calls))

    def test_ambiguous_name_from_other_current_project_is_rejected(self):
        other = os.path.join(self.temp.name, "other")
        os.makedirs(other)
        self.server.cwd = other
        self.server.project = "other"
        self.server._project_info = {}
        result = self.server.call_tool("memory_search", {"query": "release token", "project": "same"})
        self.assertTrue(result.get("isError", False))
        self.assertIn("歧义", result["content"][0]["text"])
        self.assertFalse(any(method == "GET" for method, _url, _body in self.server.backend.calls))

    def test_metadata_states_are_visible_to_models(self):
        states = ("suggested", "accepted", "implemented", "verified")
        labels = ("提议", "已接受", "已实施", "有验证证据")
        items = [{"id": i, "kind": "decision", "content": "方案", "epistemic_state": state} for i, state in enumerate(states)]
        for output in (_fmt_memory(items), self.server._fmt_items(items)):
            for label in labels:
                self.assertIn(label, output)


if __name__ == "__main__":
    unittest.main()
