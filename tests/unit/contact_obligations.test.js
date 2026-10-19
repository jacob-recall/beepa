import assert from 'node:assert/strict';
import {createCRMState,applyCRMOperation,canonicalCRMJSON} from '../../shared/model/crm.js';
import {ControlDomain} from '../../master/control-domain.mjs';
import {ControlCore} from '../../master/control-core.mjs';

const manager='@manager:test',owner='@david:test',other='@elliot:test';
function fixture(){
 let state=createCRMState({workspace_id:'!crm:test',manager_mxid:manager}),sequence=0;
 const f={now:1700000000000,reads:0};
 const op=(type,entity_id,payload,base_revision=0)=>({schema:1,workspace_id:state.workspace_id,type,entity_id,payload,base_revision,op_id:'op-'+ ++sequence});
 const apply=(operation,code='accepted')=>{const r=applyCRMOperation(state,operation);assert.equal(r.outcome.code,code);state=r.state;return operation;};
 apply(op('person.create','person',{label:'Same shared person'}));
 for(const [suffix,mxid] of [['d',owner],['e',other]]){
  apply(op('binding.create','binding-'+suffix,{person_id:'person',owner_mxid:mxid,generation:'gen',contact:{source:'imessage',network_id:'same@example.com'},contact_version:'$contact-'+suffix}));
  apply(op('list.create','list-'+suffix,{name:'List '+suffix}));
  apply(op('member.set','member-'+suffix,{list_id:'list-'+suffix,person_id:'person',binding_id:'binding-'+suffix}));
 }
 const domain=new ControlDomain({api:async()=>{throw Error('Unexpected network/write');},bootstrap:async()=>{},manager,roster:async()=>({})});
 domain.clock=()=>f.now;domain.state=()=>state;
 domain.liveContact=async binding=>{f.reads++;assert(domain.contacts.some(c=>c.owner_mxid===binding.owner_mxid&&c.contact_version===binding.contact_version));};
 for(const [suffix,mxid] of [['d',owner],['e',other]]){
  domain.accounts.set(mxid,{generation:'gen',journal_epoch:'journal'});
  domain.health.set(mxid,{connected:true,updated_at:f.now});
  domain.contacts.push({owner_mxid:mxid,source:'imessage',network_id:'same@example.com',contact_version:'$contact-'+suffix,person_id:'profile',display_name:'Same name'});
 }
 return Object.assign(f,{domain,op,apply,state:()=>state,change:patch=>Object.assign(f,patch)});
}
{
 const f=fixture(),rows=await f.domain.contactUpdates();assert.equal(rows.length,2);
 assert.deepEqual(rows.map(r=>[r.owner,r.binding_id,r.due_at,r.history_known]),[[owner,'binding-d',null,false],[other,'binding-e',null,false]]);
 assert(rows.every(r=>r.status_label==='No shared conversation linked'&&r.conversation_ids.length===0));
 assert.equal((await f.domain.contactUpdates(null,{owner})).length,1);
 assert.equal((await f.domain.contactUpdates(null,{source:'whatsapp'})).length,0);
 const operation=await f.domain.prepareContactObligation({list_id:'list-d',binding_id:'binding-d',due_at:f.now+60000});f.apply(operation);
 assert.equal(f.reads,1);assert.equal(f.state().lists['list-d'].contact_obligations['binding-d'].created_at,f.now);
 assert.equal((await f.domain.contactUpdates('list-e'))[0].due_at,null,'same person under another account has independent obligations');
 const before=canonicalCRMJSON(f.state());f.apply(operation);assert.equal(canonicalCRMJSON(f.state()),before,'replay is idempotent');
 const snapshot=JSON.parse(JSON.stringify(f.state()));assert.equal(snapshot.lists['list-d'].contact_obligations['binding-d'].due_at,f.now+60000);
 f.change({now:f.now+1000});const snooze=await f.domain.prepareContactObligation({list_id:'list-d',binding_id:'binding-d',snooze_until:f.now+300000});f.apply(snooze);
 assert.equal(snooze.payload.created_at,operation.payload.created_at);assert.equal(snooze.payload.due_at,operation.payload.due_at);
 const resolved=await f.domain.prepareContactObligation({list_id:'list-d',binding_id:'binding-d',resolved:true});f.apply(resolved);
 assert.equal((await f.domain.contactUpdates('list-d'))[0].waiting,'resolved');
 const reopen=await f.domain.prepareContactObligation({list_id:'list-d',binding_id:'binding-d',resolved:false});f.apply(reopen);assert.equal(reopen.payload.created_at,f.now);
}
for(const mutation of ['owner','generation','membership','extra','deadline']){
 const f=fixture(),operation=await f.domain.prepareContactObligation({list_id:'list-d',binding_id:'binding-d'});
 if(mutation==='owner')operation.payload.owner_mxid=other;
 if(mutation==='generation')operation.payload.generation='old';
 if(mutation==='membership')operation.payload.membership_operation='old';
 if(mutation==='extra')operation.payload.room_id='!fake:test';
 if(mutation==='deadline')operation.payload.due_at=-1;
 const result=applyCRMOperation(f.state(),operation);assert.equal(result.outcome.accepted,false,mutation);
}
for(const change of ['revoked','generation','removed','readded','binding-switch','account-excluded']){
 const f=fixture(),operation=await f.domain.prepareContactObligation({list_id:'list-d',binding_id:'binding-d',due_at:f.now+60000});f.apply(operation);
 const pending=await f.domain.prepareContactObligation({list_id:'list-d',binding_id:'binding-d',snooze_until:f.now+120000});
 if(change==='revoked')f.domain.contacts=f.domain.contacts.filter(c=>c.owner_mxid!==owner);
 if(change==='generation')f.domain.accounts.get(owner).generation='replacement';
 if(change==='account-excluded')f.domain.allowedOwner=mxid=>mxid!==owner;
 if(['removed','readded'].includes(change)){
  f.apply(f.op('member.remove','member-d',{list_id:'list-d',person_id:'person'},1));
  if(change==='readded')f.apply(f.op('member.set','member-d',{list_id:'list-d',person_id:'person',binding_id:'binding-d'},2));
 }
 if(change==='binding-switch')f.apply(f.op('member.set','member-d',{list_id:'list-d',person_id:'person',binding_id:'binding-e'},1));
 const rows=await f.domain.contactUpdates('list-d');assert(rows.length===0||rows[0].due_at===null,change+' must not revive a previous account/membership deadline');
 await assert.rejects(f.domain.validateReviewScope({kind:'list',operation:pending}),/unavailable|refused|Assertion/);
}
{
 const f=fixture();f.apply(await f.domain.prepareContactObligation({list_id:'list-d',binding_id:'binding-d',due_at:f.now+60000}));
 const room={id:'!new:test',owner,source:'imessage',profile:'profile'};f.domain.rooms.set(room.id,room);
 let receipt='unknown',timestamp=f.now-10000,trackingFresh=true;
 let mirrored='$sent';f.domain.history=async()=>({messages:[{id:mirrored}]});f.domain.tracking=()=>({fresh:trackingFresh,messages:[{event_id:'$sent',incoming:false,timestamp,delivery:receipt}]});
 assert.equal((await f.domain.contactUpdates('list-d'))[0].waiting,'contact_pending');
 receipt='sent';assert.equal((await f.domain.contactUpdates('list-d'))[0].waiting,'contact_pending','earlier native send cannot satisfy a new task');
 timestamp=f.now+1;trackingFresh=false;assert.equal((await f.domain.contactUpdates('list-d'))[0].waiting,'contact_pending','stale positive evidence does not auto-resolve');
 assert.equal((await f.domain.contactUpdates('list-d'))[0].fresh,false,'source health alone cannot authorize a reminder');
 trackingFresh=true;mirrored='$newer';assert.equal((await f.domain.contactUpdates('list-d'))[0].fresh,false,'tracking must catch up with the latest mirrored message');mirrored='$sent';const [row]=await f.domain.contactUpdates('list-d');assert.equal(row.waiting,'contacted');assert.equal(row.source_evidence.event_id,'$sent');
 assert.equal(f.state().lists['list-d'].contact_obligations['binding-d'].resolved,false,'derived completion never rewrites canonical intent');
}
{
 const f=fixture();f.domain.refresh=async()=>{};f.domain.hydrateAccounts=async()=>{};f.domain.apply=async operation=>{f.apply(operation);return {accepted:true};};
 const core=new ControlCore({path:':memory:',domain:f.domain,clock:()=>f.now,telegram:async()=>{throw Error('No transport authorized in fixture');},model:async()=>{throw Error('No model needed');}});
 try{
  const rows=await core.tool('contact_updates',{owner});assert.equal(rows.length,1);
  const draft=await core.tool('prepare_contact_obligation',{list_id:'list-d',binding_id:'binding-d',due_at:f.now+60000});
  assert.equal((await f.domain.contactUpdates('list-d'))[0].due_at,null,'model can only prepare a review');
  assert(core.review(draft).includes('David'));assert(!core.review(draft).includes(owner));assert.equal((await core.confirm(draft.id)).state,'submitted');
  assert.equal((await f.domain.contactUpdates('list-d'))[0].due_at,f.now+60000);
  assert.equal((await core.confirm(draft.id)).state,'submitted');
 }finally{core.db.close();}
}
console.log('Contact obligations: canonical account-bound dates, shared membership revocation/reassignment, no implicit timers, and exact fresh native completion passed.');
