import assert from 'node:assert/strict';
import {mkdtempSync,rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {ControlCore,TOOLS,chunks} from '../../master/control-core.mjs';
import {ControlDomain,message,defaultMonitoring} from '../../master/control-domain.mjs';
import {createCRMState,applyCRMOperation} from '../../shared/model/crm.js';

const OWNER='@alice:fixture',ROOM='!chat:fixture';
let now=1000000;
const evt=(id,own=false)=>({event_id:'$'+id,type:'m.room.message',sender:OWNER,origin_server_ts:now+1,content:{msgtype:'m.text',body:'Exact text '+id,'com.jkali.from_me':own}});
function fixture(path=':memory:') {
  const sent=[],writes=[];
  const domain={rooms:new Map([[ROOM,{id:ROOM}]]),refresh:async()=>{},hydrateAccounts:async()=>{},rules:()=>[],lists:()=>[],updates:async()=>[],
    prepareSend:async({body,room_id})=>({id:'draft-1',kind:'send',created:now,room_id,owner:OWNER,body,conversation:{owner:OWNER,title:'Alice',source:'fixture'},content:{'com.jkali.send_mode':'direct'}}),
    validateSend:async()=>{},executeSend:async d=>{writes.push(d);return {proposal_id:'$proposal'};},history:async()=>({conversation:{id:ROOM,owner:OWNER,title:'Alice',source:'fixture'},messages:[],start:'0',cursor:null}),
    api:async()=>({chunk:[],end:'0'})};
  const core=new ControlCore({path,domain,clock:()=>now,telegram:async(method,p)=>{sent.push({method,...p});return {message_id:sent.length};},model:async()=>({content:'Hello'})});
  core.set('operator',{user:1,chat:1});
  return {core,domain,sent,writes};
}
function incoming(id,text,user=1,extra={}){return {update_id:id,message:{message_id:id,chat:{id:user,type:'private'},from:{id:user},text,...extra}};}

// Pairing is single-use, private and time limited; arbitrary people get no data.
{
 const {core,sent}=fixture();core.disconnect();const url=core.pairLink('test_bot').url,code=new URL(url).searchParams.get('start');
 core.ingest([incoming(1,'/start wrong'),incoming(2,'/start '+code,1,{chat:{id:1,type:'group'}})]);await core.process();assert.equal(core.status().paired,false);
 core.ingest([incoming(3,'/start '+code)]);await core.process();await core.flush();assert.equal(core.status().paired,true);assert.equal(sent.length,1);
 core.ingest([incoming(4,'/lists',2),incoming(3,'/start '+code)]);await core.process();await core.flush();assert.equal(sent.length,1);
 core.disconnect();const expired=new URL(core.pairLink('test_bot').url).searchParams.get('start');now+=900001;core.ingest([incoming(5,'/start '+expired)]);await core.process();assert.equal(core.status().paired,false);core.db.close();
}
// The model can prepare, but has no confirmation tool. A duplicate confirmation
// cannot submit twice; context revocation prevents execution.
{
 const {core,domain,writes}=fixture();assert(!TOOLS.some(t=>t.function.name==='confirm'));
 await core.tool('prepare_send',{room_id:ROOM,body:'Hello'});assert.equal(writes.length,0);
 await Promise.all([core.confirm('draft-1'),core.confirm('draft-1')]);assert.equal(writes.length,1);
 assert.equal((await core.confirm('draft-1')).state,'submitted');
 core.db.exec('DELETE FROM drafts');await core.tool('prepare_send',{room_id:ROOM,body:'Second'});
 domain.validateSend=async()=>{throw Error('permissions changed');};await assert.rejects(core.confirm('draft-1'),/permissions changed/);assert.equal(writes.length,1);core.db.close();
}
// Ambiguous delivery/submission never retries automatically after restart.
{
 const dir=mkdtempSync(join(tmpdir(),'beepa-control-test-')),path=join(dir,'state.db');
 try{const {core}=fixture(path);await core.tool('prepare_send',{room_id:ROOM,body:'Hello'});core.enqueue('test','Hello');core.set('api:interrupted',{state:'processing'});core.db.exec("UPDATE drafts SET state='executing'; UPDATE outbox SET state='sending'");core.db.close();
 const next=fixture(path);assert.equal(next.core.value('api:interrupted').state,'unknown');assert.equal((await next.core.confirm('draft-1')).state,'unknown');await next.core.flush();assert.equal(next.sent.length,0);assert.equal(next.writes.length,0);next.core.db.close();}finally{rmSync(dir,{recursive:true,force:true});}
}
// Durable outgoing messages deduplicate, and stale/revoked alert policies cancel.
{
 const {core,domain,sent}=fixture();domain.rules=()=>[{room:ROOM,list_id:'list',revision:2,channels:['telegram']}];
 core.enqueue('old','Private preview',{room:ROOM,alert:true,rules:[{id:'list',revision:1}]});
 core.enqueue('good','Notification',{room:ROOM,alert:true,rules:[{id:'list',revision:2}]});core.enqueue('good','Duplicate');await core.flush();await core.flush();assert.equal(sent.length,1);assert.equal(sent[0].text,'Notification');
 core.enqueue('revoke','No longer shared',{room:ROOM});domain.rooms.clear();await core.flush();assert.equal(sent.length,1);core.db.close();
}
// Monitoring starts at now, ignores backlog/edits/foreign events and detects
// exact source-owned IDs. Overlapping lists produce one alert.
{
 const {core,domain,sent}=fixture();let end='0',events=[];
 domain.rules=()=>['a','b'].map(list_id=>({room:ROOM,owner:OWNER,title:'Alice',source:'test',list_id,list_name:list_id,revision:1,channels:['telegram'],all_messages:false,preview:false}));
 domain.api=async(_,path)=>{const from=new URL('http://fixture'+path).searchParams.get('from');return {chunk:from===end?[]:events,end};};
 await core.monitor();end='1';events=[evt('incoming'),evt('outgoing',true),{...evt('foreign'),sender:'@intruder:test'},{...evt('edit'),content:{...evt('edit').content,'m.relates_to':{rel_type:'m.replace'}}}];
 await core.monitor();await core.flush();await core.monitor();await core.flush();assert.equal(sent.length,1);assert(sent[0].text.includes('a, b'));assert(!sent[0].text.includes('Exact text'));core.db.close();
}
// Exact history pagination and provenance; edited originals retain identity.
{
 assert.equal(message({...evt('foreign'),sender:'@x:test'},OWNER),null);
 assert.equal(message({...evt('redacted'),unsigned:{redacted_because:{}}},OWNER),null);
 const edited=evt('original');edited.unsigned={'m.relations':{'m.replace':{sender:OWNER,content:{'m.new_content':{msgtype:'m.text',body:'Revised'}}}}};assert.equal(message(edited,OWNER).text,'Revised');
 const domain=new ControlDomain({api:async()=>{},bootstrap:async()=>{},manager:'@manager:fixture',roster:async()=>({})});domain.rooms.set(ROOM,{id:ROOM,owner:OWNER,title:'Alice',source:'test',state:[]});
 const events=Array.from({length:32},(_,i)=>({...evt(i+1),origin_server_ts:now+i+1})).reverse();
 domain.api=async(_,path)=>{const u=new URL('http://fixture'+path),from=Number(u.searchParams.get('from')||0);return {start:'0',chunk:from===0?events:[],end:String(events.length)};};
 const a=await domain.history(ROOM),b=await domain.history(ROOM,15,a.cursor);assert.equal(a.messages.length,15);assert.equal(a.messages.at(-1).id,'$32');assert.equal(b.messages.at(-1).id,'$17');assert(!a.messages.some(x=>b.messages.some(y=>x.id===y.id)));
 domain.api=async(_,path)=>{const from=new URL('http://fixture'+path).searchParams.get('from');return {start:'32',chunk:from==='32'?[{...evt('late-old'),origin_server_ts:now+1000,content:{...evt('late-old').content,'com.jkali.origin_ts':now-5000}}]:[],end:'33'};};
 const backfilled=await domain.history(ROOM);assert.equal(backfilled.messages.at(-1).id,'$32','backfilled old messages do not become the latest reply');
 const edit={...evt('edit-32'),content:{'m.relates_to':{rel_type:'m.replace',event_id:'$32'},'m.new_content':{msgtype:'m.text',body:'Edited 32'}}};
 domain.api=async(_,path)=>({start:'33',chunk:new URL('http://fixture'+path).searchParams.get('from')==='33'?[edit]:[],end:'34'});
 assert.equal((await domain.history(ROOM)).messages.at(-1).text,'Edited 32');
 domain.api=async(_,path)=>({start:'34',chunk:new URL('http://fixture'+path).searchParams.get('from')==='34'?[{...evt('redaction'),type:'m.room.redaction',content:{redacts:'$edit-32'}}]:[],end:'35'});
 assert.equal((await domain.history(ROOM)).messages.at(-1).text,'Exact text 32','redacting an edit restores the original');


}
// Canonical policy validation and stale revisions; monitoring never grants Direct.
{
 let state=createCRMState({workspace_id:'!crm:fixture',manager_mxid:'@manager:fixture'}),i=0;
 const apply=(type,payload,base)=>{const r=applyCRMOperation(state,{schema:1,workspace_id:state.workspace_id,op_id:'op'+ ++i,type,entity_id:'list',base_revision:base,payload});state=r.state;return r.outcome;};
 assert(apply('list.create',{name:'Watching',purpose:'monitoring'},0).accepted);
 assert(apply('list.monitoring',{...defaultMonitoring(),enabled:true},1).accepted);
 assert.equal(apply('list.monitoring',{...defaultMonitoring(),waiting_them_ms:1},2).accepted,false);
 assert.equal(apply('list.monitoring',defaultMonitoring(),1).code,'revision-conflict');
 assert(apply('list.watch',{room_id:ROOM,owner_mxid:OWNER,enabled:true,due_at:null,snooze_until:null,resolved_event:null},2).accepted);
 assert.equal(Object.keys(state.bindings).length,0);assert.equal(Object.keys(state.memberships).length,0);
}
assert.equal(chunks('😀'.repeat(5000)).join(''),'😀'.repeat(5000));
console.log('Telegram: pairing, operator isolation, reviewed sends, concurrent confirmation, restart, dedup, revocation, canonical policy and exact history passed');
