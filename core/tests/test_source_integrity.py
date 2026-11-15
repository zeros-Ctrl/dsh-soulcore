"""Regression tests for actual source scope and retryable extraction failures."""
import copy
import os
import re
import tempfile
import unittest

from soulcore.config import DEFAULTS
from soulcore.engine import Engine
from soulcore.events import Event
from soulcore.llm import MockLLM


class SourceLLM(MockLLM):
    def __init__(self):
        super().__init__()
        self.jobs = []
        self.bad_shape = None

    def chat_json(self, messages, **kwargs):
        if "TASK:EXTRACT" not in messages[0]["content"]:
            return super().chat_json(messages, **kwargs)
        self.jobs.append(messages[1]["content"])
        if self.bad_shape is not None:
            return self.bad_shape
        text = messages[1]["content"]
        sources = [int(i) for i in re.findall(r"来源编号#(\d+)", text)]
        original = re.search(r"\[来源编号#\d+\][^\n]*?: (.+)", text).group(1)
        evidence = [int(i) for i in re.findall(r"^#(\d+) verification ", text, re.M)]
        return {"memories": [{"kind": "decision", "content": original,
                               "about": "project", "importance": .9, "source_ids": [sources[0]],
                               "evidence_ids": evidence,
                               "epistemic_state": "verified" if evidence else "accepted"}]}


class SourceIntegrity(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.now = 1800000000.0
        cfg = copy.deepcopy(DEFAULTS)
        cfg["data_dir"] = self.temp.name
        cfg["memory"]["extract_every"] = 1000
        self.llm = SourceLLM()
        self.engine = Engine(cfg, llm=self.llm, clock=lambda: self.now)

    def tearDown(self):
        self.engine.close()
        self.temp.cleanup()

    def event(self, text="", sid="A", event_id="", offset=0):
        return Event("local", "project", "demo", "owner", "主人", text,
                     session_id=sid, event_id=event_id, ts=self.now + offset)

    def proof(self, ev, event_id="proof", user_source=""):
        evidence = {"origin": "tool_result", "tool_call_id": event_id,
                    "command": "python -m unittest", "exit_code": 0}
        if user_source:
            evidence["source_user_event_id"] = user_source
        return self.engine.store.append_turn(ev, "tool", "Ran 3 tests in 0.010s\n\nOK",
                source_event_id=event_id, evidence=evidence, audience="owner")

    def test_missing_or_nonlist_memory_output_preserves_watermark_and_retries(self):
        for bad in ({}, {"memories": "invalid"}, {"memories": None}):
            with self.subTest(bad=bad):
                ev = self.event("我们决定采用 SQLite", event_id=str(bad))
                self.engine.observe(ev)
                old = self.engine.store.coverage(ev.path)["covered_id"]
                self.llm.bad_shape = bad
                with self.assertRaises(ValueError):
                    self.engine.flush_event(ev)
                self.assertEqual(self.engine.store.coverage(ev.path)["covered_id"], old)
                self.assertGreater(self.engine.store.coverage(ev.path)["backlog_turns"], 0)
                self.llm.bad_shape = None
                self.engine.flush_event(ev)
                self.assertEqual(self.engine.store.coverage(ev.path)["backlog_turns"], 0)

    def test_legitimate_empty_memory_list_commits_coverage(self):
        ev = self.event("我们决定采用 SQLite", event_id="e")
        self.engine.observe(ev)
        self.llm.bad_shape = {"memories": []}
        self.engine.flush_event(ev)
        self.assertEqual(self.engine.store.coverage(ev.path)["backlog_turns"], 0)

    def test_historical_sessions_keep_their_identity_and_do_not_borrow_tests(self):
        a = self.event("A会话决定 SQLite 存储采用 WAL 模式并保持离线", sid="A", event_id="a", offset=-100)
        b = self.event("B会话决定界面播放帧动画，间隔为两秒", sid="B", event_id="b")
        self.engine.observe(a)
        self.engine.observe(b)
        self.proof(b, "proof-b", user_source="b")
        self.engine.flush_event(b)
        self.assertEqual(len(self.llm.jobs), 2)
        self.assertNotIn("[工具证据]", self.llm.jobs[0])
        self.assertIn("[工具证据]", self.llm.jobs[1])
        rows = self.engine.store.db.execute("SELECT * FROM items WHERE kind='decision' ORDER BY id").fetchall()
        self.assertEqual([r["session_id"] for r in rows], ["A", "B"])
        self.assertEqual([r["epistemic_state"] for r in rows], ["accepted", "verified"])

    def test_later_round_proof_does_not_verify_earlier_chunk(self):
        a = self.event("第一轮决定采用 SQLite", event_id="a", offset=-100)
        b = self.event("换个话题，第二轮决定采用 Postgres", event_id="b")
        self.engine.observe(a)
        self.engine.observe(b)
        self.proof(b, "proof-b", user_source="b")
        self.engine.flush_event(b)
        self.assertEqual(len(self.llm.jobs), 2)
        self.assertNotIn("[工具证据]", self.llm.jobs[0])
        self.assertIn("[工具证据]", self.llm.jobs[1])

    def test_explicit_tool_edge_survives_late_rescue_and_sets_verification_time(self):
        a = self.event("第一轮决定采用 SQLite", event_id="a", offset=-100)
        b = self.event("换个话题，第二轮决定采用 Postgres", event_id="b", offset=-50)
        self.engine.observe(a)
        self.engine.observe(b)
        self.proof(self.event(sid="A"), "proof-a", user_source="a")
        self.engine.flush_event(b)
        self.assertIn("[工具证据]", self.llm.jobs[0])
        self.assertNotIn("[工具证据]", self.llm.jobs[1])
        rows = self.engine.store.db.execute("SELECT * FROM items WHERE kind='decision' ORDER BY id").fetchall()
        self.assertEqual(rows[0]["epistemic_state"], "verified")
        self.assertEqual(rows[0]["valid_from"], self.now)
        self.assertEqual(rows[1]["valid_from"], self.now - 50)

    def test_unknown_session_cannot_provide_verification_evidence(self):
        ev = self.event("旧会话决定采用 SQLite", sid="", event_id="old")
        self.engine.observe(ev)
        self.proof(ev, "proof-old", user_source="old")
        self.engine.flush_event(ev)
        self.assertNotIn("[工具证据]", self.llm.jobs[0])

    def test_tool_remember_is_suggested_and_explicit_adoption_is_accepted(self):
        ev = self.event(event_id="tool")
        model = self.engine.remember(ev, "可以考虑采用 SQLite", kind="decision")
        accepted_ev = self.event("记住，我们已经决定采用 Postgres", event_id="accepted")
        self.assertEqual(self.engine._control(accepted_ev), "记住了。")
        rejected_ev = self.event("记住，我们还没有决定采用 Redis", event_id="not-accepted")
        self.engine._control(rejected_ev)
        rows = self.engine.store.db.execute("SELECT * FROM items WHERE kind='decision'").fetchall()
        by_content = {r["content"]: r["epistemic_state"] for r in rows}
        self.assertEqual(self.engine.store.db.execute("SELECT epistemic_state FROM items WHERE id=?", (model,)).fetchone()[0], "suggested")
        self.assertEqual(by_content["我们已经决定采用 Postgres"], "accepted")
        self.assertEqual(by_content["我们还没有决定采用 Redis"], "suggested")
        text = self.engine._memory_style_text(self.engine.store.by_path(ev.path, kinds=("decision",)))
        self.assertIn("提议", text)
        self.assertIn("已接受", text)


if __name__ == "__main__":
    unittest.main()
