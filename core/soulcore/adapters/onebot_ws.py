"""QQ 接入：NapCat 正向 WebSocket（OneBot v11）。

为什么不用 HTTP 上报：上报要求 soulcore 监听固定端口并把地址填进 NapCat；WS 是 soulcore
主动连 NapCat，NapCat 那边只配一项（WebSocket 服务器），而且收发走同一条连接，断线能重连。

只用标准库：`socket` / `ssl` / `base64` / `hashlib` + 自己编解 RFC 6455 帧。
支持：握手鉴权、掩码帧、分片重组、ping/pong、关闭帧、指数退避重连、按 `echo` 匹配 action 结果。
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import queue
import select
import socket
import ssl
import struct
import threading
import time
import uuid
from urllib.parse import urlparse

from .onebot import OneBotAdapter

log = logging.getLogger("soulcore.qq.ws")

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class WSClient:
    """最小可用的 WebSocket 客户端：只做文本帧 + ping/pong + 重连。"""

    def __init__(self, url: str, token: str = "", self_id: str = "", timeout: float = 15.0,
                 idle_timeout: float = 75.0):
        self.url = url
        self.token = token or ""
        self.self_id = str(self_id or "")
        self.timeout = timeout
        self.idle_timeout = float(idle_timeout or 0)
        self.sock: socket.socket | None = None
        self.stop = threading.Event()
        self.pending: dict[str, queue.Queue] = {}
        self._wlock = threading.Lock()
        self.on_open = None            # 连接建立后回调（用于 get_login_info）

    # ------------------------------------------------------------------ 生命周期
    def run_forever(self, on_event) -> None:
        delay = 1.0
        while not self.stop.is_set():
            try:
                self._connect()
                delay = 1.0
                log.info("已连接 %s", self.url)
                if callable(self.on_open):
                    # on_open 里要发 action 并等 echo，必须和读循环并行，否则自己等自己
                    threading.Thread(target=self._safe_open, daemon=True, name="onebot-ws-hello").start()
                self._loop(on_event)
            except Exception as ex:
                if not self.stop.is_set():
                    log.warning("WebSocket 断开：%s（%d 秒后重连）", ex, int(delay))
            finally:
                self._close_sock()
            if self.stop.wait(delay):
                break
            delay = min(30.0, delay * 2)

    def _safe_open(self) -> None:
        try:
            self.on_open()
        except Exception as ex:      # 连上但握手动作失败：不影响收消息
            log.warning("连接后握手动作失败：%s", ex)

    def close(self) -> None:
        self.stop.set()
        self._close_sock()

    def _close_sock(self) -> None:
        sock, self.sock = self.sock, None
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass

    # ------------------------------------------------------------------ 连接与握手
    def _connect(self) -> None:
        u = urlparse(self.url)
        if u.scheme not in ("ws", "wss") or not u.hostname:
            raise ValueError(f"ws_url 不合法：{self.url!r}")
        port = u.port or (443 if u.scheme == "wss" else 80)
        sock = socket.create_connection((u.hostname, port), timeout=self.timeout)
        if u.scheme == "wss":
            sock = ssl.create_default_context().wrap_socket(sock, server_hostname=u.hostname)
        key = base64.b64encode(os.urandom(16)).decode()
        path = (u.path or "/") + (("?" + u.query) if u.query else "")
        headers = [
            f"GET {path} HTTP/1.1",
            f"Host: {u.hostname}:{port}",
            "Upgrade: websocket",
            "Connection: Upgrade",
            f"Sec-WebSocket-Key: {key}",
            "Sec-WebSocket-Version: 13",
        ]
        if self.token:
            headers.append(f"Authorization: Bearer {self.token}")
        if self.self_id:
            headers.append(f"X-Self-ID: {self.self_id}")
        headers.append("X-Client-Role: Universal")
        sock.sendall(("\r\n".join(headers) + "\r\n\r\n").encode())
        resp = self._read_http_head(sock)
        first = resp.split("\r\n", 1)[0]
        if " 101 " not in first:
            raise ConnectionError(f"握手失败：{first}")
        accept = base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()
        if f"sec-websocket-accept: {accept}".lower() not in resp.lower():
            raise ConnectionError("Sec-WebSocket-Accept 不匹配（可能连到了普通 HTTP 端口）")
        sock.settimeout(None)
        self.sock = sock

    @staticmethod
    def _read_http_head(sock: socket.socket) -> str:
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = sock.recv(4096)
            if not chunk:
                raise ConnectionError("握手时连接被关闭")
            buf += chunk
            if len(buf) > 65536:
                raise ConnectionError("握手响应过大")
        return buf.split(b"\r\n\r\n", 1)[0].decode("latin-1")

    # ------------------------------------------------------------------ 收帧
    def _recv_exact(self, n: int) -> bytes:
        sock = self.sock
        if sock is None:
            raise ConnectionError("连接已关闭")
        buf = b""
        while len(buf) < n:
            chunk = sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("连接被另一端关闭")
            buf += chunk
        return buf

    def _read_frame(self) -> tuple[bool, int, bytes]:
        b1, b2 = self._recv_exact(2)
        fin = bool(b1 & 0x80)
        opcode = b1 & 0x0F
        masked = bool(b2 & 0x80)
        length = b2 & 0x7F
        if length == 126:
            length = struct.unpack(">H", self._recv_exact(2))[0]
        elif length == 127:
            length = struct.unpack(">Q", self._recv_exact(8))[0]
        mask = self._recv_exact(4) if masked else None
        data = self._recv_exact(length) if length else b""
        if mask:
            data = bytes(c ^ mask[i % 4] for i, c in enumerate(data))
        return fin, opcode, data

    def _wait_readable(self) -> None:
        """空闲检测：idle_timeout 秒没收到任何帧就发一次 ping，再等一轮还没动静就判定连接已死。

        NapCat 默认每 30 秒发一次心跳事件；没开心跳时也会回 pong，所以不会误判。
        进程被杀、网线拔掉这类"半开连接"，之前会一直阻塞在 recv 上，永远不重连。"""
        sock = self.sock
        if sock is None or not self.idle_timeout:
            return
        pinged = False
        while not self.stop.is_set():
            if isinstance(sock, ssl.SSLSocket) and sock.pending():
                return
            r, _, _ = select.select([sock], [], [], self.idle_timeout)
            if r:
                return
            if pinged:
                raise ConnectionError(f"{int(self.idle_timeout * 2)} 秒没有任何数据，连接可能已断开")
            self._send_frame(0x9, b"hb")
            pinged = True

    def _loop(self, on_event) -> None:
        buf = bytearray()
        frame_op = 0
        while not self.stop.is_set():
            self._wait_readable()
            fin, op, data = self._read_frame()
            if op == 0x9:                      # ping
                self._send_frame(0xA, data)
                continue
            if op == 0xA:                      # pong
                continue
            if op == 0x8:                      # close
                raise ConnectionError("服务端关闭了连接")
            if op == 0x0:                      # 分片续帧
                buf += data
            else:
                buf = bytearray(data)
                frame_op = op
            if not fin:
                continue
            payload = bytes(buf)
            buf = bytearray()
            if frame_op != 0x1:
                continue                       # 只关心文本帧
            try:
                msg = json.loads(payload.decode("utf-8", "replace") or "{}")
            except ValueError:
                log.warning("收到非 JSON 帧：%r", payload[:120])
                continue
            self._dispatch(msg, on_event)

    def _dispatch(self, msg: dict, on_event) -> None:
        echo = msg.get("echo")
        if echo and echo in self.pending:
            try:
                self.pending[echo].put_nowait(msg)
            except queue.Full:
                pass
            return
        try:
            on_event(msg)
        except Exception:
            log.exception("处理 QQ 事件失败")

    # ------------------------------------------------------------------ 发帧
    def _send_frame(self, opcode: int, payload: bytes) -> None:
        sock = self.sock
        if sock is None:
            raise ConnectionError("连接已关闭")
        header = bytearray([0x80 | opcode])
        n = len(payload)
        if n < 126:
            header.append(0x80 | n)
        elif n < 65536:
            header.append(0x80 | 126)
            header += struct.pack(">H", n)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", n)
        mask = os.urandom(4)
        header += mask
        masked = bytes(c ^ mask[i % 4] for i, c in enumerate(payload))
        with self._wlock:
            sock.sendall(bytes(header) + masked)

    def call_action(self, action: str, params: dict | None = None, timeout: float = 20.0) -> dict:
        echo = uuid.uuid4().hex
        q: queue.Queue = queue.Queue(maxsize=1)
        self.pending[echo] = q
        try:
            self._send_frame(0x1, json.dumps(
                {"action": action, "params": params or {}, "echo": echo}, ensure_ascii=False).encode())
            try:
                return q.get(timeout=timeout)
            except queue.Empty:
                raise TimeoutError(f"OneBot action 超时：{action}")
        finally:
            self.pending.pop(echo, None)


def probe(url: str, token: str = "", timeout: float = 8.0) -> dict:
    """设置页"测试连接"用：连一次 NapCat，问 get_login_info，然后断开。

    不影响正在运行的 QQ 连接（另开一条）。返回 {ok, user_id, nickname} 或 {ok: False, error, hint}。"""
    c = WSClient(url or "", token=token or "", timeout=timeout, idle_timeout=0)
    try:
        try:
            c._connect()
        except ValueError:
            return {"ok": False, "error": f"地址格式不对：{url!r}", "hint": "应该像 ws://127.0.0.1:3001"}
        except (ConnectionRefusedError, socket.timeout, TimeoutError):
            return {"ok": False, "error": "连不上 NapCat",
                    "hint": "确认 NapCat 已启动并登录，WebUI「网络配置」里的 WebSocket 服务器已启用，端口和这里一致"}
        except ConnectionError as ex:
            msg = str(ex)
            if "401" in msg or "403" in msg:
                return {"ok": False, "error": "token 不对（NapCat 拒绝了连接）",
                        "hint": "把 NapCat WebSocket 服务器里的 Token 原样粘贴过来；注意不是 WebUI 的登录 token"}
            if "Accept" in msg or "握手失败" in msg:
                return {"ok": False, "error": "这个端口不是 WebSocket 服务器",
                        "hint": "可能填成了 HTTP 服务端口或 WebUI 端口（6099）。在 NapCat「网络配置」里新建的是 WebSocket 服务器吗？"}
            return {"ok": False, "error": f"连接失败：{msg}"}
        except OSError as ex:
            return {"ok": False, "error": f"连接失败：{ex}"}
        echo = uuid.uuid4().hex
        c.sock.settimeout(timeout)
        c._send_frame(0x1, json.dumps({"action": "get_login_info", "params": {}, "echo": echo}).encode())
        end = time.time() + timeout
        while time.time() < end:
            fin, op, data = c._read_frame()
            if op == 0x8:
                return {"ok": False, "error": "NapCat 主动断开了连接",
                        "hint": "通常是 token 不对：把 NapCat WebSocket 服务器里的 Token 原样粘贴过来"}
            if op != 0x1:
                continue
            try:
                msg = json.loads(data.decode("utf-8", "replace") or "{}")
            except ValueError:
                continue
            if msg.get("retcode") == 1403:
                return {"ok": False, "error": "token 不对（retcode 1403）",
                        "hint": "把 NapCat WebSocket 服务器里的 Token 原样粘贴过来"}
            if msg.get("echo") != echo:
                continue                      # 心跳、生命周期事件等
            d = msg.get("data") or {}
            if not d.get("user_id"):
                return {"ok": False, "error": f"NapCat 返回异常：{str(msg)[:160]}", "hint": "QQ 是否已扫码登录？"}
            return {"ok": True, "user_id": str(d.get("user_id")), "nickname": d.get("nickname") or ""}
        return {"ok": False, "error": "连上了，但 NapCat 没有回应", "hint": "QQ 是否已扫码登录？"}
    except (socket.timeout, TimeoutError):
        return {"ok": False, "error": "连上了，但 NapCat 没有回应", "hint": "QQ 是否已扫码登录？"}
    except Exception as ex:   # 设置页只要一个可读的结果，不能抛
        return {"ok": False, "error": f"连接失败：{ex}"}
    finally:
        c._close_sock()


class OneBotWSAdapter:
    """把 WS 收到的上报喂给 OneBotAdapter，发送时走 WS 的 send_* action。

    事件转换、白名单、限流、记忆写入都复用 `OneBotAdapter`，这里只负责传输。
    """

    def __init__(self, engine, cfg: dict):
        self.cfg = cfg
        self.client = WSClient(cfg.get("ws_url") or "ws://127.0.0.1:3001",
                               token=cfg.get("access_token", ""),
                               self_id=cfg.get("self_id", ""),
                               idle_timeout=cfg.get("ws_idle_timeout_s", 75))
        self.onebot = OneBotAdapter(engine, cfg, api_fn=self.client.call_action)
        self.client.on_open = self._hello
        self.thread: threading.Thread | None = None

    def _hello(self) -> None:
        """连上后问一次登录信息，自动拿到机器人自己的 QQ 号，不用手填 self_id。"""
        try:
            res = self.client.call_action("get_login_info", {}, timeout=8)
        except Exception as ex:
            log.info("get_login_info 失败（不影响收消息）：%s", ex)
            return
        data = res.get("data") or {}
        uid = str(data.get("user_id") or "")
        if uid:
            self.onebot.self_id = uid
            log.info("已连接 NapCat：机器人 QQ=%s（%s）", uid, data.get("nickname") or "")

    def start(self) -> "OneBotWSAdapter":
        self.thread = threading.Thread(target=self.client.run_forever, args=(self.onebot.on_post,),
                                       daemon=True, name="onebot-ws")
        self.thread.start()
        return self

    def stop(self) -> None:
        self.client.close()
        self.onebot.stop()

    @property
    def connected(self) -> bool:
        return self.client.sock is not None

    def wait_connected(self, timeout: float = 5.0) -> bool:
        end = time.time() + timeout
        while time.time() < end:
            if self.connected:
                return True
            time.sleep(0.05)
        return False
