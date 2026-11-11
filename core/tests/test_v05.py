"""v0.5：设置页 QQ 卡片背后的后端能力。

  - probe()：设置页"测试连接"，各种失败要给出能看懂的提示
  - /v1/qq/status、/v1/shutdown（切换常驻 / 重启常驻进程用，退出后要释放锁）
  - 主人的私聊自动放行；transport 默认 ws

   python -m unittest discover -s tests -v
"""
import json
import os
import socket
import tempfile
import threading
import time
import unittest
import netutil
import urllib.request

from test_soulcore import Base, make_engine
from test_v04 import FakeNapCat
from soulcore.adapters.onebot import OneBotAdapter
from soulcore.adapters.onebot_ws import probe
from soulcore.config import load_config
from soulcore.lockfile import InstanceLock


def _free_port() -> int:
    s = socket.socket(); p = netutil.bind_random(s); s.close()
    return p


class _OneShot:
    """收到一个连接就回一段固定的 HTTP 响应（模拟"不是 WS 服务"、"token 不对"）。"""

    def __init__(self, response: bytes):
        self.srv = socket.socket()
        netutil.bind_random(self.srv); self.srv.listen(1)
        self.port = self.srv.getsockname()[1]
        threading.Thread(target=self._run, args=(response,), daemon=True).start()

    def _run(self, response):
        try:
            c, _ = self.srv.accept()
            buf = b""
            while b"\r\n\r\n" not in buf:
                buf += c.recv(4096)
            c.sendall(response)
            time.sleep(0.2)
            c.close()
        except OSError:
            pass

    def close(self):
        self.srv.close()


class TestProbe(unittest.TestCase):
    def test_ok(self):
        fake = FakeNapCat()
        try:
            r = probe(f"ws://127.0.0.1:{fake.port}", "tk", timeout=5)
            self.assertTrue(r["ok"], r)
            self.assertEqual(r["user_id"], "999")
            self.assertEqual(r["nickname"], "小汐")
        finally:
            fake.close()

    def test_refused(self):
        r = probe(f"ws://127.0.0.1:{_free_port()}", timeout=2)
        self.assertFalse(r["ok"])
        self.assertIn("连不上", r["error"])
        self.assertIn("NapCat", r["hint"])

    def test_bad_url(self):
        r = probe("http://127.0.0.1:3001", timeout=2)
        self.assertFalse(r["ok"])
        self.assertIn("地址格式不对", r["error"])

    def test_not_websocket(self):
        srv = _OneShot(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")
        try:
            r = probe(f"ws://127.0.0.1:{srv.port}", timeout=3)
            self.assertFalse(r["ok"])
            self.assertIn("不是 WebSocket", r["error"])
        finally:
            srv.close()

    def test_wrong_token(self):
        srv = _OneShot(b"HTTP/1.1 401 Unauthorized\r\nContent-Length: 0\r\n\r\n")
        try:
            r = probe(f"ws://127.0.0.1:{srv.port}", "bad", timeout=3)
            self.assertFalse(r["ok"])
            self.assertIn("token 不对", r["error"])
        finally:
            srv.close()


class TestShutdown(Base):
    def test_shutdown_releases_lock(self):
        from soulcore.server import serve
        eng, cfg = make_engine(self.tmp, self.clock)
        cfg["server"]["port"] = 0
        lock_path = os.path.join(self.tmp, "soulcore.lock")
        th = threading.Thread(target=serve, args=(eng, cfg), kwargs={"lock_path": lock_path}, daemon=True)
        th.start()
        info = None
        for _ in range(100):
            info = InstanceLock.read(lock_path) if os.path.exists(lock_path) else None
            if info and info.get("port"):
                break
            time.sleep(0.05)
        self.assertTrue(info and info.get("port"), "serve 没写锁文件")
        base = f"http://127.0.0.1:{info['port']}"
        hdr = {"Authorization": f"Bearer {info['token']}", "Content-Type": "application/json"}

        with urllib.request.urlopen(urllib.request.Request(base + "/v1/qq/status", headers=hdr), timeout=5) as r:
            self.assertEqual(json.loads(r.read())["enabled"], False)

        req = urllib.request.Request(base + "/v1/shutdown", data=b"{}", headers=hdr, method="POST")
        with urllib.request.urlopen(req, timeout=5) as r:
            self.assertTrue(json.loads(r.read())["ok"])
        th.join(10)
        self.assertFalse(th.is_alive(), "shutdown 之后 serve 应该返回")
        self.assertFalse(os.path.exists(lock_path), "正常退出应该删掉锁信息文件")
        other = InstanceLock(lock_path)
        self.assertTrue(other.acquire(), "锁应该已经释放，新的实例能拿到")
        other.release()

    def test_shutdown_requires_token(self):
        from soulcore.server import serve
        eng, cfg = make_engine(self.tmp, self.clock)
        cfg["server"]["port"] = 0
        lock_path = os.path.join(self.tmp, "soulcore.lock")
        httpd, _, stop = serve(eng, cfg, block=False, lock_path=lock_path)
        try:
            port = httpd.server_address[1]
            req = urllib.request.Request(f"http://127.0.0.1:{port}/v1/shutdown", data=b"{}", method="POST")
            with self.assertRaises(urllib.error.HTTPError) as cm:
                urllib.request.urlopen(req, timeout=5)
            self.assertEqual(cm.exception.code, 401)
        finally:
            stop.set(); httpd.shutdown(); httpd.server_close()
            httpd.soulcore_lock.release()
            eng.close()


class TestOwnerPrivateAllowed(Base):
    def test_owner_private_needs_no_whitelist(self):
        eng, cfg = make_engine(self.tmp, self.clock, owner={"qq": "10001"},
                               qq={"private_whitelist": [], "allow_all_private": False})
        ad = OneBotAdapter(eng, cfg["qq"])
        try:
            msg = lambda uid: {"post_type": "message", "message_type": "private", "user_id": uid, "self_id": 999,
                               "message_id": 1, "sender": {"nickname": "x"},
                               "message": [{"type": "text", "data": {"text": "在吗"}}]}
            ev, reply = ad.to_event(msg(10001))
            self.assertIsNotNone(ev, "主人的私聊应该自动放行")
            self.assertTrue(reply)
            self.assertEqual(eng._trust(ev), "owner")
            ev2, _ = ad.to_event(msg(20002))
            self.assertIsNone(ev2, "其他人仍然需要白名单")
        finally:
            ad.stop()
            eng.close()


class TestBindRetry(unittest.TestCase):
    """Windows 动态端口范围里有 Hyper-V/WinNAT 保留段：随机端口偶尔被拒（10013），要重试。"""

    def _err(self):
        e = PermissionError(13, "access denied")
        e.winerror = 10013
        return e

    def test_random_port_retries_on_10013(self):
        from unittest import mock
        from soulcore import server
        calls = []

        def fake(addr, handler):
            calls.append(addr)
            if len(calls) < 3:
                raise self._err()
            return "httpd"
        with mock.patch.object(server, "ThreadingHTTPServer", side_effect=fake):
            self.assertEqual(server.bind_http_server("127.0.0.1", 0, None), "httpd")
        self.assertEqual(len(calls), 3)

    def test_fixed_port_does_not_retry(self):
        from unittest import mock
        from soulcore import server
        with mock.patch.object(server, "ThreadingHTTPServer", side_effect=self._err()) as m:
            with self.assertRaises(PermissionError):
                server.bind_http_server("127.0.0.1", 8765, None)
        self.assertEqual(m.call_count, 1)


class TestDefaults(unittest.TestCase):
    def test_transport_defaults_to_ws(self):
        cfg, _ = load_config(None)
        self.assertEqual(cfg["qq"]["transport"], "ws")
        self.assertEqual(cfg["qq"]["ws_url"], "ws://127.0.0.1:3001")


if __name__ == "__main__":
    unittest.main()
