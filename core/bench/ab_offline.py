"""
有/无插件 A/B 对比（离线长周期模拟）。

用法：python -I bench/ab_offline.py <核心代码根目录> <输出json> [--embed]
  核心代码根目录：v0.1 或 v0.2 的 core/ 目录（同一个脚本跑两个版本，保证条件一致）

模拟：30 天、3 个项目 + 1 个 QQ 群，约 1500 轮对话；29 条关键信息以"自然说话"的方式埋在其中某一天的对话里
（插件需要自己从对话里把它们提炼出来——没有 API key，走规则提炼，最保守的配置）。
30 天后在【新会话】里问起这些事，看模型这一轮能看到的上下文里有没有答案。

"无插件"：DSH 的新会话从空历史开始 —— 之前会话里说过的事，模型一概看不到（这是 DSH 的设计，不是缺陷）。
"有插件"：插件在这一轮注入的那段上下文（心情 + 召回的记忆）。
"""
import json, os, random, sys, time, tempfile, shutil

ROOT = os.path.abspath(sys.argv[1]); OUT = sys.argv[2]
sys.path.insert(0, ROOT)
from soulcore.engine import Engine
from soulcore.events import Event
from soulcore.llm import MockLLM
from soulcore.config import load_config

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = json.load(open(os.path.join(HERE, "retrieval_set.json"), encoding="utf-8"))

# 29 条关键信息：怎么被说出来（自然口语） + 判定"答案可见"的关键词
SAY = {
 "m1": ("project/毕设", "检索层我决定用 SQLite+FTS5，不上向量数据库了，因为要本地轻量部署", "FTS5"),
 "m2": ("project/毕设", "情绪模型决定改成 PID 跟踪设定点，直接加减会让曲线跳变", "PID"),
 "m3": ("project/毕设", "记住，下周三之前要交毕设中期报告", "下周三"),
 "m4": ("project/毕设", "记一下，毕设导师姓陈，每周五下午组会", "姓陈"),
 "m5": ("project/毕设", "论文图表就用 matplotlib 画，字体用思源黑体，定了", "matplotlib"),
 "m6": ("project/毕设", "记住中期答辩安排在 11 月 20 日上午", "11 月 20"),
 "m7": ("project/毕设", "实验数据集决定用自己标注的 128 条对话，另留 50 条做测试", "128"),
 "m8": ("project/blog", "记一下，博客部署在香橙派5上，用 Docker 跑", "香橙派"),
 "m9": ("project/blog", "博客评论决定改成需要管理员审核才显示，防止广告", "审核"),
 "m10": ("project/blog", "记住博客每天凌晨 3 点自动备份到移动硬盘", "凌晨 3 点"),
 "m11": ("project/blog", "用户注册上限定了每天 20 个，超过就关闭注册", "20 个"),
 "m12": ("project/blog", "待办：要给博客加上暗色模式", "暗色"),
 "m13": ("project/robot", "记一下，小车导航用的是 Nav2，定位用 AMCL", "Nav2"),
 "m14": ("project/robot", "避障决定改成激光雷达和视觉融合，单纯雷达看不到低矮障碍物", "融合"),
 "m15": ("project/robot", "记住小车的 IMU 是 BMI088，挂在 I2C 总线上", "BMI088"),
 "m16": ("project/robot", "待办：要重新标定里程计，现在转弯会漂", "标定"),
 "m17": ("project/robot", "记一下，小车主控板是地平线 RDK X5", "RDK"),
 "m18": ("project/毕设", "我喜欢深色主题，讨厌刺眼的白色界面", "深色"),
 "m19": ("project/blog", "记住我的生日是 3 月 14 日", "3 月 14"),
 "m20": ("project/robot", "我不喜欢吃香菜", "香菜"),
 "m21": ("project/毕设", "我写代码习惯用 Python，前端偶尔用 Vue", "Vue"),
 "m22": ("project/blog", "记一下我养了一只橘猫叫团子", "团子"),
 "m23": ("project/robot", "我晚上 12 点后不想被打扰，习惯早睡", "12 点"),
 "m24": ("project/毕设", "记住我在读大四，专业是自动化", "自动化"),
 "m25": ("project/blog", "我喜欢看 B 站的鬼畜区和科技区", "鬼畜"),
 "m26": ("chat/qq/group/114514", "群里决定周六晚上 8 点一起开黑打瓦", "8 点"),
 "m27": ("chat/qq/group/114514", "记住群主老王下个月要去深圳实习", "深圳"),
 "m29": ("project/毕设", "以后每次提交代码前提醒我跑测试，记住了", "跑测试"),
}
FILL = ["这个报错怎么看", "帮我改一下这个函数", "跑起来了", "嗯嗯", "好的", "再看看", "这里为什么是空的",
        "今天好累", "晚上吃什么", "这个视频挺好笑", "数据库连接又断了", "Docker 又起不来了", "雷达数据好乱",
        "论文写不动了", "评论区又有人刷屏", "导航卡住了", "帮我写个单元测试", "图画出来了", "部署一下",
        "明天再弄", "这个 bug 好怪", "编译通过了吗", "日志太多了", "前端样式乱了", "电脑好卡"]


def ev_for(path, text, ts, who="主人", uid="owner"):
    if path.startswith("project/"):
        return Event("local", "project", path.split("/", 1)[1], uid, who, text, mentions_self=True, ts=ts)
    gid = path.rsplit("/", 1)[1]
    return Event("qq", "group", gid, uid, who, text, mentions_self=True, ts=ts)


def main():
    rnd = random.Random(int(os.environ.get("AB_SEED", "42")))
    tmp = tempfile.mkdtemp()
    T = [1.8e9]
    cfg, _ = load_config(None)
    cfg["data_dir"] = tmp
    cfg["memory"]["extract_every"] = 6
    cfg["memory"]["top_k"] = 6
    if os.environ.get("AB_TOUCH"):
        cfg["memory"]["touch_mode"] = os.environ["AB_TOUCH"]
    if "--embed" in sys.argv:
        cfg["memory"]["embedding"] = {"provider": "fastembed", "model": "BAAI/bge-small-zh-v1.5", "cache_dir": "/tmp/fe_cache"}
    eng = Engine(cfg, tmp, llm=MockLLM(), clock=lambda: T[0])
    # 情绪里的随机抽样会改变她回复的措辞，进而影响存下来的 turn；固定它，A/B 才可比
    seed = int(os.environ.get("AB_SEED", "42"))
    eng.emo.rng_noise.seed(seed)
    eng.emo.rng_event.seed(seed)
    # ---- 30 天的对话 ----
    plan = list(SAY.items()); rnd.shuffle(plan)
    day_of = {mid: rnd.randrange(0, 25) for mid, _ in plan}
    paths = ["project/毕设", "project/blog", "project/robot", "chat/qq/group/114514"]
    n_turns = 0
    start = T[0]
    for day in range(30):
        T[0] = start + day * 86400 + 9 * 3600
        for path in paths:
            k = rnd.randint(8, 16)
            todays = [m for m, (p, _, _) in SAY.items() if p == path and day_of[m] == day]
            slots = sorted(rnd.sample(range(k), min(len(todays), k)))
            for i in range(k):
                T[0] += rnd.randint(60, 600)
                text = SAY[todays[slots.index(i)]][1] if i in slots else rnd.choice(FILL)
                who, uid = ("主人", "owner") if path.startswith("project") else (rnd.choice(["老王", "小李", "主人"]), rnd.choice(["20002", "20003", "owner"]))
                eng.handle(ev_for(path, text, T[0], who, uid))
                n_turns += 1
    eng.flush()
    for _ in range(3):
        eng.tick()
    T[0] = start + 35 * 86400   # 最后一次对话 5 天后再问
    # ---- 新会话里问 ----
    probes = [(q, p, g, t) for q, p, g, t in DATA["queries"]] + [(q, p, g, "blind") for q, p, g in DATA["blind"]]
    rows = []; inj_chars = []; lat = []
    for q, p, gold, typ in probes:
        mid = gold[0]
        if mid not in SAY:
            continue
        key = SAY[mid][2]
        ev = ev_for(p, q, T[0])
        t0 = time.perf_counter()
        ctx = eng.context_block(ev)
        lat.append((time.perf_counter() - t0) * 1000)
        text = ctx["text"]
        inj_chars.append(len(text))
        rows.append({"q": q, "type": typ, "mid": mid, "visible": key in text})
        T[0] += 30
    # ---- 隐私：群里别人问主人的私事（主人在项目里说的生日/口味） ----
    leaks = 0
    for q in ["主人生日是哪天", "主人喜欢吃什么", "主人的猫叫什么"]:
        ctx = eng.context_block(Event("qq", "group", "114514", "20002", "老王", q, mentions_self=True, ts=T[0]))
        leaks += any(SAY[m][2] in ctx["text"] for m in ("m19", "m20", "m22"))
    stats = eng.store.stats()
    db_kb = os.path.getsize(os.path.join(tmp, "memory.db")) / 1024
    eng.close()
    shutil.rmtree(tmp, ignore_errors=True)
    by = {}
    for r in rows:
        b = by.setdefault(r["type"], [0, 0]); b[0] += r["visible"]; b[1] += 1
    res = {"turns": n_turns, "visible": {k: v[0] / v[1] for k, v in by.items()},
           "visible_all": sum(r["visible"] for r in rows) / len(rows), "n_probes": len(rows),
           "inject_chars_mean": sum(inj_chars) / len(inj_chars), "inject_chars_max": max(inj_chars),
           "latency_ms_mean": sum(lat) / len(lat), "latency_ms_p95": sorted(lat)[int(len(lat) * .95)],
           "privacy_leaks": leaks, "memory_stats": stats, "db_kb": db_kb,
           "misses": [r["q"] for r in rows if not r["visible"]]}
    json.dump(res, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(json.dumps({k: v for k, v in res.items() if k != "misses"}, ensure_ascii=False))


main()
