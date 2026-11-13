"""v0.9 R1：Markdown 镜像的并发写。

原来的写法在 self.lock 外面做"读 → 改 → os.replace"：后台提炼线程、tick 线程和 HTTP 线程
同时写同一个 notes.md 时会互相覆盖，被覆盖的行下一轮 sync_markdown() 会判成"用户删了"，
记忆永久丢失。这里用三个线程对着同一个分支猛写，验证不再丢行。
"""
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from soulcore.memory import MemoryStore, atomic_write

CORE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


def make_store(tmp, name="data"):
    return MemoryStore(os.path.join(tmp, name), {"auto_supersede": True})


class AtomicWriteTest(Base):
    def test_retries_on_permission_error(self):
        path = os.path.join(self.tmp, "notes.md")
        with open(path, "w", encoding="utf-8") as f:
            f.write("old")
        calls = {"n": 0}
        real = os.replace

        def flaky(src, dst):
            calls["n"] += 1
            if calls["n"] < 3:
                raise PermissionError("file busy")
            return real(src, dst)

        with patch("soulcore.memory.os.replace", side_effect=flaky):
            atomic_write(path, "new")
        self.assertGreaterEqual(calls["n"], 3)
        with open(path, encoding="utf-8") as f:
            self.assertEqual(f.read(), "new")

    def test_gives_up_after_retries(self):
        path = os.path.join(self.tmp, "notes.md")
        with patch("soulcore.memory.os.replace", side_effect=PermissionError("file busy")):
            with self.assertRaises(PermissionError):
                atomic_write(path, "x", retries=2)

    def test_no_stale_tmp_left_after_success(self):
        path = os.path.join(self.tmp, "a.md")
        atomic_write(path, "hello")
        self.assertFalse(os.path.exists(path + ".tmp"))
        with open(path, encoding="utf-8") as f:
            self.assertEqual(f.read(), "hello")


class MirrorConcurrencyTest(Base):
    """两个写线程 + 一个改行线程，同一个 project/demo 分支。"""

    PATH = "project/demo"

    def _churn(self, st, errors, stop):
        """循环 supersede / _annotate_mirror_line（这两条是原来最容易撕掉别人行的路径）"""
        while not stop.is_set():
            try:
                with st.lock:
                    rows = st.db.execute(
                        "SELECT id FROM items WHERE path=? AND kind='fact' AND invalid_at IS NULL "
                        "ORDER BY RANDOM() LIMIT 3", (self.PATH,)).fetchall()
                for r in rows:
                    st.supersede(r["id"], None)
                    st._annotate_mirror_line(self.PATH, "fact", r["id"], "note=churn")
                time.sleep(0.002)
            except Exception as ex:          # noqa: BLE001 —— 线程里的异常要带回主线程判断
                errors.append(ex)
                return

    def _run_round(self, round_no: int) -> None:
        st = make_store(self.tmp, f"data{round_no}")
        self.addCleanup(st.close)
        errors: list[BaseException] = []
        stop = threading.Event()
        ids: list[int] = []
        ids_lock = threading.Lock()

        def writer(tag: str):
            try:
                for i in range(200):
                    # dedup=False：内容确实很像，别让去重把 400 条并成几十条
                    iid = st.write(self.PATH, "fact", f"{tag} 第 {i} 条事实：端口 {i} 走 /api/{tag}/{i}",
                                   0.6, dedup=False)
                    if iid:
                        with ids_lock:
                            ids.append(iid)
            except Exception as ex:          # noqa: BLE001
                errors.append(ex)

        churn = threading.Thread(target=self._churn, args=(st, errors, stop), name="churn")
        t1 = threading.Thread(target=writer, args=("甲",), name="w1")
        t2 = threading.Thread(target=writer, args=("乙",), name="w2")
        churn.start(); t1.start(); t2.start()
        t1.join(120); t2.join(120)
        stop.set(); churn.join(120)
        self.assertFalse(errors, f"并发写抛出异常：{errors[:3]}")
        self.assertEqual(len(ids), 400)

        stats = st.sync_markdown(force=True)
        self.assertEqual(stats["deleted"], 0, f"第 {round_no} 轮同步把 {stats['deleted']} 条记忆判成了删除")

        # 每条非 turn、非 deleted 的条目都要在 Markdown 里留下 id 注释
        rows = st.db.execute(
            "SELECT id, path, kind FROM items WHERE kind!='turn' AND status!='deleted'").fetchall()
        self.assertGreaterEqual(len(rows), 400)
        md = []
        for d, _, files in os.walk(st.md_root):
            for fn in files:
                if fn.endswith(".md"):
                    with open(os.path.join(d, fn), encoding="utf-8") as f:
                        md.append(f.read())
        blob = "\n".join(md)
        missing = [r["id"] for r in rows if f"id={r['id']}" not in blob]
        self.assertEqual(missing, [], f"第 {round_no} 轮有 {len(missing)} 条记忆在 md 里没有 id 注释")

    def test_concurrent_write_three_rounds(self):
        for r in range(3):
            with self.subTest(round=r):
                self._run_round(r)

    def test_supersede_then_sync_keeps_history(self):
        """被取代的行只加注释、不改正文：同步时不能当成用户删了。"""
        st = make_store(self.tmp, "hist")
        self.addCleanup(st.close)
        old = st.write(self.PATH, "fact", "博客部署在香橙派上", 0.7, reconcile=False)
        new = st.write(self.PATH, "fact", "博客已经迁到阿里云了", 0.7, reconcile=False)
        self.assertTrue(st.supersede(old, new))
        stats = st.sync_markdown(force=True)
        self.assertEqual(stats["deleted"], 0)
        with open(os.path.join(st.md_root, "project", "demo", "notes.md"), encoding="utf-8") as f:
            text = f.read()
        self.assertIn(f"id={old}", text)
        self.assertIn(f"superseded_by={new}", text)
        self.assertIn("博客部署在香橙派上", text)
