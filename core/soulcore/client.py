"""soulcore.client —— 给短命进程（Claude Code hook / MCP server / 命令行）用的后端客户端。

这些进程每次执行都要在几十毫秒内给出答案，不能每次都初始化 Engine（要读配置、开数据库、
跑迁移），所以统一走**本地常驻后端**（已有的 `serve` 单实例）：

  1. `resolve_home()`  决定数据目录（和 DSH 共享同一份记忆）；
  2. `Backend.discover()`  读 `soulcore.lock` + 探 `/v1/health`，找到正在跑的后端；
  3. `Backend.ensure()`    没找到就**后台分离**地拉起一个（带 `--idle-exit-min`，没人用会自己退）；
  4. `Backend.request()`   带 Bearer token 调接口，token 失效/连接断了自动重读锁文件重试一次。

只用标准库，Python 3.10+。
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

log = logging.getLogger("soulcore")

CORE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUN_PY = os.path.join(CORE_DIR, "run.py")

# 启动过程中写这个标记：10 秒内已经有人在启动，就别再 spawn 一个（单实例锁会兜底，
# 但多个 hook 并发各起一个进程会反复 spawn/退出，日志也乱）
STARTING_MARK = "serve.starting"
STARTING_TTL = 10.0


class BackendUnavailable(RuntimeError):
    """后端没启动 / 连不上。调用方自己决定降级（hook 一律 fail-open）。"""


def user_home() -> str:
    return os.path.expanduser("~")


def resolve_home(create: bool = True) -> str:
    """数据目录：SOULCORE_HOME → ~/.dsh/soulcore（和 DSH 共享）→ ~/.soulcore。"""
    env = os.environ.get("SOULCORE_HOME")
    if env:
        home = os.path.abspath(os.path.expanduser(env))
    elif os.path.isdir(os.path.join(user_home(), ".dsh")):
        home = os.path.join(user_home(), ".dsh", "soulcore")
    else:
        home = os.path.join(user_home(), ".soulcore")
    if create:
        ensure_home(home)
    return home


def ensure_home(home: str) -> str:
    """第一次使用时建目录，并写入默认配置和人设模板（api_key 留空，不打扰用户）"""
    os.makedirs(home, exist_ok=True)
    cfg = os.path.join(home, "config.json")
    if not os.path.exists(cfg):
        src = os.path.join(CORE_DIR, "config.example.json")
        try:
            shutil.copyfile(src, cfg)
        except OSError:
            with open(cfg, "w", encoding="utf-8") as f:
                json.dump({"data_dir": "./data", "llm": {"api_key": ""}}, f, ensure_ascii=False, indent=2)
    persona = os.path.join(home, "persona.md")
    if not os.path.exists(persona):
        src = os.path.join(CORE_DIR, "persona.example.md")
        try:
            shutil.copyfile(src, persona)
        except OSError:
            with open(persona, "w", encoding="utf-8") as f:
                f.write("你叫{name}。\n")
    return home


def pid_alive(pid) -> bool:
    """进程还在不在（不验证是不是同一个程序；调用方再用 /v1/health 确认）"""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h:
            return False
        try:
            code = ctypes.c_ulong()
            if not k32.GetExitCodeProcess(h, ctypes.byref(code)):
                return False
            return code.value == STILL_ACTIVE
        finally:
            k32.CloseHandle(h)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _pythonw() -> str | None:
    """Windows 上优先 pythonw.exe：分离启动时不弹黑框"""
    if os.name != "nt":
        return None
    exe = sys.executable or ""
    if exe.lower().endswith("python.exe"):
        cand = exe[:-len("python.exe")] + "pythonw.exe"
        if os.path.isfile(cand):
            return cand
    return None


class Backend:
    def __init__(self, home: str | None = None, python: str | None = None, mock: bool = False,
                 idle_exit_min: int = 240):
        self.home = home or resolve_home()
        self.python = python or sys.executable or ("python3" if os.name != "nt" else "python")
        self.mock = mock
        self.idle_exit_min = int(idle_exit_min)
        self._base: str | None = None
        self._token: str = ""
        self._lock = threading.Lock()

    # ---------------- 发现 / 启动 ----------------
    @property
    def lock_path(self) -> str:
        return os.path.join(self.home, "soulcore.lock")

    def discover(self) -> tuple[str, str] | None:
        """锁文件里的后端还活着吗？活着返回 (base_url, token)。"""
        from .lockfile import InstanceLock
        info = InstanceLock.read(self.lock_path)
        if not info or not info.get("port"):
            return None
        if info.get("pid") and not pid_alive(info["pid"]):
            return None
        base = "http://127.0.0.1:%s" % info["port"]
        token = str(info.get("token") or "")
        try:
            req = urllib.request.Request(base + "/v1/health",
                                         headers={"Authorization": f"Bearer {token}"})
            with urllib.request.urlopen(req, timeout=0.5) as r:
                if r.status != 200:
                    return None
        except (urllib.error.URLError, OSError, ValueError):
            return None
        self._base, self._token = base, token
        return base, token

    def ensure(self, start: bool = True, wait_s: float = 3.0) -> tuple[str, str] | None:
        """确保后端可用。start=False 时只找、不拉进程（比如 UserPromptSubmit 不能拖慢输入）。"""
        found = self.discover()
        if found or not start:
            return found
        with self._lock:
            found = self.discover()
            if found:
                return found
            self._spawn()
            deadline = time.time() + max(0.2, float(wait_s))
            while time.time() < deadline:
                time.sleep(0.1)
                found = self.discover()
                if found:
                    return found
        return None

    def _spawn(self) -> None:
        logs = os.path.join(self.home, "logs")
        os.makedirs(logs, exist_ok=True)
        ensure_home(self.home)
        marker = os.path.join(logs, STARTING_MARK)
        try:
            if os.path.exists(marker) and time.time() - os.path.getmtime(marker) < STARTING_TTL:
                log.info("已经有进程在启动 soulcore 后端，只等待")
                return
        except OSError:
            pass
        try:
            with open(marker, "w", encoding="utf-8") as f:
                f.write(str(os.getpid()))
        except OSError:
            pass
        cfg = os.path.join(self.home, "config.json")
        py = _pythonw() or self.python
        cmd = [py, "-I", RUN_PY, "--config", cfg]
        if self.mock:
            cmd.append("--mock")
        # 一定要随机端口：固定 8765 会和用户已有的常驻后端 / DSH 抢端口
        cmd += ["serve", "--port", "0"]
        if self.idle_exit_min > 0:
            cmd += ["--idle-exit-min", str(self.idle_exit_min)]
        env = {
            **os.environ,
            "SOULCORE_HOME": self.home,
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUTF8": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        env.pop("SOULCORE_TOKEN", None)          # 让后端自己生成 token 写进锁文件
        env.pop("SOULCORE_STDIN_WATCHDOG", None)  # 分离进程没有 stdin 管道可监听
        kwargs: dict = {}
        if os.name == "nt":
            DETACHED_PROCESS = 0x00000008
            CREATE_NEW_PROCESS_GROUP = 0x00000200
            CREATE_NO_WINDOW = 0x08000000
            si = subprocess.STARTUPINFO()
            si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            kwargs.update(creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW,
                          startupinfo=si)
        else:
            kwargs["start_new_session"] = True
        log_path = os.path.join(logs, "serve.log")
        try:
            with open(log_path, "ab") as out:
                subprocess.Popen(cmd, cwd=CORE_DIR, env=env, stdin=subprocess.DEVNULL,
                                 stdout=out, stderr=subprocess.STDOUT, close_fds=True, **kwargs)
        except OSError as ex:
            log.warning("启动 soulcore 后端失败：%s（看 %s）", ex, log_path)

    # ---------------- 请求 ----------------
    def _call(self, base: str, token: str, method: str, path: str, body, timeout: float):
        data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
        req = urllib.request.Request(base + path, data=data, method=method.upper(), headers={
            "Authorization": f"Bearer {token}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read()
                return json.loads(raw.decode() or "null")
        except urllib.error.HTTPError as ex:
            if ex.code == 401:
                raise _Unauthorized() from ex
            raise BackendUnavailable(f"HTTP {ex.code}: {ex.read()[:200]!r}") from ex
        except (urllib.error.URLError, OSError) as ex:
            raise _ConnLost(str(ex)) from ex

    def request(self, method: str, path: str, body=None, timeout: float = 5.0):
        """带 token 调后端。401 / 连接被拒时重新 discover 一次再重试。"""
        found = (self._base, self._token) if (self._base and self._token) else self.discover()
        if found is None:
            raise BackendUnavailable("soulcore 后端没有启动")
        try:
            return self._call(found[0], found[1], method, path, body, timeout)
        except _Unauthorized:
            # 常驻进程重启过：端口/token 都变了，重读锁文件再来
            found = self.discover()
            if found is None:
                raise BackendUnavailable("soulcore 后端重启了，但没找到新实例")
            return self._call(found[0], found[1], method, path, body, timeout)
        except _ConnLost as ex:
            time.sleep(0.2)
            found = self.discover()
            if found is None:
                raise BackendUnavailable(f"soulcore 后端连不上：{ex}") from ex
            return self._call(found[0], found[1], method, path, body, timeout)

    def health(self) -> dict | None:
        try:
            return self.request("GET", "/v1/health", timeout=1.0)
        except BackendUnavailable:
            return None

    def shutdown(self) -> bool:
        try:
            self.request("POST", "/v1/shutdown", {}, timeout=3.0)
            return True
        except BackendUnavailable:
            return False


class _Unauthorized(Exception):
    pass


class _ConnLost(Exception):
    pass
