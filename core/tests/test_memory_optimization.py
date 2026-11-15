"""End-to-end integrity checks for the memory/cost integration, entirely offline."""
import copy
import os
import tempfile
import unittest
from types import SimpleNamespace

from soulcore.budget import select_memories, split_extraction_batches
from soulcore.config import DEFAULTS
from soulcore.engine import Engine
from soulcore.events import Event
from soulcore.extractor import compact_turn, format_transcript
from soulcore.llm import MockLLM


class RecordingLLM(MockLLM):
    def __init__(self):
        super().__init__(seed=7)
        self.jobs = []
        self.fail = False
        self.reply_calls = []

    def chat_json(self, messages, **kwargs):
        self.jobs.append(messages)
        if self.fail and "TASK:EXTRACT" in messages[0]["content"]:
            raise RuntimeError("temporary extractor outage")
        return super().chat_json(messages, **kwargs)

    def chat(self, messages, **kwargs):
        self.reply_calls.append(kwargs)
        return "好的，我会完成任务。"


class OptimizationIntegration(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.now = 1800000000.0
        cfg = copy.deepcopy(DEFAULTS)
        cfg["data_dir"] = self.temp.name
        cfg["memory"]["extract_every"] = 1000
        cfg["llm"]["max_tokens"] = 700
        self.llm = RecordingLLM()
        self.engine = Engine(cfg, llm=self.llm, clock=lambda: self.now)

    def tearDown(self):
        self.engine.close()
        self.temp.cleanup()

    def event(self, text, **kwargs):
        return Event("local", "project", "demo", "owner", "主人", text,
                     ts=self.now, session_id="session-A", **kwargs)

    def test_compaction_saves_full_sources_and_exposes_excerpt_coverage(self):
        original = "  开始\n" + "普通材料。" * 500 + "\n必须保留取消的方案，原因是部署受限。\n" + "其他材料。" * 500 + "\n结束  "
        ev = self.event("")
        self.engine.pre_compact(ev, [{"role": "user", "id": "m1", "content": original}])
        row = self.engine.store.db.execute("SELECT * FROM items WHERE kind='turn'").fetchone()
        self.assertEqual(row["content"], original)
        jobs = [m for m in self.llm.jobs if "TASK:EXTRACT" in m[0]["content"]]
        self.assertIn("必须保留取消的方案", jobs[0][1]["content"])
        self.assertIn(f"来源编号#{row['id']}", jobs[0][1]["content"])
        status = self.engine.store.coverage(ev.path)
        self.assertEqual(status["backlog_turns"], 0)
        self.assertEqual(status["recent_omissions"][0]["original_chars"], len(original))
        before = self.engine.store.db.execute("SELECT COUNT(*) FROM items WHERE kind='turn'").fetchone()[0]
        self.engine.pre_compact(ev, [{"role": "user", "id": "m1", "content": original}])
        self.assertEqual(self.engine.store.db.execute("SELECT COUNT(*) FROM items WHERE kind='turn'").fetchone()[0], before)

    def test_failed_extraction_keeps_backlog_and_retry_references_sources(self):
        ev = self.event("我们决定使用 SQLite，因为部署要轻", event_id="e1")
        self.engine.observe(ev)
        self.llm.fail = True
        with self.assertRaises(RuntimeError):
            self.engine.flush_event(ev)
        self.assertEqual(self.engine.store.coverage(ev.path)["covered_id"], 0)
        self.assertEqual(self.engine.store.coverage(ev.path)["backlog_turns"], 1)
        self.llm.fail = False
        self.engine.flush_event(ev)
        self.assertEqual(self.engine.store.coverage(ev.path)["backlog_turns"], 0)
        notes = self.engine.store.by_path(ev.path, kinds=("decision",))
        self.assertTrue(notes)
        self.assertTrue(notes[0].source_ids)

    def test_same_name_projects_have_independent_extraction_watermarks(self):
        cwd_a, cwd_b = os.path.join(self.temp.name, "a", "demo"), os.path.join(self.temp.name, "b", "demo")
        os.makedirs(cwd_a)
        os.makedirs(cwd_b)
        a = self.event("我们决定使用 SQLiteA，因为只做离线", cwd=cwd_a, event_id="a1")
        b = self.event("我们决定使用 PostgresB，因为需要联网", cwd=cwd_b, event_id="b1")
        self.engine.observe(a)
        self.engine.observe(b)
        self.assertNotEqual(a.project_id, b.project_id)
        self.assertEqual(a.path, b.path)  # Compatibility mode preserves the old path.
        self.engine.flush_event(a)
        self.assertEqual(self.engine.store.coverage(a.path, project_id=a.project_id)["backlog_turns"], 0)
        self.assertEqual(self.engine.store.coverage(b.path, project_id=b.project_id)["backlog_turns"], 1)
        self.engine.flush_event(b)
        found = self.engine.store.search("SQLiteA", b.path, project_id=b.project_id)
        self.assertFalse(any("SQLiteA" in i.content for i in found))

    def test_unchanged_profile_avoids_second_model_call(self):
        self.engine.store.write("core/people/owner", "fact", "主人学习自动化", .9)
        self.assertTrue(self.engine.reflect(force=True))
        calls = sum("TASK:PROFILE" in m[0]["content"] for m in self.llm.jobs)
        self.assertFalse(self.engine.reflect(force=True))
        self.assertEqual(sum("TASK:PROFILE" in m[0]["content"] for m in self.llm.jobs), calls)

    def test_low_mood_does_not_reduce_project_answer_budget(self):
        from soulcore.emotion import logit
        self.engine.emo.x = logit(.05)
        reply = self.engine.handle(self.event("请修正这个测试失败的问题"))
        self.assertTrue(reply)
        self.assertEqual(self.llm.reply_calls[-1]["max_tokens"], 700)
        self.assertIsNone(self.engine.reply_policy(self.event("")))

    def test_typed_tool_evidence_is_saved_separately_from_dialogue(self):
        ev = self.event("")
        self.engine.pre_compact(ev, [
            {"role": "user", "content": "我们决定先验证测试", "id": "u"},
            {"role": "tool", "content": "Ran 8 tests in 0.010s\n\nOK", "id": "t",
             "evidence": {"origin": "tool_result", "tool_call_id": "call1", "command": "python -m unittest",
                          "exit_code": 0}},
        ])
        row = self.engine.store.db.execute("SELECT * FROM items WHERE kind='evidence'").fetchone()
        self.assertEqual(row["evidence_type"], "verification")
        self.assertEqual(len(self.engine.store.recent_turns(ev.path, 12)), 1)
        jobs = [m for m in self.llm.jobs if "TASK:EXTRACT" in m[0]["content"]]
        self.assertIn("工具证据", jobs[0][1]["content"])

    def test_rescued_repeated_messages_without_ids_remain_distinct(self):
        ev = self.event("")
        self.engine.pre_compact(ev, [{"role": "user", "content": "你好"},
                                    {"role": "user", "content": "你好"}])
        rows = self.engine.store.db.execute("SELECT id,content FROM items WHERE kind='turn'").fetchall()
        self.assertEqual(len(rows), 2)
        self.assertNotEqual(rows[0]["id"], rows[1]["id"])


class ElasticBudget(unittest.TestCase):
    def test_critical_records_survive_tiny_budget_as_whole_records(self):
        decision = SimpleNamespace(id=1, content="必须使用 SQLite。" * 100, kind="decision", pinned=False, invalid_at=None)
        irrelevant = SimpleNamespace(id=2, content="闲聊历史" * 100, kind="fact", pinned=False, invalid_at=None)
        selected, status = select_memories([irrelevant, decision], 20)
        self.assertEqual(selected, [decision])
        self.assertTrue(status["critical_overflow"])
        self.assertEqual(status["omitted_ids"], [2])

    def test_extraction_splits_rounds_without_losing_or_reordering_sources(self):
        turns = [SimpleNamespace(id=i + 1, role="user" if i % 2 == 0 else "assistant",
                                 content="讨论材料" * 70, created=float(i), speaker="主人") for i in range(6)]
        batches = split_extraction_batches(turns, format_transcript, max_tokens=1000)
        self.assertEqual([t.id for batch in batches for t in batch], list(range(1, 7)))
        self.assertTrue(all([t.role for t in batch] == ["user", "assistant"] for batch in batches))

    def test_excerpt_keeps_middle_constraint_and_failure_reason(self):
        text = "描述。" * 500 + "必须保证项目正常跟进；失败原因是凭证过期。" + "描述。" * 500
        self.assertIn("失败原因是凭证过期", compact_turn(text, "assistant", 300))


if __name__ == "__main__":
    unittest.main()
