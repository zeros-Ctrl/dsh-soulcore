"""Run source-selectable offline comparisons sequentially, retaining every seed.

python -X utf8 -B core/bench/run_improvements.py --before BASELINE/core --out RESULTS
Optional: --after CURRENT/core --surf-source SURFCORE --seeds 42 73 101 211 307
No live APIs; source hashes and all subprocess exits are saved in run-manifest.json.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import time


def source_hashes(core):
    files=sorted((core/'soulcore').rglob('*.py'))
    return {str(p.relative_to(core)).replace('\\','/'):hashlib.sha256(p.read_bytes()).hexdigest() for p in files}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--before',type=Path,required=True)
    parser.add_argument('--after',type=Path,default=Path(__file__).resolve().parent.parent)
    parser.add_argument('--out',type=Path,required=True)
    parser.add_argument('--surf-source',type=Path)
    parser.add_argument('--seeds',type=int,nargs='+',default=[42,73,101,211,307])
    args=parser.parse_args()
    bench=Path(__file__).resolve().parent
    out=args.out.resolve();out.mkdir(parents=True,exist_ok=True)
    cores={'before':args.before.resolve(),'after':args.after.resolve()}
    tasks=[]
    prefix=[sys.executable,'-X','utf8','-I','-B']
    for label,core in cores.items():
        command=prefix+[str(bench/'eval_improvements.py'),'--core',str(core),'--out',str(out/(label+'-components.json'))]
        if label=='after' and args.surf_source:command+=['--surf-source',str(args.surf_source.resolve())]
        tasks.append((label+'-components',command))
        tasks.append((label+'-retrieval',prefix+[str(bench/'eval_retrieval.py'),str(core),'--json']))
    for seed in args.seeds:
        for label,core in cores.items():
            name=f'{label}-longmem-{seed}'
            tasks.append((name,prefix+[str(bench/'eval_longmem.py'),str(core),str(out/(name+'.json')),
                                      '--seed',str(seed),'--split-stable']))
    env=dict(os.environ,PYTHONIOENCODING='utf-8',PYTHONUTF8='1',PYTHONDONTWRITEBYTECODE='1')
    env.pop('SOULCORE_BENCH_EMBED',None)
    initial={label:source_hashes(core) for label,core in cores.items()}
    manifest={'created_at':time.strftime('%Y-%m-%dT%H:%M:%S%z'),'python':sys.version,'platform':platform.platform(),
              'seeds':args.seeds,'source_hashes':initial,'tasks':[],
              'timing':'sequential; component timing excludes import/index creation; latency paths differ',
              'fixture':'repository synthetic cases; MockLLM and lexical-hash512; not official LongMemEval'}
    for name,command in tasks:
        print('START '+name,flush=True)
        started=time.perf_counter()
        done=subprocess.run(command,capture_output=True,text=True,encoding='utf-8',errors='replace',env=env,cwd=bench)
        (out/(name+'.log')).write_text(done.stdout+'\nSTDERR\n'+done.stderr,encoding='utf-8')
        manifest['tasks'].append({'name':name,'exit_code':done.returncode,'elapsed_seconds':time.perf_counter()-started,
                                  'command':command,'log':name+'.log'})
        (out/'run-manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
        print('DONE '+name+' exit='+str(done.returncode),flush=True)
        if done.returncode:
            print(done.stderr[-2000:],flush=True)
            return done.returncode
        if name.endswith('-retrieval'):(out/(name+'.json')).write_text(done.stdout,encoding='utf-8')
    manifest['source_unchanged']=all(initial[label]==source_hashes(core) for label,core in cores.items())
    manifest['complete']=True
    (out/'run-manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
    if not manifest['source_unchanged']:raise RuntimeError('Source changed during measurement; rerun with frozen code')
    print('ALL COMPARISONS COMPLETE',flush=True)
    return 0


if __name__=='__main__':sys.exit(main())
