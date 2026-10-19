import assert from 'node:assert/strict';
import { createCRMMembers } from '../../apps/master/crm-members.js';
import { createCRMStore } from '../../apps/master/crm-store.js';
import { membershipKey } from '../../shared/model/crm.js';

const ROOM = '!crm:master', PROPOSALS = '!proposals:master', MANAGER = '@manager:master', OWNER = '@user:master';
const OP = 'com.jkali.crm.operation', REQUEST = 'com.jkali.crm.direct_request', RESULT = 'com.jkali.crm.direct_result';
const clone = value => JSON.parse(JSON.stringify(value));
const deferred = () => { let resolve, reject; const promise = new Promise((a,b) => { resolve=a; reject=b; }); return {promise,resolve,reject}; };
const operation = (id, type, entity, base, payload) => ({schema:1,op_id:id,workspace_id:ROOM,type,entity_id:entity,base_revision:base,payload});
function fixture() {
  const h = {current:true,counter:0,calls:[],events:[],results:new Map(),resultRooms:new Map(),transactions:new Map(),roomHook:null,metadataHook:null,directHook:null,applied:0};
  h.contact = {owner_mxid:OWNER,source:'imessage',network_id:'person@example.invalid',contact_version:'7',display_name:'Synthetic Person'};
  h.contacts = [h.contact];
  h.account = {generation:'generation-one',journal_epoch:'journal-one',proposal_room_id:PROPOSALS};
  h.accounts = {[OWNER]:h.account};
  const state = (type,content,key='',sender=OWNER) => ({type,state_key:key,sender,content});
  h.roomState = [state('m.room.create',{}),state('com.jkali.proposals',{}),
    state('com.jkali.crm.capability',{schema:1,owner_mxid:OWNER,generation:h.account.generation,journal_epoch:h.account.journal_epoch,ordered_policy:true,direct_prepare_commit:true,direct_cancel:true}),
    state('m.room.member',{membership:'join'},MANAGER),state('m.room.member',{membership:'join'},OWNER),
    state('m.room.power_levels',{users:{[OWNER]:100,[MANAGER]:50},events_default:100,state_default:100,events:{'com.jkali.proposal':50,[REQUEST]:50},invite:100,kick:100,ban:100,redact:100}),
    state('m.room.join_rules',{join_rule:'invite'}),state('m.room.history_visibility',{history_visibility:'shared'}),state('m.room.guest_access',{guest_access:'forbidden'})];
  h.addEvent = body => { const event={type:OP,sender:MANAGER,room_id:ROOM,event_id:'$event'+h.events.length,content:clone(body)}; h.events.push(event); return event.event_id; };
  h.roomStates = {[PROPOSALS]:h.roomState};
  h.publish = (command, content, sender=OWNER, owner=OWNER) => {
    const account=h.accounts[owner];h.resultRooms.set(command,account.proposal_room_id);
    h.results.set(command,state(RESULT,{schema:1,command_id:command,owner_mxid:owner,generation:account.generation,journal_epoch:account.journal_epoch,...content},command,sender));
  };
  h.addAccount = () => {
    const owner='@david:master', account={generation:'generation-david',journal_epoch:'journal-david',proposal_room_id:'!proposals-david:master'};
    h.accounts[owner]=account;
    h.roomStates[account.proposal_room_id]=h.roomState.map(event=>{
      const next=clone(event);next.sender=owner;
      if(next.state_key===OWNER)next.state_key=owner;
      if(next.type==='com.jkali.crm.capability')Object.assign(next.content,{owner_mxid:owner,generation:account.generation,journal_epoch:account.journal_epoch});
      if(next.type==='m.room.power_levels')next.content.users={[owner]:100,[MANAGER]:50};
      return next;
    });
    const contact={...h.contact,owner_mxid:owner,network_id:'person.david@example.invalid',contact_version:'12'};h.contacts.push(contact);return contact;
  };
  h.addEvent(operation('seed-list','list.create','list',0,{name:'Outreach'}));
  h.api = async (method,path,body) => {
    h.calls.push({method,path,body:body && clone(body)});
    if (method==='GET' && path.includes('/messages?')) {
      const from=Number(new URL(path,'https://fixture.invalid').searchParams.get('from') || 0);
      const chunk=clone(h.events.slice(from));
      return chunk.length ? {chunk,end:String(h.events.length)} : {chunk:[]};
    }
    if (method==='GET') {
      const room=Object.keys(h.roomStates).find(id=>path==='/_matrix/client/v3/rooms/'+encodeURIComponent(id)+'/state');
      assert.ok(room,'known synthetic proposal room');
      if(h.roomHook) await h.roomHook();
      return clone([...h.roomStates[room],...[...h.results].filter(([id])=>h.resultRooms.get(id)===room).map(([,event])=>event)]);
    }
    assert.equal(method,'PUT');
    if(path.includes('/send/'+OP+'/')) {
      if(h.metadataHook) await h.metadataHook(body);
      if(!h.transactions.has(path)) h.transactions.set(path,h.addEvent(body));
      return {event_id:h.transactions.get(path)};
    }
    const owner=Object.keys(h.accounts).find(id=>path.startsWith('/_matrix/client/v3/rooms/'+encodeURIComponent(h.accounts[id].proposal_room_id)+'/send/'+REQUEST+'/crm_direct_'));
    assert.ok(owner,'known synthetic Direct destination');assert.equal(body.owner_mxid,owner);
    if(h.directHook) await h.directHook(body,path);
    if(!h.transactions.has(path)) {
      h.transactions.set(path,'$direct'+h.transactions.size);
      if(body.phase==='prepare') h.publish(body.command_id,{state:'prepared',ticket_id:'a'.repeat(64),phase:'prepare'},owner,owner);
      else if(body.phase==='cancel')h.publish(body.command_id,{state:'canceled',phase:'cancel',safe_to_change_membership:true},owner,owner);
      else { h.applied++; h.publish(body.command_id,{state:'applied',phase:'commit'},owner,owner); }
    }
    return {event_id:h.transactions.get(path)};
  };
  h.store = createCRMStore({api:h.api,bootstrap:async()=>({room_id:ROOM,manager_mxid:MANAGER,schema_version:1}),managerMxid:MANAGER,isCurrent:()=>h.current});
  h.newMembers = () => createCRMMembers({store:h.store,api:h.api,managerMxid:MANAGER,accountFor:owner=>h.accounts[owner] || null,contacts:()=>h.contacts,isCurrent:()=>h.current,uuid:()=> 'id'+ ++h.counter,polls:2,wait:async()=>{}});
  h.members=h.newMembers();
  h.plan = contact => h.members.planAddition({listId:'list',contact:contact || h.contact});
  h.direct = phase => h.calls.filter(call=>call.method==='PUT' && call.path.includes('/send/'+REQUEST+'/') && (!phase || call.body.phase===phase));
  h.metadata = () => h.calls.filter(call=>call.method==='PUT' && call.path.includes('/send/'+OP+'/'));
  h.member = plan => h.store.snapshot().state.memberships[membershipKey('list',plan.binding.person_id)];
  return h;
}
let passed=0, failed=0;
async function test(name,run) { try {await run(); passed++;} catch(error) {failed++; console.error('FAIL crm_members:',name,'\n',error.stack);} }

await test('real reducer stores prepare, membership, commit in that order',async()=>{
  const h=fixture(),plan=await h.plan();
  const result=await h.members.execute(plan);
  assert.equal(result.state,'applied'); assert.equal(h.member(plan).binding_id,plan.binding.binding_id);
  const writes=h.calls.filter(c=>c.method==='PUT');
  const prepared=writes.findIndex(c=>c.body.phase==='prepare'), member=writes.findIndex(c=>c.body.type==='member.set'), commit=writes.findIndex(c=>c.body.phase==='commit');
  assert.ok(prepared>=0 && member>prepared && commit>member); assert.equal(h.applied,1);
  assert.equal(h.direct('prepare')[0].body.membership_operation_id,plan.member.op_id);
  assert.equal(h.direct('commit')[0].body.ticket_id,'a'.repeat(64));
});
await test('contacts absent from currently shared snapshot cannot be planned or persisted',async()=>{
  const h=fixture(); h.contacts=[];
  await assert.rejects(h.plan(),/Contact|shared|changed/i);
  assert.equal(h.metadata().length,0); assert.equal(h.direct().length,0);
});
await test('revocation between plan and execution cannot persist contact metadata',async()=>{
  const h=fixture(),plan=await h.plan(); h.contacts=[];
  await assert.rejects(h.members.execute(plan),/Contact|shared|changed/i);
  assert.equal(h.metadata().length,0); assert.equal(h.direct().length,0);
});
for(const [name,mutate] of [
  ['creator',s=>{s.find(e=>e.type==='m.room.create').sender='@foreign:master';}],
  ['capability sender',s=>{s.find(e=>e.type==='com.jkali.crm.capability').sender='@foreign:master';}],
  ['generation',s=>{s.find(e=>e.type==='com.jkali.crm.capability').content.generation='old';}],
  ['journal epoch',s=>{s.find(e=>e.type==='com.jkali.crm.capability').content.journal_epoch='old';}],
  ['third former member',s=>s.push({type:'m.room.member',state_key:'@foreign:master',sender:OWNER,content:{membership:'leave'}})],
  ['ordinary message privilege',s=>{s.find(e=>e.type==='m.room.power_levels').content.events['m.room.message']=50;}],
  ['encryption',s=>s.push({type:'m.room.encryption',state_key:'',sender:OWNER,content:{}})],
  ['duplicate capability',s=>s.push(clone(s.find(e=>e.type==='com.jkali.crm.capability')))],
]) await test('reject untrusted proposal room '+name,async()=>{
  const h=fixture(),plan=await h.plan(); mutate(h.roomState);
  await assert.rejects(h.members.execute(plan),/not ready|account/i); assert.equal(h.direct().length,0);
});
await test('metadata write failure after prepare never commits Direct',async()=>{
  const h=fixture(),plan=await h.plan();
  h.metadataHook=async body=>{if(body.type==='member.set') throw new Error('metadata offline');};
  await assert.rejects(h.members.execute(plan),/metadata offline/);
  assert.equal(h.direct('prepare').length,1); assert.equal(h.direct('commit').length,0); assert.equal(h.member(plan),undefined);
});
await test('canonical conflict at metadata PUT never commits Direct',async()=>{
  const h=fixture(),plan=await h.plan(); let raced=false;
  h.metadataHook=async body=>{if(body.type==='member.set' && !raced){raced=true;const rival={...clone(body),op_id:'other-tab-member'};Object.assign(rival.payload.direct_request,{command_id:rival.op_id,membership_operation_id:rival.op_id});h.addEvent(rival);}};
  await assert.rejects(h.members.execute(plan),/revision-conflict|assignment changed/);
  assert.equal(h.direct('commit').length,0); assert.equal(h.member(plan).operation_id,'other-tab-member');
});
await test('lost accepted prepare response resumes exact command without second prepare',async()=>{
  const h=fixture(),plan=await h.plan(); let lost=false;
  h.directHook=async(body,path)=>{if(body.phase==='prepare'&&!lost){lost=true;h.publish(body.command_id,{state:'prepared',ticket_id:'a'.repeat(64)});h.transactions.set(path,'$accepted');throw new Error('prepare reply lost');}};
  await assert.rejects(h.members.execute(plan),/reply lost/);
  assert.equal((await h.members.execute(plan)).state,'applied'); assert.equal(h.direct('prepare').length,1); assert.equal(h.direct('commit').length,1);
});
await test('ambiguous prepare failure retries identical body and transaction',async()=>{
  const h=fixture(),plan=await h.plan(); let lost=false;
  h.directHook=async body=>{if(body.phase==='prepare'&&!lost){lost=true;throw new Error('before acceptance');}};
  await assert.rejects(h.members.execute(plan),/before acceptance/); await h.members.execute(plan);
  const requests=h.direct('prepare'); assert.equal(requests.length,2); assert.deepEqual(requests[0],requests[1]);
});
await test('deferred source completion leaves no membership until prepared and retries existing command',async()=>{
  const h=fixture(),plan=await h.plan();
  h.directHook=async(body,path)=>{if(body.phase==='prepare')h.transactions.set(path,'$pending-prepare');};
  await assert.rejects(h.members.execute(plan),/awaiting.*sync service/);
  assert.equal(h.member(plan),undefined);assert.equal(h.direct('commit').length,0);
  h.publish(plan.member.op_id,{state:'prepared',ticket_id:'a'.repeat(64)});
  assert.equal((await h.members.execute(plan)).state,'applied');assert.equal(h.direct('prepare').length,1);
});
await test('source rejection leaves list membership absent and never commits',async()=>{
  const h=fixture(),plan=await h.plan();
  h.directHook=async(body,path)=>{if(body.phase==='prepare'){h.publish(body.command_id,{state:'rejected',reason:'contact-version-changed'});h.transactions.set(path,'$rejected');}};
  await assert.rejects(h.members.execute(plan),/contact-version-changed/);
  assert.equal(h.member(plan),undefined);assert.equal(h.direct('commit').length,0);
});
await test('lost commit response resumes canonical member/result and never reapplies after manual Private',async()=>{
  const h=fixture(),plan=await h.plan(); let lost=false;
  h.directHook=async(body,path)=>{if(body.phase==='commit'&&!lost){lost=true;h.applied++;h.publish(body.command_id,{state:'applied'});h.transactions.set(path,'$accepted');throw new Error('commit reply lost');}};
  await assert.rejects(h.members.execute(plan),/reply lost/); assert.ok(h.member(plan));
  h.applied=0; // User manually selected Private after the original application.
  const reopened=h.newMembers(); assert.equal((await reopened.resume(h.member(plan))).state,'applied');
  assert.equal(h.applied,0); assert.equal(h.direct('commit').length,1);
});
await test('accepted membership with lost write reply resumes prepared ticket from canonical history',async()=>{
  const h=fixture(),plan=await h.plan();let lost=false;
  h.metadataHook=async body=>{if(body.type==='member.set'&&!lost){lost=true;h.addEvent(body);throw new Error('membership reply lost');}};
  await assert.rejects(h.members.execute(plan),/membership reply lost/);
  assert.equal(h.direct('commit').length,0);await h.store.refresh();assert.ok(h.member(plan));
  assert.equal((await h.newMembers().resume(h.member(plan))).state,'applied');
  assert.equal(h.direct('prepare').length,1);assert.equal(h.direct('commit').length,1);
  assert.equal(h.metadata().filter(c=>c.body.type==='member.set').length,1);
});
await test('no existing conversation keeps membership and requires user approval without commit',async()=>{
  const h=fixture(),plan=await h.plan();
  h.directHook=async(body,path)=>{if(body.phase==='prepare'){h.publish(body.command_id,{state:'approval_required',reason:'contact-no_existing_conversation'});h.transactions.set(path,'$approval');}};
  const result=await h.members.execute(plan); assert.equal(result.state,'approval_required');
  assert.equal(h.member(plan).removed,false); assert.equal(h.direct('commit').length,0);
  await h.newMembers().resume(h.member(plan)); assert.equal(h.direct().length,1);
});
await test('contact version changing during room point-read prevents prepare',async()=>{
  const h=fixture(),plan=await h.plan(); h.roomHook=async()=>{h.contacts=[{...h.contact,contact_version:'8'}];};
  await assert.rejects(h.members.execute(plan),/Contact|changed/); assert.equal(h.direct().length,0);
});
await test('source result author/owner/epoch must match exact request authority',async()=>{
  for(const mutation of [r=>r.sender='@foreign:master',r=>r.content.owner_mxid='@other:master',r=>r.content.generation='old',r=>r.content.journal_epoch='old']){
    const h=fixture(),plan=await h.plan();h.publish(plan.member.op_id,{state:'prepared',ticket_id:'a'.repeat(64)});mutation(h.results.get(plan.member.op_id));
    await assert.rejects(h.members.execute(plan),/different account or session/);assert.equal(h.direct().length,0);assert.equal(h.member(plan),undefined);
  }
});
await test('session changes during deferred source read never emit a request',async()=>{
  const h=fixture(),plan=await h.plan(),gate=deferred(),reached=deferred();
  h.roomHook=async()=>{reached.resolve();await gate.promise;};
  const pending=h.members.execute(plan);await reached.promise;h.current=false;gate.resolve();
  await assert.rejects(pending,/session changed/);assert.equal(h.direct().length,0);
});
await test('membership removal while commit room verification is pending prevents commit',async()=>{
  const h=fixture(),plan=await h.plan(); let changed=false;
  h.roomHook=async()=>{const member=h.member(plan);if(member&&!changed){changed=true;h.addEvent(operation('remove-concurrent','member.remove',member.membership_id,member.revision,{list_id:'list',person_id:member.person_id}));await h.store.refresh();}};
  await assert.rejects(h.members.execute(plan),/assignment changed|List changed|membership/i);
  assert.equal(h.member(plan).removed,true);assert.equal(h.direct('commit').length,0);
});
await test('caller cannot mutate frozen reviewed identity through original contact/account references',async()=>{
  const h=fixture(),plan=await h.plan(); h.contact.network_id='different@example.invalid';h.account.proposal_room_id='!different:master';
  assert.equal(plan.binding.contact.network_id,'person@example.invalid');assert.equal(plan.account.proposal_room_id,PROPOSALS);
  await assert.rejects(h.members.execute(plan));assert.equal(h.direct().length,0);
});
await test('list archive received after prepare cannot create membership or commit',async()=>{
  const h=fixture(),plan=await h.plan();
  h.directHook=async body=>{if(body.phase==='prepare')h.addEvent(operation('archive-concurrent','list.archive','list',1,{}));};
  await assert.rejects(h.members.execute(plan),/List changed/);
  assert.equal(h.member(plan),undefined);assert.equal(h.direct('commit').length,0);
});
await test('binding contact snapshot changed during prepare cannot receive membership',async()=>{
  const h=fixture(),plan=await h.plan();
  h.directHook=async body=>{if(body.phase==='prepare')h.addEvent(operation('binding-concurrent','binding.update',plan.binding.binding_id,1,{contact_version:'8',availability:'shared'}));};
  await assert.rejects(h.members.execute(plan),/Contact binding changed/);
  assert.equal(h.member(plan),undefined);assert.equal(h.direct('commit').length,0);
});

const removeOperation = (member,id='remove-confirmed') => operation(id,'member.remove',member.membership_id,member.revision,{list_id:member.list_id,person_id:member.person_id});
await test('removal confirms cancellation before metadata and keeps already-applied Direct unchanged',async()=>{
  const h=fixture(),plan=await h.plan();await h.members.execute(plan);const old=h.member(plan),remove=removeOperation(old);
  assert.equal(h.applied,1);assert.equal((await h.members.applyChange(remove)).accepted,true);
  const cancel=h.calls.findIndex(c=>c.body?.phase==='cancel'),removed=h.calls.findIndex(c=>c.body?.op_id===remove.op_id);
  assert.ok(cancel>=0 && removed>cancel);assert.equal(h.member(plan).removed,true);assert.equal(h.applied,1);
  assert.equal(h.direct('cancel')[0].body.command_id,old.operation_id);
  assert.equal((await h.members.applyChange(remove)).duplicate,true);assert.equal(h.direct('cancel').length,1);
});
await test('pending unknown cancellation refuses membership mutation until exact result is safe',async()=>{
  const h=fixture(),plan=await h.plan();await h.members.execute(plan);const old=h.member(plan),remove=removeOperation(old);
  h.directHook=async(body,path)=>{if(body.phase==='cancel'){h.transactions.set(path,'$cancel-pending');h.publish(body.command_id,{state:'canceling',phase:'cancel',safe_to_change_membership:false});}};
  await assert.rejects(h.members.applyChange(remove),/still being reconciled/);
  assert.equal(h.member(plan).removed,false);assert.equal(h.metadata().filter(c=>c.body.type==='member.remove').length,0);
  h.publish(old.operation_id,{state:'canceled',phase:'cancel',safe_to_change_membership:true});
  assert.equal((await h.members.applyChange(remove)).accepted,true);assert.equal(h.direct('cancel').length,1);
});
await test('lost accepted cancellation response reconciles without another request or early metadata',async()=>{
  const h=fixture(),plan=await h.plan();await h.members.execute(plan);const remove=removeOperation(h.member(plan));let lost=false;
  h.directHook=async(body,path)=>{if(body.phase==='cancel'&&!lost){lost=true;h.transactions.set(path,'$cancel-accepted');h.publish(body.command_id,{state:'canceled',phase:'cancel',safe_to_change_membership:true});throw new Error('cancel response lost');}};
  await assert.rejects(h.members.applyChange(remove),/cancel response lost/);assert.equal(h.member(plan).removed,false);
  assert.equal((await h.members.applyChange(remove)).accepted,true);assert.equal(h.direct('cancel').length,1);
});
await test('canonical membership lacking a source preparation is canceled before removal and old replay remains blocked',async()=>{
  const h=fixture(),plan=await h.plan();
  for(const op of [...plan.setup,plan.member])h.addEvent(op);await h.store.refresh();
  assert.equal(h.direct().length,0);assert.equal((await h.members.applyChange(removeOperation(h.member(plan)))).accepted,true);
  assert.equal(h.direct('cancel').length,1);assert.equal(h.direct('prepare').length,0);
  await assert.rejects(h.members.execute(plan),/assignment changed/);assert.equal(h.direct('prepare').length,0);
});
await test('revoked contact and changed binding version still cancel original immutable request',async()=>{
  const h=fixture(),plan=await h.plan();await h.members.execute(plan);const old=h.member(plan);
  h.addEvent(operation('binding-revoked','binding.update',plan.binding.binding_id,1,{contact_version:'8',availability:'private'}));await h.store.refresh();h.contacts=[];
  assert.equal((await h.members.applyChange(removeOperation(old))).accepted,true);
  const request=h.direct('cancel')[0].body;
  assert.equal(request.contact_version,'7');assert.deepEqual(request.contact,plan.binding.contact);
  assert.equal(request.binding_id,plan.binding.binding_id);assert.equal(request.command_id,old.operation_id);
  assert.equal(h.applied,1);
});
await test('changed source generation cannot cancel through a new authority or remove old membership',async()=>{
  const h=fixture(),plan=await h.plan();await h.members.execute(plan);const old=h.member(plan);h.account.generation='replacement-generation';
  await assert.rejects(h.members.applyChange(removeOperation(old)),/account changed/);
  assert.equal(h.direct('cancel').length,0);assert.equal(h.member(plan).removed,false);
});
await test('active sender reassignment cancels old command before new source preparation and canonical replacement',async()=>{
  const h=fixture(),first=await h.plan();await h.members.execute(first);const old=h.member(first),other=h.addAccount();
  const second=await h.members.planAddition({listId:'list',contact:other,personId:old.person_id});
  assert.equal(second.member.entity_id,old.membership_id);assert.equal(second.member.base_revision,old.revision);
  assert.equal((await h.members.execute(second)).state,'applied');
  const cancel=h.calls.findIndex(c=>c.body?.command_id===old.operation_id && c.body?.phase==='cancel');
  const prepare=h.calls.findIndex(c=>c.body?.command_id===second.member.op_id && c.body?.phase==='prepare');
  const replacement=h.calls.findIndex(c=>c.body?.op_id===second.member.op_id);
  assert.ok(cancel>=0 && prepare>cancel && replacement>prepare);
  assert.equal(h.member(second).binding_id,second.binding.binding_id);assert.equal(h.member(second).revision,2);
  assert.equal(h.direct('cancel')[0].body.owner_mxid,OWNER);
  assert.equal(h.direct('prepare').at(-1).body.owner_mxid,'@david:master');
  const before=h.direct().length;await assert.rejects(h.members.execute(first),/assignment changed/);await assert.rejects(h.members.resume(old),/assignment changed/);
  assert.equal(h.direct().length,before,'late old browser cannot resurrect earlier grant');
});
await test('failed cancellation leaves sender assignment and new source metadata unchanged',async()=>{
  const h=fixture(),first=await h.plan();await h.members.execute(first);const old=h.member(first),other=h.addAccount();
  const second=await h.members.planAddition({listId:'list',contact:other,personId:old.person_id});
  const metadata=h.metadata().length;
  h.directHook=async(body,path)=>{if(body.phase==='cancel'){h.transactions.set(path,'$unknown');h.publish(body.command_id,{state:'canceling',phase:'cancel',safe_to_change_membership:false});}};
  await assert.rejects(h.members.execute(second),/still being reconciled/);
  assert.equal(h.member(first).binding_id,first.binding.binding_id);assert.equal(h.metadata().length,metadata);
  assert.equal(h.direct('prepare').length,1);
});
await test('archive cancels exact reviewed active commands and remains unarchived when any cancellation is unknown',async()=>{
  const h=fixture(),first=await h.plan();await h.members.execute(first);
  const other=h.addAccount(),second=await h.plan(other);await h.members.execute(second);
  const active=[first.member.op_id,second.member.op_id].sort();
  const archive=operation('archive-guarded','list.archive','list',1,{membership_operations:active});
  h.directHook=async(body,path)=>{if(body.phase==='cancel' && body.owner_mxid==='@david:master'){h.transactions.set(path,'$unknown');h.publish(body.command_id,{state:'canceling',phase:'cancel',safe_to_change_membership:false},'@david:master','@david:master');}};
  await assert.rejects(h.members.applyChange(archive),/still being reconciled/);
  assert.equal(h.store.snapshot().state.lists.list.archived,false);assert.equal(h.metadata().filter(c=>c.body.type==='list.archive').length,0);
  h.publish(second.member.op_id,{state:'canceled',phase:'cancel',safe_to_change_membership:true},'@david:master','@david:master');
  assert.equal((await h.members.applyChange(archive)).accepted,true);assert.equal(h.store.snapshot().state.lists.list.archived,true);
  assert.equal(h.direct('cancel').length,2,'recovery never repeats completed cancellations');assert.equal(h.applied,2);
});
await test('new concurrent member after archive review blocks archival through canonical reducer',async()=>{
  const h=fixture(),first=await h.plan();await h.members.execute(first);
  const archive=operation('archive-race','list.archive','list',1,{membership_operations:[first.member.op_id]});
  const other=h.addAccount(),second=await h.plan(other);let injected=false;
  h.directHook=async body=>{if(body.phase==='cancel'&&!injected){injected=true;for(const op of [...second.setup,second.member])h.addEvent(op);}};
  const outcome=await h.members.applyChange(archive);
  assert.equal(outcome.accepted,false);assert.equal(outcome.code,'membership-conflict');assert.equal(h.store.snapshot().state.lists.list.archived,false);
});
await test('immutable plan freezes nested native identity, owner and request correlation',async()=>{
  const h=fixture(),plan=await h.plan();
  assert.throws(()=>{plan.member.payload.direct_request.contact_version='8';},TypeError);
  assert.throws(()=>{plan.binding.contact.network_id='altered@example.invalid';},TypeError);
  assert.equal(plan.member.payload.direct_request.command_id,plan.member.op_id);
});

async function restoredFixture() {
  const h=fixture(),first=await h.plan();await h.members.execute(first);
  const member=h.member(first);
  await h.members.applyChange(operation('archive-for-restore','list.archive','list',1,{membership_operations:[member.operation_id]}));
  assert.equal((await h.store.append(operation('restore-list','list.restore','list',2,{}))).accepted,true);
  return {h,first,member};
}
await test('restoration and original resume never replay Direct; explicit renewal creates fresh intent',async()=>{
  const {h,first,member}=await restoredFixture();h.applied=0; // manual Private after archive
  const before=h.direct().length;
  await assert.rejects(h.members.resume(member),/Direct setup refused/);
  assert.equal(h.direct().length,before);assert.equal(h.applied,0);
  const plan=await h.members.planRenewal(member);
  assert.equal(h.direct().length,before,'planning does not write permissions');
  assert.notEqual(plan.member.op_id,first.member.op_id);
  assert.equal(plan.member.entity_id,member.membership_id);assert.equal(plan.member.base_revision,member.revision);
  assert.equal(plan.binding.binding_id,first.binding.binding_id);
  assert.equal((await h.members.execute(plan)).state,'applied');
  assert.equal(h.member(plan).revision,2);assert.equal(h.applied,1);
  assert.equal(h.direct('cancel').length,1,'archive cancellation is reused');
  const writes=h.calls.filter(c=>c.body?.command_id===plan.member.op_id||c.body?.op_id===plan.member.op_id);
  assert.deepEqual(writes.filter(c=>c.method==='PUT').map(c=>c.body.phase||c.body.type),['prepare','member.set','commit']);
  await assert.rejects(h.members.resume(member),/assignment changed/);
});
await test('explicit renewal after manual Private gets a new command, not an old success replay',async()=>{
  const h=fixture(),first=await h.plan();await h.members.execute(first);h.applied=0;
  const old=h.member(first),plan=await h.members.planRenewal(old);
  assert.equal(h.applied,0);await h.members.execute(plan);
  assert.equal(h.applied,1);assert.notEqual(plan.member.op_id,old.operation_id);
  assert.equal(h.direct('cancel')[0].body.command_id,old.operation_id);
});
await test('renewal uses latest shared contact version while canceling original immutable scope',async()=>{
  const {h,member}=await restoredFixture();h.contact.contact_version='8';
  const plan=await h.members.planRenewal(member);
  assert.equal(plan.binding.contact_version,'8');assert.equal(plan.previous.direct_request.contact_version,'7');
  assert.equal(plan.setup[0].type,'binding.update');await h.members.execute(plan);
  assert.equal(h.direct('prepare').at(-1).body.contact_version,'8');
  assert.equal(h.direct('cancel')[0].body.contact_version,'7');
});
await test('renewal requires active membership, active list and currently shared same-generation account',async()=>{
  for(const kind of ['unshared','generation','archived','removed','stale']){
    const h=fixture(),first=await h.plan();await h.members.execute(first);const old=h.member(first),before=h.direct().length;
    if(kind==='unshared')h.contacts=[];
    if(kind==='generation')h.account.generation='other';
    if(kind==='archived')await h.store.append(operation('archived','list.archive','list',1,{membership_operations:[old.operation_id]}));
    if(kind==='removed')await h.store.append(removeOperation(old));
    const supplied=kind==='stale'?{...old,revision:0}:old;
    await assert.rejects(h.members.planRenewal(supplied),/shared|changed|active list/i);
    assert.equal(h.direct().length,before);
  }
});
await test('contact revocation after renewal planning cannot cancel or prepare permissions',async()=>{
  const {h,member}=await restoredFixture(),plan=await h.members.planRenewal(member),before=h.direct().length;
  h.contacts=[];await assert.rejects(h.members.execute(plan),/changed/);assert.equal(h.direct().length,before);
});
await test('two explicit renewal plans cannot both apply over the same membership revision',async()=>{
  const {h,member}=await restoredFixture(),one=await h.members.planRenewal(member),two=await h.members.planRenewal(member);
  await h.members.execute(one);const before=h.direct().length;
  await assert.rejects(h.members.execute(two),/assignment changed/);assert.equal(h.direct().length,before);
});
await test('lost renewal prepare reply retries the retained exact plan without another grant',async()=>{
  const {h,member}=await restoredFixture(),plan=await h.members.planRenewal(member);let lost=false;
  h.directHook=async(body,path)=>{if(body.command_id===plan.member.op_id&&body.phase==='prepare'&&!lost){lost=true;h.publish(body.command_id,{state:'prepared',phase:'prepare',ticket_id:'a'.repeat(64)});h.transactions.set(path,'$accepted-prepare');throw Error('lost renewal prepare');}};
  await assert.rejects(h.members.execute(plan),/lost renewal prepare/);
  assert.equal((await h.newMembers().execute(plan)).state,'applied');
  assert.equal(h.direct('prepare').filter(c=>c.body.command_id===plan.member.op_id).length,1);
  assert.equal(h.member(plan).revision,2);
});
await test('lost renewal commit reply resumes canonical member without reapplying after manual privacy',async()=>{
  const {h,member}=await restoredFixture(),plan=await h.members.planRenewal(member);let lost=false;
  h.directHook=async(body,path)=>{if(body.command_id===plan.member.op_id&&body.phase==='commit'&&!lost){lost=true;h.applied++;h.publish(body.command_id,{state:'applied',phase:'commit'});h.transactions.set(path,'$accepted-renewal');throw Error('lost renewal commit');}};
  await assert.rejects(h.members.execute(plan),/lost renewal commit/);h.applied=0;
  assert.equal((await h.newMembers().resume(h.member(plan))).state,'applied');assert.equal(h.applied,0);
  assert.equal(h.direct('commit').filter(c=>c.body.command_id===plan.member.op_id).length,1);
});
await test('renewal plan retains immutable current contact and membership context',async()=>{
  const {h,member}=await restoredFixture(),plan=await h.members.planRenewal(member);
  assert.throws(()=>{plan.binding.contact.network_id='foreign@example.invalid';},TypeError);
  assert.throws(()=>{plan.previous.direct_request.contact_version='9';},TypeError);
  h.contact.contact_version='9';assert.equal(plan.binding.contact_version,'7');
});

console.log(`crm_members: ${passed} passed, ${failed} failed`);
if(failed) process.exitCode=1;
