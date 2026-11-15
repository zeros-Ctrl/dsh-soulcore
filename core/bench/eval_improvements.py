"""Offline, source-selectable memory/affect comparisons. No live APIs or user data.

python -I -B bench/eval_improvements.py --core PATH --out result.json
Optional --surf-source PATH runs the actual SurfCore pure ranking/rendering APIs.
The common 512-dimension feature vectors are deterministic lexical test fixtures,
not a learned embedding model. Results are component comparisons, not a leaderboard.
"""
from __future__ import annotations
import argparse
import collections
import copy
import hashlib
import inspect
import json
import math
import os
from pathlib import Path
import re
import statistics
import subprocess
import sys
import tempfile
import time

HERE = Path(__file__).resolve().parent
DIM = 512
NOW = 1800000000.0
IMP = {"decision": .8, "fact": .75, "preference": .7, "todo": .7, "self": .9}


def estimated_tokens(text):
    return sum(math.ceil(len(p) / 3) if p.isascii() else math.ceil(len(p.encode("utf-8")) / 2)
               for p in re.findall(r"[\x00-\x7f]+|[^\x00-\x7f]+", str(text or "")))


def feature_vector(text):
    features = []
    for token in re.findall(r"[a-z0-9_]+|[\u4e00-\u9fff]+", text.lower()):
        if token.isascii():
            features.append(token)
        else:
            features.extend(token)
            features.extend(token[i:i+2] for i in range(len(token)-1))
    v = [0.0] * DIM
    for term, count in collections.Counter(features).items():
        digest = hashlib.sha256(term.encode("utf-8")).digest()
        v[int.from_bytes(digest[:4], "big") % DIM] += math.sqrt(count)
    norm = math.sqrt(sum(x*x for x in v)) or 1
    return [x / norm for x in v]


class FixtureEmbedder:
    name = "benchmark:lexical-hash512-v1"
    def embed(self, texts):
        return [feature_vector(text) for text in texts]


def percentile(values, fraction):
    values = sorted(values)
    return values[min(len(values)-1, max(0, math.ceil(len(values)*fraction)-1))] if values else None


def run(core, out, surf_source=None):
    sys.path.insert(0, str(core))
    from soulcore.config import DEFAULTS
    from soulcore.engine import Engine
    from soulcore.events import Event
    from soulcore.extractor import compact_turn, format_transcript
    from soulcore.llm import MockLLM
    from soulcore.memory import MemoryStore
    from soulcore.emotion import Emotion, Params
    from soulcore.appraisal import appraise_detail, heuristic_appraise
    from soulcore.persona import build_context_block

    data = json.loads((HERE / "retrieval_set.json").read_text(encoding="utf-8"))
    result = {"core": str(core), "measurement": "offline fixtures; estimated tokens are not billable usage",
              "schema_version": 1, "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
    event_fields = inspect.signature(Event).parameters
    def event(text="", **extras):
        return Event("local", "project", "demo", "owner", "主人", text, ts=NOW,
                     **{k:v for k,v in extras.items() if k in event_fields})

    class Recorder(MockLLM):
        def __init__(self):
            super().__init__()
            self.jobs = []
        def chat_json(self, messages, **kwargs):
            self.jobs.append(copy.deepcopy(messages))
            return super().chat_json(messages, **kwargs)

    def make_engine(tmp, llm):
        cfg = copy.deepcopy(DEFAULTS)
        cfg["data_dir"] = tmp
        cfg["memory"]["extract_every"] = 10000
        cfg["llm"]["api_key"] = ""
        cfg["embedding"] = {}
        cfg["qq"]["enabled"] = False
        cfg["bilibili"]["enabled"] = False
        return Engine(cfg, tmp, llm=llm, clock=lambda: NOW)

    # Original data and extraction excerpts are measured separately.
    sources = []
    for target_length in (1000, 5000, 12000):
        for role in ("user", "assistant"):
            middle = "必须保留离线部署约束；取消向量方案的原因是内存不足。"
            filler = "普通材料并非项目结论。" * ((target_length//2)//12+1)
            original = "  开始\n" + filler + "\n" + middle + "\n" + filler + "\n结束  "
            llm = Recorder()
            with tempfile.TemporaryDirectory(prefix="soulcore-compare-") as tmp:
                eng = make_engine(tmp, llm)
                try:
                    eng.pre_compact(event(), [{"role": role, "id": "original-1", "content": original}])
                    rows = eng.store.db.execute("SELECT content FROM items WHERE kind='turn'").fetchall()
                    stored = rows[0]["content"] if rows else ""
                    excerpt = compact_turn(original, role, 800 if role == "user" else 300)
                    sources.append({"role":role, "input_chars":len(original), "stored_chars":len(stored),
                                    "exact_original": stored == original,
                                    "retention_ratio":len(stored)/len(original),
                                    "middle_constraint_in_excerpt":middle in excerpt,
                                    "excerpt_chars":len(excerpt)})
                finally:
                    eng.close()
    result["source_preservation"] = sources

    with tempfile.TemporaryDirectory(prefix="soulcore-profile-") as tmp:
        llm = Recorder(); eng = make_engine(tmp, llm)
        try:
            eng.store.write("core/people/owner", "fact", "主人学习自动化，代码通常使用Python。", .9)
            for _ in range(20):
                eng.reflect(force=True)
            jobs = [m for m in llm.jobs if "TASK:PROFILE" in m[0]["content"]]
            result["unchanged_profile"] = {"attempts":20, "model_calls":len(jobs),
                                           "input_chars":sum(len(x["content"]) for m in jobs for x in m)}
        finally:
            eng.close()

    # Independent source IDs must not be confused with legitimate repetition.
    with tempfile.TemporaryDirectory(prefix="soulcore-events-") as tmp:
        llm = Recorder(); eng = make_engine(tmp, llm)
        try:
            ev = event(session_id="session-A")
            message = {"role":"user", "id":"stable-message-id", "content":"项目决定采用 SQLite，因为离线部署。"}
            for _ in range(5):
                eng.pre_compact(ev, [message])
            count = eng.store.db.execute("SELECT COUNT(*) FROM items WHERE kind='turn'").fetchone()[0]
            result["same_event_redelivery"] = {"deliveries":5,"stored_turns":count}
        finally:
            eng.close()

    isolation=[]
    for repetition in range(10):
        with tempfile.TemporaryDirectory(prefix="soulcore-projects-") as tmp:
            eng=make_engine(tmp,Recorder())
            try:
                a=Path(tmp)/"a"/"demo"; b=Path(tmp)/"b"/"demo"
                a.mkdir(parents=True); b.mkdir(parents=True)
                events=[event("项目数据库采用SQLiteA",cwd=str(a),session_id="A"),
                        event("项目数据库采用PostgresB",cwd=str(b),session_id="B")]
                for ev in events:
                    eng.observe(ev)
                    kwargs=dict(dedup=False,mirror=False)
                    if "project_id" in inspect.signature(eng.store.write).parameters:
                        kwargs["project_id"]=getattr(ev,"project_id","")
                    eng.store.write(ev.path,"decision",ev.text,.9,**kwargs)
                for index,ev in enumerate(events):
                    kwargs=dict(k=5,touch=False)
                    if "project_id" in inspect.signature(eng.store.search).parameters:
                        kwargs["project_id"]=getattr(ev,"project_id","")
                    hits=eng.store.search("项目数据库",ev.path,**kwargs)
                    own="SQLiteA" if index==0 else "PostgresB"
                    other="PostgresB" if index==0 else "SQLiteA"
                    isolation.append({"repetition":repetition,"project":index,
                                      "own_visible":any(own in h.content for h in hits),
                                      "other_project_visible":any(other in h.content for h in hits)})
            finally:
                eng.close()
    result["same_name_project_isolation"]=isolation

    # Existing repository fixtures (not newly generated holdout data).
    appraisal = {}
    for filename in ("appraisal_set.jsonl", "appraisal_heldout.jsonl"):
        cases = [json.loads(line) for line in (HERE/filename).read_text(encoding="utf-8").splitlines() if line]
        details = [{"text":c["text"],"gold":c["label"],
                    "predicted":heuristic_appraise(c["text"],False,"小汐"),"hard":c["hard"]} for c in cases]
        appraisal[filename] = {"n":len(details),"accuracy":statistics.mean(d["gold"]==d["predicted"] for d in details),
                               "details":details}
    task_feedback = ("你这个方法错了", "你把配置改坏了", "这个检索结果不对", "这段代码太慢了",
                     "你这方法错了，你真蠢", "对不起，刚才把配置改坏了")
    result["appraisal"] = appraisal
    result["task_feedback"] = [{"text":s, "event":appraise_detail(s).event,
                                 "about_me":appraise_detail(s).about_me,
                                 "cause":getattr(appraise_detail(s),"cause","")} for s in task_feedback]

    # A scheduled replay against an uninterrupted process, with random noise enabled.
    emotion_rows = []
    checkpoints = [0,5,15,30,60,120,180,240,360,720,1440,2160,2880]
    for seed in (42,73,101,211,307):
        params = Params(w_lonely=0, noise_sigma=.15)
        continuous = Emotion(params,seed=seed,t0=0)
        continuous.event("praise",t=0,amp=.8)
        continuous.event("scold",t=120,amp=-.8)
        continuous.advance(180)
        resumed = Emotion.from_dict(continuous.to_dict())
        for minute in checkpoints:
            sample = Emotion(params,seed=seed,t0=0)
            sample.event("praise",t=0,amp=.8)
            if minute>=120: sample.event("scold",t=120,amp=-.8)
            sample.advance(minute)
            emotion_rows.append({"seed":seed,"minute":minute,"mood":sample.mood,
                                 "arousal":getattr(sample,"arousal",None)})
        continuous.advance(480); resumed.advance(480)
        emotion_rows.append({"seed":seed,"minute":480,"mood":continuous.mood,
                             "arousal":getattr(continuous,"arousal",None),
                             "restart_mood_error":abs(continuous.mood-resumed.mood)})
    result["emotion_curve"] = emotion_rows

    # These are local policy ceilings, not actual generated tokens or human ratings.
    policies=[]
    if (core / "soulcore" / "behavior.py").is_file():
        from soulcore.behavior import expression_profile
        for mood in (.05,.2,.35,.5,.65,.8,.95):
            for arousal in (.2,.35,.7):
                for task in (False,True):
                    policy=expression_profile(mood,arousal,task=task)
                    policies.append({"mood":mood,"arousal":arousal,"task":task,
                                     "max_tokens":policy.max_tokens,"max_messages":policy.max_messages,
                                     "sharing":policy.sharing,"initiative_multiplier":policy.initiative_multiplier})
    result["expression_policy"]={"supported":bool(policies),"rows":policies,
                                  "measurement":"configured local ceilings; not realized tokens or human likeness"}

    queries = [dict(query=q,path=p,gold=g,category=t) for q,p,g,t in data["queries"]]
    for name in ("heldout","heldout_hard","blind"):
        queries.extend(dict(query=q,path=p,gold=g,category=name) for q,p,g in data.get(name,[]))
    corpus = []
    for j,(p,k,c) in enumerate(data["distractors"]):
        corpus.append(dict(id=f"d{j}",path=p,kind=k,text=c,importance=.2,created=NOW-86400*10+j*1800))
    for j,(mid,(p,k,c)) in enumerate(data["memories"].items()):
        corpus.append(dict(id=mid,path=p,kind=k,text=c,importance=IMP[k],created=NOW-86400*9+j*18000))
    fixture = {"dimension":DIM,"now":NOW,"corpus":corpus,"queries":queries,
               "vectors":[feature_vector(r["text"]) for r in corpus],
               "query_vectors":[feature_vector(q["query"]) for q in queries]}
    common = []
    with tempfile.TemporaryDirectory(prefix="soulcore-retrieval-") as tmp:
        store = MemoryStore(tmp,{},clock=lambda:NOW,embedder=FixtureEmbedder())
        ids = {}
        try:
            for r in corpus:
                ids[store.write(r["path"],r["kind"],r["text"],r["importance"],ts=r["created"],
                                mirror=False,dedup=False)] = r["id"]
            params = inspect.signature(store.search).parameters
            for q in queries:
                kwargs = dict(k=5,extra_paths=["core/people/local/owner","core/self"],touch=False,
                              allow_projects=q["path"].startswith("project/"))
                if "allow_projects" not in params: kwargs.pop("allow_projects")
                hits = store.search(q["query"],q["path"],**kwargs)
                got = [ids.get(h.id) for h in hits]
                rank = next((i+1 for i,x in enumerate(got) if x in q["gold"]),None)
                common.append({**q,"ids":got,"rank":rank})
            # Warm all query vectors and then measure 5 rounds, excluding creation/import.
            latency = []
            for repetition in range(5):
                for q in queries:
                    before=time.perf_counter()
                    kwargs=dict(k=5,extra_paths=["core/people/local/owner","core/self"],touch=False,
                                allow_projects=q["path"].startswith("project/"))
                    if "allow_projects" not in params: kwargs.pop("allow_projects")
                    store.search(q["query"],q["path"],**kwargs)
                    latency.append((time.perf_counter()-before)*1000)
            result["component_latency_ms"]={"n":len(latency),"median":statistics.median(latency),
                                            "p95":percentile(latency,.95),"samples":latency}
            # Common candidate memories; budget affects assembly, not retrieved ranking.
            budgets=[]
            for token_budget in (100,300,600,1000,1600,0):
                hit_count=0; estimated=[]; original_tokens=[]; preserved_critical=0; critical_total=0
                for q,row in zip(queries,common):
                    kwargs=dict(k=5,extra_paths=["core/people/local/owner","core/self"],touch=False,
                                allow_projects=q["path"].startswith("project/"))
                    if "allow_projects" not in params: kwargs.pop("allow_projects")
                    hits=store.search(q["query"],q["path"],**kwargs)
                    kwargs=dict(name="小汐",mood_text="平静",rel={},memories=hits,person_notes=[],now=NOW,
                                max_chars=1600,include_stable=False)
                    if "token_budget" in inspect.signature(build_context_block).parameters:
                        kwargs["token_budget"]=token_budget
                    text=build_context_block(**kwargs)
                    hit_count += any(data["memories"][mid][2] in text for mid in q["gold"])
                    estimated.append(estimated_tokens(text))
                    original_tokens.append(sum(estimated_tokens(h.content) for h in hits))
                    critical=[h for h in hits if h.kind in ("decision","todo") or h.pinned]
                    critical_total+=len(critical)
                    preserved_critical+=sum(h.content in text for h in critical)
                budgets.append({"configured_budget":token_budget,"supported":"token_budget" in inspect.signature(build_context_block).parameters,
                                "n":len(queries),"answer_record_visible":hit_count/len(queries),
                                "estimated_tokens_mean":statistics.mean(estimated),
                                "critical_complete_fraction":preserved_critical/critical_total if critical_total else None})
            result["budget_sweep"]=budgets
        finally:
            store.close()
    result["common_retrieval"]=common
    result["fixture"]={"queries":len(queries),"memories":len(corpus),"feature_dimension":DIM,
                       "sha256":hashlib.sha256(json.dumps(fixture,ensure_ascii=False,sort_keys=True).encode()).hexdigest()}
    if surf_source:
        fixture_path=out.with_name(out.stem+"-shared-fixture.json")
        fixture_path.write_text(json.dumps(fixture,ensure_ascii=False),encoding="utf-8")
        surf_out=out.with_name(out.stem+"-surfcore.json")
        completed=subprocess.run(["node",str(HERE/"compare_surfcore.mjs"),str(surf_source),str(fixture_path),str(surf_out)],
                                 capture_output=True,text=True,encoding="utf-8")
        if completed.returncode:
            raise RuntimeError("SurfCore adapter failed: "+completed.stderr[-2000:])
        result["surfcore_result_file"]=str(surf_out)
    out.parent.mkdir(parents=True,exist_ok=True)
    out.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps({"out":str(out),"queries":len(queries),"profile_calls":result["unchanged_profile"]["model_calls"],
                      "original_exact":sum(r["exact_original"] for r in sources)},ensure_ascii=False))


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--core",type=Path,required=True)
    parser.add_argument("--out",type=Path,required=True)
    parser.add_argument("--surf-source",type=Path)
    args=parser.parse_args()
    args.out.parent.mkdir(parents=True,exist_ok=True)
    run(args.core.resolve(),args.out.resolve(),args.surf_source.resolve() if args.surf_source else None)
