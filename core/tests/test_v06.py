"""表情包与刷 B站的回归测试。全离线：不连 QQ、不请求 B站。

重点盯三类过去的坑：
  1. QQ 表情 id 错位（旧表有 20 多个 ID 发出来是别的表情）
  2. 认不出的标记原样发出去
  3. B站评论（公开内容）能进本地 DSH 编程会话的上下文
"""
import os
import sys
import tempfile
import shutil
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from soulcore.stickers import (FACE_TABLE, FACE_COMMON, find_face, parse_reply,
                               prompt_hint, strip_tags, StickerLib)
from soulcore.bili_watch import parse_feed, parse_comments


class Clock:
    def __init__(self, t=1.8e9):
        self.t = t

    def __call__(self):
        return self.t


class TestFaceIds(unittest.TestCase):
    """表情 ID 必须是 QQ 官方那套，别再用按面板顺序编号的旧表。"""

    def test_key_ids(self):
        self.assertEqual(find_face("微笑"), 14)
        self.assertEqual(find_face("笑哭"), 182)
        self.assertEqual(find_face("doge"), 179)
        self.assertEqual(find_face("赞"), 76)
        self.assertEqual(find_face("呲牙"), 13)
        self.assertEqual(find_face("捂脸"), 264)
        self.assertEqual(find_face("吃瓜"), 271)

    def test_old_wrong_mapping_is_gone(self):
        # 旧表把 43 当糗大了；官方 43 是跳跳，100 才是糗大了
        self.assertEqual(find_face("糗大了"), 100)
        self.assertEqual(find_face("跳跳"), 43)
        # 旧表把"强"发成 79（胜利），现在走赞
        self.assertEqual(find_face("强"), 76)

    def test_unknown_face(self):
        self.assertIsNone(find_face("完全不存在的表情"))

    def test_prompt_common_are_all_known(self):
        for name in FACE_COMMON:
            self.assertIn(name, FACE_TABLE, name)


class TestParseReply(unittest.TestCase):
    def test_text_and_face(self):
        segs = parse_reply("哈哈[表情:笑哭]好的")
        self.assertEqual([s["type"] for s in segs], ["text", "face", "text"])
        self.assertEqual(segs[1]["data"]["id"], "182")

    def test_unknown_tag_vanishes(self):
        self.assertEqual(parse_reply("[表情:不存在]"), [])
        self.assertEqual(strip_tags("[表情:不存在]").strip(), "")

    def test_sticker_without_lib_vanishes(self):
        # 没有图库时，[图:无语] 既不能发图，也不能把原文发出去
        self.assertEqual(parse_reply("[图:无语]", None), [])
        self.assertEqual(strip_tags("[图:无语]").strip(), "")

    def test_text_around_unknown_sticker_survives(self):
        segs = parse_reply("给你看[图:无语]", None)
        self.assertEqual([s["type"] for s in segs], ["text"])
        self.assertEqual(segs[0]["data"]["text"], "给你看")

    def test_image_limit(self):
        tmp = tempfile.mkdtemp()
        try:
            for name in ("a.png", "b.png"):
                with open(os.path.join(tmp, name), "wb") as f:
                    f.write(b"\x89PNG\r\n\x1a\n")
            lib = StickerLib(tmp)
            segs = parse_reply("[图:a][图:b]", lib, max_images=1)
            self.assertEqual([s["type"] for s in segs], ["image"])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_prompt_hint_without_lib_has_no_image_tag(self):
        hint = prompt_hint(None)
        self.assertNotIn("[图:", hint)
        self.assertIn("[表情:", hint)

    def test_prompt_hint_with_lib_mentions_image_tag(self):
        tmp = tempfile.mkdtemp()
        try:
            with open(os.path.join(tmp, "a.png"), "wb") as f:
                f.write(b"\x89PNG\r\n\x1a\n")
            hint = prompt_hint(StickerLib(tmp))
            self.assertIn("[图:", hint)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestBiliParse(unittest.TestCase):
    FEED = (
        "   1. BV1bW411n7fY   152s  弹幕 724772  播放129497799  【春晚鬼畜】赵本山  @UP-Sings [鬼畜与怀旧]\n"
        "   2. BV1LNh9zBEtL   182s  弹幕  84186  播放 35490705  猫咪小镇  @爱鼓捣的邢志磊\n"
    )

    def test_feed_two_items(self):
        items = parse_feed(self.FEED)
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]["bv"], "BV1bW411n7fY")
        self.assertEqual(items[1]["up"], "爱鼓捣的邢志磊")   # 末尾没有命中词也要拿到 UP 名
        self.assertIn("猫咪小镇", items[1]["title"])

    def test_single_digit_date(self):
        # toLocaleString 不补零：11/5 这种以前整条丢掉
        text = ("[1] ♥100  甲（mid 1）  2026/11/5 08:03:05  rpid=1\n    五号的评论\n"
                "[2] ♥200  乙（mid 2）  2026/11/11 23:00:00  rpid=2\n    十一号的评论\n")
        got = parse_comments(text, 5)
        self.assertEqual(len(got), 2)
        self.assertTrue(any("五号的评论" in g for g in got))

    def test_pinned_tag_not_in_author(self):
        text = ("[2] 【置顶】♥9  李四（mid 2）  2026/11/11 23:00:00  rpid=2\n    置顶评论\n")
        got = parse_comments(text, 5)
        self.assertEqual(len(got), 1)
        self.assertTrue(got[0].startswith("李四"), got[0])


class TestBiliPrivacy(unittest.TestCase):
    """B站标题可以记，评论不许入库，更不许进本地编程会话。"""

    def _engine(self, qq_enabled=True, bili_enabled=True):
        from soulcore.engine import Engine
        from soulcore.config import load_config
        from soulcore.llm import MockLLM
        tmp = tempfile.mkdtemp()
        cfg, _ = load_config(None)
        cfg["data_dir"] = tmp
        cfg["qq"]["enabled"] = qq_enabled
        cfg["qq"]["bili"] = {"enabled": bili_enabled}
        return Engine(cfg, tmp, llm=MockLLM(), clock=Clock()), tmp

    def test_titles_written_as_known(self):
        eng, tmp = self._engine()
        try:
            eng._bili_remember(["某个标题"])
            items = eng.store.by_path("core/self/bili", limit=5)
            self.assertTrue(items)
            self.assertEqual(items[0].trust, "known")
            self.assertIn("某个标题", items[0].content)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_local_session_cannot_recall_known_items(self):
        eng, tmp = self._engine()
        try:
            eng._bili_remember(["某个标题"])
            # 本地编程会话：主路径是项目，core/self 只能作为额外分支被召回，受 trust 过滤
            owner_only = eng.store.search("某个标题", "project/myrobot", k=5,
                                          extra_paths=["core/self"],
                                          allowed_trusts=("owner",))
            self.assertFalse([h for h in owner_only if "某个标题" in h.content])
            qq_like = eng.store.search("某个标题", "project/myrobot", k=5,
                                       extra_paths=["core/self"],
                                       allowed_trusts=("owner", "known"))
            self.assertTrue([h for h in qq_like if "某个标题" in h.content])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_no_watch_when_qq_disabled(self):
        eng, tmp = self._engine(qq_enabled=False)
        try:
            called = []
            eng._bili_fetch = lambda bcfg: called.append(bcfg) or {}
            eng._bili_refresh_async()
            self.assertEqual(called, [])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_no_watch_when_bili_disabled(self):
        eng, tmp = self._engine(bili_enabled=False)
        try:
            called = []
            eng._bili_fetch = lambda bcfg: called.append(bcfg) or {}
            eng._bili_refresh_async()
            self.assertEqual(called, [])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
