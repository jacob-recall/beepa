import assert from 'node:assert/strict';
import {mkdtempSync,rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {ControlCore} from '../../master/control-core.mjs';
import {ControlDomain,defaultMonitoring} from '../../master/control-domain.mjs';
import {createCRMState,applyCRMOperation,withReminderEpisode} from '../../shared/model/crm.js';
import {notificationIdentity} from '../../master/notification-routing.mjs';
import {telegramNotificationKey} from '../../master/notification-routing-adapters.mjs';
const owner='@owner:test',manager='@manager:test',room='!room:test',space='!space:test';
function fixture({routing=true,path=':memory:'}={}){
 const h={now:1700000000000,sent:[],jobs:new Map()};let state=createCRMState({workspace_id:'!crm:test',manager_mxid:manager});
 const domain=new ControlDomain({manager,bootstrap:async()=>{},roster:async()=>({[owner]:space}),clock:()=>h.now,api:async()=>{throw Error('Unexpected request');}});
 domain.state=()=>state;domain.refresh=async()=>{};domain.hydrateAccounts=async()=>{};
 h.apply=op=>{const r=applyCRMOperation(state,op);assert(r.outcome.accepted,r.outcome.code);state=r.state;return r.outcome;};domain.store={refresh:async()=>{},append:async op=>h.apply(op)};
 h.addList=id=>{
  h.apply(domain.operation('list.create',id,0,{name:id,purpose:'monitoring'}));
  h.apply(domain.operation('list.watch',id,1,{room_id:room,owner_mxid:owner,enabled:true,due_at:null,snooze_until:null,resolved_event:null}));
  h.apply(domain.operation('list.monitoring',id,2,{...defaultMonitoring(),enabled:true,activity_enabled:false,channels:['telegram']}));
 };h.addList('First');domain.accounts.set(owner,{generation:'g1',journal_epoch:'j1'});
 const projection={schema:1,state:'ready',owner_mxid:owner,generation:'g1',journal_epoch:'j1',bridge_epoch:'b1',room_id:'!native:test',mirror_room_id:room,source:'imessage',updated_ts:h.now,coverage:'native_history',complete:true,
  messages:[{event_id:'$a',incoming:true,order:'1',timestamp:h.now-2000,answered_by:null},{event_id:'$b',incoming:true,order:'2',timestamp:h.now-1000,answered_by:null}]};
 const stateEvent=(type,content,state_key='')=>({type,content,state_key,sender:owner});
 const r={id:room,owner,native:'!native:test',source:'imessage',title:'Contact',level:'direct',state:[stateEvent('m.room.create',{'com.jkali.mirror_of':'!native:test'}),stateEvent('com.jkali.share_level',{level:'direct'}),stateEvent('com.jkali.conversation_tracking',projection)]};domain.rooms.set(room,r);
 const events=projection.messages.map(m=>({event_id:m.event_id,type:'m.room.message',sender:owner,origin_server_ts:m.timestamp,content:{msgtype:'m.text',body:m.event_id,'com.jkali.from_me':false}}));
 domain.api=async(method,path)=>{
  assert.equal(method,'GET');
  if(path.includes(encodeURIComponent(space)))return [stateEvent('m.room.create',{type:'m.space'}),stateEvent('m.space.child',{via:['test']},room)];
  if(path.endsWith('/state'))return r.state;
  if(path.includes('/event/'))return events.find(e=>e.event_id===decodeURIComponent(path.split('/').at(-1)));
  return {start:'end',end:'end',chunk:new URL('http://test'+path).searchParams.has('from')?[]:events};
 };
 const browser=async data=>{
  if(data.action==='control_dispatcher')return {id:'a'.repeat(32),version:1,owner:'control',follows:[]};
  if(data.action==='control_enqueue'){
   const e=data.entries[0],s={kind:e.kind,room:e.room,event:e.event,due:e.refs.due,slot:e.refs.slot,...(e.refs.episode?{episode:e.refs.episode}:{})},id=notificationIdentity(s);
   if(!h.jobs.has(id))h.jobs.set(id,{id,state:'accepted'});return {devices:1,outcomes:[h.jobs.get(id)]};
  }
  return {devices:1,outcomes:data.ids.map(id=>h.jobs.get(id)||{id,state:'missing'})};
 };
 const open=()=>{h.core=new ControlCore({path,domain,browser:routing?browser:null,clock:()=>h.now,model:async()=>{throw Error('No model');},telegram:async(_,data)=>{h.sent.push(data);if(h.uncertain)throw Error('Response lost');return {message_id:h.sent.length};}});h.core.set('operator',{user:1,chat:1});};open();
 h.advance=ms=>{h.now+=ms;projection.updated_ts=h.now;domain.health.set(owner,{connected:true,updated_at:h.now});};h.advance(0);
 h.edit=async(changes,list_id='First',event_id='$a')=>{const op=await domain.prepareObligation({list_id,room_id:room,event_id,...changes});h.apply(op);return op;};
 h.poll=async()=>{await h.core.monitor();await h.core.flush();};h.restart=()=>{h.core.db.close();open();};h.close=()=>h.core.db.close();
 return Object.assign(h,{domain,projection,events,state:()=>state});
}
// Temporary loss of verification preserves durable work; explicit changes still revoke it.
for(const routing of [true,false])for(const outcome of ['resume','reply','remove']){
 const dir=mkdtempSync(join(tmpdir(),'beepa-reminder-offline-')),h=fixture({routing,path:join(dir,'control.db')});try{
  await h.edit({due_at:h.now-1});await h.core.monitor();assert.equal(h.core.status().pending,1);
  h.domain.health.set(owner,{connected:false,updated_at:h.now});await h.poll();assert.equal(h.sent.length,0);assert.equal(h.core.status().pending,1);
  h.restart();await h.poll();assert.equal(h.sent.length,0,'restart cannot bypass freshness');
  if(outcome==='reply'){h.projection.messages[0].answered_by='$b';h.projection.messages[1].incoming=false;h.events[1].content['com.jkali.from_me']=true;}
  if(outcome==='remove')h.apply(h.domain.prepareWatch({list_id:'First',room_id:room,enabled:false}));
  h.advance(60001);await h.poll();await h.poll();assert.equal(h.sent.length,outcome==='resume'?1:0,outcome);
  assert.equal(h.core.status().pending,0,JSON.stringify({routing,outcome,rows:h.core.db.prepare('SELECT state,next_try,reference FROM outbox').all(),now:h.now}));h.restart();await h.poll();assert.equal(h.sent.length,outcome==='resume'?1:0,'no duplicate after restart');
 }finally{h.close();rmSync(dir,{recursive:true,force:true});}
}
for(const failure of ['timeout',500,403,404,'private']){
 const h=fixture();try{
  await h.edit({due_at:h.now-1});await h.core.monitor();const api=h.domain.api;
  h.domain.api=async(method,path)=>{if(path.endsWith('/state')){
   if(failure==='private')return (await api(method,path)).map(e=>e.type==='com.jkali.share_level'?{...e,content:{level:'private'}}:e);
   throw Object.assign(Error('Verification outage'),{status:failure});
  }return api(method,path);};
  await h.core.flush();assert.equal(h.sent.length,0);assert.equal(h.core.status().pending,['timeout',500].includes(failure)?1:0);
  h.domain.api=api;h.advance(60001);await h.poll();assert.equal(h.sent.length,['timeout',500].includes(failure)?1:0);
 }finally{h.close();}
}
for(const routing of [true,false]){
 const h=fixture({routing});try{
  const due=h.now-1;await h.edit({due_at:due});await h.core.monitor();assert.equal(h.core.status().pending,1);
  const snooze=await h.edit({snooze_until:h.now+60000});assert.equal(snooze.payload.due_at,due);assert(snooze.payload.episode_at);
  await h.poll();assert.equal(h.sent.length,0);h.advance(60001);await h.poll();await h.poll();assert.equal(h.sent.length,1,'queued snoozed reminder resumes once');
  const ref=h.core.ref(1,1);assert.equal(ref.due,due);assert.equal(ref.episode,snooze.payload.episode_at);
  await h.edit({resolved:true});await h.poll();await h.edit({resolved:false});await h.poll();assert.equal(h.sent.length,2,'explicit reopening starts a fresh cycle');
  await h.edit({resolved:false});await h.poll();assert.equal(h.sent.length,2,'unchanged edit does not restart the cycle');
 }finally{h.close();}
}
{
 const dir=mkdtempSync(join(tmpdir(),'beepa-reminder-episode-')),h=fixture({path:join(dir,'control.db')});try{
  await h.edit({due_at:h.now-1});await h.poll();assert.equal(h.sent.length,1);
  const legacy={kind:'reminder',room,event:'$a',due:h.now-1,slot:h.now-1};assert.equal(h.core.notificationRouter.status(notificationIdentity(legacy)).attempts[0].state,'pending');
  await h.edit({snooze_until:h.now+60000});await h.poll();h.restart();h.advance(60001);await h.poll();assert.equal(h.sent.length,2);
  h.restart();await h.poll();await h.poll();assert.equal(h.sent.length,2,'restart never replays either cycle');
  assert(h.core.db.prepare("SELECT id FROM outbox WHERE id LIKE ?").get(telegramNotificationKey(legacy)+':%'),'legacy ID preserved');
 }finally{h.close();rmSync(dir,{recursive:true,force:true});}
}
{
 const h=fixture();try{
  h.addList('Second');await h.edit({due_at:h.now-1});await h.edit({due_at:h.now-1},'Second');await h.poll();assert.equal(h.sent.length,1);assert.match(h.sent[0].text,/First, Second/);
  await h.edit({snooze_until:h.now+60000});await h.poll();h.advance(60001);await h.poll();assert.equal(h.sent.length,2);assert.match(h.sent[1].text,/Lists: First/);assert(!h.sent[1].text.includes('Second'),'another list does not acquire a new reminder cycle');
 }finally{h.close();}
}
{
 const h=fixture();try{
  h.apply(h.domain.preparePolicy({list_id:'First',waiting_us_ms:60000}));h.advance(60000);
  const before=(await h.domain.updates('First'))[0].obligations.find(o=>o.event_id==='$a').due_at;
  const op=await h.edit({snooze_until:h.now+60000});assert.equal(op.payload.due_at,before,'snoozing inherits and preserves a relative list deadline');
  await h.poll();h.projection.messages[0].answered_by='$b';h.advance(60001);await h.poll();assert(!h.sent.some(m=>m.text.includes('Message: $a')),'answered snoozed obligation does not remind');
 }finally{h.close();}
}
{
 const h=fixture();try{
  await h.edit({due_at:h.now-1});h.uncertain=true;await h.poll();assert.equal(h.sent.length,1);
  const old=h.core.db.prepare('SELECT id FROM outbox').get().id;assert.equal(h.core.db.prepare('SELECT state FROM outbox WHERE id=?').get(old).state,'unknown');
  await h.edit({snooze_until:h.now+60000});h.uncertain=false;await h.poll();h.advance(60001);await h.poll();assert.equal(h.sent.length,2);assert.equal(h.core.db.prepare('SELECT state FROM outbox WHERE id=?').get(old).state,'unknown');
 }finally{h.close();}
}
{
 const payload={room_id:room,owner_mxid:owner,event_id:'$a',due_at:10,snooze_until:null,resolved:false};
 assert.equal(withReminderEpisode(undefined,payload,20).episode_at,undefined,'first identical deadlines retain cross-list deduplication');
 const snoozed=withReminderEpisode(payload,{...payload,snooze_until:30},20);assert.equal(snoozed.episode_at,20);
 assert.equal(withReminderEpisode(snoozed,snoozed,40).episode_at,20);
 assert.equal(withReminderEpisode(snoozed,{...snoozed,snooze_until:null},10).episode_at,21,'clock rollback cannot reuse the old cycle');
 const h=fixture();try{h.apply(h.domain.operation('list.obligation','First',h.domain.list('First').revision,snoozed));
  for(const bad of [undefined,0,-1,19,true]){const p={...snoozed};if(bad===undefined)delete p.episode_at;else p.episode_at=bad;
   const op={...h.domain.operation('list.obligation','First',h.domain.list('First').revision,p),payload:p};assert.equal(applyCRMOperation(h.state(),op).outcome.accepted,false);}
  const stale={...snoozed,snooze_until:40};const op={...h.domain.operation('list.obligation','First',h.domain.list('First').revision,stale),payload:stale};
  assert.equal(applyCRMOperation(h.state(),op).outcome.accepted,false,'an old editor cannot change a timer while retaining its old cycle');
 }finally{h.close();}
 for(const episode of [null,0,-1,true,1.5])assert.throws(()=>notificationIdentity({kind:'reminder',room,event:'$a',due:10,slot:10,episode}));
}
{
 const h=fixture();try{
  await h.edit({due_at:h.now-1});
  const watch=h.domain.prepareWatch({list_id:'First',room_id:room,snooze_until:h.now+60000});h.apply(watch);
  const message=await h.edit({snooze_until:h.now+120000});assert(message.payload.episode_at>watch.payload.episode_at);
  const nextWatch=h.domain.prepareWatch({list_id:'First',room_id:room,snooze_until:h.now+180000});h.apply(nextWatch);assert(nextWatch.payload.episode_at>message.payload.episode_at);
 }finally{h.close();}
}
console.log('Message reminder episodes: queued/accepted/unknown snooze, reopen, legacy IDs, overlap, restart, inherited deadlines, source replies, schema and clock rollback passed.');
