import assert from 'node:assert/strict';
import {ControlCore} from '../../master/control-core.mjs';
import {ControlDomain} from '../../master/control-domain.mjs';

const owner='@david:fixture',other='@elliot:fixture',room='!ada-d:fixture',room2='!ada-e:fixture';
const call=(name,args)=>({role:'assistant',tool_calls:[{id:'call-'+name,type:'function',function:{name,arguments:JSON.stringify(args)}}]});
const answer=content=>({role:'assistant',content});
function fixture(){
  const h={now:1700000000000,steps:[],prompts:[],sent:[],refreshes:0};
  const state={lists:{},bindings:{},memberships:{},control:{revision:0,settings:{allow_model_context:true}}};
  const domain={rooms:new Map([[room,{id:room,owner,native:'!native-d:fixture',title:'Ada',source:'imessage',consent:{level:'direct'}}],
    [room2,{id:room2,owner:other,native:'!native-e:fixture',title:'Ada',source:'whatsapp',consent:{level:'share'}}]]),
    accounts:new Map([[owner,{generation:'g1'}],[other,{generation:'g2'}]]),contacts:[],
    state:()=>state,settings:()=>state.control.settings,contextStamp:ControlDomain.prototype.contextStamp,
    refresh:async()=>{h.refreshes++;},hydrateAccounts:async()=>{},summary:r=>({id:r.id,owner:r.owner,title:r.title,source:r.source}),
    liveRoom:async id=>{assert(domain.rooms.has(id));return domain.rooms.get(id);},
    history:async id=>({conversation:domain.summary(domain.rooms.get(id)),messages:[{id:'$incoming',ts:h.now,from_me:false,text:'Can we meet?'}],cursor:'older'})};
  const core=new ControlCore({path:':memory:',domain,clock:()=>h.now,
    telegram:async(method,payload)=>{if(h.blocked)throw Object.assign(Error('blocked'),{status:403});h.sent.push({method,payload});return {message_id:h.sent.length};},
    model:async messages=>{h.prompts.push(structuredClone(messages));assert(h.steps.length,'unexpected model call');return h.steps.shift()(messages);}});
  core.set('operator',{user:7,chat:7});
  Object.assign(h,{core,domain,state,ask:async(text,id,ref=null,session)=>{await core.command(text,ref,'update:'+id,session);await core.flush();},close:()=>core.db.close()});return h;
}
const prior=messages=>messages.find(m=>m.content?.startsWith('Recent exchanges'))?.content;
async function choices(h,id=1){
  h.steps.push(()=>call('search_conversations',{query:'Ada'}),()=>answer('Which account? 1. David (iMessage), 2. Elliot (WhatsApp).'));
  await h.ask('Show Ada’s messages',id);
}

// A real tool lookup and its displayed ordering survive the next short reply.
// The model receives both account/room bindings, never only an ambiguous name.
{
 const h=fixture();try{
  await choices(h);
  h.steps.push(messages=>{const context=prior(messages);assert(context.includes(room));assert(context.includes(room2));assert(context.includes('2. Elliot'));
    assert(h.refreshes>=2,'context is refreshed before followup and model delivery');return call('history',{room_id:room2});});
  await h.ask('the second one',2);
  assert.match(h.sent.at(-1).payload.text,/Ada · WhatsApp\nElliot’s account/);
  h.steps.push(messages=>{assert(prior(messages).includes('"cursor":"older"'));return answer('I can show older messages from Elliot’s WhatsApp conversation.');});
  await h.ask('older please',3);
 }finally{h.close();}
}

// Expiry, canonical access, source identity, shared catalog and conversation
// changes erase prior text before the next hosted-model call.
for(const change of ['expiry','account','contact','private','settings','new','disconnect','model-disabled']){
 const h=fixture();try{
  await choices(h);
  if(change==='expiry')h.now+=600001;
  if(change==='account')h.domain.accounts.set(owner,{generation:'replacement'});
  if(change==='contact')h.domain.contacts.push({owner_mxid:owner,network_id:'new-shared-handle'});
  if(change==='private')h.domain.rooms.delete(room);
  if(change==='settings')h.state.control.revision++;
  if(change==='new')await h.ask('/new',2);
  if(change==='disconnect'){h.core.disconnect();h.core.set('operator',{user:8,chat:8});}
  if(change==='model-disabled'){
    h.state.control.settings.allow_model_context=false;
    await assert.rejects(h.ask('second one',2),/disabled/);
    h.state.control.settings.allow_model_context=true;
  }
  h.steps.push(messages=>{assert.equal(prior(messages),undefined,change);return answer('Which account and conversation?');});
  await h.ask('second one',3);
 }finally{h.close();}
}

// An explicit quoted route wins over the last unquoted exchange. Cross-room
// tool requests still fail before history access, even with plausible names.
{
 const h=fixture();try{
  await choices(h);
  h.steps.push(messages=>{assert.equal(prior(messages),undefined);return call('history',{room_id:room2});},
    messages=>{assert.match(messages.at(-1).content,/another conversation/);return answer('Please start a new request to change conversations.');});
  await h.ask('show it',2,{room});
  await assert.rejects(h.ask('show it',3,{unknown_quote:true}),/no longer available/);
 }finally{h.close();}
}

// An API session cannot inherit Telegram context or another API session.
{
 const h=fixture();try{
  await choices(h);
  h.steps.push(messages=>{assert.equal(prior(messages),undefined);return answer('API A question');});
  await h.ask('choose a conversation',2,null,'api-session:a');
  h.steps.push(messages=>{assert.equal(prior(messages),undefined);return answer('API B question');});
  await h.ask('second one',3,null,'api-session:b');
  h.steps.push(messages=>{assert(prior(messages).includes('API A question'));assert(!prior(messages).includes('API B'));return answer('API A answer');});
  await h.ask('that one',4,null,'api-session:a');
  h.steps.push(messages=>{assert(prior(messages).includes('2. Elliot'));assert(!prior(messages).includes('API A'));return answer('Telegram answer');});
  await h.ask('second one',5);
 }finally{h.close();}
}

// Clarification yes must not approve an unrelated, fully delivered pending
// review. Explicit IDs keep the deterministic confirmation route available.
{
 const h=fixture();try{
  h.core.draft({id:'pending-review',kind:'send',created:h.now});
  h.core.enqueue('old-review','Exact unrelated message',{draft:'pending-review',review:true});await h.core.flush();
  await choices(h);
  let confirmations=0;h.core.confirm=async id=>{assert.equal(id,'pending-review');confirmations++;return {state:'submitted'};};
  await assert.rejects(h.ask('yes',2),/last exchange was not a draft review/);
  assert.equal(confirmations,0);
  await h.ask('/confirm pending-review',3);assert.equal(confirmations,1);
 }finally{h.close();}
}

// Failed or incomplete delivery is not conversation memory. The cache never
// goes to sqlite, is bounded, and drops whole oversized exchanges.
{
 const h=fixture();try{
  h.blocked=true;await choices(h);h.blocked=false;
  h.steps.push(messages=>{assert.equal(prior(messages),undefined);return answer('Choose an account.');});await h.ask('second one',2);
  assert.equal(h.core.db.prepare("SELECT count(*) AS n FROM kv WHERE v LIKE '%Which account?%'").get().n,0);
  for(let i=0;i<25;i++)h.core.rememberDialogue('session-'+i,'key-'+i,{response:'x'},'answer');
  assert.equal(h.core.dialogues.size,20);
  h.core.rememberDialogue('large','large',{response:'x'.repeat(32001)},'answer');assert.deepEqual(h.core.dialogues.get('large').frames,[]);
  h.now+=600001;h.core.pruneDialogues();assert.equal(h.core.dialogues.size,0);
 }finally{h.close();}
}
console.log('Telegram followups: scoped choices, revocation, expiry, API isolation, delivery and confirmation boundaries pass');
