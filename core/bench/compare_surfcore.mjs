/** Actual SurfCore pure APIs with shared lexical test vectors; no runtime or API calls. */
import fs from 'node:fs';
import path from 'node:path';
import os from 'node:os';
import { pathToFileURL } from 'node:url';
const [source, fixturePath, outputPath] = process.argv.slice(2);
const vm = await import(pathToFileURL(path.join(source, 'src/vector-memory.js')).href);
const f = JSON.parse(fs.readFileSync(fixturePath, 'utf8'));
const entries = f.corpus.map((r,i) => ({id:r.id,offset:i,path:r.path,text:r.text,title:'',
  importance:r.importance*100,createdAt:new Date(r.created*1000).toISOString(),scope:'shared',sensitive:false}));
const index = {manifest:{dimension:f.dimension,entries},vectors:Float32Array.from(f.vectors.flat())};
const rows=[]; const latencies=[];
const recordPaths=new Map(f.corpus.map(r=>[r.id,r.path]));
const temp=fs.mkdtempSync(path.join(os.tmpdir(),'surfcore-component-bench-'));
const paths=vm.resolveVectorMemoryPaths(temp);
const stored=vm.writeVectorIndex(paths,{model:'lexical-test-fixture',modelSpace:'hash512-v1',dimension:f.dimension,
  entries,vectors:f.vectors,now:f.now*1000});
if(!stored.ok) throw new Error(stored.error);
function rank(q,i,topK=5,disk=false) {
  const allowed = new Set([q.path,'core/people/local/owner','core/self']);
  // External fixture adapter supplies a project filter. SurfCore itself scopes platforms.
  return vm.rankVectorMemories({query:q.query,queryVector:f.query_vectors[i],
    index:disk?vm.readVectorIndex(paths,'hash512-v1'):index,
    config:{topK,minScore:0.2},platform:'qq',now:f.now*1000,entryFilter:e=>allowed.has(recordPaths.get(e.id))});
}
for (const [i,q] of f.queries.entries()) {
  const hits=rank(q,i); const ids=hits.map(x=>x.id); const position=ids.findIndex(id=>q.gold.includes(id));
  rows.push({...q,ids,rank:position<0?null:position+1,
    rendered_text:vm.renderVectorRecallBlock(hits,{maxChars:600})});
}
for(let repeat=0;repeat<5;repeat++) for(const [i,q] of f.queries.entries()) {
  const before=performance.now(); rank(q,i); latencies.push(performance.now()-before);
}
const sorted=[...latencies].sort((a,b)=>a-b);
const diskLatencies=[];
for(let repeat=0;repeat<5;repeat++) for(const [i,q] of f.queries.entries()) {
  const before=performance.now(); rank(q,i,5,true); diskLatencies.push(performance.now()-before);
}
const diskSorted=[...diskLatencies].sort((a,b)=>a-b);
const lengths=[];
for(const size of [1000,5000,12000]) {
  const filler='普通材料并非项目结论。'.repeat(Math.floor(Math.floor(size/2)/12)+1);
  const text='  开始\n'+filler+'\n必须保留离线部署约束；取消向量方案的原因是内存不足。\n'+filler+'\n结束  ';
  const records=vm.recordsFromInboxLines([JSON.stringify({at:new Date(f.now*1000).toISOString(),platform:'qq',
    kind:'private',senderId:'owner',isOwner:true,text})],{now:f.now*1000});
  lengths.push({input_chars:text.length,stored_chars:records[0]?.text.length??0,exact_original:records[0]?.text===text,
    note:'SurfCore vector intake is redacted/clipped material, not a raw conversation archive'});
}
fs.writeFileSync(outputPath,JSON.stringify({source,fixture:'shared deterministic lexical-hash512 vectors',
  project_filter:'external adapter; not native SurfCore project isolation',common_retrieval:rows,
  latency_ms:{n:latencies.length,median:sorted[Math.floor(sorted.length/2)],p95:sorted[Math.ceil(.95*sorted.length)-1],samples:latencies},
  disk_search_latency_ms:{n:diskLatencies.length,median:diskSorted[Math.floor(diskSorted.length/2)],
    p95:diskSorted[Math.ceil(.95*diskSorted.length)-1],samples:diskLatencies},
  intake_material_lengths:lengths},null,2));
const resolvedTemp=path.resolve(temp);
if(path.dirname(resolvedTemp)!==path.resolve(os.tmpdir())||!path.basename(resolvedTemp).startsWith('surfcore-component-bench-'))
  throw new Error('Unexpected benchmark temporary directory');
fs.rmSync(resolvedTemp,{recursive:true,force:true});
console.log(JSON.stringify({out:outputPath,queries:rows.length}));
