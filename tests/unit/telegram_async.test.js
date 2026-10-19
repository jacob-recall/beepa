import assert from 'node:assert/strict';
import {mkdtempSync,rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {ControlCore} from '../../master/control-core.mjs';
import {createControlService} from '../../master/telegram-service.mjs';

const bot={id:123,is_bot:true,username:'fixture_bot'},operator={user:7,chat:7};
function fixture(path=':memory:'){
 const f={now:1700000000000,sent:[],updates:[],offline:false,modelCalls:0};
 const domain={rooms:new Map(),settings:()=>({retention_days:1}),refresh:async()=>{},hydrateAccounts:async()=>{},state:()=>({}),rules:()=>[],lists:()=>[]};
 const telegram=async(method,data)=>{
  if(f.offline)throw Error('Transport unavailable');
  if(method==='getMe')return bot;if(method==='getWebhookInfo')return {url:''};
  if(method==='getUpdates'){await new Promise(r=>setTimeout(r,5));return f.updates.splice(0);}
  if(method==='sendMessage'){f.sent.push(data);return {message_id:f.sent.length};}return true;
 };
 const core=new ControlCore({path,domain,telegram,clock:()=>f.now,model:async()=>{f.modelCalls++;if(f.modelWait)await f.modelWait;return {content:'Synthetic reply'};}});
 core.bindTransport(bot);core.set('operator',operator);core.monitor=async()=>{};
 return Object.assign(f,{domain,telegram,core});
}
const until=async fn=>{for(let i=0;i<300;i++){if(await fn())return;await new Promise(r=>setTimeout(r,5));}throw Error('Condition did not become true');};
{
 const dir=mkdtempSync(join(tmpdir(),'beepa-api-commands-')),path=join(dir,'state.db');let f=fixture(path);
 try{
  const input={request_id:'saved',text:'Summarize something',session_id:'api-session'};
  assert.equal(f.core.enqueueCommand(input).state,'queued');assert.equal(f.modelCalls,0);
  assert.equal(f.core.enqueueCommand(input).state,'queued');assert.equal(f.core.db.prepare('SELECT count(*) AS n FROM api_commands').get().n,1);
  assert.throws(()=>f.core.enqueueCommand({...input,text:'Different command'}),/different command/);
  assert.throws(()=>f.core.enqueueCommand({...input,session_id:'different'}),/different command/);
  f.core.db.close();f=fixture(path);assert.equal(f.core.commandStatus('saved').state,'queued');
  await f.core.processCommand();assert.equal(f.modelCalls,1);assert.equal(f.core.commandStatus('saved').state,'processed');assert.equal(f.core.commandStatus('saved').delivery[0].state,'pending');
  assert.equal(f.core.db.prepare('SELECT text FROM api_commands WHERE id=?').get('saved').text,'');
  await f.core.flush();assert.equal(f.core.commandStatus('saved').delivery[0].state,'accepted');
  await f.core.processCommand();assert.equal(f.modelCalls,1);assert.equal(f.core.enqueueCommand(input).state,'processed');
  f.core.command=async(_,ref,key)=>{f.core.draft({id:'associated-draft',kind:'send',created:f.now});f.core.enqueue(key,'Reviewed operation',{draft:'associated-draft'});};
  f.core.enqueueCommand({request_id:'review-job',text:'Prepare a review'});await f.core.processCommand();await f.core.flush();
  assert.equal(f.core.commandStatus('review-job').drafts[0].id,'associated-draft');
  f.core.enqueueCommand({request_id:'interrupted',text:'Do not replay'});
  f.core.db.exec("UPDATE api_commands SET state='processing' WHERE id='interrupted'");f.core.set('api:interrupted',{state:'processing'});
  f.core.db.close();f=fixture(path);assert.equal(f.core.commandStatus('interrupted').state,'unknown');await f.core.processCommand();assert.equal(f.modelCalls,0);
  assert.equal(f.core.db.prepare('SELECT text FROM api_commands WHERE id=?').get('interrupted').text,'');
  f.core.enqueueCommand({request_id:'cancel-me',text:'Queued text'});assert.equal(f.core.cancelCommand('cancel-me').state,'canceled');
  f.core.enqueueCommand({request_id:'disconnect-me',text:'Queued text'});f.core.disconnect();assert.equal(f.core.commandStatus('disconnect-me').state,'canceled');
  f.core.set('operator',operator);f.core.enqueueCommand({request_id:'rotate-me',text:'Queued text'});f.core.bindTransport({...bot,id:456});assert.equal(f.core.commandStatus('rotate-me').state,'canceled');
  assert.equal(f.core.commandStatus('saved').delivery[0].state,'accepted','retired bot outcomes remain attached to the request');
  assert.equal(f.core.commandStatus('review-job').drafts[0].id,'associated-draft','stable draft IDs survive retirement of provider references');
  f.core.set('operator',operator);f.core.enqueueCommand({request_id:'expired',text:'Queued text'});f.now+=86400001;f.core.maintain();assert.equal(f.core.commandStatus('expired').state,'expired');
  f.core.set('api:legacy',{state:'processed'});assert.equal(f.core.enqueueCommand({request_id:'legacy',text:'Never rerun an old request'}).state,'processed');
  for(let i=0;i<100;i++)f.core.enqueueCommand({request_id:'capacity-'+i,text:'/help'});
  assert.throws(()=>f.core.enqueueCommand({request_id:'capacity-overflow',text:'/help'}),/queue is full/);
  assert.equal(f.core.status().commands.length,10);assert(!JSON.stringify(f.core.status().commands).includes('Queued text'));
 }finally{f.core.db.close();rmSync(dir,{recursive:true,force:true});}
}

// Actual HTTP requests remain responsive while a model call is suspended.
// Telegram offsets advance only after those inputs are saved.
{
 const f=fixture();let release;f.modelWait=new Promise(resolve=>{release=resolve;});
 const service=createControlService({config:{manager:'@manager:test',roster:{},model:'fixture'},...f,authenticate:async token=>({user_id:token==='manager'?'@manager:test':'@other:test'}),host:'127.0.0.1',port:0,monitorInterval:10000,retryInterval:10,commandInterval:5});
 const address=await service.start(),url='http://127.0.0.1:'+address.port+'/control';
 const req=async(data,token='manager')=>{const response=await fetch(url,{method:'POST',headers:{Authorization:'Bearer '+token},body:JSON.stringify(data)});return {status:response.status,data:await response.json()};};
 try{
  await until(async()=>(await req({action:'status'})).data.ready);
  const first={action:'command',request_id:'slow',text:'Think about this',session_id:'same-session'};
  assert.equal((await req(first,'source')).status,403);
  assert.equal((await req(first)).data.state,'queued');await until(()=>f.modelCalls===1);
  const started=performance.now();assert.equal((await req({...first,request_id:'second',text:'/help'})).data.state,'queued');
  assert(performance.now()-started<1000,'submission cannot wait for the held model');
  assert.equal((await req(first)).data.state,'processing');assert.equal((await req({action:'command_status',request_id:'slow'})).data.state,'processing');
  for(const action of ['lists','rules']){
    const readStarted=performance.now();
    const response=await Promise.race([req({action}),new Promise((_,reject)=>{const t=setTimeout(()=>reject(Error(action+' waited for the model')),1000);t.unref();})]);
    assert.equal(response.status,200);assert(performance.now()-readStarted<1000);
  }
  assert.equal((await req({action:'command_status',request_id:'slow'},'source')).status,403);
  assert.equal((await req({...first,text:'Changed'})).status,400);
  assert.equal((await req({action:'command_cancel',request_id:'slow'})).status,400);
  assert.equal((await req({action:'command_cancel',request_id:'second'})).data.state,'canceled');
  f.updates.push({update_id:1,message:{message_id:1,chat:{id:7,type:'private'},from:{id:7},text:'/help'}});
  await until(()=>f.core.value('offset')===2);assert.equal(f.core.db.prepare('SELECT state FROM inbox WHERE id=1').get().state,'pending');assert.equal(f.sent.length,0);
  release();await until(async()=>(await req({action:'command_status',request_id:'slow'})).data.delivery.some(d=>d.state==='accepted'));
  await until(()=>f.core.db.prepare('SELECT state FROM inbox WHERE id=1').get().state==='done');assert.equal(f.modelCalls,1);assert.equal(f.sent.length,2);
  f.offline=true;await until(async()=>!(await req({action:'status'})).data.transport_ready);
  assert.equal((await req({action:'command',request_id:'offline',text:'/help'})).data.state,'queued');
  f.offline=false;await until(async()=>(await req({action:'command_status',request_id:'offline'})).data.state==='processed');
  f.core.model=async()=>{throw Error('Provider SECRET');};await req({action:'command',request_id:'failed',text:'Model question'});
  await until(async()=>(await req({action:'command_status',request_id:'failed'})).data.state==='failed');
  assert(!(await req({action:'command_status',request_id:'failed'})).data.error.includes('SECRET'));
  assert.equal(f.core.db.prepare('SELECT text FROM api_commands WHERE id=?').get('failed').text,'');
 }finally{release();await service.stop();f.core.db.close();}
}

// The real HTTP settings path and monitor can run during the provider wait.
// Revoking hosted context must discard a returned tool call before it can draft.
{
 const f=fixture();let release,modelReturned=false,toolCalls=0,monitored=0;
 f.allowed=true;f.revision=0;f.domain.settings=()=>({retention_days:1,allow_model_context:f.allowed});
 f.domain.contextStamp=()=>String(f.revision);f.domain.state=()=>({control:{revision:f.revision}});
 f.domain.store={refresh:async()=>{}};f.domain.operation=(_,__,revision,settings)=>({revision,settings});
 f.domain.validateOperation=async()=>{};f.domain.apply=async operation=>{f.allowed=operation.settings.allow_model_context;f.revision++;};
 f.core.tool=async()=>{toolCalls++;throw Error('Revoked model must not invoke tools');};
 f.core.model=async()=>{f.modelCalls++;await new Promise(resolve=>{release=resolve;});modelReturned=true;return {tool_calls:[{id:'call',type:'function',function:{name:'prepare_send',arguments:'{}'}}]};};
 f.core.monitor=async()=>{monitored++;f.core.enqueue('monitor-during-model','Synthetic concurrent alert');};
 const service=createControlService({config:{manager:'@manager:test',roster:{},model:'fixture'},...f,authenticate:async()=>({user_id:'@manager:test'}),host:'127.0.0.1',port:0,monitorInterval:10000,commandInterval:5});
 const address=await service.start(),url='http://127.0.0.1:'+address.port+'/control';
 const req=async data=>{const r=await fetch(url,{method:'POST',headers:{Authorization:'Bearer manager'},body:JSON.stringify(data)});assert.equal(r.status,200);return r.json();};
 try{
  await until(async()=>(await req({action:'status'})).ready);
  await req({action:'command',request_id:'revoked-model',text:'Prepare a reply'});await until(()=>f.modelCalls===1);
  await service.monitorOnce();assert.equal(monitored,1);assert.equal(f.sent.length,1);assert(!modelReturned);
  assert.equal((await req({action:'settings_update',revision:0,settings:{allow_model_context:false}})).settings.allow_model_context,false);
  release();await until(() => f.core.commandStatus('revoked-model').state==='failed');
  assert.equal(toolCalls,0);assert.equal(f.core.db.prepare('SELECT count(*) AS n FROM drafts').get().n,0);
  assert.equal((await req({action:'lists'})).lists.length,0);
 }finally{release?.();await service.stop();f.core.db.close();}
}

// A completed model must reacquire the domain lease. It cannot run its scope
// check or mutate a draft while a list refresh is still in progress.
{
 const f=fixture();let releaseModel,releaseRead,holdRead=false,readStarted=false;
 f.core.model=async()=>{f.modelCalls++;await new Promise(resolve=>{releaseModel=resolve;});return {content:'Synthetic resumed answer'};};
 f.domain.refresh=async()=>{if(holdRead){readStarted=true;await new Promise(resolve=>{releaseRead=resolve;});}};
 const service=createControlService({config:{manager:'@manager:test',roster:{}},...f,authenticate:async()=>({user_id:'@manager:test'}),host:'127.0.0.1',port:0,monitorInterval:10000,commandInterval:5});
 await service.start();
 try{
  await until(()=>f.core.status().health.startup?.error===null&&f.core.value('transport_identity'));
  await service.handle({action:'command',request_id:'lease',text:'Question'});await until(()=>f.modelCalls===1);
  holdRead=true;const reading=service.handle({action:'lists'});await until(()=>readStarted);
  releaseModel();await new Promise(resolve=>setTimeout(resolve,20));
  assert.equal(f.core.commandStatus('lease').state,'processing');assert.equal(f.sent.length,0);
  holdRead=false;releaseRead();await reading;
  await until(()=>f.core.commandStatus('lease').state==='processed');assert.equal(f.modelCalls,1);
 }finally{releaseModel?.();releaseRead?.();await service.stop();f.core.db.close();}
}

// Disconnect/re-pair (even to the same operator) invalidates a suspended model.
// Bot rotation with reused update IDs must not finish the new bot's inbox row.
for(const rotate of [false,true]){
 const f=fixture();let release;
 f.core.model=async()=>{f.modelCalls++;await new Promise(resolve=>{release=resolve;});return {content:'Old operator context'};};
 const service=createControlService({config:{manager:'@manager:test',roster:{}},...f,authenticate:async()=>({user_id:'@manager:test'}),host:'127.0.0.1',port:0,monitorInterval:10000,commandInterval:10000});
 await service.start();
 try{
  await until(()=>f.core.status().health.startup?.error===null&&f.core.value('transport_identity'));
  f.core.ingest([{update_id:21,message:{message_id:21,chat:{id:7,type:'private'},from:{id:7},text:'Think'}}]);
  const command=service.commandsOnce();await until(()=>f.modelCalls===1);
  if(rotate){f.core.bindTransport({...bot,id:456});f.core.ingest([{update_id:21,message:{text:'new bot row'}}]);}
  else await service.handle({action:'disconnect'});
  f.core.set('operator',operator);release();await command;
  assert.equal(f.sent.length,0);assert.equal(f.core.db.prepare('SELECT count(*) AS n FROM outbox').get().n,0);
  if(rotate)assert.equal(f.core.db.prepare('SELECT state FROM inbox WHERE id=21').get().state,'pending');
 }finally{release?.();await service.stop();f.core.db.close();}
}
console.log('Async commands: responsive HTTP reads/settings and monitoring during model waits; serialized continuation; revocation and identity isolation; durable commands and no replay passed.');
