"""v0.6：主动聊天（initiative.py）+ 跟进时间解析（timeparse.py）。

全部用模拟时钟，不联网、不真的发 QQ。

   python -m unittest discover -s tests -v
"""
import json
import os
import random
import time
import unittest
import urllib.request

import netutil
from test_soulcore import Base, Clock, make_engine
from soulcore.events import Event
from soulcore.initiative import Initiative, in_quiet
from soulcore.llm import HeuristicLLM, MockLLM
from soulcore.timeparse import parse_followup

OWNER = "10001"


def local(y, mo, d, h=0, mi=0) -> float:
    return time.mktime((y, mo, d, h, mi, 0, 0, 0, -1))


class Yes(random.Random):
    """概率判定永远通过：让测试只验证"想不想说、能不能说"，不受随机性影响。"""
    def random(self):
        return 0.0


class SkipLLM(MockLLM):
    def chat(self, messages, **kw):
        return "SKIP"


def make(tmp, clock, *, sender=None, rng=None, **init):
    cfg_init = {"enabled": True, "dry_run": True, **init}
    eng, cfg = make_engine(tmp, clock, owner={"qq": OWNER, "name": "阿泽"},
                           qq={"private_whitelist": []}, initiative=cfg_init)
    it = Initiative(eng, sender=sender, rng=rng or Yes())
    eng.initiative = it
    return eng, it


def owner_qq(clock, text):
    return Event("qq", "private", OWNER, OWNER, "阿泽", text, mentions_self=True, ts=clock())


def owner_local(clock, text):
    return Event("local", "project", "myrobot", "owner", "阿泽", text, mentions_self=True, ts=clock())


class TestTimeparse(unittest.TestCase):
    def test_cases(self):
        now = local(2026, 11, 11, 14, 0)          # 周三 14:00
        f = lambda s: parse_followup(s, now)
        self.assertEqual(f("我明天下午三点答辩"), local(2026, 11, 12, 17, 0))     # 事件 +2 小时
        self.assertEqual(f("周五面试"), local(2026, 11, 13, 18, 0))              # 只有日期 → 当天 18:00
        self.assertEqual(f("下周一组会"), local(2026, 11, 16, 18, 0))
        self.assertEqual(f("12月3号考试"), local(2026, 12, 3, 18, 0))
        self.assertEqual(f("今晚8点半有个汇报"), local(2026, 11, 11, 22, 30))
        self.assertEqual(f("今天期末考试"), local(2026, 11, 11, 21, 0))
        self.assertEqual(f("明天下午3:30 体检"), local(2026, 11, 12, 17, 30))
        self.assertEqual(f("1月5日复试"), local(2027, 1, 5, 18, 0))             # 已过的月日 → 明年
        self.assertIsNone(f("我考虑一下明天"))                                   # 不是事件
        self.assertIsNone(f("昨天考试考砸了"))                                   # 已经发生
        self.assertIsNone(f("明天天气不错"))

    def test_quiet_hours(self):
        self.assertTrue(in_quiet(local(2026, 11, 11, 3, 0), ["00:00", "09:00"]))
        self.assertFalse(in_quiet(local(2026, 11, 11, 9, 0), ["00:00", "09:00"]))
        self.assertTrue(in_quiet(local(2026, 11, 11, 23, 30), ["23:00", "08:00"]))    # 跨午夜
        self.assertTrue(in_quiet(local(2026, 11, 12, 7, 0), ["23:00", "08:00"]))
        self.assertFalse(in_quiet(local(2026, 11, 11, 12, 0), ["23:00", "08:00"]))


class TestFollowup(Base):
    def setUp(self):
        super().setUp()
        self.clock = Clock(local(2026, 11, 11, 14, 0))

    def _tell_exam(self, eng):
        ev = owner_qq(self.clock, "我明天下午三点答辩，有点紧张")
        eng.observe(ev)
        eng.commit_reply(ev, "加油！")
        eng._maybe_extract(ev, force=True)
        eng.flush()

    def test_extract_due_then_followup_draft_then_send(self):
        sent = []
        eng, it = make(self.tmp, self.clock, sender=lambda ev, text: sent.append((ev.reply_target, text)) or True)
        self._tell_exam(eng)
        due = [r for r in eng.store.db.execute("SELECT content, due_at, trust FROM items WHERE due_at IS NOT NULL")]
        self.assertEqual(len(due), 1, "应该提炼出一条带跟进时刻的 todo")
        self.assertEqual(due[0]["due_at"], local(2026, 11, 12, 17, 0))
        self.assertEqual(due[0]["trust"], "owner")

        self.clock.t = local(2026, 11, 12, 12, 0)            # 还没到时候
        r = it.check()
        self.assertNotEqual(r["action"], "draft", r)

        self.clock.t = local(2026, 11, 12, 18, 0)            # 答辩结束后
        r = it.check()
        self.assertEqual(r["action"], "draft", r)
        self.assertEqual(r["thought"]["kind"], "followup")
        self.assertEqual(sent, [], "试运行模式不应该真的发")
        st = it.status()
        self.assertEqual(len(st["drafts"]), 1)
        self.assertEqual(st["today"], 1)

        self.assertTrue(it.draft_action(st["drafts"][0]["id"], "send")["ok"])
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0][0], {"type": "private", "user_id": OWNER})
        turns = eng.store.recent_turns(f"chat/qq/private/{OWNER}")
        self.assertEqual(turns[-1].role, "assistant")         # 写进了私聊分支，他回复时上下文连贯
        self.assertEqual(it.status()["drafts"], [])

        self.clock.skip(4)                                     # 同一件事不会再问一遍
        r = it.check(force=True)
        self.assertNotEqual((r.get("thought") or {}).get("kind"), "followup", r)
        eng.close()

    def test_stranger_event_never_followed_up(self):
        eng, it = make(self.tmp, self.clock)
        eng.store.write("chat/qq/group/1", "todo", "路人说明天下午三点面试", 0.7, trust="stranger",
                        due_at=local(2026, 11, 12, 17, 0))
        self.clock.t = local(2026, 11, 12, 18, 0)
        self.assertEqual(it.thoughts(it._load(), self.clock()), [])
        eng.close()


class TestFeedback(Base):
    def setUp(self):
        super().setUp()
        self.clock = Clock(local(2026, 11, 11, 19, 0))

    def test_responded_and_ignored(self):
        sent = []
        eng, it = make(self.tmp, self.clock, sender=lambda ev, t: sent.append(t) or True, dry_run=False)
        eng.store.write("core/people/owner", "todo", "阿泽今天下午考驾照科目二", 0.7,
                        due_at=self.clock() - 600)
        r = it.check()
        self.assertEqual(r["action"], "sent", r)
        self.assertEqual(len(sent), 1)

        self.clock.skip(1)                                     # 1 小时后他回了
        eng.observe(owner_qq(self.clock, "过了！一把过"))
        self.assertEqual(it.status()["unanswered"], 0)
        self.assertIn("responded", [e[0] for e in eng.emo.recent])
        self.assertEqual(it.status()["reply_rate"], 1.0)

        # 第二天他没回
        self.clock.t = local(2026, 11, 12, 19, 0)
        eng.store.write("core/people/owner", "todo", "阿泽今天去体检", 0.7, due_at=self.clock() - 600)
        self.assertEqual(it.check()["action"], "sent")
        self.clock.skip(5)
        it.check()
        self.assertEqual(it.status()["unanswered"], 1)
        self.assertIn("ignored", [e[0] for e in eng.emo.recent])
        self.assertEqual(it.status()["reply_rate"], 0.5)
        eng.close()


class TestLongRun(Base):
    def test_30_days_ignored_owner(self):
        """30 天模拟，他一直不回：勿扰时段 0 条、每天 ≤3 条、间隔 ≥3 小时、连续 2 条没回就停；
        第 20 天他开口后恢复。"""
        start = local(2026, 11, 1, 0, 0)
        self.clock = Clock(start)
        sent = []
        eng, it = make(self.tmp, self.clock, sender=lambda ev, t: sent.append(self.clock()) or True,
                       rng=random.Random(42), dry_run=False, base_rate_per_hour=2.0)
        for day in range(30):
            if day == 20:
                self.clock.t = start + 20 * 86400 + 12 * 3600
                eng.observe(owner_qq(self.clock, "最近忙，刚看到消息"))
            # 每天早上让她知道一件今天的事，保证有话可说
            eng.store.write("core/people/owner", "todo", f"阿泽第{day}天要去组会", 0.7,
                            due_at=start + day * 86400 + 10 * 3600)
            for step in range(288):
                self.clock.t = start + day * 86400 + step * 300
                it.check()
        quiet = [t for t in sent if in_quiet(t, ["00:00", "09:00"])]
        self.assertEqual(quiet, [], "勿扰时段发了消息")
        per_day = {}
        for t in sent:
            per_day[time.strftime("%m-%d", time.localtime(t))] = per_day.get(time.strftime("%m-%d", time.localtime(t)), 0) + 1
        self.assertTrue(all(n <= 3 for n in per_day.values()), per_day)
        gaps = [b - a for a, b in zip(sent, sent[1:])]
        self.assertTrue(all(g >= 3 * 3600 for g in gaps), gaps)
        before = [t for t in sent if t < start + 20 * 86400]
        after = [t for t in sent if t >= start + 20 * 86400]
        self.assertEqual(len(before), 2, "连续 2 条没回之后应该一直等他先开口")
        self.assertGreaterEqual(len(after), 1, "他开口后应该恢复主动")
        self.assertLessEqual(len(after), 2)
        eng.close()


class TestBlockersAndCare(Base):
    def test_recent_qq_chat_blocks(self):
        self.clock = Clock(local(2026, 11, 11, 19, 0))
        eng, it = make(self.tmp, self.clock)
        eng.store.write("core/people/owner", "todo", "阿泽今天下午面试", 0.7, due_at=self.clock() - 600)
        eng.observe(owner_qq(self.clock, "我到家了"))
        self.clock.skip(0.2)
        r = it.check()
        self.assertEqual(r["action"], "wait")
        self.assertIn("刚在 QQ 上聊过", r["reason"])
        eng.close()

    def test_busy_in_dsh_only_care(self):
        self.clock = Clock(local(2026, 11, 11, 19, 0))
        eng, it = make(self.tmp, self.clock)
        eng.store.write("core/people/owner", "todo", "阿泽今天下午面试", 0.7, due_at=self.clock() - 600)
        eng.observe(owner_local(self.clock, "帮我看看这个 bug"))
        r = it.check()
        self.assertEqual(r["action"], "wait", "在 DSH 里忙的时候不该拿面试的事打扰")
        self.clock.t = local(2026, 11, 11, 23, 30)              # 深夜还在写
        eng.observe(owner_local(self.clock, "还差一个测试"))
        r = it.check()
        self.assertEqual(r["action"], "draft", r)
        self.assertEqual(r["thought"]["kind"], "care")
        eng.close()

    def test_quiet_hours_block(self):
        self.clock = Clock(local(2026, 11, 11, 3, 0))
        eng, it = make(self.tmp, self.clock)
        eng.store.write("core/people/owner", "todo", "阿泽昨晚通宵赶论文", 0.7, due_at=self.clock() - 600)
        r = it.check()
        self.assertEqual(r["action"], "wait")
        self.assertEqual(r["reason"], "勿扰时段")
        eng.close()

    def test_disabled_and_no_owner(self):
        eng, it = make(self.tmp, self.clock, enabled=False)
        self.assertEqual(it.check(force=False)["action"], "off")
        eng.cfg["owner"]["qq"] = ""
        self.assertIn("QQ 号", it.check(force=True)["reason"])
        eng.close()


class TestCompose(Base):
    def setUp(self):
        super().setUp()
        self.clock = Clock(local(2026, 11, 11, 19, 0))

    def test_skip_marks_thought_done(self):
        eng, it = make(self.tmp, self.clock)
        eng.llm = SkipLLM()
        eng.store.write("core/people/owner", "todo", "阿泽今天下午面试", 0.7, due_at=self.clock() - 600)
        r = it.check()
        self.assertEqual(r["action"], "skip", r)
        self.clock.skip(4)
        r = it.check()
        self.assertNotEqual(r["action"], "skip", "同一个念头被 SKIP 之后不该反复想")
        self.assertEqual(it.status()["drafts"], [])
        eng.close()

    def test_failed_delivery_keeps_followup_retryable(self):
        eng, it = make(self.tmp, self.clock, sender=lambda ev, text: False, dry_run=False)
        self.addCleanup(eng.close)
        iid = eng.store.write("core/people/owner", "todo", "阿泽今天下午面试", 0.7,
                              due_at=self.clock() - 600)
        self.assertEqual(it.check(force=True)["action"], "error")
        self.assertNotIn(f"item:{iid}", it._load()["done_refs"])
        it.sender = lambda ev, text: True
        self.assertEqual(it.check(force=True)["action"], "sent")
        self.assertIn(f"item:{iid}", it._load()["done_refs"])

    def test_reply_during_compose_cancels_stale_message(self):
        sent = []
        eng, it = make(self.tmp, self.clock, sender=lambda ev, text: sent.append(text) or True, dry_run=False)
        self.addCleanup(eng.close)
        eng.store.write("core/people/owner", "todo", "阿泽今天下午面试", 0.7,
                        due_at=self.clock() - 600)

        def compose(th, now):
            eng.observe(owner_qq(self.clock, "我已经到家了"))
            return "面试怎么样了？"
        it.compose = compose
        result = it.check(force=True)
        self.assertEqual(result["action"], "wait", result)
        self.assertEqual(sent, [])
        self.assertEqual(it._load()["done_refs"], [])

    def test_topic_muted_during_compose_and_cancelled_draft(self):
        sent = []
        eng, it = make(self.tmp, self.clock, sender=lambda ev, text: sent.append(text) or True)
        self.addCleanup(eng.close)
        iid = eng.store.write("core/people/owner", "todo", "阿泽今天下午面试", 0.7,
                              due_at=self.clock() - 600)

        def compose(th, now):
            eng.store.meta_set("initiative_muted_topics", ["面试"])
            return "面试怎么样了？"
        it.compose = compose
        self.assertEqual(it.check(force=True)["action"], "wait")
        eng.store.meta_set("initiative_muted_topics", [])
        it.compose = lambda th, now: "面试怎么样了？"
        draft = it.check(force=True)
        self.assertEqual(draft["action"], "draft")
        self.assertNotIn(f"item:{iid}", it._load()["done_refs"])
        eng.store.forget(iid)
        result = it.draft_action(draft["qid"], "send")
        self.assertFalse(result["ok"], result)
        self.assertEqual(sent, [])

    def test_no_api_key(self):
        eng, it = make(self.tmp, self.clock)
        eng.llm = HeuristicLLM()
        eng.store.write("core/people/owner", "todo", "阿泽今天下午面试", 0.7, due_at=self.clock() - 600)
        r = it.check()
        self.assertEqual(r["action"], "error")
        self.assertIn("API Key", r["reason"])
        self.assertIn("API Key", it.status()["last_reason"])
        eng.close()

    def test_prompt_has_reason_and_no_fake_user_turn(self):
        seen = {}

        class Spy(MockLLM):
            def chat(self, messages, **kw):
                seen["msgs"] = messages
                return "答辩怎么样啦？"
        eng, it = make(self.tmp, self.clock)
        eng.llm = Spy()
        eng.store.write("core/people/owner", "todo", "阿泽今天下午答辩", 0.7, due_at=self.clock() - 600)
        r = it.check()
        self.assertEqual(r["text"], "答辩怎么样啦？")
        msgs = seen["msgs"]
        self.assertIn("主动", msgs[0]["content"])
        self.assertIn("答辩", msgs[0]["content"])
        self.assertTrue(msgs[-1]["content"].startswith("[系统提示"))
        self.assertFalse(any(m["role"] == "user" and "你想问问他" in m["content"] for m in msgs),
                         "念头不应该被伪装成他说的话")
        eng.close()


class TestServerEndpoints(Base):
    def test_status_and_run(self):
        from soulcore.server import serve
        self.clock = Clock(local(2026, 11, 11, 19, 0))
        eng, cfg = make_engine(self.tmp, self.clock, owner={"qq": OWNER},
                               initiative={"enabled": True, "dry_run": True})
        cfg["server"]["port"] = 0
        httpd, _, stop = serve(eng, cfg, block=False, lock_path=os.path.join(self.tmp, "soulcore.lock"))
        try:
            info = json.load(open(os.path.join(self.tmp, "soulcore.lock"), encoding="utf-8"))
            base = f"http://127.0.0.1:{info['port']}"
            hdr = {"Authorization": f"Bearer {info['token']}", "Content-Type": "application/json"}
            with urllib.request.urlopen(urllib.request.Request(base + "/v1/initiative/status", headers=hdr)) as r:
                st = json.loads(r.read())
            self.assertTrue(st["enabled"] and st["dry_run"] and st["owner_set"])
            self.assertFalse(st["can_send"])                    # 没开 QQ
            eng.store.write("core/people/owner", "todo", "阿泽今天下午面试", 0.7, due_at=self.clock() - 600)
            req = urllib.request.Request(base + "/v1/initiative/run", data=b'{"force": true}', headers=hdr, method="POST")
            with urllib.request.urlopen(req) as r:
                res = json.loads(r.read())
            self.assertEqual(res["action"], "draft", res)
            req = urllib.request.Request(base + "/v1/initiative/draft", headers=hdr, method="POST",
                                         data=json.dumps({"id": res["qid"], "action": "send"}).encode())
            with urllib.request.urlopen(req) as r:
                self.assertIn("QQ 没有连接", json.loads(r.read())["error"])
        finally:
            stop.set(); httpd.shutdown(); httpd.server_close()
            httpd.soulcore_lock.release()
            eng.close()


if __name__ == "__main__":
    unittest.main()
