import assert from 'node:assert/strict';
import {ControlCore} from '../../master/control-core.mjs';
import {createControlService} from '../../master/telegram-service.mjs';

const config={manager:'@manager:test',roster:{},model:'fixture'};
function fixture(){
  const f={offline:true,menuFails:false,webhook:false,updates:[],sent:[],refreshes:0,monitors:0,sourceDown:false};
  const domain={rooms:new Map(),health:new Map(),roster:async()=>({'@source:test':'!source:test'}),allowedOwner:()=>true,store:{refresh:async()=>{}},refresh:async()=>{f.refreshes++;if(f.sourceDown)throw Error('untrusted source secret');},hydrateAccounts:async()=>{},state:()=>({}),settings:()=>({}),
    rules:()=>[{list_id:'canonical',room:'!room:test',channels:['browser']}],lists:()=>[{list_id:'canonical'}],updates:async()=>[],preparePolicy:()=>{},};
  const telegram=async(method,data)=>{
    if(f.offline)throw Error('untrusted provider secret');
    if(method==='getMe')return {id:123,is_bot:true,username:'fixture_bot'};
    if(method==='getWebhookInfo')return {url:f.webhook?'https://existing.invalid/':''};
    if(method==='setMyCommands'){if(f.menuFails)throw Error('menu');return true;}
    if(method==='getUpdates'){await new Promise(r=>setTimeout(r,5));return f.updates.splice(0);}
    if(method==='sendMessage'){f.sent.push(data);return {message_id:f.sent.length};}
    return true;
  };
  const core=new ControlCore({path:':memory:',domain,telegram,model:async()=>({content:'Fixture'})});
  core.monitor=async()=>{f.monitors++;await domain.refresh();};
  const service=createControlService({config,domain,core,telegram,host:'127.0.0.1',port:0,monitorInterval:10,retryInterval:10,authenticate:async token=>{if(token==='manager')return {user_id:config.manager};if(token==='user')return {user_id:'@user:test'};throw Error('bad credential');}});
  return Object.assign(f,{domain,core,service});
}
const until=async fn=>{for(let i=0;i<200;i++){if(await fn())return;await new Promise(r=>setTimeout(r,5));}throw Error('Condition did not become true');};
{
 const f=fixture(),address=await f.service.start(),url='http://127.0.0.1:'+address.port+'/control';
 const request=async(data,token='manager')=>{const r=await fetch(url,{method:'POST',headers:{Authorization:'Bearer '+token},body:typeof data==='string'?data:JSON.stringify(data)});return {code:r.status,value:await r.json()};};
 try{
   await until(async()=>(await request({action:'status'})).value.domain_ready);
   assert.equal((await request('not json','user')).code,403);assert.equal((await request('not json','invalid')).code,401);
   let s=(await request({action:'status'})).value;assert.equal(s.ready,false);assert.equal(s.transport_ready,false);assert(s.health.telegram_setup.error);
   for(const action of ['rules','lists','updates','settings','ping'])assert.equal((await request({action})).code,200,action+' works while Telegram is down');
   assert.equal((await request({action:'ping'},'user')).code,403);
   assert.equal((await request({action:'ping',account:22})).code,400);
   assert.equal((await request({action:'ping'})).value.accounts[0].master.status,'unknown');
   assert.deepEqual((await request({action:'rules'})).value.rules,f.domain.rules());
   for(const action of ['pair','test'])assert.match((await request({action})).value.error,/Telegram is reconnecting/);
   assert.match((await request({action:'command',request_id:'unpaired',text:'/help'})).value.error,/Connect Telegram first/);
   await until(()=>f.monitors>0);assert(f.refreshes>1);
   // Existing webhook blocks this poller, never domain/browser functionality.
   f.offline=false;f.webhook=true;await new Promise(r=>setTimeout(r,25));assert.equal((await request({action:'status'})).value.transport_ready,false);
   f.webhook=false;f.menuFails=true;await until(async()=>(await request({action:'status'})).value.transport_ready);
   await until(async()=>/menu unavailable/.test((await request({action:'status'})).value.health.telegram_setup?.error||''));
   const link=(await request({action:'pair'})).value.url,code=new URL(link).searchParams.get('start');
   f.updates.push({update_id:1,message:{message_id:1,chat:{id:7,type:'private'},from:{id:7},text:'/start '+code}});
   await until(()=>f.sent.length===1);assert.equal((await request({action:'status'})).value.paired,true);
   // An outage after pairing does not erase the binding or block the browser.
   f.offline=true;await until(async()=>!(await request({action:'status'})).value.transport_ready);
   assert.equal((await request({action:'rules'})).code,200);assert.equal((await request({action:'status'})).value.paired,true);
   f.core.enqueue('deferred','Held while transport is offline');const n=f.sent.length;
   await new Promise(r=>setTimeout(r,30));assert.equal(f.sent.length,n);
   f.offline=false;f.menuFails=false;await until(()=>f.sent.length===n+1);assert.equal(f.sent.at(-1).text,'Held while transport is offline');
   s=(await request({action:'status'})).value;assert.equal(s.ready,true);assert.equal(s.health.telegram_setup.error,null);assert(!JSON.stringify(s).includes('secret'));
   f.sourceDown=true;await until(async()=>!!(await request({action:'status'})).value.health.monitoring?.error);
   assert.equal((await request({action:'rules'})).code,400,'failed source refresh never serves cached rules');
 }finally{await f.service.stop();f.core.db.close();}
}
console.log('Telegram service: authenticated production HTTP, independent domain readiness, webhook/outage recovery, retained pairing and deferred delivery passed.');
