import assert from 'node:assert/strict';
import {ControlDomain,message} from '../../master/control-domain.mjs';
import {ControlCore,historyText} from '../../master/control-core.mjs';

const owner='@owner:test',room='!room:test',now=1700000000000;
const event=(i,content={msgtype:'m.text',body:'Exact '+i})=>({event_id:'$'+i,sender:owner,type:'m.room.message',origin_server_ts:now-100000+i,content});
function fixture(){
 const events=Array.from({length:32},(_,i)=>event(i)),domain=new ControlDomain({api:async()=>{},bootstrap:async()=>{},manager:'@manager:test',roster:async()=>({})});
 domain.clock=()=>now;domain.rooms.set(room,{id:room,native:'!native:test',owner,title:'Person',source:'imessage',consent:{level:'direct'},state:[]});
 domain.api=async(_,path)=>{const from=Number(new URL('http://test'+path).searchParams.get('from')||0);return {start:'0',chunk:events.slice(from),end:String(events.length)};};
 domain.health.set(owner,{updated_at:now/1000,last_ingestion:now/1000-2,last_delivery:now/1000-1,connected:true});
 return {events,domain};
}
{
 for(const type of ['m.image','m.video','m.audio','m.file','unknown.attachment']){
  const original=event(1,{msgtype:type,body:'Caption\nunchanged 😀',filename:'file.bin'});
  assert.equal(message(original,owner).text,'['+type+']\nCaption\nunchanged 😀');
  assert.equal(message({...original,content:{msgtype:type}},owner).text,'['+type+']');
  original.unsigned={'m.relations':{'m.replace':{sender:owner,content:{'m.new_content':{msgtype:type,body:'Edited caption'}}}}};
  assert.equal(message(original,owner).text,'['+type+']\nEdited caption');
 }
 assert.equal(message(event(1,{msgtype:'m.text',body:'Verbatim\ntext'}),owner).text,'Verbatim\ntext');
}
{
 const {domain,events}=fixture();const latest=await domain.history(room,15);
 assert.deepEqual(latest.messages.map(m=>m.id),Array.from({length:15},(_,i)=>'$'+(i+17)));
 assert.equal(latest.availability.available_count,32);assert.equal(latest.availability.fresh,true);
 assert.equal(latest.availability.last_ingestion_at,now-2000);assert.equal(latest.availability.native_history_complete,false,'mirror pagination cannot prove native completeness');
 events.push(event(32));const earlier=await domain.history(room,15,latest.cursor);
 assert.deepEqual(earlier.messages.map(m=>m.id),Array.from({length:15},(_,i)=>'$'+(i+2)));
 assert(!earlier.messages.some(m=>latest.messages.some(n=>n.id===m.id)));
 for(const change of [{history_incomplete:1},{history_pages_pending:1},{pending_events:1},{connected:false},{stage_errors:['source']},{updated_at:now-420001},{updated_at:now+60000}]){
  domain.health.set(owner,{updated_at:now,connected:true,...change});
  assert.equal((await domain.history(room)).availability.fresh,false,JSON.stringify(change));
 }
 domain.health.delete(owner);assert.equal((await domain.history(room)).availability.reported_at,null);
}
{
 const {domain,events}=fixture();domain.accounts.set(owner,{generation:'generation',journal_epoch:'journal'});
 const tracking={schema:1,state:'ready',owner_mxid:owner,generation:'generation',journal_epoch:'journal',bridge_epoch:'bridge',room_id:'!native:test',mirror_room_id:room,source:'imessage',updated_ts:now,coverage:'native_history',complete:true,total_messages:events.length,
  messages:events.map((e,i)=>({event_id:e.event_id,incoming:true,timestamp:e.origin_server_ts,order:String(i+1),answered_by:null}))};
 domain.rooms.get(room).state=[{type:'com.jkali.conversation_tracking',state_key:'',sender:owner,content:tracking}];
 assert.equal((await domain.history(room)).availability.native_history_complete,true);
 events.push(event(32));assert.equal((await domain.history(room)).availability.native_history_complete,false,'new mirror arrival invalidates completeness until native evidence catches up');
}
{
 const {domain}=fixture(),sent=[];
 domain.health.set(owner,{connected:false,updated_at:now-500000,last_ingestion:(now-500000)/1000});
 const result=await domain.history(room),text='Long exact body 😀\n'.repeat(450);
 result.messages=[{id:'$long',ts:now-1000,from_me:false,text}];
 const core=new ControlCore({path:':memory:',domain,telegram:async()=>{},model:async()=>{throw Error('Exact replay must not call model');}});
 try{
  core.set('operator',{user:1,chat:1});core.enqueueHistory('replay',result);
  for(const row of core.db.prepare('SELECT payload FROM outbox ORDER BY rowid').all())sent.push(JSON.parse(row.payload).text);
  assert(sent.length>1);assert(sent.every(t=>/^\[\d+\/\d+\]/.test(t)));
  const joined=sent.map(t=>t.replace(/^\[\d+\/\d+\]\n/,'')).join('');
  assert(joined.includes('Recent messages may still be syncing.'));assert(joined.includes('Person · iMessage'));
  assert(!joined.includes('Last source ingestion'));assert(!joined.includes('Available: 32 messages'));
  assert(joined.endsWith(text));assert(!joined.includes('$long'));assert(!joined.includes(owner));
  const reference=JSON.parse(core.db.prepare('SELECT reference FROM outbox LIMIT 1').get().reference);
  assert.equal(reference.room,room);assert.equal(reference.cursor,result.cursor);assert(reference.response_scope);
  assert.equal(core.db.prepare('SELECT count(*) AS n FROM drafts').get().n,0);
 }finally{core.db.close();}
}
console.log('Context replay: exact captions/media, concurrent-arrival pagination, honest availability and sync times, source completeness and lossless multipart rendering passed.');

{
 const conversation={id:'!hidden:fixture',owner:'@jkali:master',owner_name:'Jacob',title:'Billy Batista (Recall)',source:'gmessages'};
 const result={conversation,availability:{fresh:true},messages:[
  {id:'$hidden1',ts:Date.parse('2026-10-18T06:59:00Z'),from_me:true,text:'Exact outgoing text'},
  {id:'$hidden2',ts:Date.parse('2026-10-18T07:00:00Z'),from_me:false,text:'Exact incoming text'},
  {id:'$hidden3',ts:Date.parse('2026-10-18T18:39:58Z'),from_me:false,text:'Second incoming\nline two'},
 ]};
 const rendered=historyText(result,'America/Los_Angeles');
 assert(rendered.startsWith('Billy Batista (Recall) · Google Messages\nJacob’s account · Pacific Time'));
 assert.equal(rendered.split('Sun, Oct 18, 2026').length,2);assert.equal(rendered.split('Sat, Oct 17, 2026').length,2);
 assert(rendered.includes('11:59 PM · Jacob\nExact outgoing text'));assert(rendered.includes('12:00 AM · Billy Batista (Recall)'));
 assert(rendered.endsWith('11:39 AM · Billy Batista (Recall)\nSecond incoming\nline two'));
 assert(!/\$hidden|@jkali|!hidden|syncing|T18:39/.test(rendered));
 result.messages=[{id:'$dst',ts:Date.parse('2026-03-08T09:59:00Z'),text:'Before',from_me:true},{id:'$dst2',ts:Date.parse('2026-03-08T10:00:00Z'),text:'After',from_me:true}];
 const dst=historyText(result,'America/Los_Angeles');assert(dst.includes('1:59 AM'));assert(dst.includes('3:00 AM'));assert.equal(dst.split('Sun, Mar 8, 2026').length,2);
 assert(historyText({...result,messages:[]},'UTC').includes('No shared messages yet.'));
 console.log('Readable history: real names, hidden IDs, grouped local days, midnight, DST and exact text passed.');
}
