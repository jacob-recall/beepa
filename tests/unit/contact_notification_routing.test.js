import assert from 'node:assert/strict';
import {mkdtempSync,rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {ControlCore} from '../../master/control-core.mjs';
import {notificationIdentity} from '../../master/notification-routing.mjs';

function fixture(path=':memory:'){
 const f={now:Date.parse('2026-10-19T10:30:00Z'),sent:[],jobs:new Map(),revoked:false,completed:false,fresh:true};
 f.rule={list_id:'list',list_name:'Follow up',revision:1,binding_id:'binding',owner:'@david:test',generation:'gen',created_at:f.now-100000,episode_at:f.now-100000,
  due_at:f.now-1,snooze_until:null,title:'Shared contact',source:'imessage',channels:['browser','telegram'],routing:null};
 f.rules=[f.rule];
 const entry=()=>({binding:{binding_id:'binding',owner_mxid:f.rule.owner,generation:'gen',person_id:'person'},member:{},contact:{source:'imessage',network_id:'target@example.test'},
  obligation:{created_at:f.rule.created_at,episode_at:f.rule.episode_at}});
 const domain={rooms:new Map(),settings:()=>({retention_days:30}),refresh:async()=>{},hydrateAccounts:async()=>{},rules:()=>[],updates:async()=>[],
  contactRules:()=>f.revoked?[]:f.rules,contactMember:()=>f.revoked?null:entry(),liveContact:async()=>{if(f.revoked)throw Error('revoked');},memberRooms:()=>[],
  contactUpdates:async list_id=>f.revoked?[]:[{kind:'contact',binding_id:'binding',owner:f.rule.owner,generation:'gen',created_at:f.rule.created_at,episode_at:f.rule.episode_at,due_at:f.rule.due_at,snooze_until:f.rule.snooze_until,
    waiting:f.completed?'contacted':'uncontacted',status_label:f.completed?'Confirmed outreach recorded':'No shared conversation linked',title:f.rule.title,source:'imessage',list_id,fresh:f.fresh}],
  liveRoom:async()=>{throw Error('Contact must not invent a conversation');},history:async()=>{throw Error('No conversation history');}};
 const browser=async data=>{
  if(data.action==='control_dispatcher')return {version:1,id:'a'.repeat(32),owner:'control',follows:[]};
  if(data.action==='control_enqueue'){
   const e=data.entries[0];assert.deepEqual(Object.keys(e).sort(),['contact','kind','refs']);assert.equal(e.kind,'contact_reminder');
   const id=notificationIdentity(e.contact);if(!f.jobs.has(id))f.jobs.set(id,{id,state:f.browserState||'pending',entry:e});return {outcomes:[f.jobs.get(id)]};
  }
  return {outcomes:data.ids.map(id=>f.jobs.get(id)||{id,state:'missing'})};
 };
 f.core=new ControlCore({path,domain,browser,clock:()=>f.now,model:async()=>{throw Error('No model in reminders');},telegram:async(_,p)=>{f.sent.push(p);if(f.failure)throw f.failure;return {message_id:f.sent.length};}});
 f.core.set('operator',{user:7,chat:7});f.domain=domain;f.tick=async()=>{await f.core.monitor();await f.core.flush();};f.close=()=>f.core.db.close();return f;
}
{
 const f=fixture();try{
  f.rules.push({...f.rule,list_id:'overlap',list_name:'Another list'});
  await f.tick();await f.tick();assert.equal(f.sent.length,1);assert.equal(f.jobs.size,1);assert(f.sent[0].text.includes('Follow up, Another list'));
  const ref=f.core.ref(7,1);assert(ref.contact);assert.equal(ref.room,undefined);assert(!JSON.stringify(ref).includes('Shared contact'));
  await f.core.command('status',ref,'quoted-status');await f.core.flush();assert.equal(f.sent.length,2);assert(f.sent[1].text.includes('No shared conversation linked'));
  f.revoked=true;await assert.rejects(f.core.command('status',ref,'revoked-status'),/no longer shared/);
 }finally{f.close();}
}
for(const completion of [false,true]){
 const f=fixture();try{
  await f.core.monitor();f.fresh=false;await f.tick();assert.equal(f.core.status().pending,1);assert.equal(f.sent.length,0);
  f.completed=completion;f.fresh=true;f.now+=60001;await f.tick();await f.tick();assert.equal(f.sent.length,completion?0:1);assert.equal(f.core.status().pending,0);
 }finally{f.close();}
}
for(const reason of ['revoked','completed','stale','snoozed']){
 const f=fixture();try{
  await f.core.monitor();
  if(reason==='revoked')f.revoked=true;if(reason==='completed')f.completed=true;if(reason==='stale')f.fresh=false;if(reason==='snoozed')f.rule.snooze_until=f.now+60000;
  await f.core.flush();assert.equal(f.sent.length,0,reason);assert.equal(f.core.status().pending,reason==='stale'?1:0);
  if(reason==='stale'){f.fresh=true;f.now+=60001;await f.tick();await f.tick();assert.equal(f.sent.length,1);}
 }finally{f.close();}
}
{
 const f=fixture();try{
  f.rule.routing={primary:'telegram',fallback:true};f.failure=Object.assign(Error('blocked'),{status:403});
  await f.tick();assert.equal(f.jobs.size,0);await f.tick();assert.equal(f.jobs.size,1);assert.equal(f.sent.length,1);
 }finally{f.close();}
}
{
 const f=fixture();try{
  f.rule.routing={primary:'telegram',fallback:true};f.failure=Error('lost acknowledgment');
  await f.tick();await f.tick();assert.equal(f.sent.length,1);assert.equal(f.jobs.size,0,'uncertain Telegram delivery never falls back');
 }finally{f.close();}
}
{
 const f=fixture();try{
  f.rule.routing={primary:'browser',fallback:true};f.browserState='failed';
  await f.tick();await f.tick();assert.equal(f.jobs.size,1);assert.equal(f.sent.length,1);
 }finally{f.close();}
}
{
 const f=fixture();try{
  f.rule.channels=['telegram'];f.rule.schedule={timezone:'UTC',mode:'working',days:[1,2,3,4,5,6,7],start_minute:660,end_minute:720};
  await f.tick();assert.equal(f.sent.length,0);assert.equal(f.core.status().pending,1);
  f.now+=1800000;await f.tick();assert.equal(f.sent.length,1);
  f.rule.snooze_until=f.now+60000;f.rule.episode_at=f.now;f.rule.revision++;
  await f.tick();assert.equal(f.sent.length,1);
  f.now+=60001;await f.tick();assert.equal(f.sent.length,2,'new explicit snooze episode resumes without changing original deadline');
  assert(f.sent[1].text.includes(f.core.when(f.rule.due_at)));await f.tick();assert.equal(f.sent.length,2);
 }finally{f.close();}
}
{
 const dir=mkdtempSync(join(tmpdir(),'contact-routing-')),path=join(dir,'control.db');let f=fixture(path);
 try{f.rule.channels=['telegram'];await f.tick();assert.equal(f.sent.length,1);f.close();f=fixture(path);f.rule.channels=['telegram'];await f.tick();assert.equal(f.sent.length,0,'accepted deadline survives restart');}
 finally{f.close();rmSync(dir,{recursive:true,force:true});}
}
{
 const f=fixture();try{
  await f.tick();const ref=f.core.ref(7,1);let calls=0;
  f.core.model=async()=>++calls===1?{tool_calls:[{id:'wrong',type:'function',function:{name:'prepare_send',arguments:JSON.stringify({room_id:'!wrong:test',body:'Wrong recipient'})}}]}:{content:'Choose the current contact route.'};
  await f.core.command('Tell them Tuesday',ref,'wrong-route');assert.equal(f.core.db.prepare('SELECT count(*) n FROM drafts').get().n,0);
  await f.core.flush();assert(f.sent.at(-1).text.includes('Choose the current contact route.'),'reply must not inherit the alert renderer');
  assert.equal(f.core.ref(7,f.sent.length).alert,undefined);
  await f.core.command('status',ref,'revoked-before-delivery');f.revoked=true;const before=f.sent.length;await f.core.flush();assert.equal(f.sent.length,before);
 }finally{f.close();}
}
{
 const base={kind:'contact_reminder',binding:'binding',owner:'@owner:test',generation:'gen',created:1,episode:2,due:3,slot:3};
 for(const patch of [{room:'!fake:test'},{owner:'not-an-account'},{binding:'../escape'},{episode:0},{slot:2}])assert.throws(()=>notificationIdentity({...base,...patch}));
 for(const patch of [{owner:'@other:test'},{generation:'new'},{episode:3},{created:2}])assert.notEqual(notificationIdentity(base),notificationIdentity({...base,...patch}));
}
console.log('Contact reminders: typed identity, list overlap, exact quoted task, revocation, native completion, schedules/snooze, fallback, unknown outcomes and restart passed.');
