"""在 QQ 里刷 B 站：首页推荐、按关键词搜、读字幕了解视频内容。

只调用 dsh-bili-whale 的只读命令（feed / search / subtitles / comments），
不评论、不点赞、不收藏、不发私信，不会动账号。
拆成两层是刻意的：
  - 视频标题可以入库（不会变）；
  - 评论区、搜索结果、字幕都是任何人能写的公开内容，只给当次对话当素材，绝不写进记忆，
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
            "items": items[:3],                 # 带 BV：聊天里"第二个讲了啥"要靠它定位视频
            "first_bv": first["bv"],
            "comments": comments}


# ------------------------------------------------------------------ 搜索
_SEARCH_HEAD = re.compile(r"^\[(\d+)\]\s+(.+)$")
_SEARCH_META = re.compile(r"BV=(BV[0-9A-Za-z]{10})\s+UP=(.*?)\s+播放=(\S+).*?时长=(\S+)")


def parse_search(text: str, limit: int = 5) -> list[dict]:
    """search.mjs 的输出：
         [1] 标题
             BV=BV1xx  UP=某人  播放=123  弹幕=4  时长=3:21  日期=2026/11/5
             简介: ...
             标签: ...
    """
    items: list[dict] = []
    cur: dict | None = None
    for line in (text or "").splitlines():
        m = _SEARCH_HEAD.match(line.strip())
        if m:
            cur = {"title": m.group(2).strip(), "bv": "", "up": "", "play": "", "duration": "", "desc": ""}
            items.append(cur)
            continue
        if cur is None:
            continue
        s = line.strip()
        mm = _SEARCH_META.search(s)
        if mm:
            cur.update(bv=mm.group(1), up=mm.group(2).strip(), play=mm.group(3), duration=mm.group(4))
        elif s.startswith("简介:"):
            cur["desc"] = s[3:].strip()[:80]
    return [it for it in items if it["bv"]][:limit]


def search_videos(cfg: dict, keyword: str, limit: int = 5) -> list[dict]:
    """按关键词搜视频（只读）。失败（含风控）返回空列表。"""
    kw = re.sub(r"\s+", " ", keyword or "").strip()[:30]
    if not kw:
        return []
    tools, node = resolve_tools(cfg), resolve_node(cfg)
    out = run_cmd(["search", kw], tools, node, int(cfg.get("search_timeout_s", 20)))
    return parse_search(out, limit)


# ------------------------------------------------------------------ 字幕
def fetch_subtitles(cfg: dict, bv: str, work_dir: str, max_chars: int = 8000) -> dict:
    """拉官方 / AI 字幕全文（只读）。

    返回 {"title", "up", "text", "reason"}：text 为空时 reason 说明原因（没有字幕 / 失败）。
    字幕文件写到 soulcore 自己的 work_dir，读完即删，不往 dsh-bili-whale 的目录里堆东西。"""
    if not re.fullmatch(r"BV[0-9A-Za-z]{10}", bv or ""):
        return {"title": "", "up": "", "text": "", "reason": "BV 号不对"}
    os.makedirs(work_dir, exist_ok=True)
    tools, node = resolve_tools(cfg), resolve_node(cfg)
    out = run_cmd(["subtitles", bv, "--out", work_dir, "--print", "0"], tools, node,
                  int(cfg.get("subtitle_timeout_s", 45)))
    title = up = ""
    plain = ""
    for line in out.splitlines():
        if line.startswith("标题:"):
            title = line[3:].strip()
        elif line.startswith("UP:"):
            up = re.split(r"\s{2,}", line[3:].strip())[0]
        elif line.startswith("PLAIN:"):
            plain = line[6:].split("（")[0].strip()
    if not out:
        return {"title": "", "up": "", "text": "", "reason": "没拉到（网络或风控）"}
    if not plain:
        # 登录过期时 B站 字幕接口照样 code=0、只是轨道数为 0，和"真没字幕"长得一样：查一次登录态区分开
        if check_login(cfg) is False:
            return {"title": title, "up": up, "text": "", "reason": "B站账号登录过期了，读不了字幕"}
        return {"title": title, "up": up, "text": "", "reason": "这个视频没有字幕"}
    text = ""
    try:
        # 只认 work_dir 里的文件：输出里的路径来自外部进程，不能让它把我们引去读别的文件
        real = os.path.realpath(plain)
        if os.path.commonpath([real, os.path.realpath(work_dir)]) == os.path.realpath(work_dir):
            with open(real, encoding="utf-8", errors="replace") as f:
                text = f.read()
    except (OSError, ValueError) as ex:
        log.warning("读字幕失败：%s", ex)
    finally:
        _cleanup(work_dir, bv)
    text = text.strip()
    if len(text) > max_chars:
        text = text[:max_chars] + "……（后面省略）"
    return {"title": title, "up": up, "text": text, "reason": "" if text else "字幕是空的"}


_LOGIN_CACHE: dict = {"at": 0.0, "ok": None}


def check_login(cfg: dict, max_age_s: float = 3600) -> bool | None:
    """B站登录态是否有效（跑只读的 session 命令，结果缓存 1 小时）。判断不了返回 None。"""
    import time as _time
    now = _time.time()
    if _LOGIN_CACHE["ok"] is not None and now - _LOGIN_CACHE["at"] < max_age_s:
        return _LOGIN_CACHE["ok"]
    out = run_cmd(["session"], resolve_tools(cfg), resolve_node(cfg), 30)
    ok = None
    if "登录态无效" in out or "-101" in out or "未登录" in out:
        ok = False
    elif out:
        ok = True
    if ok is not None:
        _LOGIN_CACHE.update(at=now, ok=ok)
        if ok is False:
            log.warning("B站登录已过期：读不了字幕。去 ~/.bili-ai/credentials.json 换新的 cookie")
    return ok


def _cleanup(work_dir: str, bv: str) -> None:
    try:
        for fn in os.listdir(work_dir):
            if fn.startswith(bv + "-"):
                os.remove(os.path.join(work_dir, fn))
    except OSError:
        pass
