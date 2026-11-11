"""v0.6.1：刷 B站的同步闸门、不重复刷、默认不抓评论。

   python -m unittest discover -s tests -v
"""
import tempfile
import time
import unittest
from unittest import mock

from test_soulcore import Clock, make_engine
from soulcore import bili_watch
from soulcore.events import Event

QQ_BILI = {"enabled": True, "bili": {"enabled": True, "chat_min_gap_s": 600, "every_min": 180}}


def ask(eng, text="你在干嘛呢"):
    return eng.prepare(Event("qq", "private", "1", "1", "x", text, mentions_self=True))


class TestBiliSyncGate(unittest.TestCase):
    def test_failing_bili_does_not_block_every_message(self):
        """B站一直失败（超时/风控/候选看完）：chat_min_gap_s 内只同步试一次，不是每句都卡。"""
        calls = []
        clk = Clock()
        with mock.patch("soulcore.bili_watch.watch_once", side_effect=lambda c: calls.append(clk()) or {}):
            eng, _ = make_engine(tempfile.mkdtemp(), clk, qq=QQ_BILI)
            for _ in range(5):
                clk.t += 60                       # 5 分钟内连发 5 条
                ask(eng)
            time.sleep(0.3)
            self.assertEqual(len(calls), 1, "间隔内应该只同步刷一次")
            clk.t += 600                          # 过了 chat_min_gap_s 才再试
            ask(eng)
            time.sleep(0.3)
            self.assertEqual(len(calls), 2)
            eng.close()

    def test_first_trigger_fetches_once_not_twice(self):
        """第一次触发：同步刷完不应紧接着再起一次后台刷。"""
        calls = []
        hit = {"titles": ["一个视频"], "first_bv": "BV1", "comments": []}
        with mock.patch("soulcore.bili_watch.watch_once", side_effect=lambda c: calls.append(1) or hit):
            eng, _ = make_engine(tempfile.mkdtemp(), Clock(), qq=QQ_BILI)
            ctx = ask(eng)
            time.sleep(0.5)
            self.assertEqual(len(calls), 1)
            self.assertIn("一个视频", ctx["system"])
            ask(eng)                              # 有缓存：直接用，不再刷
            time.sleep(0.3)
            self.assertEqual(len(calls), 1)
            eng.close()

    def test_background_busy_skips_sync(self):
        """后台正在刷时，聊天不再同步刷一遍。"""
        calls = []
        with mock.patch("soulcore.bili_watch.watch_once", side_effect=lambda c: calls.append(1) or {}):
            eng, _ = make_engine(tempfile.mkdtemp(), Clock(), qq=QQ_BILI)
            eng._bili_busy.set()
            ask(eng)
            self.assertEqual(calls, [])
            eng._bili_busy.clear()
            eng.close()


class TestWatchOnceNoComments(unittest.TestCase):
    FEED = "  1. BV1xx411c7mD    60s  弹幕     12  播放     3456  某个视频标题  @某UP主 \n"

    def test_default_skips_comments_call(self):
        cmds = []
        with mock.patch.object(bili_watch, "run_cmd", side_effect=lambda args, *a, **k: cmds.append(args[0]) or self.FEED):
            got = bili_watch.watch_once({})
        self.assertEqual(cmds, ["feed"], "评论不入库也不进提示词，默认不该再多一次请求")
        self.assertEqual(got["titles"], ["某个视频标题"])
        self.assertEqual(got["comments"], [])

    def test_with_comments_opt_in(self):
        cmds = []
        with mock.patch.object(bili_watch, "run_cmd", side_effect=lambda args, *a, **k: cmds.append(args[0]) or self.FEED):
            bili_watch.watch_once({"with_comments": True})
        self.assertEqual(cmds, ["feed", "comments"])


if __name__ == "__main__":
    unittest.main()
