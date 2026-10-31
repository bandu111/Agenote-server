// 使用独立临时目录和真实 QMD 引擎验证隔离、撤权和中文检索，不使用客户资料。
import {spawn} from 'node:child_process';
import {mkdtemp,rm,realpath} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join,resolve} from 'node:path';
import {fileURLToPath} from 'node:url';
import {createHash} from 'node:crypto';
import assert from 'node:assert/strict';
const hash=value=>createHash('sha256').update(value).digest('hex');
const directory=await mkdtemp(join(tmpdir(),'yanshu-qmd-'));
const child=spawn(process.execPath,[fileURLToPath(new URL('./server.mjs',import.meta.url))],{
  env:{...process.env,QMD_PORT:'0',QMD_STORAGE:directory},windowsHide:true,stdio:['ignore','pipe','pipe'],
});
let stderr='';child.stderr.on('data',data=>stderr+=data.toString());
try {
  const port=await new Promise((resolve,reject)=>{
    const timer=setTimeout(()=>reject(new Error('startup_timeout')),30000);
    child.once('exit',code=>{clearTimeout(timer);reject(new Error(`startup_exit_${code}: ${stderr.slice(-600)}`));});
    child.stdout.on('data',data=>{const line=data.toString().match(/QMD_READY (\{[^\n]+\})/);if(line){clearTimeout(timer);resolve(JSON.parse(line[1]).port);}});
  });
  async function request(path,body) {
    const start=performance.now();
    const response=await fetch(`http://127.0.0.1:${port}/${path}`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body),signal:AbortSignal.timeout(180000)});
    return {status:response.status,body:await response.json(),ms:Math.round(performance.now()-start)};
  }
  const tenant=hash('synthetic-owner-a'),scope=hash('synthetic-owner-a:p1');
  const nodes=[
    {id:hash('windows'),text:'# Windows 桌面范围\nDesktop Windows delivery was only proposed and not approved. 客户提出新增桌面端，Windows尚未获得批准，不属于当前正式交付范围。'},
    {id:hash('mobile'),text:'# 双端交期\nAndroid and iOS delivery deadline is November 15, 2026. 我方承诺于2026年11月15日交付Android和iOS双端测试报告。'},
    {id:hash('recipe'),text:'# 午餐菜单\nTomato soup ingredients. 午餐西红柿汤需要西红柿和鸡蛋，与项目合同无关。'},
  ];
  const base={tenant,scope,revision:1,nodes,limit:3};
  const first=await request('search',{...base,query:process.env.QMD_SEMANTIC==='1'?'新增电脑端交付是否已经同意':'Windows'});
  assert.equal(first.status,200,JSON.stringify(first.body));
  assert.equal(first.body.hits[0]?.nodeId,hash('windows'),JSON.stringify(first.body));
  const warm=await request('search',{...base,query:'Windows'});
  const other=await request('search',{...base,tenant:hash('b'),scope:hash('b:p1'),nodes:[],query:'Windows'});
  assert.deepEqual(other.body.hits,[]);
  const revoked=await request('sync',{tenant,revision:2,nodes:[]});assert.equal(revoked.status,200);
  const stale=await request('search',{...base,query:'Windows'});assert.equal(stale.status,409);
  const empty=await request('search',{...base,revision:2,nodes:[],query:'Windows'});assert.deepEqual(empty.body.hits,[]);
  console.log(JSON.stringify({passed:6,engine:first.body.mode,cold_ms:first.ms,warm_ms:warm.ms,top1_correct:true,account_isolation:true,revocation:true,stale_revision_rejected:true}));
} finally {
  child.kill('SIGTERM');
  await new Promise(resolve=>{if(child.exitCode!==null)resolve();else {child.once('exit',resolve);setTimeout(()=>{child.kill('SIGKILL');resolve();},8000).unref();}});
  // 删除前确认是本次新建的系统临时目录，禁止对计算出的任意路径递归删除。
  const target=await realpath(directory),base=await realpath(tmpdir());
  if(target.startsWith(base+ (process.platform==='win32'?'\\':'/')) && target.split(/[\\/]/).at(-1).startsWith('yanshu-qmd-')) await rm(target,{recursive:true,force:true});
}
