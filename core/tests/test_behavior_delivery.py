"""QQ reply delivery: bounded bubbles and truthful partial-send history."""
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from soulcore.adapters.onebot import OneBotAdapter, split_reply
from soulcore.events import Event
from soulcore.memory import MemoryStore
from soulcore.ratelimit import RateLimiter


class RecordingEngine:
    def __init__(self, directory):
        self.store = MemoryStore(directory)
        self.output = "第一条分享。\n\n第二条分享。\n\n第三条分享。"
        self.policy = SimpleNamespace(max_messages=3)
        self.processed = threading.Event()
        self.discarded = []
        self.calls = 0

    def handle(self, ev, *, reply, record_reply):
        self.calls += 1
        self.store.write(ev.path, "turn", ev.text, 0.8, role="user", dedup=False)
        return self.output if reply else None

    def reply_policy(self, _ev):
        return self.policy

    def commit_reply(self, ev, text):
        self.store.write(ev.path, "turn", text, 0.8, role="assistant", dedup=False)
        self.processed.set()

    def discard_reply(self, ev):
        self.discarded.append(ev.path)
        self.processed.set()


class TestReplySplitting(unittest.TestCase):
    def test_conservative_split_preserves_all_paragraphs_and_cap(self):
        text = "第一段。\n\n第二段。\n\n第三段。\n\n第四段。"
        parts = split_reply(text, 2)
        self.assertEqual(len(parts), 2)
        self.assertEqual("\n\n".join(parts), text)
        self.assertEqual(split_reply(text, 1), [text])
        self.assertEqual(len(split_reply(text, 100)), 3)

    def test_structured_answers_are_never_split(self):
        for text in ("解释\n\n```python\nx = 1\n\nprint(x)\n```\n\n结果",
                     "先做两件事\n\n- 运行测试\n- 查看结果\n\n最后提交",
                     "步骤\n\n1. 保存\n2. 检查\n\n完成",
                     "说明\n\n| 列 | 值 |\n|---|---|\n\n结尾"):
            with self.subTest(text=text):
                self.assertEqual(split_reply(text, 3), [text])


class TestReplyDelivery(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.engine = RecordingEngine(self.tmp.name)
        self.responses = []
        self.api_calls = []
        def api(action, body):
            self.api_calls.append((action, body))
            return self.responses.pop(0) if self.responses else {"status": "ok", "retcode": 0}
        self.adapter = OneBotAdapter(self.engine, {"min_interval_s": 0, "max_per_min": 100,
                                                  "typing_delay": False}, api_fn=api)
        self.ev = Event("qq", "private", "42", "42", text="哈哈", mentions_self=True,
                        reply_target={"type": "private", "user_id": "42"})

    def tearDown(self):
        self.adapter.stop()
        self.adapter.worker.join(timeout=2)
        self.engine.store.close()
        self.tmp.cleanup()

    def text_sent(self):
        return ["".join(s["data"].get("text", "") for s in body["message"] if s["type"] == "text")
                for _action, body in self.api_calls]

    def assistant_history(self):
        return [r["content"] for r in self.engine.store.db.execute(
            "SELECT content FROM items WHERE role='assistant' ORDER BY id").fetchall()]

    def run_worker(self):
        self.adapter.q.put((self.ev, True))
        self.assertTrue(self.engine.processed.wait(2), "worker did not finish")

    def test_normal_reply_generates_once_and_commits_all_delivered_parts(self):
        self.run_worker()
        self.assertEqual(self.engine.calls, 1)
        self.assertEqual(self.text_sent(), ["第一条分享。", "第二条分享。", "第三条分享。"])
        self.assertEqual(self.assistant_history(), [self.engine.output])

    def test_partial_failure_commits_only_successful_prefix_without_retry(self):
        self.responses = [{"status": "ok", "retcode": 0}, {"status": "failed", "retcode": 1}]
        self.run_worker()
        self.assertEqual(self.text_sent(), ["第一条分享。", "第二条分享。"])
        self.assertEqual(self.assistant_history(), ["第一条分享。"])
        self.assertEqual(self.engine.discarded, [])

    def test_total_failure_does_not_create_assistant_history(self):
        self.responses = [{"status": "failed", "retcode": 1}]
        self.run_worker()
        self.assertEqual(len(self.api_calls), 1)
        self.assertEqual(self.assistant_history(), [])
        self.assertEqual(self.engine.discarded, [self.ev.path])

    def test_task_limit_one_keeps_legacy_single_payload(self):
        self.engine.policy = SimpleNamespace(max_messages=1)
        self.engine.output = "  保存代码。\n\n运行测试。  "
        self.run_worker()
        self.assertEqual(self.text_sent(), [self.engine.output])
        self.assertEqual(self.assistant_history(), [self.engine.output])

    def test_delivery_guard_is_rechecked_per_part(self):
        checks = []
        def guard():
            checks.append(True)
            return len(checks) == 1
        self.ev._soulcore_delivery_guard = guard
        delivered = self.adapter.send_reply(self.ev, self.engine.output, 3)
        self.assertEqual(delivered, "第一条分享。")
        self.assertEqual(len(checks), 2)
        self.assertEqual(len(self.api_calls), 1)

    def test_limiter_is_respected_for_each_part(self):
        class Limited:
            calls = 0
            def acquire(self, _key, max_wait=120):
                self.calls += 1
                return self.calls == 1
        limiter = Limited()
        self.adapter.limiter = limiter
        delivered = self.adapter.send_reply(self.ev, self.engine.output, 3)
        self.assertEqual(delivered, "第一条分享。")
        self.assertEqual(limiter.calls, 2)
        self.assertEqual(len(self.api_calls), 1)

    def test_typing_delay_remains_per_part(self):
        self.adapter.cfg["typing_delay"] = True
        with patch("soulcore.adapters.onebot.time.sleep") as sleep:
            self.adapter.send_reply(self.ev, "一条。\n\n两条。", 2)
        self.assertEqual(sleep.call_count, 2)

    def test_default_limiter_spaces_short_bubbles_with_bounded_waits(self):
        now, times, waits = [1000.0], [], []
        self.adapter.limiter = RateLimiter(3, 10, 60, clock=lambda: now[0])
        self.adapter.cfg["typing_delay"] = True
        def api(_action, _body):
            times.append(now[0])
            return {"status": "ok", "retcode": 0}
        def sleep(delay):
            waits.append(delay)
            now[0] += delay
        self.adapter.api_fn = api
        text = "一条。\n\n两条。\n\n三条。"
        with patch("soulcore.adapters.onebot.time.sleep", side_effect=sleep):
            delivered = self.adapter.send_reply(self.ev, text, 3)
        self.assertEqual(delivered, text)
        self.assertEqual(len(times), 3)
        self.assertTrue(all(right - left >= 3 - 1e-10 for left, right in zip(times, times[1:])))
        self.assertTrue(all(wait <= 6 for wait in waits))

    def test_long_interval_does_not_block_additional_bubbles(self):
        self.adapter.limiter = RateLimiter(30, 10, 60, clock=lambda: 1000)
        with patch("soulcore.adapters.onebot.time.sleep") as sleep:
            delivered = self.adapter.send_reply(self.ev, self.engine.output, 3)
        self.assertEqual(delivered, "第一条分享。")
        self.assertEqual(len(self.api_calls), 1)
        sleep.assert_not_called()

    def test_initiative_send_interface_remains_single_payload(self):
        self.assertTrue(self.adapter.send(self.ev, self.engine.output))
        self.assertEqual(self.text_sent(), [self.engine.output])


if __name__ == "__main__":
    unittest.main()
