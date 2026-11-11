"""在 QQ 里偶尔去 B 站刷一下。

只调用 dsh-bili-whale 的只读命令（feed / comments），不评论、不点赞、不收藏，不会动账号。
拆成两层是刻意的：
  - 视频标题可以入库（不会变）；
  - 评论区是任何人写的公开内容，只给当次对话当素材，绝不写进记忆，
    否则就重新打开了"公开内容 → 本地编程 agent"的注入路径。
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess

log = logging.getLogger("soulcore.bili.watch")

DEFAULT_TOOLS = os.path.join(
    os.path.expanduser("~"), ".dsh", "plugins", "dsh-bili-whale", "tools", "bili.mjs"
)

_FEED_LINE = re.compile(r"^\s*\d+\.\s*(BV[0-9A-Za-z]{10})\s+(.+)$")
_PIN_TAG = re.compile(r"^【[^】]*】\s*")
_COMMENT_HEAD = re.compile(
    r"^\[\d+\]\s+(?:【[^】]*】\s*)?[♥\d]*\s*(.+?)\s+\d{4}/\d{1,2}/\d{1,2}")


def _no_window() -> int:
    return 0x08000000 if os.name == "nt" else 0   # CREATE_NO_WINDOW


def resolve_tools(cfg: dict) -> str:
    return (cfg.get("tools_path") or "").strip() or DEFAULT_TOOLS


def resolve_node(cfg: dict) -> str:
    n = (cfg.get("node") or "").strip()
    if n and os.path.isfile(n):
        return n
    return shutil.which("node") or "node"


def run_cmd(args: list[str], tools: str, node: str, timeout: int = 45) -> str:
    if not os.path.isfile(tools):
        log.warning("找不到 bili.mjs：%s", tools)
        return ""
    try:
        r = subprocess.run([node, tools, *args], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout,
                           cwd=os.path.dirname(tools), creationflags=_no_window())
    except subprocess.TimeoutExpired:
        log.warning("B站命令超时：%s", args)
        return ""
    except Exception as ex:
        log.warning("B站命令失败 %s: %s", args, ex)
        return ""
    return (r.stdout or "").strip()


def parse_feed(text: str) -> list[dict]:
    items = []
    for line in text.splitlines():
        m = _FEED_LINE.match(line)
        if not m:
            continue
        bv, rest = m.group(1), m.group(2)
        tm = re.search(r"播放\s*\d+\s+(.+?)\s+@", rest)
        title = tm.group(1).strip() if tm else rest.strip()
        um = re.search(r"@([^\s\[]+)", rest)      # 命中词是可选的，没有也要拿到 UP 名
        items.append({"bv": bv, "title": title, "up": um.group(1) if um else ""})
    return items


def parse_comments(text: str, limit: int = 3) -> list[str]:
    out, lines = [], text.splitlines()
    for i, line in enumerate(lines):
        line = _PIN_TAG.sub("", line)          # 【置顶】这类标签要先去，不然会混进作者名
        m = _COMMENT_HEAD.match(line)
        if not m:
            continue
        body = lines[i + 1].strip() if i + 1 < len(lines) else ""
        if not body or body.startswith("└"):
            continue
        who = re.sub(r"\s*[（(]mid\s*\d+[）)]", "", m.group(1)).strip()
        out.append(f"{who}：{body[:80]}")
        if len(out) >= limit:
            break
    return out


def watch_once(cfg: dict) -> dict:
    """刷一次。返回 {"titles": [...], "comments": [...], "first_bv": str}；失败返回空 dict。"""
    tools, node = resolve_tools(cfg), resolve_node(cfg)
    timeout = int(cfg.get("timeout_s", 45))
    n = max(1, min(5, int(cfg.get("max_candidates", 3))))

    items = parse_feed(run_cmd(["feed", "--n", str(n)], tools, node, timeout))
    if not items:
        return {}
    first = items[0]
    # 评论区是公开内容，不入库也不进提示词；默认不再抓，省一次网络请求和最长 timeout_s 的等待
    comments = []
    if cfg.get("with_comments", False):
        comments = parse_comments(
            run_cmd(["comments", first["bv"], "--ps", "3"], tools, node, timeout), 2
        )
    return {"titles": [it["title"] for it in items[:3]],
            "first_bv": first["bv"],
            "comments": comments}
