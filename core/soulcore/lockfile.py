"""单实例锁：同一份数据目录只能有一个 soulcore 在跑。

锁文件里同时存 `{pid, port, token, started, version}`——DSH 插件靠它**接入已在运行的进程**，
而不是再起一个。第二个进程抢不到锁就打印信息后退出，不会出现两个进程同时写
`memory.db` / `emotion.json` 的情况。

实现上分两个文件：
  soulcore.lock         放 JSON 信息（可以被任何进程随便读，原子替换写入）
  soulcore.lock.guard   只用来加操作系统锁（Windows `msvcrt` / POSIX `fcntl`）
分开是因为 Windows 的文件锁会挡住同一字节区间的读，锁着数据文件就读不出信息了。

进程被杀时操作系统会自动释放锁，所以残留的锁文件不会导致"永远起不来"，
是不是真的活着要靠 `pid` + `/v1/health` 判断。
"""
from __future__ import annotations

import json
import os
import time

try:                      # Windows
    import msvcrt
except ImportError:       # pragma: no cover
    msvcrt = None
try:                      # Linux / macOS
    import fcntl
except ImportError:       # pragma: no cover
    fcntl = None


class InstanceLock:
    def __init__(self, path: str):
        self.path = path
        self.guard = path + ".guard"
        self.fh = None

    def acquire(self) -> bool:
        """抢锁。成功返回 True；已有实例持有返回 False。"""
        os.makedirs(os.path.dirname(os.path.abspath(self.path)) or ".", exist_ok=True)
        fh = open(self.guard, "a+", encoding="utf-8")
        try:
            fh.seek(0)
            if msvcrt is not None:
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            elif fcntl is not None:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            try:
                fh.close()
            except OSError:
                pass
            return False
        self.fh = fh
        return True

    def write(self, info: dict) -> None:
        """原子写：先写 .tmp 再替换，别人不会读到半截 JSON。"""
        if self.fh is None:
            return
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(info, f, ensure_ascii=False)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass
        os.replace(tmp, self.path)

    def release(self) -> None:
        fh, self.fh = self.fh, None
        if fh is None:
            return
        # 正常退出时删掉信息文件：别人就不会拿着一个已经没人监听的端口/token 去接入
        info = self.read(self.path) if os.path.exists(self.path) else None
        if info and info.get("pid") == os.getpid():
            try:
                os.remove(self.path)
            except OSError:
                pass
        try:
            if msvcrt is not None:
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            elif fcntl is not None:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            try:
                fh.close()
            except OSError:
                pass

    @staticmethod
    def read(path: str) -> dict | None:
        """读锁文件里的信息（不抢锁）。文件正在被替换的瞬间可能读不到，重试几次。"""
        for _ in range(3):
            try:
                with open(path, encoding="utf-8") as f:
                    txt = f.read().strip()
                return json.loads(txt) if txt else None
            except (OSError, ValueError):
                time.sleep(0.05)
        return None

    @staticmethod
    def default_path(home: str) -> str:
        """锁文件放在 soulcore home（和 config.json 同级），不放在 data_dir。"""
        return os.path.join(home, "soulcore.lock")
