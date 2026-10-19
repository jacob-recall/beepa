import assert from 'node:assert/strict';
import {mkdtempSync,rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {ControlCore} from '../../master/control-core.mjs';
import {monitoringDeliveryPlan} from '../../shared/model/crm.js';

function fixture(path=':memory:'){
 const h={now:1700000000010,sent:[],events:new Map(),revoked:new Set()};
 const roomA='!ada-david:test',roomB='!ada-elliot:test';
 h.rules=[{room:roomA,owner:'@david:test',title:'Ada',source:'imessage'},{room:roomB,owner:'@elliot:test',title:'Ada',source:'whatsapp'}].map(r=>({...r,list_id:'shared',list_name:'Shared list',revision:1,channels:['telegram'],all_messages:false,preview:false,presentation:{mode:'digest',interval_ms:60000}}));
 for(const r of h.rules)h.events.set(r.room,[]);
 const domain={rooms:new Map(h.rules.map(r=>[r.room,r])),settings:()=>({retention_days:30}),refresh:async()=>{},hydrateAccounts:async()=>{},rules:()=>h.rules,updates:async()=>[],
  liveRoom:async id=>{if(h.revoked.has(id))throw Object.assign(Error('revoked'),{code:'scope_changed'});if(h.offline===id)throw Error('temporary outage');return domain.rooms.get(id);},
  history:async id=>{await domain.liveRoom(id);const r=domain.rooms.get(id),events=h.events.get(id);return {conversation:{...r,id},messages:events.map(e=>({id:e.event_id,text:e.content.body,from_me:false,ts:e.origin_server_ts})),start:String(events.length),cursor:null};},
  api:async(method,path)=>{const u=new URL('https://fixture'+path),id=decodeURIComponent(u.pathname.split('/')[5]),events=h.events.get(id);return {chunk:events.slice(Number(u.searchParams.get('from'))),end:String(events.length)};}};
 const options={path,domain,clock:()=>h.now,model:async()=>{throw Error('Model must not guess a digest route');},telegram:async(method,payload)=>{h.sent.push({method,...payload});if(h.failure)throw h.failure;return {message_id:h.sent.length};}};
 h.core=new ControlCore(options);h.core.set('operator',{user:1,chat:1});
 h.message=id=>{const r=domain.rooms.get(id),events=h.events.get(id);events.push({event_id:'$'+id.slice(1)+'-'+events.length,sender:r.owner,type:'m.room.message',origin_server_ts:h.now,content:{msgtype:'m.text',body:'Synthetic '+id}});};
 h.poll=async()=>{await h.core.monitor();await h.core.flush();};h.roomA=roomA;h.roomB=roomB;h.options=options;h.close=()=>h.core.db.close();return h;
}
{
 const h=fixture();try{
  await h.poll();h.message(h.roomA);h.message(h.roomA);h.message(h.roomB);await h.poll();assert.equal(h.sent.length,0);assert.equal(h.core.status().pending,3);
  h.now+=60000;await h.poll();assert.equal(h.sent.length,1);assert.match(h.sent[0].text,/3 messages in 2 conversations/);assert.equal(h.sent[0].reply_markup.inline_keyboard.length,2);
  const ref=h.core.ref(1,1);assert.equal(ref.bundle.length,2);assert(!JSON.stringify(ref).includes('Synthetic'));assert.equal(h.core.status().pending,0);
  await assert.rejects(h.core.command('yes',ref,'choose'),/Choose a conversation/);
  await h.core.command('2',ref,'selected');await h.core.flush();assert.match(h.sent.at(-1).text,/Ada · WhatsApp\nElliot’s account/);
  await h.poll();assert.equal(h.sent.length,2,'accepted source events never reappear in another digest');
 }finally{h.close();}
}
{
 const h=fixture();try{
  for(const r of h.rules)r.presentation.mode='grouped';await h.poll();h.message(h.roomA);h.message(h.roomA);h.message(h.roomB);await h.poll();h.now+=60000;await h.poll();
  assert.equal(h.sent.length,2);assert.equal(h.core.ref(1,1).bundle.length,1);assert.match(h.sent[0].text,/2 messages in 1 conversation/);
 }finally{h.close();}
}
{
 const h=fixture();try{
  await h.poll();h.message(h.roomA);await h.core.monitor();h.now+=120000;h.message(h.roomB);await h.core.monitor();h.now+=120000;await h.core.flush();
  assert.equal(h.sent.length,1,'overdue collection windows collapse after delivery resumes');assert.match(h.sent[0].text,/2 messages in 2 conversations/);
 }finally{h.close();}
}
{
 const h=fixture();try{
  await h.poll();h.message(h.roomA);h.message(h.roomB);await h.poll();h.now+=60000;await h.poll();
  const callback=(id,user,data)=>({update_id:id,callback_query:{id:'callback-'+id,from:{id:user},message:{message_id:1,chat:{id:1,type:'private'}},data}});
  h.core.ingest([callback(10,2,'context:1')]);await h.core.process();await h.core.flush();assert.equal(h.sent.length,1,'unpaired callback cannot select a route');
  h.core.ingest([callback(11,1,'context:1')]);await h.core.process();await h.core.flush();assert.match(h.sent.at(-1).text,/Ada · WhatsApp\nElliot’s account/);
  h.revoked.add(h.roomB);h.core.ingest([callback(12,1,'context:1')]);await h.core.process();await h.core.flush();assert.match(h.sent.at(-1).text,/Could not complete/);assert(!h.sent.at(-1).text.includes('Synthetic'));
 }finally{h.close();}
}
{
 const h=fixture();try{
  await h.poll();for(let i=0;i<60;i++)h.message(h.roomA);await h.poll();h.now+=60000;await h.poll();
  assert.equal(h.sent.length,2,'large collections split into bounded groups');assert.equal(h.core.status().pending,0);
  assert.equal(h.core.ref(1,1).bundle[0].events.length,50);assert.equal(h.core.ref(1,2).bundle[0].events.length,10);
 }finally{h.close();}
}
{
 const h=fixture();try{
  h.rules[0].preview=true;h.rules.push({...h.rules[0],list_id:'immediate',list_name:'Immediate',presentation:null,preview:false});await h.poll();h.message(h.roomA);await h.poll();assert.equal(h.sent.length,1);assert.match(h.sent[0].text,/New incoming/);assert(!h.sent[0].text.includes('Shared list'));assert(!h.sent[0].text.includes('Synthetic'),'a later digest cannot grant preview to an immediate alert');
  h.now+=120000;await h.poll();assert.equal(h.sent.length,1);
 }finally{h.close();}
}
{
 const h=fixture();try{
  await h.poll();h.message(h.roomA);h.message(h.roomB);await h.poll();h.now+=60000;h.failure=Object.assign(Error('rate limit'),{status:429});await h.poll();assert.equal(h.core.status().pending,2);
  h.failure=null;h.revoked.add(h.roomB);h.now+=60000;await h.poll();assert.equal(h.sent.length,2);assert(!h.sent[1].text.includes('Elliot'));assert.equal(h.core.status().pending,0);
 }finally{h.close();}
}
{
 const h=fixture();try{
  await h.poll();h.message(h.roomA);h.message(h.roomB);await h.poll();h.now+=60000;h.offline=h.roomB;
  await h.core.flush();assert.equal(h.sent.length,1);assert(!h.sent[0].text.includes('Elliot'));assert.equal(h.core.status().pending,1,'temporary permission lookup failure preserves the other group item');
  h.offline=null;h.now+=60001;await h.poll();await h.poll();assert.equal(h.sent.length,2);assert(h.sent[1].text.includes('Elliot'));assert.equal(h.core.status().pending,0);
 }finally{h.close();}
}
{
 const dir=mkdtempSync(join(tmpdir(),'beepa-groups-'));const h=fixture(join(dir,'control.db'));try{
  await h.poll();h.message(h.roomA);h.message(h.roomB);await h.poll();h.now+=60000;h.failure=Error('lost acknowledgment');await h.poll();assert.equal(h.sent.length,1);assert.equal(h.core.db.prepare("SELECT count(*) n FROM outbox WHERE state='unknown'").get().n,2);
  h.close();h.core=new ControlCore(h.options);h.failure=null;await h.core.flush();assert.equal(h.sent.length,1,'uncertain group is never blindly resent after restart');
 }finally{h.close();rmSync(dir,{recursive:true,force:true});}
}
{
 const h=fixture();try{
  for(const r of h.rules)r.schedule={timezone:'UTC',mode:'quiet',days:[1,2,3,4,5,6,7],start_minute:0,end_minute:1440};
  for(let i=0;i<500;i++)h.core.enqueue('quiet-'+i,'Queued',{room:h.roomA,event:'$'+i,alert:true,rules:[{id:'shared',revision:1}]});
  await h.core.command('/help',null,'help');await h.core.flush();assert.match(h.sent[0].text,/Beepa is connected/);
 }finally{h.close();}
}
assert.equal(monitoringDeliveryPlan([{list_id:'a',presentation:{mode:'digest',interval_ms:60000}}],120000,120001,'!r:test').ready_at,180000);
console.log('Notification groups: digest choices, account routing, per-conversation groups, immediate overlap, retries, revocation, restart uncertainty and command priority pass');
