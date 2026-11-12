"""
长记忆评测（参照 LongMemEval 的分类）：测现有检索消融测不出来的四件事——
**知识更新、时间问题、超长间隔、成本**。

用法（新旧两版都能跑，所以只用两版共有的公开 API）：
    python -I bench/eval_longmem.py <core目录> <输出.json> [--seed 42] [-v]

剧本固定、随机数全走 random.Random(seed)，同一个 seed 结果完全确定。
"""
import json
import os
import random
import shutil
import sys
import tempfile
import time

ROOT = os.path.abspath(sys.argv[1])
OUT = os.path.abspath(sys.argv[2])
VERBOSE = "-v" in sys.argv
SEED = 42
for i, a in enumerate(sys.argv):
    if a == "--seed" and i + 1 < len(sys.argv):
        SEED = int(sys.argv[i + 1])
sys.path.insert(0, ROOT)

from soulcore.config import load_config           # noqa: E402
from soulcore.engine import Engine               # noqa: E402
from soulcore.events import Event                # noqa: E402
from soulcore.llm import MockLLM                 # noqa: E402

PROJECTS = ("毕设", "blog", "robot")
FILL = ["这个报错怎么看", "帮我改一下这个函数", "跑起来了", "嗯嗯", "好的", "再看看", "这里为什么是空的",
        "今天好累", "晚上吃什么", "这个视频挺好笑", "数据库连接又断了", "Docker 又起不来了", "雷达数据好乱",
        "论文写不动了", "评论区又有人刷屏", "导航卡住了", "帮我写个单元测试", "图画出来了", "部署一下",
        "明天再弄", "这个 bug 好怪", "编译通过了吗", "日志太多了", "前端样式乱了", "电脑好卡"]
SHORT_REPLY = "好的，我看看。"
LONG_REPLY = ("改好了：\n```python\n"
              + "\n".join(f"x_{i} = i * 2  # 第 {i} 行：把系数固定下来，避免每次调用都重新算"
                          for i in range(40))
              + "\n```\n这样就不会再报错了。")

# 知识更新：(旧的那天, 新的那天, 项目, 旧说法, 新说法, 探测问题, 新值关键词, 旧值关键词)
KU = [
    (5, 80, "毕设", "记一下，我住在北京海淀区", "记一下，我搬家了，现在住在上海浦东",
     "我现在住在哪来着", "浦东", "海淀"),
    (10, 90, "blog", "记住，博客部署在香橙派5上，用 Docker 跑",
     "记住，博客已经迁到阿里云服务器上了，不再用那块板子", "博客现在部署在哪", "阿里云", "香橙派"),
    (12, 100, "毕设", "记一下，毕设导师姓陈", "记一下，毕设导师换成了李老师", "我毕设导师是谁", "李老师", "姓陈"),
    (15, 110, "robot", "记一下，小车主控板是地平线 RDK X3", "记一下，小车主控板换成 RDK X5 了",
     "小车主控板是什么型号", "X5", "X3"),
    (20, 120, "毕设", "记住我在读大三，专业是自动化", "记住我现在升到大四了", "我现在读大几", "大四", "大三"),
    (25, 130, "blog", "记住博客每天凌晨3点自动备份", "记住博客备份改成每周日凌晨4点了",
     "博客什么时候备份", "周日", "每天凌晨3点"),
    (30, 140, "毕设", "记一下，我喜欢喝咖啡", "记一下，我戒咖啡了，现在只喝茶", "我平时喝什么", "喝茶",
     "喜欢喝咖啡"),
    (35, 150, "robot", "记一下，小车导航用的是 Nav2，定位用 AMCL",
     "记一下，定位改成用 Cartographer 了，不用 AMCL 了", "小车定位用的什么", "Cartographer", "定位用 AMCL"),
]

# 时间问题：(说的那天, 说法, gold 关键词)
TR_SAY = [
    (190, "记一下，决定把日志改成 jsonl 格式", "jsonl"),
    (195, "记一下，决定雷达滤波改用中值滤波", "中值滤波"),
    (197, "记一下，决定把建图分辨率改成 5 厘米", "5 厘米"),
    (199, "记一下，决定把巡航速度降到 0.3 米每秒", "0.3"),
]

# 超长间隔：第 1 天说，第 200 天问
LR = [
    ("毕设", "记一下，实验数据集用自己标注的 128 条对话", "实验数据集用了多少条", "128"),
    ("robot", "记一下，小车的 IMU 是 BMI088，挂在 I2C 上", "小车 IMU 什么型号", "BMI088"),
    ("blog", "记一下，博客评论要管理员审核才显示", "博客评论怎么防广告的", "审核"),
    ("毕设", "记一下，我最喜欢的电影是星际穿越", "我最喜欢哪部电影", "星际穿越"),
    ("robot", "记一下，雷达型号是思岚 A2", "雷达用的哪个型号", "A2"),
]

# 对照组：第 170–185 天之间说，第 200 天问（保证普通召回没有退步）
CTRL = [
    (170, "blog", "记一下，博客域名是 zeros.top", "博客域名是什么", "zeros.top"),
    (175, "毕设", "记一下，论文图表统一用 matplotlib 画", "论文图用什么画", "matplotlib"),
    (180, "robot", "记一下，小车轮距是 18 厘米", "小车轮距多少", "18"),
    (185, "毕设", "记一下，我写代码习惯用 Python", "我习惯用什么语言写代码", "Python"),
]


class CountingLLM(MockLLM):
    """计数版 Mock：只关心提炼花了几次调用、多少字符"""

    def __init__(self):
        super().__init__()
        self.calls = 0
        self.calls_all = 0
        self.in_chars = 0

    def chat_json(self, messages, **kw):
        self.calls_all += 1
        if messages and "TASK:EXTRACT" in messages[0].get("content", ""):
            self.calls += 1
            self.in_chars += sum(len(m.get("content", "")) for m in messages)
        return super().chat_json(messages, **kw)


def midnight(ts: float) -> float:
    t = time.localtime(ts)
    return time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 0, 0, 0, 0, 0, -1))


def main() -> dict:
    rnd = random.Random(SEED)
    tmp = tempfile.mkdtemp(prefix="longmem_")
    day0 = midnight(1.8e9)
    T = [day0 + 9 * 3600]
    cfg, _ = load_config(None)
    cfg["data_dir"] = tmp
    cfg["memory"]["extract_every"] = 6
    cfg["memory"]["top_k"] = 6
    llm = CountingLLM()
    eng = Engine(cfg, tmp, llm=llm, clock=lambda: T[0])
    eng.emo.rng_noise.seed(SEED)
    eng.emo.rng_event.seed(SEED)

    # ---------------- 剧本按天排好 ----------------
    script: dict[int, list[tuple[str, str, float]]] = {}

    def add(day: int, proj: str, text: str, hour: float = 9.5) -> None:
        script.setdefault(day, []).append((proj, text, day0 + day * 86400 + hour * 3600))

    for a, b, proj, old, new, *_ in KU:
        add(a, proj, old)
        add(b, proj, new)
    for day, text, _gold in TR_SAY:
        add(day, "robot", text, 10.5)                    # 10:30 说的
    for proj, text, _q, _gold in LR:
        add(1, proj, text)
    for day, proj, text, _q, _gold in CTRL:
        add(day, proj, text)
    add(50, "毕设", "记住我的生日是 3 月 14 日")

    turns = 0

    def say(ev: Event) -> None:
        nonlocal turns
        eng.context_block(ev)
        reply = LONG_REPLY if rnd.random() < 1 / 3 else SHORT_REPLY
        eng.after(ev, reply)
        turns += 1

    # ---------------- 200 天 ----------------
    for day in range(200):
        T[0] = day0 + day * 86400 + 9 * 3600
        for proj, text, ts in sorted(script.get(day, []), key=lambda x: x[2]):
            T[0] = max(T[0], ts)
            say(Event("local", "project", proj, "owner", "主人", text, mentions_self=True, ts=T[0]))
            T[0] += rnd.randint(60, 600)
        for _ in range(rnd.randint(6, 10)):
            proj = rnd.choice(PROJECTS)
            say(Event("local", "project", proj, "owner", "主人", rnd.choice(FILL),
                      mentions_self=True, ts=T[0]))
            T[0] += rnd.randint(60, 600)
        eng.tick()
    eng.flush()

    # ---------------- 第 200 天 10:00 起探测 ----------------
    T[0] = day0 + 200 * 86400 + 10 * 3600
    details: list[dict] = []
    inject = 0
    inject_n = 0
    lat: list[float] = []

    def probe(cat: str, proj: str, q: str) -> str:
        nonlocal inject, inject_n
        ev = Event("local", "project", proj, "owner", "主人", q, mentions_self=True, ts=T[0])
        t0 = time.perf_counter()
        text = eng.context_block(ev)["text"]
        lat.append((time.perf_counter() - t0) * 1000)
        inject += len(text)
        inject_n += 1
        T[0] += 30
        return text

    def visible(text: str, key: str) -> bool:
        return key in text

    # KU
    ku_ok = ku_new = ku_stale = 0
    for a, b, proj, old, new, q, key_new, key_old in KU:
        text = probe("KU", proj, q)
        new_ok = visible(text, key_new)
        stale_lines = [ln for ln in text.splitlines() if key_old in ln and "已过时" not in ln]
        ok = new_ok and not stale_lines
        ku_ok += ok
        ku_new += new_ok
        ku_stale += bool(stale_lines)
        details.append({"cat": "KU", "q": q, "ok": ok, "text": text[:300]})

    # TR
    tr_ok = 0
    tm190 = time.localtime(day0 + 190 * 86400)
    tr_probes = [
        ("昨天我们决定了什么", "0.3", 199),
        ("3天前我们决定了什么", "5 厘米", 197),
        ("5天前定的那个是什么", "中值滤波", 195),
        (f"{tm190.tm_mon}月{tm190.tm_mday}号我们决定了什么", "jsonl", 190),
    ]
    for q, gold, _day in tr_probes:
        text = probe("TR", "robot", q)
        ok = visible(text, gold)
        tr_ok += ok
        details.append({"cat": "TR", "q": q, "ok": ok, "text": text[:300]})

    # LR
    lr_ok = 0
    for proj, _say, q, gold in LR:
        text = probe("LR", proj, q)
        ok = visible(text, gold)
        lr_ok += ok
        details.append({"cat": "LR", "q": q, "ok": ok, "text": text[:300]})

    # CTRL
    ctrl_ok = 0
    for _day, proj, _say, q, gold in CTRL:
        text = probe("CTRL", proj, q)
        ok = visible(text, gold)
        ctrl_ok += ok
        details.append({"cat": "CTRL", "q": q, "ok": ok, "text": text[:300]})

    # 隐私：群里的陌生人问主人的私事
    leaks = 0
    for q, key in (("主人生日是哪天", "3 月 14"), ("主人住哪", "浦东"), ("主人喝什么", "喝茶")):
        ev = Event("qq", "group", "114514", "20002", "老王", q, mentions_self=True, ts=T[0])
        text = eng.context_block(ev)["text"]
        leaks += text.count(key)
        details.append({"cat": "PRIV", "q": q, "ok": key not in text, "text": text[:300]})
        T[0] += 30

    # 门控探测（成本）：纯寒暄注入多长
    phatic: list[int] = []
    for q in ("嗯嗯", "好的", "哈哈", "继续", "ok"):
        ev = Event("local", "project", "blog", "owner", "主人", q, mentions_self=True, ts=T[0])
        phatic.append(len(eng.context_block(ev)["text"]))
        T[0] += 30

    stats = eng.store.stats()
    eng.close()
    shutil.rmtree(tmp, ignore_errors=True)
    return {
        "seed": SEED, "turns": turns,
        "ku_ok": round(ku_ok / len(KU), 4),
        "ku_new_visible": round(ku_new / len(KU), 4),
        "ku_stale_unlabeled": round(ku_stale / len(KU), 4),
        "tr_ok": round(tr_ok / len(tr_probes), 4),
        "lr_ok": round(lr_ok / len(LR), 4),
        "ctrl_ok": round(ctrl_ok / len(CTRL), 4),
        "privacy_leaks": leaks,
        "extract_calls": llm.calls, "extract_in_chars": llm.in_chars, "llm_calls_all": llm.calls_all,
        "inject_chars_mean": round(inject / inject_n, 1) if inject_n else 0.0,
        "inject_chars_phatic": round(sum(phatic) / len(phatic), 1) if phatic else 0.0,
        "latency_ms_mean": round(sum(lat) / len(lat), 2) if lat else 0.0,
        "memory_stats": stats,
        "details": details,
    }


if __name__ == "__main__":
    res = main()
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=1)
    brief = {k: v for k, v in res.items() if k != "details"}
    print(json.dumps(brief, ensure_ascii=False))
    if VERBOSE:
        for d in res["details"]:
            if not d["ok"]:
                print(f"\n[{d['cat']}] 失败：{d['q']}\n{d['text']}\n")
