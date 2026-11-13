"""v0.9 R3：稳定块 /v1/persona/stable，以及 include_stable=False 的注入。

人格锚点、主人画像、"她对自己的认识"一天才变一次，应该放进 system prompt 命中缓存；
每轮注入的上下文里只留易变的部分（心情、亲密度、记忆、slips）。
"""
import hashlib
import json
import os
import shutil
import sys
import tempfile
import unittest
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from soulcore.config import load_config
from soulcore.engine import Engine
from soulcore.events import Event
from soulcore.llm import MockLLM

CORE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def engine(self, **over):
        cfg, _ = load_config(None)
        cfg["data_dir"] = os.path.join(self.tmp, "data")
        for k, v in over.items():
            cfg[k].update(v) if isinstance(v, dict) else cfg.__setitem__(k, v)
        return Engine(cfg, self.tmp, llm=MockLLM(), clock=lambda: 1.8e9)


def local_ev(text="今天天气不错", project="demo"):
    return Event("local", "project", project, "owner", "主人", text, mentions_self=True, ts=1.8e9)


class StableBlockTest(Base):
    def test_version_is_sha1_prefix(self):
        eng = self.engine()
        st = eng.stable_block()
        self.assertIn("version", st)
        self.assertEqual(len(st["version"]), 12)
        want = hashlib.sha1((st["anchor"] + st["profile_owner"] + st["profile_self"]).encode()).hexdigest()[:12]
        self.assertEqual(st["version"], want)

    def test_anchor_in_text(self):
        eng = self.engine()
        st = eng.stable_block()
        self.assertTrue(st["anchor"])
        self.assertIn("## 保持人设", st["text"])
        self.assertIn(st["anchor"], st["text"])
        self.assertIn(st["anchor"], st["text"].split("\n")[1])

    def test_profile_changes_text_and_version(self):
        eng = self.engine()
        before = eng.stable_block()["version"]
        eng.store.meta_set("profile_owner", "主人是丁真，喜欢技术和视频话题")
        eng.store.meta_set("profile_self", "小汐清楚自己是角色，不是鱼")
        st = eng.stable_block()
        self.assertIn("## 关于主人（整体印象）", st["text"])
        self.assertIn("丁真", st["text"])
        self.assertIn("## 你对自己的认识", st["text"])
        self.assertIn("不是鱼", st["text"])
        self.assertNotEqual(st["version"], before)

    def test_owner_profile_only_in_owner_scenes(self):
        eng = self.engine(owner={"qq": "10001"})
        eng.store.meta_set("profile_owner", "主人的秘密画像")
        # 本地照给
        self.assertEqual(eng.stable_block({"platform": "local", "scene": "project"})["profile_owner"],
                         "主人的秘密画像")
        # 主人的 QQ 私聊照给
        self.assertEqual(eng.stable_block({"platform": "qq", "scene": "private", "user_id": "10001"})["profile_owner"],
                         "主人的秘密画像")
        # 群聊/别人私聊不给
        for ev in ({"platform": "qq", "scene": "group", "user_id": "10001"},
                   {"platform": "qq", "scene": "private", "user_id": "20002"},
                   {"platform": "bilibili", "scene": "comment", "user_id": "x"}):
            with self.subTest(**ev):
                self.assertEqual(eng.stable_block(ev)["profile_owner"], "")
                self.assertNotIn("秘密画像", eng.stable_block(ev)["text"])


class IncludeStableTest(Base):
    def test_context_block_without_stable(self):
        eng = self.engine()
        eng.store.meta_set("profile_owner", "主人喜欢猫")
        eng.store.meta_set("profile_self", "她会自嘲")
        ev = local_ev()
        full = eng.context_block(ev)["text"]
        part = eng.context_block(ev, include_stable=False)["text"]
        self.assertIn("保持人设", full)
        self.assertIn("你对自己的认识", full)
        self.assertIn("关于对方", full)
        self.assertNotIn("保持人设", part)
        self.assertNotIn("你对自己的认识", part)
        self.assertNotIn("关于对方", part)
        self.assertIn("内心状态", part)
        self.assertLess(len(part), len(full))

    def test_slips_still_shown_without_stable(self):
        eng = self.engine()
        eng._add_slips(["别用客服式结尾"])
        part = eng.context_block(local_ev(), include_stable=False)["text"]
        self.assertIn("最近注意", part)
        self.assertIn("别用客服式结尾", part)


class StableEndpointTest(Base):
    def test_get_persona_stable_and_context_flag(self):
        from soulcore.server import serve
        eng = self.engine()
        eng.store.meta_set("profile_owner", "主人是丁真")
        cfg, _ = load_config(None)
        cfg["data_dir"] = os.path.join(self.tmp, "data")
        cfg["server"]["port"] = 0
        httpd, _, stop = serve(eng, cfg, block=False, lock_path=os.path.join(self.tmp, "soulcore.lock"))
        try:
            with open(os.path.join(self.tmp, "soulcore.lock"), encoding="utf-8") as f:
                info = json.load(f)
            base = f"http://127.0.0.1:{info['port']}"
            hdr = {"Authorization": f"Bearer {info['token']}", "Content-Type": "application/json"}
            with urllib.request.urlopen(urllib.request.Request(base + "/v1/persona/stable", headers=hdr)) as r:
                st = json.loads(r.read())
            self.assertEqual(set(st), {"version", "anchor", "profile_owner", "profile_self", "text"})
            self.assertIn("丁真", st["text"])

            body = json.dumps({"event": local_ev().to_dict(), "include_stable": False}).encode()
            req = urllib.request.Request(base + "/v1/hooks/context", data=body, headers=hdr, method="POST")
            with urllib.request.urlopen(req) as r:
                part = json.loads(r.read())
            self.assertIn("内心状态", part["text"])
            self.assertNotIn("保持人设", part["text"])
            self.assertNotIn("关于对方", part["text"])

            body = json.dumps({"event": local_ev().to_dict()}).encode()
            req = urllib.request.Request(base + "/v1/hooks/context", data=body, headers=hdr, method="POST")
            with urllib.request.urlopen(req) as r:
                full = json.loads(r.read())
            self.assertIn("保持人设", full["text"])
        finally:
            stop.set(); httpd.shutdown(); httpd.server_close()
            httpd.soulcore_lock.release()
            eng.close()


if __name__ == "__main__":
    unittest.main()
