"""事件识别评测：v0.1（关键词）vs v0.2（打分+否定+对象+反讽）。python -I bench/eval_appraisal.py"""
import json, os, sys, types
HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, os.path.dirname(HERE))
from soulcore.appraisal import heuristic_appraise as v02

def load_v01(path):
    if not path or not os.path.exists(path): return None
    m = types.ModuleType("a01"); exec(open(path, encoding="utf-8").read().replace("from .emotion import EVENTS", "EVENTS={}"), m.__dict__)
    return lambda t: m.heuristic_appraise(t, False)

def run(fn, data):
    ok = [fn(d["text"]) == d["label"] for d in data]
    e = [o for o, d in zip(ok, data) if not d["hard"]]; h = [o for o, d in zip(ok, data) if d["hard"]]
    return dict(acc=sum(ok) / len(ok), easy=sum(e) / max(1, len(e)), hard=sum(h) / max(1, len(h)), n=len(ok))

if __name__ == "__main__":
    v01 = load_v01(sys.argv[1] if len(sys.argv) > 1 else "")
    res = {}
    for name in ("appraisal_set", "appraisal_heldout"):
        data = [json.loads(l) for l in open(os.path.join(HERE, name + ".jsonl"), encoding="utf-8")]
        res[name] = {"v0.2": run(lambda t: v02(t, False, "小汐"), data)}
        if v01: res[name]["v0.1"] = run(v01, data)
        if "-v" in sys.argv:
            for d in data:
                p = v02(d["text"], False, "小汐")
                if p != d["label"]: print(f"  ✗ [{name}] {d['text']}  {d['label']} -> {p}")
    print(json.dumps(res, ensure_ascii=False, indent=1))
