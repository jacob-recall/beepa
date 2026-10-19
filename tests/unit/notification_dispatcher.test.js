import assert from 'node:assert/strict';
import {ControlCore} from '../../master/control-core.mjs';
import {notificationIdentity} from '../../master/notification-routing.mjs';
import {telegramNotificationKey} from '../../master/notification-routing-adapters.mjs';

function fixture(){
 const h={now:1700000000000,events:[],sent:[],browserJobs:new Map(),handoffCalls:0};
 h.rules=[{room:'!room:test',owner:'@owner:test',title:'Contact',source:'imessage',list_id:'list',list_name:'Watch',revision:1,channels:['browser','telegram'],all_messages:false,routing:null}];
 const domain={settings:()=>({retention_days:30}),rooms:new Map(h.rules.map(r=>[r.room,r])),rules:()=>h.rules,refresh:async()=>{},hydrateAccounts:async()=>{},
  liveRoom:async()=>h.rules[0],updates:async()=>h.updates||[],history:async()=>({start:String(h.events.length),messages:[]}),
  api:async(_,path)=>{const url=new URL('https://fixture'+path);if(path.includes('/event/'))return h.events.find(e=>e.event_id===decodeURIComponent(path.split('/').at(-1)));
   return {chunk:h.events.slice(Number(url.searchParams.get('from'))),end:String(h.events.length)};}};
 const browser=async data=>{
  if(data.action==='control_dispatcher'){h.handoffCalls++;if(h.failHandoff)throw Error('response lost');return {id:'a'.repeat(32),version:1,owner:'control',follows:h.imports||[]};}
  if(data.action==='control_enqueue'){
   const e=data.entries[0],s={kind:e.kind,room:e.room,event:e.event,...(e.kind==='message'?{outgoing:e.refs.outgoing}:{due:e.refs.due,slot:e.refs.slot})},id=notificationIdentity(s);
   if(!h.browserJobs.has(id))h.browserJobs.set(id,{id,state:h.browserResult||'accepted',entry:e});return {devices:1,outcomes:[h.browserJobs.get(id)]};
  }
  assert.equal(data.action,'control_outcomes');return {devices:1,outcomes:data.ids.map(id=>h.browserJobs.get(id)||{id,state:'missing'})};
 };
 h.core=new ControlCore({path:':memory:',domain,browser,clock:()=>h.now,model:async()=>{throw Error('No model');},telegram:async(_,data)=>{h.sent.push(data);if(h.telegramError)throw h.telegramError;return {message_id:h.sent.length};}});h.core.set('operator',{user:1,chat:1});
 h.message=()=>{const id='$'+h.events.length;h.events.push({event_id:id,sender:'@owner:test',type:'m.room.message',origin_server_ts:h.now,content:{msgtype:'m.text',body:'Synthetic event'}});return {kind:'message',room:'!room:test',event:id,outgoing:false};};
 h.poll=async()=>{await h.core.monitor();await h.core.flush();};h.close=()=>h.core.db.close();return h;
}
{
 const h=fixture();try{
  h.message();await h.poll();assert(h.core.status().routing_ready);assert.equal(h.sent.length,0);assert.equal(h.browserJobs.size,0,'skip pre-watch history');
  h.message();await h.poll();await h.poll();assert.equal(h.sent.length,1);assert.equal(h.browserJobs.size,1);assert.equal(h.handoffCalls,1);
 }finally{h.close();}
}
// Resume the browser's lagging cursor even when Telegram already advanced and
// marked the event observed. Reuse the existing Telegram delivery, once.
{
 const h=fixture();try{
  const source=h.message();h.imports=[{room:source.room,owner:'@owner:test',cursor:'0',started:h.now}];
  h.core.db.prepare('INSERT INTO cursors VALUES(?,?,?)').run(source.room,'1',h.now);
  h.core.enqueue(telegramNotificationKey(source),'Previously accepted');await h.core.flush();await h.poll();await h.poll();
  assert.equal(h.sent.length,1);assert.equal(h.browserJobs.size,1);assert.equal(h.core.db.prepare('SELECT count(*) n FROM notification_import_cursors').get().n,0);
 }finally{h.close();}
}
// A lost handoff response cannot start competing discovery. Retry the same
// browser handoff, then preserve the frozen position and catch up normally.
{
 const h=fixture();try{
  h.failHandoff=true;await assert.rejects(h.poll(),/response lost/);assert(!h.core.status().routing_ready);h.message();h.imports=[{room:'!room:test',cursor:'0',started:h.now}];
  h.failHandoff=false;await h.poll();assert.equal(h.sent.length,1);assert.equal(h.browserJobs.size,1);
 }finally{h.close();}
}
{
 const h=fixture();try{
  h.rules[0].routing={primary:'telegram',fallback:true};h.telegramError=Object.assign(Error('blocked'),{status:403});await h.poll();h.message();await h.poll();assert.equal(h.browserJobs.size,0);
  await h.poll();assert.equal(h.browserJobs.size,1);assert.equal(h.sent.length,1);
 }finally{h.close();}
}
{
 const h=fixture();try{
  h.rules[0].routing={primary:'browser',fallback:true};h.browserResult='unknown';await h.poll();const source=h.message();await h.poll();await h.poll();assert.equal(h.sent.length,0);
  h.browserJobs.get(notificationIdentity(source)).state='failed';await h.poll();assert.equal(h.sent.length,1);
 }finally{h.close();}
}
// Reminder discovery is channel independent, retains per-policy slots, and
// cancels before dispatch once an incoming obligation is resolved.
{
 const h=fixture();try{
  h.rules[0].routing={primary:'browser',fallback:true};await h.poll();const s=h.message();await h.poll();
  h.updates=[{id:s.room,list_id:'list',owner:'@owner:test',title:'Contact',fresh:true,obligations:[{event_id:s.event,due_at:h.now-1,waiting:'waiting_for_us'}]}];
  await h.poll();assert.equal([...h.browserJobs.values()].filter(j=>j.entry.kind==='reminder').length,1);assert.equal(h.sent.length,0);
  h.rules[0].repeat_ms=60000;h.now+=60000;h.updates[0].obligations[0].waiting='answered';await h.poll();assert.equal(h.browserJobs.size,2);
 }finally{h.close();}
}
console.log('notification dispatcher: actual monitor, cursor handoff, old delivery dedup, retryable handoff, both directions of fallback, and reminders passed');
{
 const h=fixture();try{
  h.now=Date.parse('2026-10-19T10:30:00Z');h.rules[0].channels=['telegram'];
  h.rules[0].schedule={timezone:'UTC',mode:'working',days:[1,2,3,4,5,6,7],start_minute:660,end_minute:720};
  h.rules[0].presentation={mode:'digest',interval_ms:1800000};
  await h.poll();h.message();await h.poll();assert.equal(h.sent.length,0);assert.equal(h.core.status().pending,1,'outbox retains original collection time during quiet hours');
  h.now+=1800000;await h.poll();assert.equal(h.sent.length,1,'completed digest sends when delivery window opens without another collection delay');
 }finally{h.close();}
}
