// QMD 只索引后端已授权的 LlamaIndex 文本节点，不开放文件系统或跨账号 MCP 查询。
import http from 'node:http';
import {mkdir, writeFile, readdir, unlink, readFile} from 'node:fs/promises';
import {resolve, join, basename} from 'node:path';
import {createHash} from 'node:crypto';
import {createStore, Maintenance} from '@tobilu/qmd';

const root = resolve(process.env.QMD_STORAGE || '../../storage/qmd');
await mkdir(root, {recursive:true});
const semantic = process.env.QMD_SEMANTIC === '1';
const rerank = semantic && process.env.QMD_RERANK === '1';
const store = await createStore({dbPath:join(root,'index.sqlite'),config:{collections:{}}});
const generations = new Map();
let queue = Promise.resolve();
let pending = 0;
const hash = value => createHash('sha256').update(value).digest('hex');
const hex = value => typeof value === 'string' && /^[a-f0-9]{64}$/.test(value);

async function operate(body, search) {
  if (!hex(body.scope) || !hex(body.tenant) || !Number.isSafeInteger(body.revision) || body.revision<0 || !Array.isArray(body.nodes) || body.nodes.length>2000)
    throw new Error('invalid_scope');
  for (const n of body.nodes) if (!hex(n.id) || typeof n.text !== 'string' || n.text.length>30000) throw new Error('invalid_node');
  if (new Set(body.nodes.map(n=>n.id)).size!==body.nodes.length) throw new Error('duplicate_node');
  const collection = `s-${body.scope}`;
  const directory = join(root, body.scope);
  const marker = join(directory,'revision.json');
  await mkdir(directory,{recursive:true});
  let saved;
  try {saved=JSON.parse(await readFile(marker,'utf8'));} catch {saved={revision:-1,generation:''};}
  // 旧快照不能在撤权之后重新写回索引。
  if (body.revision<saved.revision) throw new Error('stale_revision');
  const generation=hash(JSON.stringify(body.nodes.map(n=>[n.id,hash(n.text)]).sort((a,b)=>a[0].localeCompare(b[0]))));
  if (generations.get(body.scope)!==generation) {
    const wanted=new Set(body.nodes.map(n=>`${n.id}.md`));
    for(const file of await readdir(directory)) if (/^[a-f0-9]{64}\.md$/.test(file) && !wanted.has(file)) await unlink(join(directory,file));
    for(const n of body.nodes) await writeFile(join(directory,`${n.id}.md`),n.text,'utf8');
    await store.addCollection(collection,{path:directory,pattern:'*.md'});
    const updated = await store.update({collections:[collection]});
    if(updated.removed) {
      const maintenance = new Maintenance(store.internal);
      maintenance.deleteInactiveDocs(); maintenance.cleanupOrphanedContent();
      maintenance.cleanupOrphanedVectors(); maintenance.clearLLMCache();
    }
    if(semantic && body.nodes.length) await store.embed({collection,maxDocsPerBatch:8});
    generations.set(body.scope,generation);
  }
  await writeFile(marker,JSON.stringify({revision:body.revision,generation,tenant:body.tenant}));
  if(!search || !body.nodes.length) return {mode:semantic?'qmd-hybrid':'qmd-bm25',hits:[]};
  if(typeof body.query!=='string' || body.query.length>2000) throw new Error('invalid_query');
  const limit=Math.max(1,Math.min(12,Number(body.limit)||6));
  const results=semantic
    ? await store.search({queries:[{type:'lex',query:body.query},{type:'vec',query:body.query}],collections:[collection],limit,candidateLimit:12,rerank})
    : await store.searchLex(body.query,{collection,limit});
  const ids=new Set(body.nodes.map(n=>n.id));
  return {mode:semantic?'qmd-hybrid':'qmd-bm25',hits:results.map(r=>({nodeId:basename(r.file||r.filepath).replace(/\.md$/,''),score:r.score})).filter(r=>ids.has(r.nodeId)&&Number.isFinite(r.score))};
}

const server=http.createServer(async(req,res)=>{
  if(req.method==='GET' && req.url==='/health') {res.end(JSON.stringify({status:'ok',engine:'qmd',version:'2.8.3',semantic,rerank}));return;}
  if(req.method!=='POST'||!['/search','/sync'].includes(req.url)) {res.writeHead(404).end();return;}
  if(pending>=8) {res.writeHead(503).end();return;}
  pending++;
  try {
    let length=0; const chunks=[];
    for await(const chunk of req) {length+=chunk.length;if(length>8*1024*1024) throw new Error('payload_limit');chunks.push(chunk);}
    const body=JSON.parse(Buffer.concat(chunks).toString('utf8'));
    const task=queue.then(async()=>{
      if(req.url==='/sync') {
        if(!hex(body.tenant)) throw new Error('invalid_tenant');
        for(const scope of await readdir(root)) {
          if(!hex(scope)) continue;
          let marker;try {marker=JSON.parse(await readFile(join(root,scope,'revision.json'),'utf8'));}catch {continue;}
          if(marker.tenant!==body.tenant || marker.revision>body.revision) continue;
          const old=new Set((await readdir(join(root,scope))).filter(f=>f.endsWith('.md')).map(f=>f.slice(0,-3)));
          await operate({...body,scope,nodes:body.nodes.filter(n=>old.has(n.id))},false);
        }
        return {synced:true};
      }
      return operate(body,true);
    });
    queue=task.catch(()=>{});
    const result=await task;
    res.setHeader('Content-Type','application/json');res.end(JSON.stringify(result));
  } catch(error) {res.writeHead(409,{'Content-Type':'application/json'}).end(JSON.stringify({error:error.message}));}
  finally {pending--;}
});
server.requestTimeout=120000;
server.listen(Number(process.env.QMD_PORT||8182),'127.0.0.1',()=>console.log('QMD_READY',JSON.stringify({semantic,port:server.address().port})));
async function close(){server.close();await queue;await store.close();process.exit(0);}
process.on('SIGTERM',close);process.on('SIGINT',close);
