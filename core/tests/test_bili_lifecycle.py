"""Deterministic shutdown regressions for independent video worker threads."""
import tempfile
import threading
import time
import unittest
from unittest import mock

from test_bili_chat import make, priv


class BiliLifecycleTest(unittest.TestCase):
    def test_close_wakes_first_reply_and_follow_up_gap_waiters(self):
        for first_sent in (False, True):
            with self.subTest(first_sent=first_sent), tempfile.TemporaryDirectory() as tmp:
                eng = make(tmp)
                eng.bili_chat.after_reply_gap_s = 60
                composed = threading.Event()

                def chat(*args, **kwargs):
                    composed.set()
                    return "没有字幕"

                with mock.patch("soulcore.bili_watch.fetch_subtitles", return_value={
                        "title": "视频", "text": "", "reason": "没有字幕"}), \
                        mock.patch.object(eng.llm, "chat", side_effect=chat):
                    ev = priv(eng, "第一个讲了啥")
                    eng.bili_chat._start_watch(ev, {"bv": "BV1aa411c7mD", "title": "视频"})
                    if first_sent:
                        eng.bili_chat.on_reply(ev)
                    self.assertTrue(composed.wait(2))
                    worker = next(t for t in threading.enumerate() if t.name == "bili-video")
                    before = time.monotonic()
                    eng.close()
                    self.assertLess(time.monotonic() - before, 1, "reply/gap cancellation should wake immediately")
                    self.assertFalse(worker.is_alive())

    def test_close_during_fetch_does_not_prepare_after_database_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            eng = make(tmp)
            started, release = threading.Event(), threading.Event()
            closed, late = [], []
            real_prepare = eng.prepare

            def fetch(*args):
                started.set()
                release.wait(10)
                return {"title": "视频", "text": "", "reason": "没有字幕"}

            def prepare(ev):
                if closed:
                    late.append("prepare")
                    raise RuntimeError("late prepare after close")
                return real_prepare(ev)

            with mock.patch("soulcore.bili_watch.fetch_subtitles", side_effect=fetch), \
                    mock.patch.object(eng, "prepare", side_effect=prepare), \
                    mock.patch("soulcore.bili_chat.log.exception"):
                ev = priv(eng, "第一个讲了啥")
                eng.bili_chat._start_watch(ev, {"bv": "BV1aa411c7mD", "title": "视频"})
                self.assertTrue(started.wait(2))
                worker = next(t for t in threading.enumerate() if t.name == "bili-video")
                try:
                    before = time.monotonic()
                    eng.close()
                    closed.append(True)
                    self.assertLess(time.monotonic() - before, 4, "close must bound its wait for external I/O")
                finally:
                    release.set()
                    worker.join(3)
                self.assertFalse(worker.is_alive())
                self.assertEqual(late, [])

    def test_close_during_model_request_cancels_follow_up(self):
        with tempfile.TemporaryDirectory() as tmp:
            eng = make(tmp)
            started, release = threading.Event(), threading.Event()
            sent = []

            def chat(*args, **kwargs):
                started.set()
                release.wait(10)
                return "视频没有字幕"

            eng.qq_sender = lambda ev, text: sent.append(text) or True
            with mock.patch("soulcore.bili_watch.fetch_subtitles", return_value={
                    "title": "视频", "text": "", "reason": "没有字幕"}), \
                    mock.patch.object(eng.llm, "chat", side_effect=chat), \
                    mock.patch("soulcore.bili_chat.log.exception"):
                ev = priv(eng, "第一个讲了啥")
                eng.bili_chat._start_watch(ev, {"bv": "BV1aa411c7mD", "title": "视频"})
                eng.bili_chat.on_reply(ev)
                self.assertTrue(started.wait(2))
                worker = next(t for t in threading.enumerate() if t.name == "bili-video")
                try:
                    eng.close()
                finally:
                    release.set()
                    worker.join(3)
                self.assertFalse(worker.is_alive())
                self.assertEqual(sent, [])

    def test_close_during_send_does_not_commit_after_database_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            eng = make(tmp)
            started, release = threading.Event(), threading.Event()
            closed, late = [], []
            real_commit = eng.commit_reply

            def sender(ev, text):
                started.set()
                release.wait(10)
                return True

            def commit(ev, text, **kwargs):
                if closed:
                    late.append("commit")
                    return
                return real_commit(ev, text, **kwargs)

            eng.qq_sender = sender
            with mock.patch("soulcore.bili_watch.fetch_subtitles", return_value={
                    "title": "视频", "text": "", "reason": "没有字幕"}), \
                    mock.patch.object(eng.llm, "chat", return_value="没有字幕"), \
                    mock.patch.object(eng, "commit_reply", side_effect=commit), \
                    mock.patch("soulcore.bili_chat.log.exception"):
                ev = priv(eng, "第一个讲了啥")
                eng.bili_chat._start_watch(ev, {"bv": "BV1aa411c7mD", "title": "视频"})
                eng.bili_chat.on_reply(ev)
                self.assertTrue(started.wait(2))
                worker = next(t for t in threading.enumerate() if t.name == "bili-video")
                try:
                    eng.close()
                    closed.append(True)
                finally:
                    release.set()
                    worker.join(3)
                self.assertFalse(worker.is_alive())
                self.assertEqual(late, [])


if __name__ == "__main__":
    unittest.main()
