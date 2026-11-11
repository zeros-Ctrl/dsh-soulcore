"""测试用的网络小工具：在 Windows 上绑定随机端口时，遇到 WinError 10013 自动重试。

Hyper-V / WinNAT 会在动态端口范围里保留大段端口，系统偶尔把随机端口分到保留段里，
bind 就报 10013（`netsh int ipv4 show excludedportrange protocol=tcp` 可以看到保留段）。
这和被测代码无关，只会让测试随机失败，所以在这里统一重试。产品代码里对应的处理见
`soulcore.server.bind_http_server`。
"""
import socket
import time


def _retry(fn, attempts: int = 10):
    """指数退避，总共约 10 秒：实测这种拒绝有时会持续几秒，期间所有回环 bind 都失败。"""
    for i in range(attempts):
        try:
            return fn()
        except OSError as ex:
            if getattr(ex, "winerror", None) != 10013 or i == attempts - 1:
                raise
            time.sleep(min(2.0, 0.05 * (2 ** i)))


def bind_random(sock: socket.socket, host: str = "127.0.0.1") -> int:
    """把 sock 绑到 host 的随机端口，返回端口号。"""
    _retry(lambda: sock.bind((host, 0)))
    return sock.getsockname()[1]


def socketpair():
    return _retry(socket.socketpair)


def http_server(cls, handler, host: str = "127.0.0.1"):
    return _retry(lambda: cls((host, 0), handler))
