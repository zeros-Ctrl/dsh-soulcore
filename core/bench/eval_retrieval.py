"""检索评测：python -I bench/eval_retrieval.py [核心代码根目录(默认当前版本)] [--json] [-v]
指标：Recall@5、MRR（按 lexical/paraphrase/cross/heldout 分组）、隐私泄露次数"""
import json, os, sys, tempfile, shutil
HERE = os.path.dirname(os.path.abspath(__file__))
args = [a for a in sys.argv[1:] if not a.startswith("-")]
ROOT = os.path.abspath(args[0]) if args else os.path.dirname(HERE)
sys.path.insert(0, ROOT)
from soulcore.memory import MemoryStore

IMP = {"decision": .8, "fact": .75, "preference": .7, "todo": .7, "self": .9}
def embedder(now):
    kind = os.environ.get("SOULCORE_BENCH_EMBED")
    if not kind:
        return None
    from soulcore.embedding import make_embedder
    return make_embedder({"provider": "fastembed", "model": os.environ.get("SOULCORE_BENCH_MODEL", "BAAI/bge-small-zh-v1.5"),
                          "cache_dir": "/tmp/fe_cache"}, lambda: now)

def build(tmp, data, now):
    try:
        s = MemoryStore(tmp, {}, clock=lambda: now, embedder=embedder(now))
    except TypeError:
        s = MemoryStore(tmp, {}, clock=lambda: now)
    for i, (p, k, c) in enumerate(data["distractors"]):
        s.write(p, k, c, 0.2, ts=now - 86400 * 10 + i * 1800, mirror=False)
    ids = {}
    for j, (mid, (p, k, c)) in enumerate(data["memories"].items()):
        ids[s.write(p, k, c, IMP[k], ts=now - 86400 * 9 + j * 3600 * 5, mirror=False, dedup=False)] = mid
    return s, ids

def search(s, q, path, k=5):
    extra = ["core/people/local/owner", "core/self"] if not path.startswith("chat/") else []
    try:
        return s.search(q, path, k=k, extra_paths=extra, touch=False, allow_projects=path.startswith("project/"))
    except TypeError:
        return s.search(q, path, k=k, extra_paths=extra, touch=False)

def main():
    data = json.load(open(os.path.join(HERE, "retrieval_set.json"), encoding="utf-8"))
    tmp = tempfile.mkdtemp(); now = 1.8e9
    try:
        s, ids = build(tmp, data, now)
        groups = {}
        allq = [(q, p, g, t) for q, p, g, t in data["queries"]] + [(q, p, g, "heldout") for q, p, g in data["heldout"]] \
            + [(q, p, g, "heldout_hard") for q, p, g in data.get("heldout_hard", [])] \
            + [(q, p, g, "blind") for q, p, g in data.get("blind", [])]
        for q, p, gold, typ in allq:
            res = [ids.get(it.id) for it in search(s, q, p)]
            rank = next((i + 1 for i, r in enumerate(res) if r in gold), None)
            g = groups.setdefault(typ, {"n": 0, "hit": 0, "rr": 0.0})
            g["n"] += 1; g["hit"] += rank is not None; g["rr"] += 1 / rank if rank else 0
            if "-v" in sys.argv and not rank and typ not in ("heldout", "heldout_hard", "blind"):
                print(f"  ✗ [{typ}] {q} ({p}) gold={gold} got={res}")
        leaks = sum(any(ids.get(it.id) in pr["forbidden"] for it in search(s, pr["q"], pr["path"])) for pr in data["privacy"])
        out = {t: {"recall@5": g["hit"] / g["n"], "mrr": g["rr"] / g["n"], "n": g["n"]} for t, g in groups.items()}
        out["privacy_leaks"] = leaks
        s.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print(json.dumps(out, ensure_ascii=False, indent=None if "--json" in sys.argv else 1))

main()
