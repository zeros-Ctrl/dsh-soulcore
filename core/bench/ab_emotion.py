"""
情绪 A/B：python -I bench/ab_emotion.py <核心代码根目录> <输出json> [<输出曲线npz>]

一周的消息流（来自标注集里的真实句子，按标签已知），中间穿插一整天没人理。
"无插件"基线：DSH 本身没有情绪状态，模型的语气只取决于"这一句话"——这里按标注的真值给它一个
            情绪值（0.62 + 0.3×效价），等于假设它每句话都理解得完全正确（对基线最有利的假设）。
"有插件"：soulcore 的情绪引擎（v0.1 或 v0.2，事件由插件自己识别，可能识别错）。

指标
  跳变：相邻两条消息之间情绪的平均/最大变化（越小越平滑）
  镜像度：情绪与"这一句话的效价"的相关系数（越接近 1 = 越像只会跟着用户这句话走，没有自己的状态）
  记仇时长：被凶之后情绪回到基线前的平均分钟数（基线为 0：下一句就忘了）
  方向错误率：带情绪的消息之后 30 分钟，情绪变化方向与真值相反的比例（检验事件识别）
  冷落反应：一整天没人理之后的情绪下降
"""
import json, math, os, random, sys
ROOT = os.path.abspath(sys.argv[1]); OUT = sys.argv[2]; CURVE = sys.argv[3] if len(sys.argv) > 3 else None
sys.path.insert(0, ROOT)
import tempfile, shutil
from soulcore.engine import Engine
from soulcore.events import Event
from soulcore.llm import MockLLM
from soulcore.config import load_config

HERE = os.path.dirname(os.path.abspath(__file__))
VAL = {"praise": 1, "gift": 1, "success": 1, "apology": 0.5, "chat": 0, "scold": -1}
msgs = []
for fn in ("appraisal_set.jsonl", "appraisal_heldout.jsonl"):
    msgs += [json.loads(l) for l in open(os.path.join(HERE, fn), encoding="utf-8")]


def corr(a, b):
    ma, mb = sum(a) / len(a), sum(b) / len(b)
    sa = math.sqrt(sum((x - ma) ** 2 for x in a)); sb = math.sqrt(sum((y - mb) ** 2 for y in b))
    return sum((x - ma) * (y - mb) for x, y in zip(a, b)) / (sa * sb) if sa and sb else 0.0


def main():
    rnd = random.Random(int(os.environ.get("AB_SEED", "7")))
    tmp = tempfile.mkdtemp()
    t0 = 1.8e9; T = [t0]
    cfg, _ = load_config(None); cfg["data_dir"] = tmp; cfg["emotion"] = {"baseline": 0.62}
    eng = Engine(cfg, tmp, llm=MockLLM(), clock=lambda: T[0])
    base0 = 0.62
    # 7 天：第 4 天整天没人理；其余每天 9:00-23:00 随机 25~40 条
    timeline = []
    for day in range(7):
        if day == 3:
            continue
        n = rnd.randint(25, 40)
        for t in sorted(rnd.uniform(9 * 3600, 23 * 3600) for _ in range(n)):
            timeline.append((t0 + day * 86400 + t, rnd.choice(msgs)))
    trace_t, trace_m = [], []
    rec = []   # 每条消息：时间、真值效价、插件识别结果、插件情绪（消息前）、基线情绪
    def sample_until(t_end):
        while T[0] < t_end:
            T[0] = min(t_end, T[0] + 600)
            s = eng.state(); trace_t.append((T[0] - t0) / 86400); trace_m.append(s["mood"])
    for ts, m in timeline:
        sample_until(ts)
        before = eng.state()["mood"]
        info = eng.observe(Event("local", "project", "p", "owner", "主人", m["text"], mentions_self=True, ts=ts))
        rec.append({"t": ts, "label": m["label"], "val": VAL[m["label"]], "pred": info.get("event"),
                    "plugin_before": before, "baseline": base0 + 0.3 * VAL[m["label"]]})
    sample_until(t0 + 7 * 86400)
    def mood_at(t):
        i = min(range(len(trace_t)), key=lambda j: abs(trace_t[j] - (t - t0) / 86400))
        return trace_m[i]
    for r in rec:
        r["plugin_after30"] = mood_at(r["t"] + 1800)
    eng.close(); shutil.rmtree(tmp, ignore_errors=True)

    pl = [r["plugin_after30"] for r in rec]; bl = [r["baseline"] for r in rec]; v = [r["val"] for r in rec]
    def jumps(xs): return [abs(xs[i + 1] - xs[i]) for i in range(len(xs) - 1)]
    emo = [r for r in rec if r["val"] != 0]
    dir_err = sum((r["plugin_after30"] - r["plugin_before"]) * r["val"] < 0 for r in emo) / len(emo)
    # 记仇时长：被凶后多少分钟回到"被凶前的情绪"
    holds = []
    for r in rec:
        if r["label"] != "scold":
            continue
        k = next((j for j, tt in enumerate(trace_t) if tt * 86400 + t0 > r["t"]), None)
        if k is None:
            continue
        j = k
        while j < len(trace_m) and trace_m[j] < r["plugin_before"] - 0.01:
            j += 1
        holds.append((trace_t[min(j, len(trace_t) - 1)] - trace_t[k]) * 86400 / 60)
    # 冷落：第 4 天开始前 vs 结束时
    i3 = min(range(len(trace_t)), key=lambda j: abs(trace_t[j] - 3.0)); i4 = min(range(len(trace_t)), key=lambda j: abs(trace_t[j] - 3.99))
    acc = sum(r["pred"] in (r["label"], "stranger_insult" if r["label"] == "scold" else None) for r in rec) / len(rec)
    res = {
        "n_msgs": len(rec),
        "baseline": {"mean_jump": sum(jumps(bl)) / (len(bl) - 1), "max_jump": max(jumps(bl)), "mirror_corr": corr(bl, v),
                     "grudge_minutes": 0.0, "lonely_drop": 0.0},
        "plugin": {"mean_jump": sum(jumps(pl)) / (len(pl) - 1), "max_jump": max(jumps(pl)), "mirror_corr": corr(pl, v),
                   "grudge_minutes": sum(holds) / max(1, len(holds)), "lonely_drop": trace_m[i3] - trace_m[i4],
                   "direction_error": dir_err, "event_accuracy": acc,
                   "max_step_per_10min": max(jumps(trace_m))},
    }
    json.dump(res, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    if CURVE:
        json.dump({"t": trace_t, "m": trace_m, "msgs": [((r["t"] - t0) / 86400, r["val"], r["baseline"]) for r in rec]},
                  open(CURVE, "w"))
    print(json.dumps(res, ensure_ascii=False))


main()
