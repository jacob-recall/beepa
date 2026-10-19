import assert from 'node:assert/strict';
import {mkdtempSync,rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {ControlCore,redactModelText,redactModelValue} from '../../master/control-core.mjs';
import {ControlDomain} from '../../master/control-domain.mjs';

const owner='@owner:test',room='!room:test';
const call=(name,args)=>({role:'assistant',tool_calls:[{id:'call',type:'function',function:{name,arguments:JSON.stringify(args)}}]});
function fixture(path=':memory:'){
 const f={now:1700000000000,version:'v1',shared:true,offline:false,steps:[],prompts:[],sent:[],text:'Confidential source text'};
 const state={lists:{},bindings:{},memberships:{},control:{revision:0,settings:{allow_model_context:true}}};
 const conversation={id:room,owner,native:'!native:test',title:'Fixture',source:'imessage',consent:{level:'direct'}};
 const domain={rooms:new Map([[room,conversation]]),accounts:new Map(),contacts:[{owner_mxid:owner,network_id:'shared-contact',display_name:'Fixture'}],state:()=>state,settings:()=>state.control.settings,contextStamp:ControlDomain.prototype.contextStamp,
  refresh:async()=>{if(f.offline)throw Error('Temporary source outage');if(!f.shared)domain.rooms.delete(room);else domain.rooms.set(room,conversation);},hydrateAccounts:async()=>{},
  summary:r=>({...r}),liveRoom:async id=>{assert(f.shared);return domain.rooms.get(id);},
  history:async()=>({conversation,version:f.version,messages:[{id:'$message',ts:f.now,from_me:false,text:f.text}],cursor:null}),
  updates:async()=>[{id:room,context_version:f.version,latest_messages:[{id:'$message',text:f.text}]}]};
 const core=new ControlCore({path,domain,clock:()=>f.now,telegram:async(_,p)=>{f.sent.push(p);f.onSend?.();return {message_id:f.sent.length};},model:async messages=>{f.prompts.push(structuredClone(messages));assert(f.steps.length);return f.steps.shift()(messages);}});
 core.set('operator',{user:7,chat:7});return Object.assign(f,{state,domain,core});
}
async function summarize(f,key='update:summary',tool='history'){
 f.steps.push(()=>call(tool,tool==='history'?{room_id:room,display:false}:tool==='search_contacts'?{query:'Fixture'}:{}),()=>({content:f.text}));
 await f.core.command('Summarize the conversation',null,key);
}

// Unquoted tool-derived answers carry durable, ID/hash-only context evidence.
for(const change of ['revoked','account-settings','model-disabled','message-edit','updates-edit','contact-revoked']){
 const f=fixture();try{
  await summarize(f,'update:summary',change==='updates-edit'?'updates':change==='contact-revoked'?'search_contacts':'history');
  const queued=f.core.db.prepare('SELECT reference FROM outbox').get().reference;
  assert(!queued.includes(f.text));assert(queued.includes('response_scope'));if(change!=='contact-revoked')assert(queued.includes(room));
  if(change==='revoked')f.shared=false;
  if(change==='contact-revoked')f.domain.contacts=[];
  if(change==='account-settings')f.state.control.revision++;
  if(change==='model-disabled')f.state.control.settings.allow_model_context=false;
  if(change.endsWith('edit'))f.version='v2';
  await f.core.flush();assert.equal(f.sent.length,0,change);
  const row=f.core.db.prepare('SELECT state,payload FROM outbox WHERE id=?').get('update:summary:0');
  assert.equal(row.state,'canceled');assert(!row.payload.includes(f.text));assert.equal(f.core.dialogues.size,0);
  await f.core.flush();assert.equal(f.sent.length,1);assert.match(f.sent[0].text,/Shared context changed/);
  f.shared=true;f.version='v1';f.state.control.revision=0;f.state.control.settings.allow_model_context=true;
  await f.core.flush();assert.equal(f.sent.length,1,'restoring access never revives an old response');
 }finally{f.core.db.close();}
}

// A fresh source check is required between multipart deliveries; accepted
// evidence remains, but cached text/references and remaining parts are erased.
{
 const f=fixture();try{
  f.text='Confidential paragraph. '.repeat(400);await summarize(f);
  f.onSend=()=>{f.shared=false;};await f.core.flush();assert.equal(f.sent.length,1);
  const rows=f.core.db.prepare("SELECT state,payload,reference FROM outbox WHERE id LIKE 'update:summary:%'").all();
  assert.equal(rows[0].state,'accepted');assert(rows.slice(1).every(r=>r.state==='canceled'));
  assert(rows.every(r=>!r.payload.includes('Confidential paragraph')&&r.reference==='{}'));
  assert.deepEqual(f.core.ref(7,1),{unknown_quote:true});
 }finally{f.core.db.close();}
}

// Transient source outages defer without claiming delivery or discarding a
// valid answer. Restart preserves its scope and rechecks it before delivery.
{
 const dir=mkdtempSync(join(tmpdir(),'beepa-response-scope-')),path=join(dir,'state.db');let f=fixture(path);
 try{
  await summarize(f);f.offline=true;await f.core.flush();assert.equal(f.core.status().pending,1);assert.equal(f.sent.length,0);
  f.core.db.close();f=fixture(path);f.now+=60001;await f.core.flush();assert.equal(f.sent.length,1);assert(f.sent[0].text.includes(f.text));assert(!f.sent[0].text.startsWith('AI-generated answer'));assert(!f.sent[0].text.includes(room));assert(f.sent[0].text.includes('Based on shared messages'));assert(f.core.ref(7,1).response_scope.histories[room]);
 }finally{f.core.db.close();rmSync(dir,{recursive:true,force:true});}
}

// Upgrade retires legacy unscoped answers without altering validated reviews.
{
 const dir=mkdtempSync(join(tmpdir(),'beepa-legacy-response-')),path=join(dir,'state.db');let f=fixture(path);
 try{
  f.core.enqueue('legacy','Old unscoped sensitive answer');f.core.enqueue('protected','Existing review',{review:true,draft:'review'});
  f.core.db.prepare('DELETE FROM kv WHERE k=?').run('response_scope_version');f.core.db.close();f=fixture(path);
  const old=f.core.db.prepare('SELECT state,payload FROM outbox WHERE id=?').get('legacy:0');assert.equal(old.state,'canceled');assert(!old.payload.includes('sensitive'));
  assert.equal(f.core.db.prepare('SELECT state FROM outbox WHERE id=?').get('protected:0').state,'pending');
 }finally{f.core.db.close();rmSync(dir,{recursive:true,force:true});}
}

// Revocation while the hosted call is in flight prevents any later model tool.
{
 const f=fixture();try{
  f.steps.push(()=>{f.shared=false;return call('history',{room_id:room,display:false});});
  await assert.rejects(f.core.command('Summarize',null,'update:race'),/Shared context changed/);
  assert.equal(f.prompts.length,1);assert.equal(f.core.status().pending,0);
 }finally{f.core.db.close();}
}

// A history edit also clears an already-delivered dialogue before the next
// hosted request; a canceled model review cannot be confirmed by an old ID.
{
 const f=fixture();try{
  await summarize(f);await f.core.flush();f.version='v2';
  f.steps.push(messages=>{assert(!messages.some(m=>m.content?.startsWith('Recent exchanges')));return {content:'Please choose the current conversation.'};});
  await f.core.command('What about that?',null,'update:after-edit');
  f.domain.prepareSend=async({body,room_id})=>({id:'model-review',kind:'send',created:f.now,body,room_id,context_version:f.version,conversation:await f.domain.liveRoom(room_id),content:{'com.jkali.send_mode':'direct'}});
  f.steps.push(()=>call('prepare_send',{room_id:room,body:'Reviewed text'}));
  await f.core.command('Prepare a reply',null,'update:review');f.state.control.settings.allow_model_context=false;
  await f.core.flush();assert.equal((await f.core.confirm('model-review')).state,'review_required');
  assert(!f.sent.some(p=>p.text.includes('Reviewed text')));
 }finally{f.core.db.close();}
}

// Credential patterns are masked before hosted processing and dialogue caching;
// IDs, normal prose and exact authorized replay do not get rewritten.
const secrets=['sk-or-v1-'+'a'.repeat(64),'sk-'+'b'.repeat(30),'123456789:'+'c'.repeat(35),'syt_'+'d'.repeat(30),
 'ghp_'+'e'.repeat(36),'github_pat_'+'f'.repeat(40),'xoxb-'+'g'.repeat(30),'AKIA'+'H'.repeat(16),'eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjMifQ.signature'];
for(const secret of secrets){assert(!redactModelText('Here '+secret+' ends.').includes(secret));}
assert.equal(redactModelText('Meet at 12:30; room !ada:fixture; message $abc; user @owner:test.'),'Meet at 12:30; room !ada:fixture; message $abc; user @owner:test.');
assert.equal(redactModelText('Basic introduction from the bearer of good news.'),'Basic introduction from the bearer of good news.');
const labeled='password="long phrase" api_key=abc123 Authorization: Bearer bearer-value\nCookie: sid=private\nhttps://name:private@service.test/?access_token=another';
for(const hidden of ['long phrase','abc123','bearer-value','sid=private','name:private','another'])assert(!redactModelText(labeled).includes(hidden));
assert(!redactModelText('-----BEGIN PRIVATE KEY-----\nprivate material without closing boundary').includes('private material'));
assert.deepEqual(redactModelValue({token:'secret',OPENROUTER_API_KEY:'secret',room_id:room}),{token:'[REDACTED CREDENTIAL]',OPENROUTER_API_KEY:'[REDACTED CREDENTIAL]',room_id:room});
const scrubbed=redactModelValue({text:'Cookie: sid=private\nMeet at three.',room_id:room});
assert.deepEqual(JSON.parse(redactModelText(JSON.stringify(scrubbed))),scrubbed,'already-scrubbed JSON retains later routing/context fields');
{
 const f=fixture();try{
  f.text=secrets.join('\n');
  f.steps.push(messages=>{assert(!JSON.stringify(messages).includes(secrets[0]));return call('history',{room_id:room,display:false});},messages=>{
   const serialized=JSON.stringify(messages);for(const secret of secrets)assert(!serialized.includes(secret));assert(serialized.includes('[REDACTED CREDENTIAL]'));return {content:'Credentials were masked.'};
  });
  await f.core.command('Summarize using '+secrets[0],null,'update:masked');await f.core.flush();
  const memory=JSON.stringify([...f.core.dialogues.values()]);for(const secret of secrets)assert(!memory.includes(secret));
  f.core.enqueueHistory('literal',await f.domain.history(room));await f.core.flush();
  for(const secret of secrets)assert(f.sent.at(-1).text.includes(secret),'literal authorized replay stays exact');
  f.steps.push(()=>call('prepare_send',{room_id:room,body:'Use [REDACTED CREDENTIAL]'}),messages=>{assert.match(messages.at(-1).content,/master composer/);return {content:'Please complete that text in the master composer.'};});
  await f.core.command('Send the credential',null,'update:placeholder');assert.equal(f.core.db.prepare('SELECT count(*) AS n FROM drafts').get().n,0);
 }finally{f.core.db.close();}
}
console.log('Context privacy: delayed/multipart/restarted response revocation, message edits, in-flight model checks, credential masking and exact replay passed.');
