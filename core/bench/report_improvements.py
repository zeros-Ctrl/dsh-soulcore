"""Aggregate completed offline comparisons without excluding failed questions or seeds.

python -X utf8 -B core/bench/report_improvements.py RESULTS OUTPUT
Only synthetic benchmark records are read. Outputs CSV, summary JSON and data audit.
"""
import csv
import hashlib
import json
from pathlib import Path
import statistics
import sys


def read(path):return json.loads(path.read_text(encoding='utf-8'))


def write_csv(path,rows):
    keys=list(dict.fromkeys(k for r in rows for k in r))
    with path.open('w',encoding='utf-8-sig',newline='') as stream:
        writer=csv.DictWriter(stream,fieldnames=keys)
        writer.writeheader();writer.writerows(rows)


def main(source,out):
    out.mkdir(parents=True,exist_ok=True)
    manifest=read(source/'run-manifest.json')
    assert manifest.get('complete') and manifest.get('source_unchanged'), 'Incomplete or changed-source run'
    assert all(t['exit_code']==0 for t in manifest['tasks']), 'A subprocess failed'
    components={label:read(source/(label+'-components.json')) for label in ('before','after')}
    surf=read(source/'after-components-surfcore.json') if (source/'after-components-surfcore.json').exists() else None
    retrieval=[]
    for label,d in [*components.items(),*([('surfcore',surf)] if surf else [])]:
        groups={}
        for r in d['common_retrieval']:
            group=groups.setdefault(r['category'],[]);group.append(r)
        groups['all']=d['common_retrieval']
        for category,rows in groups.items():
            retrieval.append({'version':label,'category':category,'n':len(rows),
                              'recall_at_5':statistics.mean(r['rank'] is not None for r in rows),
                              'mrr':statistics.mean(1/r['rank'] if r['rank'] else 0 for r in rows)})
    write_csv(out/'retrieval.csv',retrieval)
    longmem=[];probes=[]
    fields=['turns','ku_ok','ku_valid_fact_present','ku_new_visible','ku_stale_unlabeled','tr_ok','lr_ok','ctrl_ok',
            'privacy_leaks','extract_calls','extract_in_chars','llm_calls_all','inject_chars_mean',
            'inject_chars_phatic','latency_ms_mean']
    for seed in manifest['seeds']:
        for label in ('before','after'):
            d=read(source/f'{label}-longmem-{seed}.json')
            assert d['seed']==seed
            longmem.append({'version':label,'seed':seed,**{k:d[k] for k in fields}})
            for p in d['probe_outputs']:probes.append({'version':label,'seed':seed,'category':p['cat'],
                                                      'query':p['query'],'project':p['project'],'text':p['text']})
    write_csv(out/'longmem.csv',longmem);write_csv(out/'probe-contexts.csv',probes)
    budgets=[];emotion=[];preservation=[];isolation=[];policies=[];latencies=[]
    for label,d in components.items():
        budgets += [{'version':label,**r} for r in d['budget_sweep']]
        emotion += [{'version':label,**r} for r in d['emotion_curve']]
        preservation += [{'version':label,**r} for r in d['source_preservation']]
        isolation += [{'version':label,**r} for r in d['same_name_project_isolation']]
        policies += [{'version':label,**r} for r in d['expression_policy']['rows']]
        latencies += [{'version':label,'path':'SQLite full search','sample':i,'milliseconds':v}
                      for i,v in enumerate(d['component_latency_ms']['samples'])]
    if surf:
        for key,name in [('latency_ms','RAM ranking'),('disk_search_latency_ms','read index + ranking')]:
            latencies += [{'version':'surfcore','path':name,'sample':i,'milliseconds':v}
                          for i,v in enumerate(surf[key]['samples'])]
    for filename,rows in [('budgets.csv',budgets),('emotion.csv',emotion),('preservation.csv',preservation),
                          ('project-isolation.csv',isolation),('expression-policy.csv',policies),('latency.csv',latencies)]:
        write_csv(out/filename,rows)
    summary={'seeds':manifest['seeds'],'retrieval':retrieval,'longmem':{},'components':{},
             'fixture':components['after']['fixture'],'source_hashes':manifest['source_hashes'],
             'source_unchanged':manifest['source_unchanged'],'limitations':[
                 'MockLLM and deterministic lexical-hash512; no live model or embedding API',
                 'Longmem measures context keyword visibility and valid fact presence, not answer accuracy',
                 'Project filter in SurfCore adapter is external; this is not a native capability claim',
                 'Latencies refer to different execution paths; no overall runtime ranking',
                 'Old restarted RNG is unseeded by its serializer; restart errors are this run observations',
                 'Estimated tokens and characters are proxies; no actual cost reduction established']}
    for label,d in components.items():
        rows=[r for r in longmem if r['version']==label]
        summary['longmem'][label]={k:statistics.mean(r[k] for r in rows) for k in fields}
        summary['components'][label]={
            'profile':d['unchanged_profile'],
            'project_own_visible':sum(r['own_visible'] for r in d['same_name_project_isolation']),
            'project_other_visible':sum(r['other_project_visible'] for r in d['same_name_project_isolation']),
            'project_n':len(d['same_name_project_isolation']),
            'restart_errors':[r['restart_mood_error'] for r in d['emotion_curve'] if 'restart_mood_error' in r],
            'rescue_exact_original':sum(r['exact_original'] for r in d['source_preservation']),
            'rescue_original_n':len(d['source_preservation']),
            'middle_constraints':sum(r['middle_constraint_in_excerpt'] for r in d['source_preservation']),
            'latency_median':d['component_latency_ms']['median'],
            'latency_p95':d['component_latency_ms']['p95'],
            'appraisal':{k:{'n':v['n'],'accuracy':v['accuracy']} for k,v in d['appraisal'].items()},
            'task_feedback':d['task_feedback']}
    if surf:summary['surfcore_latency']={k:{'median':surf[k]['median'],'p95':surf[k]['p95']}
                                        for k in ('latency_ms','disk_search_latency_ms')}
    (out/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')
    audit={'complete':True,'seeds':manifest['seeds'],'longmem_runs':len(longmem),
           'longmem_turns':sum(r['turns'] for r in longmem), 'retrieval_unique_questions':components['after']['fixture']['queries'],
           'emotion_rows':len(emotion),'budget_rows':len(budgets),'isolation_rows':len(isolation),
           'excluded_rows':0,'input_data_sha256':{p.name:hashlib.sha256(p.read_bytes()).hexdigest()
             for p in sorted(source.glob('*.json'))},
           'missingness':'Old arousal is intentionally unavailable; restart error exists only at restart checkpoint; budget=0 means disabled.'}
    (out/'data-audit.json').write_text(json.dumps(audit,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'out':str(out),'runs':len(longmem),'turns':audit['longmem_turns'],'excluded_rows':0},ensure_ascii=False))


if __name__=='__main__':main(Path(sys.argv[1]).resolve(),Path(sys.argv[2]).resolve())
