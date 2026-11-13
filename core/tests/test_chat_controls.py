"""Focused reproductions for conversational memory lifecycle."""
import tempfile
import unittest

from test_soulcore import Clock, make_engine
from soulcore.events import Event
from soulcore.llm import MockLLM


class ChatControls(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.clock = Clock()
        self.engine, _ = make_engine(self.tmp.name, self.clock, owner={"qq": "1"})
        self.engine.mcfg["extract_every"] = 100

    def tearDown(self):
        self.engine.close()
        self.tmp.cleanup()

    def ev(self, text, uid="1", scene="private", channel=None):
        return Event("qq", scene, channel or uid, uid, "用户" + uid, text,
                     mentions_self=True, ts=self.clock())

    def test_remember_forget_clears_sources_summary_profile_and_cache(self):
        eng = self.engine
        ev = self.ev("记住我的生日是五月二十日")
        eng.handle(ev)
        rows = eng.store.by_path("core/people/owner")
        self.assertTrue(any("生日" in r.content for r in rows))
        eng.store.write(ev.path, "summary", "用户的生日是五月二十日", .8, dedup=False,
                        user_id="1", audience="owner", source_path=ev.path)
        eng.store.meta_set("profile_owner", "用户的生日是五月二十日")
        eng.prepare(self.ev("我的生日是哪天"))
        eng.handle(self.ev("忘掉我的生日"))
        eng.reflect(force=True)
        eng.flush_event(self.ev("嗯"))
        ctx = eng.prepare(self.ev("还记得生日吗"))
        self.assertNotIn("五月二十日", ctx["system"])
        self.assertNotIn("五月二十日", str(ctx["history"]))
        self.assertEqual(eng.store.meta_get("profile_owner"), "")
        self.assertFalse(eng._last_mems.get((ev.path, "1", ""), ([], 0))[0])
        eng.handle(self.ev("记住我今天不喜欢吃辣"))
        self.assertFalse(eng.store.by_path("core/people/owner"))

    def test_group_cache_and_self_private_memory_stay_scoped(self):
        eng = self.engine
        eng.store.write("core/people/qq/2", "fact", "我住在北京海淀", .9,
                        user_id="2", trust="known", audience="chat/qq/private/2")
        eng.store.write("core/self", "self", "我答应主人保密病情糖尿病", .9)
        alice = self.ev("你记得我住哪里吗", "2", "group", "10")
        ctx = eng.prepare(alice)
        self.assertNotIn("海淀", ctx["system"])
        self.assertNotIn("糖尿病", ctx["system"])
        eng._last_mems[eng._cache_key(alice)] = ([eng.store.by_path("core/people/qq/2")[0]], self.clock())
        bob = self.ev("嗯", "3", "group", "10")
        self.assertEqual(eng.prepare(bob)["_mem_items"], [])

    def test_failed_extraction_retries_and_flush_drains_days(self):
        eng = self.engine
        class RetryLLM(MockLLM):
            fail = True
            def chat_json(self, messages, **kwargs):
                if self.fail:
                    raise RuntimeError("reproduced extraction failure")
                return super().chat_json(messages, **kwargs)
        eng.llm = RetryLLM()
        first = self.ev("我住在北京海淀")
        eng.observe(first)
        with self.assertRaises(RuntimeError):
            eng.flush_event(first)
        self.assertEqual(eng.store.meta_get("extract_ptr:" + first.path, 0), 0)
        self.clock.skip(24)
        second = self.ev("我喜欢吃辣")
        eng.observe(second)
        self.assertTrue(eng._topic_gap(second.path, 30))
        eng.llm.fail = False
        eng.flush_event(second)
        maximum = eng.store.db.execute("SELECT MAX(id) FROM items WHERE kind='turn'").fetchone()[0]
        self.assertEqual(eng.store.meta_get("extract_ptr:" + second.path), maximum)
        self.assertTrue(eng.store.by_path("core/people/owner"))

    def test_cancelled_followup_and_initiative_controls(self):
        eng = self.engine
        ev = self.ev("考试取消了")
        iid = eng.store.write(ev.path, "todo", "明天考试", .8, due_at=self.clock() + 86400,
                              user_id="1", audience="owner", source_path=ev.path)
        self.assertIn("不再跟进", eng.handle(ev))
        self.assertIsNotNone(eng.store.db.execute("SELECT invalid_at FROM items WHERE id=?", (iid,)).fetchone()[0])
        eng.handle(self.ev("别主动给我发消息"))
        self.assertTrue(eng.store.meta_get("initiative_muted"))
        self.assertIn("还没", eng.handle(self.ev("恢复主动聊天")))
        self.assertFalse(eng.store.meta_get("initiative_muted"))
        raw = self.ev("记住，明天答辩")
        eng.observe(raw)
        eng.handle(self.ev("答辩取消了"))
        eng.flush_event(raw)
        self.assertFalse(any("答辩" in it.content for it in eng.store.due_items(self.clock() + 86400)))


if __name__ == "__main__":
    unittest.main()
