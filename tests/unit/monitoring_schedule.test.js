import assert from 'node:assert/strict';
import {monitoringAllows,monitoringReminderSlot,createCRMState,applyCRMOperation} from '../../shared/model/crm.js';
import {ControlCore} from '../../master/control-core.mjs';

const schedule=(changes={})=>({timezone:'America/Los_Angeles',mode:'quiet',days:[1,2,3,4,5,6,7],start_minute:60,end_minute:120,...changes});
const allows=(s,at)=>monitoringAllows(s,Date.parse(at));
assert.equal(allows(schedule(),'2026-11-01T08:30:00Z'),false,'first fall-back 01:30');
assert.equal(allows(schedule(),'2026-11-01T09:30:00Z'),false,'repeated fall-back 01:30');
assert.equal(allows(schedule(),'2026-11-01T10:00:00Z'),true,'window ends at local 02:00');
assert.equal(allows(schedule({start_minute:120,end_minute:180}),'2026-03-08T09:59:00Z'),true);
assert.equal(allows(schedule({start_minute:120,end_minute:180}),'2026-03-08T10:00:00Z'),true,'spring skipped hour is not invented');
assert.equal(allows(schedule({timezone:'UTC',days:[7],start_minute:1320,end_minute:480}),'2026-03-02T07:59:00Z'),false,'Sunday overnight continues Monday');
assert.equal(allows(schedule({timezone:'UTC',days:[7],start_minute:1320,end_minute:480}),'2026-03-02T08:00:00Z'),true);
assert.equal(allows(schedule({mode:'working',days:[1,2,3,4,5],start_minute:540,end_minute:1020}),'2026-03-09T16:00:00Z'),true);
assert.equal(monitoringAllows(null,Date.now()),true);
assert.equal(monitoringReminderSlot(100000,60000,310000),280000,'missed repeats collapse to latest slot');
assert.equal(monitoringReminderSlot(100000,null,310000),100000);
assert.equal(monitoringReminderSlot(100000,1,310000),null);

let state=createCRMState({workspace_id:'!crm:test',manager_mxid:'@manager:test'}),seq=0;
function op(type,id,base,payload){return {schema:1,workspace_id:'!crm:test',op_id:'op-'+(++seq),type,entity_id:id,base_revision:base,payload};}
function apply(operation,accepted=true){const result=applyCRMOperation(state,operation);assert.equal(result.outcome.accepted,accepted,JSON.stringify(result.outcome));state=result.state;return result;}
apply(op('list.create','list',0,{name:'Scheduled'}));
const policy={enabled:true,all_messages:false,channels:['telegram','browser'],waiting_them_ms:null,waiting_us_ms:null,preview:false,schedule:schedule(),repeat_ms:3600000,activity_enabled:false,reminders_enabled:true};
const saved=op('list.monitoring','list',1,policy);apply(saved);assert.deepEqual(state.lists.list.monitoring,policy);assert.equal(apply(saved).outcome.duplicate,true);
for(const invalid of [{repeat_ms:1},{activity_enabled:1},{presentation:{mode:'grouped',interval_ms:500}},{presentation:{mode:'digest',interval_ms:86400001}},{presentation:{mode:'immediate',interval_ms:60000}},{schedule:{...schedule(),days:[1,1]}},{schedule:{...schedule(),start_minute:1440}},{schedule:{...schedule(),end_minute:60}},{schedule:{...schedule(),timezone:'Invalid/Nowhere'}},{schedule:{...schedule(),timezone:'+01:00'}},{schedule:{...schedule(),native_send:true}}])apply(op('list.monitoring','list',2,{...policy,...invalid}),false);
apply(op('list.monitoring','list',1,policy),false);
apply(op('list.watch','list',2,{room_id:'!ada:test',owner_mxid:'@david:test',enabled:true,due_at:null,snooze_until:null,resolved_event:null,schedule:null,repeat_ms:null,waiting_us_ms:7200000,activity_enabled:true}));
assert.equal(state.lists.list.watches['!ada:test'].waiting_us_ms,7200000);

const owner='@david:test',room='!ada:test';
function fixture(){
 const h={now:Date.parse('2026-03-02T21:59:00Z'),events:[],sent:[],obligations:[],authorized:true};
 h.rules=[{list_id:'list',list_name:'Main',revision:1,owner,room,title:'Ada',source:'imessage',channels:['telegram'],all_messages:false,preview:false}];
 const domain={rooms:new Map([[room,{}]]),settings:()=>({retention_days:30}),refresh:async()=>{},hydrateAccounts:async()=>{},
  rules:()=>h.rules,history:async()=>({start:String(h.events.length),messages:[]}),liveRoom:async()=>{assert(h.authorized);},
  api:async(method,path)=>{const from=Number(new URL('https://fixture'+path).searchParams.get('from'));return {chunk:h.events.slice(from),end:String(h.events.length)};},
  updates:async id=>h.rules.filter(r=>r.list_id===id).map(r=>({id:room,list_id:r.list_id,owner,title:'Ada',fresh:true,obligations:h.obligations}))};
 h.core=new ControlCore({path:':memory:',domain,clock:()=>h.now,model:async()=>{throw Error('No model');},telegram:async(method,payload)=>{if(h.rateLimited)throw Object.assign(Error('limit'),{status:429});h.sent.push(payload);return {message_id:h.sent.length};}});h.core.set('operator',{user:1,chat:1});
 h.message=()=>h.events.push({event_id:'$event-'+h.events.length,type:'m.room.message',sender:owner,origin_server_ts:h.now,content:{msgtype:'m.text',body:'Preview should be controlled'}});
 h.poll=async()=>{await h.core.monitor();await h.core.flush();};h.close=()=>h.core.db.close();return h;
}
{
 const h=fixture();try{
  h.rules[0].schedule=schedule({timezone:'UTC',start_minute:1320,end_minute:480});await h.poll();
  h.now+=60000;h.message();await h.poll();assert.equal(h.sent.length,0);assert.equal(h.core.status().pending,1);
  h.now=Date.parse('2026-03-03T08:00:00Z');await h.poll();assert.equal(h.sent.length,1);await h.poll();assert.equal(h.sent.length,1);
 }finally{h.close();}
}
{
 const h=fixture();try{
  h.rules[0].activity_enabled=false;h.rules[0].repeat_ms=60000;await h.poll();h.message();
  h.obligations=[{event_id:'$incoming',waiting:'waiting_for_us',due_at:h.now}];await h.poll();assert.equal(h.sent.length,1);assert.match(h.sent[0].text,/Reminder/);
  await h.poll();assert.equal(h.sent.length,1);h.now+=60000;await h.poll();assert.equal(h.sent.length,2);
  h.now+=600000;await h.poll();assert.equal(h.sent.length,3,'no missed-repeat storm');
  h.rules[0].reminders_enabled=false;h.now+=60000;await h.poll();assert.equal(h.sent.length,3);
 }finally{h.close();}
}
{
 const h=fixture();try{
  h.rules[0].repeat_ms=60000;await h.poll();h.obligations=[{event_id:'$incoming',waiting:'waiting_for_us',due_at:h.now}];
  h.rateLimited=true;await h.poll();assert.equal(h.core.status().pending,1);
  h.now+=60000;h.obligations[0].waiting='answered';h.rateLimited=false;await h.poll();assert.equal(h.sent.length,0);assert.equal(h.core.status().pending,0);
 }finally{h.close();}
}
{
 const h=fixture();try{
  h.rules[0].repeat_ms=60000;h.rules.push({...h.rules[0],list_id:'slower',list_name:'Slower',repeat_ms:120000});await h.poll();
  h.obligations=[{event_id:'$incoming',waiting:'waiting_for_us',due_at:h.now-90000}];await h.poll();assert.equal(h.sent.length,1,'overlapping repeat rates combine after an offline interval');assert.match(h.sent[0].text,/Main, Slower/);
 }finally{h.close();}
}
{
 const h=fixture();try{
  h.rules[0].preview=true;h.rules[0].list_name='Quiet preview';h.rules[0].schedule=schedule({timezone:'UTC',start_minute:0,end_minute:1440});
  h.rules.push({...h.rules[0],list_id:'other',list_name:'Open activity',schedule:null,preview:false});await h.poll();h.message();await h.poll();
  assert.equal(h.sent.length,1);assert(!h.sent[0].text.includes('Preview should'));assert(!h.sent[0].text.includes('Quiet preview'));assert(h.sent[0].text.includes('Open activity'));
 }finally{h.close();}
}
{
 const h=fixture();try{
  h.rules[0].preview=true;h.core.domain.history=async()=>({start:String(h.events.length),messages:h.events.map(e=>({id:e.event_id,text:e.content.body}))});
  await h.poll();h.rateLimited=true;h.message();await h.poll();h.events[0].content.body='Edited current preview';h.now+=60000;h.rateLimited=false;await h.poll();
  assert.equal(h.sent.length,1);assert.match(h.sent[0].text,/Edited current preview/);assert(!h.sent[0].text.includes('Preview should'));
  const refs=JSON.stringify(h.core.db.prepare('SELECT reference FROM refs').all());assert(!refs.includes('Edited current preview'));assert(!refs.includes('Preview should'));
 }finally{h.close();}
}
{
 const h=fixture();try{
  h.now=Date.parse('2026-03-02T22:00:00Z');h.rules[0].schedule=schedule({timezone:'UTC',start_minute:0,end_minute:1440});h.rules.push({...h.rules[0],list_id:'other',list_name:'Surviving',schedule:schedule({timezone:'UTC',start_minute:1320,end_minute:480})});
  await h.poll();h.message();await h.poll();assert.equal(h.sent.length,0);
  h.rules.shift();h.now=Date.parse('2026-03-03T08:00:00Z');await h.poll();assert.equal(h.sent.length,1,'remaining list still authorizes the source event');assert.match(h.sent[0].text,/Surviving/);
 }finally{h.close();}
}
console.log('Monitoring schedules: DST, overnight windows, repeat slots, quiet deferral, activity/reminder separation and independent overlap policies passed');
