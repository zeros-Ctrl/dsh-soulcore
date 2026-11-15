"""Persistent identity, provenance and coverage checks using real temporary SQLite stores."""
import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from soulcore.events import Event
from soulcore.memory import MemoryStore


class MemoryFoundationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = MemoryStore(os.path.join(self.temp.name, "data"))
        self.addCleanup(lambda: self.store.close())

    def event(self, directory="a", **kwargs):
        cwd = os.path.join(self.temp.name, directory, "same")
        os.makedirs(cwd, exist_ok=True)
        return Event("local", "project", "same", "owner", cwd=cwd,
                     session_id=kwargs.pop("session_id", "s1"), **kwargs)

    def test_uuid_survives_restart_and_git_subdirectories(self):
        ev = self.event()
        with open(os.path.join(ev.cwd, ".git"), "w", encoding="utf-8") as handle:
            handle.write("gitdir: elsewhere\n")
        sub = os.path.join(ev.cwd, "src")
        os.makedirs(sub)
        first = self.store.resolve_project_event(ev)
        from dataclasses import replace
        self.assertEqual(first.project_id, self.store.resolve_project_event(replace(ev, cwd=sub)).project_id)
        self.store.close()
        self.store = MemoryStore(os.path.join(self.temp.name, "data"))
        self.assertEqual(first.project_id, self.store.resolve_project_event(ev).project_id)

    def test_registry_preserves_legacy_but_collision_does_not_claim_it(self):
        legacy = self.store.write("project/same", "decision", "旧项目数据库决定用 SQLite", .9)
        first = self.store.resolve_project_event(self.event("a"))
        self.assertEqual(first.path, "project/same")
        self.assertEqual(first.raw["project_identity"]["legacy_unbound"], 1)
        self.assertEqual(self.store.search("SQLite", first.path, project_id=first.project_id)[0].id, legacy)
        second = self.store.resolve_project_event(self.event("b"))
        self.assertNotEqual(first.project_id, second.project_id)
        self.assertTrue(second.raw["project_identity"]["collision"])
        self.assertEqual(self.store.search("SQLite", first.path, project_id=first.project_id), [])
        a = self.store.write(first.path, "fact", "本项目数据库选 SQLite", .9, project_id=first.project_id)
        b = self.store.write(second.path, "fact", "本项目数据库选 SQLite", .9, project_id=second.project_id)
        self.assertNotEqual(a, b, "different projects cannot deduplicate by shared basename")
        self.assertEqual([r.id for r in self.store.search("SQLite", first.path, project_id=first.project_id)], [a])
        self.assertEqual([r.id for r in self.store.search("SQLite", second.path, project_id=second.project_id)], [b])
        self.assertIsNotNone(self.store.db.execute("SELECT id FROM items WHERE id=?", (legacy,)).fetchone())

    def test_strict_mode_routes_same_names_to_separate_uuid_paths(self):
        a = self.store.resolve_project_event(self.event("a"), mode="strict")
        b = self.store.resolve_project_event(self.event("b"), mode="strict")
        self.assertNotEqual(a.path, b.path)
        self.assertEqual(a.path, "project/" + a.project_id)
        self.store.append_turn(a, "user", "项目甲的验收规则", source_event_id="1")
        self.store.append_turn(b, "user", "项目乙的验收规则", source_event_id="2")
        self.assertEqual([x.content for x in self.store.recent_turns(a.path, project_id=a.project_id)], ["项目甲的验收规则"])

    def test_source_identity_idempotence_preserves_repeated_utterances(self):
        ev = self.store.resolve_project_event(self.event())
        one = self.store.append_turn(ev, "user", "继续", source_event_id="event-1")
        replay = self.store.append_turn(ev, "user", "继续", source_event_id="event-1")
        two = self.store.append_turn(ev, "user", "继续", source_event_id="event-2")
        self.assertEqual(one, replay)
        self.assertNotEqual(one, two)
        self.assertEqual(len(self.store.recent_turns(ev.path, project_id=ev.project_id)), 2)

    def test_original_whitespace_and_fuller_source_revision_are_preserved(self):
        ev = self.store.resolve_project_event(self.event())
        original = "\n  原始代码缩进\n    return value\n"
        one = self.store.append_turn(ev, "user", original, source_event_id="event-1")
        fuller = original + "尾部还有关键约束\n"
        two = self.store.append_turn(ev, "user", fuller, source_event_id="event-1")
        self.assertNotEqual(one, two)
        rows = self.store.recent_turns(ev.path, project_id=ev.project_id)
        self.assertEqual([r.content for r in rows], [original, fuller])
        self.assertEqual(rows[-1].evidence["revision_of"], one)
        self.assertEqual(self.store.append_turn(ev, "user", fuller, source_event_id="event-1"), two)

    def test_verified_requires_selected_structured_verification_evidence(self):
        ev = self.store.resolve_project_event(self.event())
        turn = self.store.append_turn(ev, "assistant", "我已经测试通过", source_event_id="a")
        tool = self.store.append_turn(ev, "tool", "12 tests passed", source_event_id="tool-1",
                evidence={"origin": "tool_result", "tool_call_id": "call-1",
                          "command": "python -m unittest", "exit_code": 0})
        ids = [turn, tool]
        unsupported = self.store.write(ev.path, "fact", "所有用例通过", .9, project_id=ev.project_id,
                source_ids=ids, epistemic_state="verified", dedup=False)
        proved = self.store.write(ev.path, "fact", "当前十二个用例通过", .9, project_id=ev.project_id,
                source_ids=ids, epistemic_state="verified", evidence={"verification_source_ids": [tool]}, dedup=False)
        states = {r["id"]: r["epistemic_state"] for r in self.store.db.execute("SELECT * FROM items")}
        self.assertEqual(states[unsupported], "implemented")
        self.assertEqual(states[proved], "verified")
        generic = self.store.append_turn(ev, "tool", "passed", source_event_id="tool-2",
                evidence={"origin": "tool_result", "tool_call_id": "call-2", "command": "echo passed", "exit_code": 0})
        self.assertEqual(self.store.db.execute("SELECT evidence_type FROM items WHERE id=?", (generic,)).fetchone()[0], "tool")
        self.assertEqual([x.role for x in self.store.recent_turns(ev.path, project_id=ev.project_id)], ["assistant"])

    def test_forgetting_evidence_withdraws_transitive_derivatives(self):
        ev = self.store.resolve_project_event(self.event())
        source = self.store.append_turn(ev, "user", "部署只能用本地 SQLite", source_event_id="source")
        fact = self.store.write(ev.path, "decision", "决定不使用远程数据库", .9,
                                project_id=ev.project_id, source_ids=[source])
        summary = self.store.write(ev.path, "summary", "运行环境已经确定", .9,
                                   project_id=ev.project_id, source_ids=[fact])
        self.store.forget(source)
        rows = self.store.db.execute("SELECT status FROM items WHERE id IN (?,?,?)", (source, fact, summary)).fetchall()
        self.assertTrue(all(r[0] == "deleted" for r in rows))
        self.assertEqual(self.store.search("远程数据库", ev.path, project_id=ev.project_id), [])
        self.assertIsNone(self.store.write(ev.path, "fact", "不能从删除的来源继续提炼", .9,
                                          project_id=ev.project_id, source_ids=[source]))

    def test_coverage_is_per_project_monotone_and_preserves_omission_details(self):
        a = self.store.resolve_project_event(self.event("a"))
        b = self.store.resolve_project_event(self.event("b"))
        a_id = self.store.append_turn(a, "user", "甲的记录", source_event_id="a")
        b_id = self.store.append_turn(b, "user", "乙的记录", source_event_id="b")
        omitted = [{"id": a_id, "omitted_chars": 10, "source_preserved": True}]
        self.store.mark_extraction_coverage(a.path, a_id, project_id=a.project_id, omissions=omitted)
        self.store.mark_extraction_coverage(a.path, 0, project_id=a.project_id)
        coverage = self.store.coverage(a.path, project_id=a.project_id)
        self.assertEqual(coverage["covered_id"], a_id)
        self.assertEqual(coverage["backlog_turns"], 0)
        self.assertEqual(coverage["recent_omissions"], omitted)
        self.assertEqual(self.store.meta_get("extract_ptr:" + a.path + ":" + a.project_id), a_id)
        self.assertEqual(self.store.coverage(b.path, project_id=b.project_id)["backlog_turns"], 1)
        self.assertGreater(b_id, a_id)

    def test_vector_model_switch_and_bad_dimensions_fall_back_to_fts(self):
        class Embedder:
            def __init__(self, name, dimensions): self.name, self.dimensions = name, dimensions
            def embed(self, texts): return [[1.0] + [0.0] * (self.dimensions - 1) for _ in texts]
        self.store.embedder = Embedder("model-A", 2)
        item = self.store.write("project/test", "fact", "检索用 SQLite", .9)
        self.store.embedder = Embedder("model-B", 3)
        self.assertIn(item, [r.id for r in self.store.search("SQLite", "project/test")])
        self.store.db.execute("UPDATE vecs SET model='model-B' WHERE id=?", (item,))
        self.store.db.commit()
        self.assertIn(item, [r.id for r in self.store.search("SQLite", "project/test")])


class MigrationTest(unittest.TestCase):
    def test_version_six_records_survive_additive_upgrade(self):
        with tempfile.TemporaryDirectory() as temp:
            db = sqlite3.connect(os.path.join(temp, "memory.db"))
            db.execute("""CREATE TABLE items(
                id INTEGER PRIMARY KEY, path TEXT NOT NULL,kind TEXT NOT NULL,role TEXT DEFAULT '',
                speaker TEXT DEFAULT '',content TEXT NOT NULL,importance REAL NOT NULL,tau_h REAL NOT NULL,
                created REAL NOT NULL,last_access REAL NOT NULL,access_count INTEGER DEFAULT 0,status TEXT DEFAULT 'hot',
                tags TEXT DEFAULT '',user_id TEXT DEFAULT '',parent_id INTEGER,trust TEXT DEFAULT 'owner',pinned INTEGER DEFAULT 0,
                valid_from REAL,invalid_at REAL,superseded_by INTEGER,due_at REAL,stale_note TEXT,
                audience TEXT DEFAULT 'owner',source_path TEXT DEFAULT '')""")
            db.execute("INSERT INTO items(id,path,kind,content,importance,tau_h,created,last_access) "
                       "VALUES(1,'project/old','decision','旧项目采用 SQLite',.9,8760,100,100)")
            db.commit()
            db.close()
            store = MemoryStore(temp, clock=lambda: 100)
            try:
                row = store.db.execute("SELECT * FROM items WHERE id=1").fetchone()
                self.assertEqual(row["content"], "旧项目采用 SQLite")
                self.assertEqual(row["project_id"], "")
                self.assertEqual(store.meta_get("schema_version"), 7)
                self.assertEqual(store.search("SQLite", "project/old")[0].id, 1)
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
