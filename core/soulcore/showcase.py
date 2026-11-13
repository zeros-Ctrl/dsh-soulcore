"""`python -m soulcore --mock showcase`：离线演示，30 秒内打印三段对照。

1. 新会话还记得：第 1 天说过的决定，第 30 天新开会话还能想起（对照"没有插件"）；
2. 知道哪些过时了：旧说法标成"已过时"，问"以前"时还能翻出来；
3. 心情是慢慢变的：夸一次 / 凶一次之后，0、10、30、60、120 分钟的心情。

全程离线（MockLLM + 临时目录），不需要 key，也不碰用户自己的数据。
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time

from .config import load_config
from .engine import Engine
from .events import Event
from .llm import MockLLM


class Clock:
    def __init__(self, t: float = 1.8e9):
        self.t = t

    def __call__(self) -> float:
        return self.t

    def skip(self, minutes: float) -> None:
        self.t += minutes * 60


def _engine(tmp: str, clock) -> Engine:
    cfg, _ = load_config(None)
    cfg["data_dir"] = os.path.join(tmp, "data")
    cfg["memory"]["extract_every"] = 2
    eng = Engine(cfg, tmp, llm=MockLLM(), clock=clock)
    eng.emo.rng_noise.seed(7)
    eng.emo.rng_event.seed(7)
    return eng


def _say(eng: Engine, project: str, text: str, reply: str = "嗯，记下了。") -> None:
    clock = eng.clock
    ev = Event("local", "project", project, "owner", "主人", text, mentions_self=True, ts=clock())
    eng.context_block(ev)
    eng.after(ev, reply)
    eng._maybe_extract(ev, force=True)


def _ask(eng: Engine, project: str, text: str) -> str:
    ev = Event("local", "project", project, "owner", "主人", text, mentions_self=True, ts=eng.clock())
    return eng.context_block(ev)["text"]


def _title(text: str) -> None:
    print("\n" + "═" * 72)
    print(text)
    print("═" * 72)


def _memory_lines(text: str) -> list[str]:
    """只留注入文本里的"相关记忆"段：演示的重点是记忆，不是心情/人设"""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if line.startswith("[相关记忆"):
            return lines[i:]
    return []


def _print_memory(text: str) -> None:
    mem = _memory_lines(text)
    if not mem:
        print("  （这次没有想起任何相关的事）")
        return
    for line in mem:
        print("  " + line)


def scene_new_session(tmp: str) -> None:
    _title("① 新会话还记得她说过的决定（第 1 天说，第 30 天问）")
    clock = Clock()
    eng = _engine(tmp, clock)
    _say(eng, "demo", "检索层决定用 SQLite+FTS5，不上向量库，因为要本地轻量部署")
    eng.flush()
    eng.close()
    clock.skip(30 * 24 * 60)                      # 过 30 天，新开会话
    eng2 = _engine(tmp, clock)
    day1_memory = _ask(eng2, "demo", "我们为什么不用向量库")
    q = "我们为什么不用向量库"
    print(f"\n问：{q}\n")
    print("【没有插件】")
    print("  模型只看得到这一句，之前 30 天的对话一条都看不到：")
    print(f"  > {q}")
    print("\n【装了 soulcore】每轮自动注入召回的记忆：")
    _print_memory(day1_memory)
    eng2.close()


def scene_stale(tmp: str) -> None:
    _title("② 知道哪些说法过时了（博客从香橙派迁到阿里云）")
    clock = Clock()
    eng = _engine(tmp, clock)
    _say(eng, "blog", "记一下，博客部署在香橙派上")
    clock.skip(60 * 24 * 60)                       # 两个月后搬走了
    _say(eng, "blog", "记一下，博客已经迁到阿里云服务器上了")
    eng.flush()
    now = _ask(eng, "blog", "博客现在部署在哪")
    then = _ask(eng, "blog", "博客以前部署在哪")
    print("\n问：博客现在部署在哪")
    _print_memory(now)
    print("\n问：博客以前部署在哪（问历史时，失效的旧说法也会翻出来并标上「已过时」）")
    _print_memory(then)
    eng.close()


def scene_mood(tmp: str) -> None:
    _title("③ 心情是慢慢变的：夸一次 / 凶一次之后的 0、10、30、60、120 分钟")
    points = (0, 10, 30, 60, 120)
    rows: dict[str, list[tuple[float, str]]] = {}
    for key, name, event in (("praise", "夸她一次", "praise"), ("scold", "凶她一次", "scold")):
        clock = Clock()
        eng = _engine(os.path.join(tmp, key), clock)     # 两个场景各用一份数据，互不污染
        eng.system_event(event)
        # 冲击不是瞬间落到情绪上的：先过一分钟，再看"刚发生"时的心情
        clock.skip(1)
        eng.emo.advance(clock() / 60.0)
        out = []
        for i, minutes in enumerate(points):
            if i:
                clock.skip(minutes - points[i - 1])
                eng.emo.advance(clock() / 60.0)
            label = eng.emo.label(None)
            out.append((eng.emo.mood, label))
        rows[name] = out
        eng.close()
    print(f"\n{'时间':<10}{'夸她一次':<26}{'凶她一次':<26}")
    for i, minutes in enumerate(points):
        cells = []
        for name in ("夸她一次", "凶她一次"):
            mood, label = rows[name][i]
            cells.append(f"{mood:.3f}（{label}）")
        clock_label = "刚发生" if minutes == 0 else f"{minutes} 分钟"
        print(f"{clock_label:<10}{cells[0]:<26}{cells[1]:<26}")
    print("\n（情绪按指数曲线回落，不是一下子变回去；数值只用于演示，聊天里她不会报数。）")


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    tmp = tempfile.mkdtemp(prefix="soulcore-showcase-")
    t0 = time.monotonic()
    try:
        scene_new_session(os.path.join(tmp, "s1"))
        scene_stale(os.path.join(tmp, "s2"))
        scene_mood(os.path.join(tmp, "s3"))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n（离线演示，用时 {time.monotonic() - t0:.1f} 秒；没有调用任何网络接口。）")
    return 0
