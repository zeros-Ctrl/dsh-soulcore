"""常驻模式：登录 Windows 后自动在后台起一个 soulcore（计划任务，隐藏窗口）。

只做两件事：注册 / 删除一条计划任务。命令本身还是 `run.py --config <cfg> serve`，
所以行为、日志、退出方式都和手动启动一致。

为什么用计划任务而不是 Windows 服务：soulcore 要读当前用户的 `~/.dsh` 目录、要连本机的
NapCat，跑在用户会话里最省事，也不需要管理员权限。
"""
from __future__ import annotations

import os
import subprocess
import sys

TASK_NAME = "soulcore-resident"


def _pythonw() -> str:
    """优先用 pythonw.exe，计划任务就不会弹黑窗。"""
    exe = sys.executable
    cand = os.path.join(os.path.dirname(exe), "pythonw.exe")
    return cand if os.path.exists(cand) else exe


def _run_py() -> str:
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "run.py")


def _schtasks(*args: str) -> tuple[int, str]:
    try:
        p = subprocess.run(["schtasks", *args], capture_output=True, text=True, timeout=30)
        return p.returncode, (p.stdout or "").strip() + ("\n" + p.stderr.strip() if p.stderr.strip() else "")
    except (OSError, subprocess.SubprocessError) as ex:
        return 1, str(ex)


def install(config_path: str) -> tuple[bool, str]:
    if os.name != "nt":
        return False, "install-service 目前只支持 Windows；Linux/macOS 可以自己写 systemd/launchd。"
    config_path = os.path.abspath(config_path)
    if not os.path.exists(config_path):
        return False, f"找不到配置文件：{config_path}"
    tr = f'"{_pythonw()}" -I "{_run_py()}" --config "{config_path}" serve'
    code, out = _schtasks("/Create", "/TN", TASK_NAME, "/TR", tr, "/SC", "ONLOGON", "/RL", "LIMITED", "/F")
    if code != 0:
        return False, f"注册计划任务失败（{code}）：{out}"
    _schtasks("/Run", "/TN", TASK_NAME)      # 立刻起一次，不用等下次登录
    return True, f"已注册计划任务 {TASK_NAME}（登录时启动，隐藏窗口），并已尝试立即启动。\n{out}"


def uninstall() -> tuple[bool, str]:
    if os.name != "nt":
        return False, "uninstall-service 目前只支持 Windows。"
    code, out = _schtasks("/Delete", "/TN", TASK_NAME, "/F")
    if code != 0:
        return False, f"删除计划任务失败（{code}）：{out}"
    return True, f"已删除计划任务 {TASK_NAME}。"


def start() -> tuple[bool, str]:
    """立刻运行一次已注册的计划任务（设置页"重启常驻进程"用：先 /v1/shutdown，再调这里）。"""
    if os.name != "nt":
        return False, "service-start 目前只支持 Windows。"
    code, out = _schtasks("/Run", "/TN", TASK_NAME)
    return code == 0, out


def status() -> tuple[bool, str]:
    if os.name != "nt":
        return False, "status 目前只支持 Windows。"
    code, out = _schtasks("/Query", "/TN", TASK_NAME, "/FO", "LIST")
    return code == 0, out
