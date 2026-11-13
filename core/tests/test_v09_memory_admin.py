"""v0.9 R4：记忆管理接口（时间线 / 遗忘 / 恢复 / 立即提炼）。"""
import json
import os
import shutil
import sys
import tempfile
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from soulcore.config import load_config
from soulcore.engine import Engine
from soulcore.events import Event
from soulcore.llm import MockLLM
from soulcore.memory import MemoryStore

CORE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def store(self, name="data"):
        st = MemoryStore(os.path.join(self.tmp, name), {})
        self.addCleanup(st.close)
        return st

    def engine(self, name="eng"):
        cfg, _ = load_config(None)
        cfg["data_dir"] = os.path.join(self.tmp, name)
        eng = Engine(cfg, self.tmp, llm=MockLLM(), clock=lambda: 1.8e9)
        self.addCleanup(eng.close)
        return eng, cfg


def md_text(st, path="project/demo"):
    with open(os.path.join(st.md_root, *path.split("/"), "notes.md"), encoding="utf-8") as f:
        return f.read()


class TimelineTest(Base):
    def test_recent_and_superseded(self):
        st = self.store()
        old = st.write("project/demo", "fact", "博客部署在香橙派上", 0.8)
        new = st.write("project/demo", "fact", "博客已经迁到阿里云服务器上了", 0.8)
        t = st.timeline(limit=10)
        self.assertEqual([x["id"] for x in t["recent"]], [new])
        self.assertEqual(len(t["superseded"]), 1)
        hit = t["superseded"][0]
        self.assertEqual(hit["old"]["id"], old)
        self.assertIn("香橙派", hit["old"]["content"])
        self.assertEqual(hit["new"]["id"], new)
        self.assertIn("阿里云", hit["new"]["content"])
        self.assertGreater(hit["at"], 0)
        # 统一字段
        for k in ("id", "path", "kind", "content", "trust", "pinned", "created",
                  "valid_from", "invalid_at", "superseded_by", "stale_note"):
            self.assertIn(k, hit["old"])

    def test_turn_not_listed(self):
        st = self.store()
        st.write("project/demo", "turn", "用户：随便聊聊", 0.2, role="user", speaker="主人")
        self.assertEqual(st.timeline(limit=10)["recent"], [])

    def test_path_filter(self):
        st = self.store()
        st.write("project/a", "fact", "项目 A 用 SQLite", 0.8)
        st.write("project/b", "fact", "项目 B 用 Postgres", 0.8)
        t = st.timeline(limit=10, path="project/a")
        self.assertEqual(len(t["recent"]), 1)
        self.assertEqual(t["recent"][0]["content"], "项目 A 用 SQLite")


class ForgetRestoreTest(Base):
    def test_forget_then_restore(self):
        st = self.store()
        iid = st.write("project/demo", "fact", "项目数据库用 MySQL", 0.8)
        self.assertIn(iid, [x.id for x in st.search("项目数据库 MySQL", "project/demo", k=5, touch=False)])

        item = st.forget(iid)
        self.assertEqual(item["id"], iid)
        self.assertNotIn(iid, [x.id for x in st.search("项目数据库 MySQL", "project/demo", k=5, touch=False)])
        self.assertTrue(st._tombstoned("项目数据库用 MySQL"), "要加墓碑，免得自动提炼又把它复活")
        self.assertNotIn(f"id={iid}", md_text(st), "Markdown 里对应的行要删掉")

        back = st.restore(iid)
        self.assertEqual(back["id"], iid)
        self.assertIn(iid, [x.id for x in st.search("项目数据库 MySQL", "project/demo", k=5, touch=False)])
        self.assertFalse(st._tombstoned("项目数据库用 MySQL"))
        self.assertIn(f"id={iid}", md_text(st), "恢复后要把 Markdown 行补回来")

    def test_restore_superseded_keeps_both(self):
        st = self.store()
        old = st.write("project/demo", "fact", "博客部署在香橙派上", 0.8)
        new = st.write("project/demo", "fact", "博客已经迁到阿里云服务器上了", 0.8)
        self.assertNotIn(old, [x.id for x in st.search("香橙派", "project/demo", k=5, touch=False)])

        self.assertTrue(st.restore(old))
        self.assertIn(old, [x.id for x in st.search("香橙派", "project/demo", k=5, touch=False)],
                      "恢复后的旧条目应该重新出现在常规检索里")
        self.assertIn(new, [x.id for x in st.search("阿里云", "project/demo", k=5, touch=False)],
                      "新条目仍然有效：两条并存，由用户决定留哪条")
        md = md_text(st)
        self.assertIn(f"id={old}", md)
        self.assertNotIn("superseded_by=", md, "恢复时要把 md 注释里的 superseded_by= 去掉")

    def test_forget_stranger_forbidden(self):
        st = self.store()
        iid = st.write("chat/qq/group/1", "fact", "群里路人的一句话", 0.6, trust="stranger")
        with self.assertRaises(PermissionError):
            st.forget(iid)
        self.assertIn(iid, [x.id for x in st.search("群里路人", "chat/qq/group/1", k=5, touch=False)])

    def test_forget_missing_returns_none(self):
        st = self.store()
        self.assertIsNone(st.forget(99999))
        self.assertIsNone(st.restore(99999))


class HttpAdminTest(Base):
    def test_forget_403_flush_and_timeline(self):
        from soulcore.server import serve
        eng, cfg = self.engine()
        stranger = eng.store.write("chat/qq/group/1", "fact", "群里路人的一句话", 0.6, trust="stranger")
        cfg["server"]["port"] = 0
        httpd, _, stop = serve(eng, cfg, block=False, lock_path=os.path.join(self.tmp, "soulcore.lock"))
        try:
            with open(os.path.join(self.tmp, "soulcore.lock"), encoding="utf-8") as f:
                info = json.load(f)
            base = f"http://127.0.0.1:{info['port']}"
            hdr = {"Authorization": f"Bearer {info['token']}", "Content-Type": "application/json"}

            def post(path, obj):
                req = urllib.request.Request(base + path, data=json.dumps(obj).encode(),
                                             headers=hdr, method="POST")
                with urllib.request.urlopen(req) as r:
                    return json.loads(r.read())

            def get(path):
                with urllib.request.urlopen(urllib.request.Request(base + path, headers=hdr)) as r:
                    return json.loads(r.read())

            # 陌生人的条目不能删
            with self.assertRaises(urllib.error.HTTPError) as cm:
                post("/v1/memory/forget", {"id": stranger})
            self.assertEqual(cm.exception.code, 403)

            # 先产生一条未提炼的 turn，再调 flush
            ev = Event("local", "project", "demo", "owner", "主人",
                       "记住，项目的检索层决定用 SQLite+FTS5", mentions_self=True, ts=1.8e9)
            eng.observe(ev)
            r = post("/v1/hooks/flush", {"event": ev.to_dict()})
            self.assertTrue(r["ok"])
            self.assertGreaterEqual(r["extracted"], 1, "flush 要真的把 turn 提炼成记忆")

            t = get("/v1/memory/timeline?path=project/demo&limit=10")
            contents = [x["content"] for x in t["recent"]]
            self.assertTrue(any("SQLite" in c for c in contents), contents)

            # HTTP forget → 检索不到；restore → 又回来
            iid = t["recent"][0]["id"]
            self.assertTrue(post("/v1/memory/forget", {"id": iid})["ok"])
            self.assertFalse(any(x["id"] == iid for x in get("/v1/memory/timeline?limit=20")["recent"]))
            self.assertTrue(post("/v1/memory/restore", {"id": iid})["ok"])
            self.assertTrue(any(x["id"] == iid for x in get("/v1/memory/timeline?limit=20")["recent"]))
        finally:
            stop.set(); httpd.shutdown(); httpd.server_close()
            httpd.soulcore_lock.release()


if __name__ == "__main__":
    unittest.main()
