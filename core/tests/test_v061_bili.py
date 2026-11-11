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


class TestBiliPrompt(unittest.TestCase):
    """实际使用中发现：抓取成功了，她却一直说"刷不了"。原因是提示词只在触发时塞一段素材，
    从没告诉她"你会刷"；触发词又窄（"你现在可以访问到b站了吗"不触发）；旧缓存也写成"刚刷到"。"""

    def _eng(self, clk, **bili):
        return make_engine(tempfile.mkdtemp(), clk, qq={"enabled": True, "bili": {"enabled": True, **bili}})[0]

    def test_ability_always_in_qq_prompt(self):
        clk = Clock()
        with mock.patch("soulcore.bili_watch.watch_once", return_value={}):
            eng = self._eng(clk)
            ctx = ask(eng, "在吗")                         # 不是触发词也要有能力说明
            self.assertIn("你会刷B站", ctx["system"])
            self.assertIn("看不到画面", ctx["system"])
            self.assertIn("按关键词搜视频", ctx["system"])
            local = eng.prepare(Event("local", "project", "p", "owner", "主人", "B站视频怎么爬"))
            self.assertNotIn("你会刷B站", local["system"], "本地 DSH 编程会话不需要这段")
            eng.close()
        eng2, _ = make_engine(tempfile.mkdtemp(), Clock(), qq={"enabled": True})   # bili 默认关
        self.assertNotIn("你会刷B站", ask(eng2, "在吗")["system"])
        eng2.close()

    def test_broader_triggers_and_age(self):
        clk = Clock()
        with mock.patch("soulcore.bili_watch.watch_once", return_value={}):
            eng = self._eng(clk)
            eng._bili_remember(["猫咪小镇上的喵当劳开业啦"])
            clk.t += 23 * 60
            ctx = ask(eng, "你现在可以访问到b站了吗")          # 以前不触发
            self.assertIn("喵当劳", ctx["system"])
            self.assertIn("23 分钟前刷的", ctx["system"])
            self.assertIn("不是对方告诉你的", ctx["system"])
            self.assertNotIn("你刚刷到的", ctx["system"])
            clk.t += 3 * 3600
            self.assertIn("3 小时前刷的", ask(eng, "最近有啥好玩的视频")["system"])
            for text in ("今天天气不错", "刷个牙去"):          # 不相干的话不加"最近刷到的"段落
                # （标题本身存在 core/self/bili 里，普通记忆检索偶尔召回是正常的）
                self.assertNotIn("你最近刷到的", ask(eng, text)["system"], text)
            eng.close()

    def test_nothing_cached_says_dont_make_up(self):
        clk = Clock()
        with mock.patch("soulcore.bili_watch.watch_once", return_value={}):
            eng = self._eng(clk)
            ctx = ask(eng, "刷到啥b站视频了")
            self.assertIn("别编视频名", ctx["system"])
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
