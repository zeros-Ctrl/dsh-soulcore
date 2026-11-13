"""v0.4：单实例锁、接入已有进程、NapCat 正向 WebSocket、主人身份合并。

   python -m unittest discover -s tests -v
"""
import base64
import hashlib
import json
import os
import socket
import struct
import threading
import time
import unittest
import netutil
import urllib.request

from test_soulcore import Base, make_engine
from soulcore.adapters.onebot_ws import OneBotWSAdapter
from soulcore.events import Event
from soulcore.lockfile import InstanceLock

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def _frame(payload: bytes, opcode: int = 0x1) -> bytes:
    """服务端→客户端：不掩码。"""
    out = bytearray([0x80 | opcode])
    n = len(payload)
    if n < 126:
        out.append(n)
    elif n < 65536:
        out.append(126)
        out += struct.pack(">H", n)
    else:
        out.append(127)
        out += struct.pack(">Q", n)
    return bytes(out) + payload


class FakeNapCat:
    """最小 NapCat 正向 WS 服务端：握手、收 action、发事件。"""

    def __init__(self):
        self.actions: list[dict] = []
        self.connected = threading.Event()
        self.replied = threading.Event()
        self.srv = socket.socket()
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        netutil.bind_random(self.srv)
        self.srv.listen(1)
        self.port = self.srv.getsockname()[1]
        self.conn = None
        threading.Thread(target=self._serve, daemon=True, name="fake-napcat").start()

    # ---------- 帧 ----------
    def _recv_json(self) -> dict:
        b1, b2 = self._recv(2)
        opcode = b1 & 0x0F
        masked = bool(b2 & 0x80)
        n = b2 & 0x7F
        if n == 126:
            n = struct.unpack(">H", self._recv(2))[0]
        elif n == 127:
            n = struct.unpack(">Q", self._recv(8))[0]
        mask = self._recv(4) if masked else b""
        data = self._recv(n)
        if masked:
            data = bytes(c ^ mask[i % 4] for i, c in enumerate(data))
        if opcode != 0x1:
            return {}
        return json.loads(data.decode("utf-8"))

    def _recv(self, n: int) -> bytes:
        buf = b""
        while len(buf) < n:
            chunk = self.conn.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("客户端断开")
            buf += chunk
        return buf

    def _send(self, obj: dict) -> None:
        self.conn.sendall(_frame(json.dumps(obj, ensure_ascii=False).encode()))

    # ---------- 流程 ----------
    def _serve(self) -> None:
        try:
            conn, _ = self.srv.accept()
            self.conn = conn
            req = b""
            while b"\r\n\r\n" not in req:
                req += conn.recv(4096)
            key = ""
            for line in req.decode("latin-1").split("\r\n"):
                if line.lower().startswith("sec-websocket-key:"):
                    key = line.split(":", 1)[1].strip()
            accept = base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()
            conn.sendall(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                          "Connection: Upgrade\r\n"
                          f"Sec-WebSocket-Accept: {accept}\r\n\r\n").encode())
            self.connected.set()
            hello = self._recv_json()               # get_login_info
            self.actions.append(hello)
            self._send({"status": "ok", "retcode": 0, "echo": hello.get("echo"),
                        "data": {"user_id": 999, "nickname": "小汐"}})
            self._send({"post_type": "message", "message_type": "private", "self_id": 999,
                        "user_id": 10001, "message_id": 1, "time": time.time(),
                        "sender": {"nickname": "主人"},
                        "message": [{"type": "text", "data": {"text": "在吗"}}]})
            for _ in range(4):
                msg = self._recv_json()
                self.actions.append(msg)
                self._send({"status": "ok", "retcode": 0, "echo": msg.get("echo"),
                            "data": {"message_id": 2}})
                if msg.get("action") == "send_private_msg":
                    self.replied.set()
                    break
        except Exception:
            pass

    def close(self) -> None:
        for s in (self.conn, self.srv):
            try:
                s and s.close()
            except OSError:
                pass


class TestLock(Base):
    def test_single_instance(self):
        p = os.path.join(self.tmp, "soulcore.lock")
        a = InstanceLock(p)
        self.assertTrue(a.acquire())
        a.write({"pid": 11, "port": 22, "token": "t"})
        self.assertEqual(InstanceLock.read(p)["port"], 22)
        b = InstanceLock(p)
        self.assertFalse(b.acquire(), "第二个实例必须抢锁失败")
        a.release()
        c = InstanceLock(p)
        self.assertTrue(c.acquire(), "锁释放后可以再抢")
        c.release()


class TestServeLock(Base):
    def test_second_serve_refused_and_token_required(self):
        from soulcore.server import serve
        lock = os.path.join(self.tmp, "soulcore.lock")
        eng, cfg = make_engine(self.tmp, self.clock, server={"port": 0, "tick_every_min": 999})
        httpd, onebot, stop = serve(eng, cfg, block=False, lock_path=lock)
        self.assertIsNotNone(httpd)
        info = InstanceLock.read(lock)
        self.assertEqual(info["pid"], os.getpid())
        self.assertTrue(info["token"])
        base = "http://127.0.0.1:%d" % httpd.server_address[1]

        def health(token=None):
            req = urllib.request.Request(base + "/v1/health",
                                         headers={"Authorization": f"Bearer {token}"} if token else {})
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status

        self.assertEqual(health(info["token"]), 200)
        with self.assertRaises(Exception):
            health()                                  # 没有 token：401
        # 同一把锁：第二个进程必须被拒
        eng2, cfg2 = make_engine(os.path.join(self.tmp, "second"), self.clock,
                                 server={"port": 0, "tick_every_min": 999})
        self.assertIsNone(serve(eng2, cfg2, block=False, lock_path=lock))
        eng2.close()
        stop.set()
        httpd.shutdown()
        httpd.server_close()
        httpd.soulcore_lock.release()
        eng.close()

    def test_attach_reads_lock_and_health(self):
        """插件侧的 attach()：读锁 + 健康检查通过就复用，不启动子进程。"""
        from soulcore.server import serve
        lock = os.path.join(self.tmp, "soulcore.lock")
        eng, cfg = make_engine(self.tmp, self.clock, server={"port": 0, "tick_every_min": 999})
        httpd, onebot, stop = serve(eng, cfg, block=False, lock_path=lock)
        info = InstanceLock.read(lock)
        # 模拟 JS 侧逻辑：pid 活 + /v1/health 200
        self.assertEqual(info["port"], httpd.server_address[1])
        req = urllib.request.Request(f"http://127.0.0.1:{info['port']}/v1/health",
                                     headers={"Authorization": f"Bearer {info['token']}"})
        with urllib.request.urlopen(req, timeout=5) as r:
            self.assertEqual(r.status, 200)
        stop.set()
        httpd.shutdown()
        httpd.server_close()
        httpd.soulcore_lock.release()
        eng.close()


class TestWS(Base):
    def test_ws_roundtrip(self):
        fake = FakeNapCat()
        try:
            eng, cfg = make_engine(
                self.tmp, self.clock,
                owner={"qq": "10001", "name": "主人"},
                qq={"enabled": True, "transport": "ws",
                    "ws_url": f"ws://127.0.0.1:{fake.port}",
                    "private_whitelist": ["10001"], "typing_delay": False, "min_interval_s": 0})
            ad = OneBotWSAdapter(eng, cfg["qq"]).start()
            self.assertTrue(fake.connected.wait(5), "客户端没连上假 NapCat")
            self.assertTrue(fake.replied.wait(8), "没等到 send_private_msg")
            self.assertEqual(fake.actions[0]["action"], "get_login_info")
            self.assertEqual(ad.onebot.self_id, "999", "应该用 get_login_info 自动拿到机器人 QQ")
            sent = [a for a in fake.actions if a.get("action") == "send_private_msg"]
            self.assertEqual(len(sent), 1)
            self.assertEqual(sent[0]["params"]["user_id"], 10001)
            text = sent[0]["params"]["message"][0]["data"]["text"]
            self.assertTrue(text)
            ad.onebot.q.join()  # 等发送确认后的写回完成
            turns = eng.store.recent_turns("chat/qq/private/10001")
            self.assertEqual([t.role for t in turns], ["user", "assistant"])
            ad.stop()
            eng.close()
        finally:
            fake.close()


class TestOwnerIdentity(Base):
    def test_owner_profile_merged(self):
        eng, _ = make_engine(self.tmp, self.clock, owner={"qq": "10001", "name": "主人"})
        # 老版本的人物档案在 core/people/local/owner，v0.4 启动时迁到 core/people/owner
        eng.store.write("core/people/local/owner", "fact", "主人喜欢深色主题", 0.9)
        eng.close()
        eng, _ = make_engine(self.tmp, self.clock, owner={"qq": "10001", "name": "主人"})
        paths = {r["path"] for r in eng.store.db.execute("SELECT DISTINCT path FROM items")}
        self.assertIn("core/people/owner", paths)
        self.assertNotIn("core/people/local/owner", paths)
        eng.close()

    def test_owner_qq_sees_projects_others_dont(self):
        eng, _ = make_engine(self.tmp, self.clock, owner={"qq": "10001", "name": "主人"})
        eng.store.write("project/dsh-soulcore", "decision", "部署用 Docker Compose，因为要一条命令起", 0.8,
                        trust="owner")
        owner_ev = Event("qq", "private", "10001", "10001", "主人", "我们那个项目怎么部署", ts=self.clock())
        self.assertIn("Docker", eng.prepare(owner_ev)["system"])
        other_ev = Event("qq", "private", "20002", "20002", "朋友", "我们那个项目怎么部署", ts=self.clock())
        self.assertNotIn("Docker", eng.prepare(other_ev)["system"])
        # 群聊也看不到
        group_ev = Event("qq", "group", "114514", "10001", "主人", "项目怎么部署", ts=self.clock())
        self.assertNotIn("Docker", eng.prepare(group_ev)["system"])
        eng.close()


if __name__ == "__main__":
    unittest.main()
