import assert from 'node:assert/strict';
import {ControlDomain,defaultMonitoring} from '../../master/control-domain.mjs';
import {ControlCore} from '../../master/control-core.mjs';
import {createCRMState,applyCRMOperation} from '../../shared/model/crm.js';

const owner='@owner:test',room='!room:test',native='!native:test',manager='@manager:test';
let now=1700000000000;
function fixture(){
  let state=createCRMState({workspace_id:'!crm:test',manager_mxid:manager}),n=0;
  const domain=new ControlDomain({api:async()=>{},bootstrap:async()=>{},manager,roster:async()=>({})});
  const apply=(type,payload)=>{const result=applyCRMOperation(state,{schema:1,op_id:'op'+ ++n,workspace_id:state.workspace_id,type,entity_id:'list',base_revision:state.lists.list?.revision||0,payload});assert(result.outcome.accepted,result.outcome.code);state=result.state;};
  apply('list.create',{name:'Watched',purpose:'monitoring'});
  apply('list.watch',{room_id:room,owner_mxid:owner,enabled:true,due_at:null,snooze_until:null,resolved_event:null});
  apply('list.monitoring',{...defaultMonitoring(),enabled:true});
  domain.state=()=>state;domain.clock=()=>now;domain.refresh=async()=>{};domain.hydrateAccounts=async()=>{};
  domain.accounts.set(owner,{generation:'gen',journal_epoch:'epoch'});
  domain.health.set(owner,{updated_at:now,connected:true});
  const payload={schema:1,state:'ready',owner_mxid:owner,generation:'gen',journal_epoch:'epoch',bridge_epoch:'native-epoch',
    room_id:native,mirror_room_id:room,source:'imessage',updated_ts:now,coverage:'native_history',complete:true,
    messages:[{event_id:'$a',incoming:false,order:'1',timestamp:100000,answered_by:null},
      {event_id:'$b',incoming:false,order:'2',timestamp:110000,answered_by:'$reply'},
      {event_id:'$reply',incoming:true,order:'3',timestamp:120000,answered_by:null}]};
  const stateEvent={type:'com.jkali.conversation_tracking',sender:owner,state_key:'',content:payload};
  const r={id:room,owner,native,source:'imessage',title:'Person',level:'direct',state:[stateEvent,
    {type:'m.room.create',state_key:'',sender:owner,content:{'com.jkali.mirror_of':native}},
    {type:'com.jkali.share_level',state_key:'',sender:owner,content:{level:'direct'}}]};domain.rooms.set(room,r);
  const events=payload.messages.map(m=>({event_id:m.event_id,type:'m.room.message',sender:owner,origin_server_ts:m.timestamp,content:{msgtype:'m.text',body:m.event_id,'com.jkali.from_me':!m.incoming}}));
  domain.api=async(_,path)=>path.endsWith('/state')?r.state:({start:'end',chunk:new URL('http://test'+path).searchParams.has('from')?[]:events,end:'end'});
  return {domain,apply,payload,stateEvent,events,r};
}
{
 const {domain,apply}=fixture(),writes=[];domain.accounts.set(owner,{generation:'gen',journal_epoch:'epoch',proposal_room_id:'!proposals:test',conversation_tracking:true});
 domain.api=async(...args)=>{writes.push(args);return {event_id:'$scope'};};
 await domain.publishMonitoringScopes();await domain.publishMonitoringScopes();assert.equal(writes.length,1);assert(writes[0][1].includes('/send/com.jkali.monitoring_scope/'));
 assert.deepEqual(writes[0][2].rooms,[native]);assert(!('waiting_us_ms' in writes[0][2]),'source hint has no independent monitoring policy');
 apply('list.watch',{room_id:room,owner_mxid:owner,enabled:false,due_at:null,snooze_until:null,resolved_event:null});
 await domain.publishMonitoringScopes();assert.deepEqual(writes[1][2].rooms,[]);
}
{
 const {domain,apply,payload,r}=fixture();payload.total_messages=3;r.unread=7;
 let [u]=await domain.updates();assert.deepEqual(u.master_unseen,{count:1,complete:true});assert.equal(u.source_unread.count,7);
 const op=await domain.prepareSeen({list_id:'list',room_id:room});apply(op.type,op.payload);
 [u]=await domain.updates();assert.equal(u.master_unseen.count,0);assert.equal(u.source_unread.count,7);assert.equal(u.obligations.at(-1).waiting,'waiting_for_us');
 payload.bridge_epoch='another';assert.equal((await domain.updates())[0].master_unseen.count,1);
 assert.equal((await domain.updates('list',{owner:'@different:test'})).length,0);
 assert.equal((await domain.updates('list',{source:'whatsapp'})).length,0);
 assert.equal((await domain.updates('list',{since:99999999})).length,0);
 await assert.rejects(domain.updates('list',{since:'yesterday'}),/Invalid update time/);
}
{
 const {domain,r}=fixture();const original=domain.state();
 domain.state=()=>({...original,persons:{person:{person_id:'person',label:'Shared person'}},bindings:{binding:{binding_id:'binding',person_id:'person',owner_mxid:owner,generation:'gen',availability:'shared',contact:{source:'imessage',network_id:'address'}}}});
 domain.contacts=[{owner_mxid:owner,source:'imessage',network_id:'address',person_id:'profile'}, {owner_mxid:'@other:test',source:'imessage',network_id:'address',person_id:'profile'}];r.profile='profile';
 const context=await domain.personContext('person',owner);assert.equal(context.bindings.length,1);assert.equal(context.conversations.length,1);
 await assert.rejects(domain.personContext('person','@other:test'),/No currently shared/);
 domain.contacts=[];await assert.rejects(domain.personContext('person',owner),/No currently shared/);
}
{
 const {domain,r}=fixture();r.state.push({type:'com.jkali.message_receipt',sender:owner,state_key:'$b',content:{read:true}});
 const [u]=await domain.updates();assert.equal(u.waiting,'waiting_both_directions');assert.equal(u.fresh,true);assert.equal(u.due_at,null);
 assert.deepEqual(u.obligations.map(o=>[o.event_id,o.waiting,o.delivery]),[['$a','waiting_for_them','unknown'],['$b','answered','read'],['$reply','waiting_for_us',null]]);
 assert.equal(u.obligations[0].delivery,'unknown','a reply does not invent delivery/read receipts');
}
{
 const {domain,apply,payload}=fixture();apply('list.monitoring',{...defaultMonitoring(),enabled:true,waiting_them_ms:60000,waiting_us_ms:120000});
 let [u]=await domain.updates();assert.equal(u.obligations[0].due_at,160000);assert.equal(u.obligations[2].due_at,240000);
 const op=await domain.prepareObligation({list_id:'list',room_id:room,event_id:'$a',resolved:true,due_at:null});apply(op.type,op.payload);
 [u]=await domain.updates();assert.equal(u.waiting,'waiting_for_us');assert.equal(u.obligations[0].waiting,'resolved');assert.equal(u.obligations[0].due_at,null);
 assert.equal(payload.messages[0].answered_by,null,'manual resolution never fabricates a native response');
 await assert.rejects(domain.prepareObligation({list_id:'list',room_id:room,event_id:'$foreign'}),/unavailable/);
}
{
 for(const mutation of [f=>f.stateEvent.sender='@forger:test',f=>f.payload.generation='old',f=>f.payload.journal_epoch='old',
   f=>f.payload.room_id='!other:test',f=>f.payload.source='whatsapp',f=>f.payload.messages[1].order='1',
   f=>f.payload.messages[0].answered_by='$b',f=>f.payload.messages[1].answered_by='$missing',f=>f.payload.messages[0].answered_by='$reply']){
   const f=fixture();mutation(f);const [u]=await f.domain.updates();assert.equal(u.waiting,'unknown');assert.equal(u.fresh,false);
 }
 const f=fixture();f.payload.updated_ts=now-420001;assert.equal((await f.domain.updates())[0].fresh,false);
 const g=fixture();g.events.push({event_id:'$new',sender:owner,type:'m.room.message',origin_server_ts:now,content:{msgtype:'m.text',body:'New answer'}});
 assert.equal((await g.domain.updates())[0].fresh,false,'caught-up mirror with stale native projection suppresses reminders');
 const h=fixture();h.r.state.push({type:'com.jkali.message_receipt',sender:'@forger:test',state_key:'$a',content:{read:true}});
 assert.equal((await h.domain.updates())[0].obligations[0].delivery,'unknown');
}
{
 const {domain,payload,events}=fixture();payload.messages=[];events.length=0;
 assert.equal((await domain.updates())[0].waiting,'uncontacted');
 payload.coverage='observed_history';payload.complete=false;assert.equal((await domain.updates())[0].waiting,'unknown');
}
{
 const {domain,apply,payload}=fixture(),sent=[];
 const core=new ControlCore({path:':memory:',domain,clock:()=>now,telegram:async(_,p)=>{sent.push(p);return {message_id:sent.length};},model:async()=>{throw Error('No model needed');}});
 core.set('operator',{user:1,chat:1});
 apply('list.obligation',{room_id:room,owner_mxid:owner,event_id:'$a',due_at:now-1,snooze_until:null,resolved:false});
 apply('list.obligation',{room_id:room,owner_mxid:owner,event_id:'$reply',due_at:now-1,snooze_until:now+60000,resolved:false});
 await core.monitor();assert.equal(sent.length,0,'startup catches up before reminding');await core.monitor();await core.flush();assert.equal(sent.length,1);assert(!sent[0].text.includes('$a'));assert.equal(core.ref(1,1).event,'$a');
 await core.monitor();await core.flush();assert.equal(sent.length,1,'message deadline idempotent');
 now+=60001;payload.updated_ts=now;domain.health.set(owner,{updated_at:now,connected:true});await core.monitor();await core.flush();assert.equal(sent.length,2);assert(!sent[1].text.includes('$reply'));assert.equal(core.ref(1,2).event,'$reply');
 const ref=core.ref(1,2);assert(await core.alertAllowed(ref));
 apply('list.obligation',{room_id:room,owner_mxid:owner,event_id:'$reply',due_at:now-60002,snooze_until:null,resolved:true});assert.equal(await core.alertAllowed(ref),false);
 core.db.close();
}
console.log('Message obligations: both directions, exact replies/receipts, canonical deadlines, snooze, resolution, stale/gapped evidence and reminder replay passed.');
