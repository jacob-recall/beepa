import assert from 'node:assert/strict';
import {ControlCore} from '../../master/control-core.mjs';
import {ControlDomain} from '../../master/control-domain.mjs';
import {createCRMState,applyCRMOperation,membershipKey} from '../../shared/model/crm.js';

function fixture(){
 let state=createCRMState({workspace_id:'!crm:test',manager_mxid:'@manager:test'}),sequence=0;
 const f={cancel:[],writes:[],sent:[],now:1700000000000};
 const domain=new ControlDomain({manager:'@manager:test',bootstrap:async()=>{},roster:async()=>({}),api:async()=>{throw Error('Unexpected native or Matrix request');},clock:()=>f.now});
 domain.state=()=>state;domain.refresh=async()=>{};domain.hydrateAccounts=async()=>{};
 const apply=op=>{const r=applyCRMOperation(state,op);assert(r.outcome.accepted,r.outcome.code);state=r.state;f.writes.push(op);return r.outcome;};
 domain.store={append:async op=>apply(op),refresh:async()=>{}};
 domain.members={applyChange:async op=>{assert(['member.remove','list.archive'].includes(op.type));f.cancel.push(op);return apply(op);}};
 const put=(type,id,base,payload)=>apply(domain.operation(type,id,base,payload,'seed-'+ ++sequence));
 put('person.create','person',0,{label:'Same person'});
 for(const [suffix,owner] of [['d','@david:test'],['e','@elliot:test']]){
  put('binding.create','binding-'+suffix,0,{person_id:'person',owner_mxid:owner,generation:'g1',contact:{source:'imessage',network_id:suffix+'@example.test'},contact_version:'$v1'});
  put('list.create','list-'+suffix,0,{name:'Outreach '+suffix,purpose:'outreach'});
  put('member.set','member-'+suffix,0,{list_id:'list-'+suffix,person_id:'person',binding_id:'binding-'+suffix});
 }
 const core=new ControlCore({path:':memory:',domain,clock:()=>f.now,telegram:async(method,data)=>{f.sent.push(data);return {message_id:f.sent.length};},model:async()=>{throw Error('Model not required');}});
 core.set('operator',{user:1,chat:1});
 return {...f,core,domain,put,apply,state:()=>state,close:()=>core.db.close(),advance:()=>{f.now+=600001;}};
}
{
 const f=fixture();try{
  const d=await f.core.tool('prepare_member_removal',{list_id:'list-d',binding_id:'binding-d'});
  assert.match(f.core.review(d),/David.*iMessage.*d@example.test/);assert.match(f.core.review(d),/Existing Direct stays enabled/);
  assert.equal(f.cancel.length,0);assert.equal(f.state().memberships[membershipKey('list-d','person')].removed,false);
  await f.core.confirm(d.id);await f.core.confirm(d.id);assert.equal(f.cancel.length,1,'single-use confirmation reaches cancellation-aware adapter once');
  assert.equal(f.state().memberships[membershipKey('list-d','person')].removed,true);assert.equal(f.state().memberships[membershipKey('list-e','person')].removed,false);
  assert.equal(f.writes.at(-1).type,'member.remove');assert.equal(f.writes.at(-1).payload.list_id,'list-d');
 }finally{f.close();}
}
for(const change of ['reassign','remove-readd','list-revision','excluded-owner','expiry']){
 const f=fixture();try{
  const d=await f.core.tool('prepare_member_removal',{list_id:'list-d',binding_id:'binding-d'});
  if(change==='reassign')f.put('member.set','member-d',1,{list_id:'list-d',person_id:'person',binding_id:'binding-e'});
  if(change==='remove-readd'){f.put('member.remove','member-d',1,{list_id:'list-d',person_id:'person'});f.put('member.set','member-d',2,{list_id:'list-d',person_id:'person',binding_id:'binding-d'});}
  if(change==='list-revision')f.put('list.rename','list-d',1,{name:'Changed'});
  if(change==='excluded-owner')f.put('control.settings','control',0,{...f.domain.settings(),allowed_accounts:['@elliot:test']});
  if(change==='expiry')f.advance();
  await assert.rejects(f.core.confirm(d.id),/changed|expired/);assert.equal(f.cancel.length,0,change);
 }finally{f.close();}
}
{
 const f=fixture();try{
  await assert.rejects(f.core.tool('prepare_member_removal',{list_id:'list-d',binding_id:'binding-e'}),/unavailable/);
  const draft=await f.core.tool('prepare_list_change',{operation:'create',name:'New outreach',purpose:'outreach'});
  assert.match(f.core.review(draft),/outreach.*New outreach/);assert.match(f.core.review(draft),/separate review.*Direct/);
  await f.core.confirm(draft.id);assert.equal(f.state().lists[draft.operation.entity_id].purpose,'outreach');assert.equal(f.cancel.length,0);
  const monitoring=await f.core.tool('prepare_list_change',{operation:'create',name:'Default monitoring'});
  assert.equal(monitoring.operation.payload.purpose,'monitoring');
  const archive=await f.core.tool('prepare_list_change',{operation:'archive',list_id:'list-d'});await f.core.confirm(archive.id);
  assert.equal((await f.core.tool('lists',{})).some(l=>l.list_id==='list-d'),false);
  assert.equal((await f.core.tool('lists',{include_archived:true})).find(l=>l.list_id==='list-d').archived,true);
  const restore=await f.core.tool('prepare_list_change',{operation:'restore',list_id:'list-d'});assert.match(f.core.review(restore),/Previously canceled unsent work is not restarted/);
  await f.core.confirm(restore.id);await f.core.confirm(restore.id);assert.equal(f.state().lists['list-d'].archived,false);assert.equal(f.cancel.length,1,'restore does not activate or resend');
  await assert.rejects(f.core.tool('prepare_list_change',{operation:'rename',list_id:'list-d',name:'Bad',purpose:'outreach'}),/purpose/);
 }finally{f.close();}
}
{
 const f=fixture();try{
  f.put('control.settings','control',0,{...f.domain.settings(),sending_mode:'read_only',allow_model_context:false,allowed_accounts:['@david:test'],retention_days:7});
  const before=await f.core.tool('settings',{}),d=await f.core.tool('prepare_global_notifications',{enabled:false});
  assert.equal(f.domain.settings().notifications_enabled,true,'preparation does not mutate');
  assert.match(f.core.review(d),/globally across every list/);await f.core.confirm(d.id);
  assert.deepEqual(f.domain.settings(),{...before.settings,notifications_enabled:false});assert.equal(f.state().control.revision,2);
  await assert.rejects(f.core.tool('prepare_global_notifications',{enabled:true,allow_model_context:true}),/Unknown tool argument/);
  await assert.rejects(f.core.tool('settings_update',{allow_model_context:true}),/Unknown tool/);
  const stale=await f.core.tool('prepare_global_notifications',{enabled:true});
  f.put('control.settings','control',2,{...f.domain.settings(),retention_days:8});
  await assert.rejects(f.core.confirm(stale.id),/settings changed.*fresh review/);assert.equal(f.domain.settings().notifications_enabled,false);
  assert.equal((await f.core.confirm(stale.id)).state,'review_required');
  await f.core.command('/settings',null,'settings');await f.core.flush();assert.match(f.sent.at(-1).text,/Automatic notifications: Off/);assert.match(f.sent.at(-1).text,/AI assistance: Off/);
 }finally{f.close();}
}
console.log('Telegram list editing: explicit purpose, archived discovery/restore, account-bound removal, cancellation dispatch, stale review rejection and restricted global notification settings passed.');
