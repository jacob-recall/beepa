import assert from 'node:assert/strict';
import { createCRMBatches } from '../../apps/master/crm-batches.js';
import { createCRMStore } from '../../apps/master/crm-store.js';
import { canonicalCRMJSON, createCRMChildEnvelope, membershipKey, verifyReviewedBatch } from '../../shared/model/crm.js';

const ROOM='!crm:master', MANAGER='@manager:master', ELLIOT='@elliot:master', DAVID='@david:master';
const OP='com.jkali.crm.operation', PROPOSAL='com.jkali.proposal', REQUEST='com.jkali.crm.batch_request', RESULT='com.jkali.crm.batch_result', ITEM='com.jkali.crm.item';
const clone=value=>JSON.parse(JSON.stringify(value));
const operation=(id,type,entity,base,payload)=>({schema:1,op_id:id,workspace_id:ROOM,type,entity_id:entity,base_revision:base,payload});
const state=(type,content,key='',sender=ELLIOT)=>({type,state_key:key,sender,content});
const deferred=()=>{let resolve;const promise=new Promise(r=>resolve=r);return {promise,resolve};};

async function fixture() {
  const h={current:true,time:1800000000000,seq:0,calls:[],events:[],accounts:{},contacts:[],rooms:{},timeline:{},results:{},transactions:new Map(),roomHook:null,metadataHook:null,proposalHook:null,batchHook:null,pageHook:null};
  h.addEvent=body=>{const e={type:OP,sender:MANAGER,room_id:ROOM,event_id:'$metadata'+h.events.length,content:clone(body)};h.events.push(e);return e.event_id;};
  h.addEvent(operation('list-create','list.create','list',0,{name:'Central outreach'}));
  for(const [owner,label] of [[ELLIOT,'elliot'],[DAVID,'david']]) {
    const account={generation:'generation-'+label,journal_epoch:'epoch-'+label,proposal_room_id:'!proposal-'+label+':master'};
    h.accounts[owner]=account;h.timeline[account.proposal_room_id]=[];h.results[account.proposal_room_id]=[];
    h.rooms[account.proposal_room_id]=[
      state('m.room.create',{},'',owner),state('com.jkali.proposals',{},'',owner),
      state('com.jkali.crm.capability',{schema:1,owner_mxid:owner,...account,ordered_policy:true,direct_prepare_commit:true,direct_cancel:true,batch:true},'',owner),
      state('m.room.member',{membership:'join'},owner,owner),state('m.room.member',{membership:'join'},MANAGER,owner),
      state('m.room.power_levels',{users:{[owner]:100,[MANAGER]:50},events_default:100,state_default:100,events:{[PROPOSAL]:50,'com.jkali.crm.direct_request':50,[REQUEST]:50},invite:100,kick:100,ban:100,redact:100},'',owner),
      state('m.room.join_rules',{join_rule:'invite'},'',owner),state('m.room.history_visibility',{history_visibility:'shared'},'',owner),state('m.room.guest_access',{guest_access:'forbidden'},'',owner)];
  }
  h.seedPerson=(id,owner=ELLIOT,approval=false,source='imessage')=>{
    const account=h.accounts[owner],binding='binding-'+id,command='member-'+id,contact={source,network_id:id+'@example.invalid'};
    h.contacts.push({owner_mxid:owner,...contact,contact_version:'7'});
    h.addEvent(operation('person-'+id,'person.create',id,0,{label:id}));
    h.addEvent(operation('binding-op-'+id,'binding.create',binding,0,{person_id:id,owner_mxid:owner,generation:account.generation,contact,contact_version:'7'}));
    h.addEvent(operation(command,'member.set','membership-'+id,0,{list_id:'list',person_id:id,binding_id:binding,direct_request:{schema:1,command_id:command,membership_operation_id:command,binding_id:binding,owner_mxid:owner,...account,contact,contact_version:'7'}}));
    h.rooms[account.proposal_room_id].push(state('com.jkali.crm.direct_result',{schema:1,command_id:command,owner_mxid:owner,generation:account.generation,journal_epoch:account.journal_epoch,phase:approval?'prepare':'commit',state:approval?'approval_required':'applied',rooms:approval?[]:[{room_id:'!local-'+id+':local',state:'applied',revision:3}]},command,owner));
  };
  h.seedPerson('ada',ELLIOT);h.seedPerson('sam',DAVID);h.seedPerson('new',DAVID,true);
  h.roomFor=(binding,id)=>({sourceId:binding.contact.source,direct:true,label:id});
  h.publish=(body,extra={})=>{
    const account=h.accounts[body.owner_mxid];
    const previous=h.results[account.proposal_room_id].find(e=>e.state_key===body.child_id)?.content;
    const items=body.phase==='cancel'?(previous?.items||[]).map(i=>({...i,state:['accepted','waiting_for_approval'].includes(i.state)?'canceled':i.state})):body.items.map(i=>({item_id:i.item_id,state:i.route.mode==='direct_existing'?'accepted':'waiting_for_approval'}));
    h.results[account.proposal_room_id]=[state(RESULT,{schema:1,batch_id:body.batch_id,child_id:body.child_id,owner_mxid:body.owner_mxid,generation:body.generation,journal_epoch:body.journal_epoch,child_hash:body.child_hash,state:body.phase==='cancel'?'canceled':'accepted',items,...extra},body.child_id,body.owner_mxid)];
  };
  h.api=async(method,path,body)=>{
    const call={method,path,body:body&&clone(body)};h.calls.push(call);
    const room=decodeURIComponent(path.split('/rooms/')[1]?.split('/')[0]||'');
    if(method==='GET'&&path.includes('/messages?')) {
      if(h.pageHook) {const answer=await h.pageHook(room,path);if(answer)return answer;}
      const events=room===ROOM?h.events:h.timeline[room];assert.ok(events,'known history room');
      const offset=Number(new URL(path,'https://fixture.invalid').searchParams.get('from')||0),chunk=clone(events.slice(offset));
      return chunk.length?{chunk,end:String(events.length)}:{chunk:[]};
    }
    if(method==='GET') {
      assert.ok(h.rooms[room],'known account room');if(h.roomHook)await h.roomHook(room);
      return clone([...h.rooms[room],...h.results[room]]);
    }
    assert.equal(method,'PUT');
    if(path.includes('/send/'+OP+'/')) {
      if(h.metadataHook)await h.metadataHook(body,path);
      if(!h.transactions.has(path))h.transactions.set(path,h.addEvent(body));
      return {event_id:h.transactions.get(path)};
    }
    assert.ok(h.rooms[room],'known write room');
    if(path.includes('/send/'+PROPOSAL+'/')) {
      if(h.proposalHook)await h.proposalHook(body,path,room);
      if(!h.transactions.has(path)){const event_id='$proposal'+(++h.seq);h.transactions.set(path,event_id);h.timeline[room].push({event_id,type:PROPOSAL,sender:MANAGER,room_id:room,content:clone(body)});}
    } else {
      assert.ok(path.includes('/send/'+REQUEST+'/'),'only typed batch writes');
      if(h.batchHook)await h.batchHook(body,path,room);
      if(!h.transactions.has(path)){h.transactions.set(path,'$batch'+(++h.seq));h.publish(body);}
    }
    return {event_id:h.transactions.get(path)};
  };
  h.store=createCRMStore({api:h.api,bootstrap:async()=>({room_id:ROOM,manager_mxid:MANAGER,schema_version:1}),managerMxid:MANAGER,isCurrent:()=>h.current});
  h.newBatches=()=>createCRMBatches({store:h.store,api:h.api,managerMxid:MANAGER,accountFor:o=>h.accounts[o],contacts:()=>h.contacts,roomFor:(...args)=>h.roomFor(...args),isCurrent:()=>h.current,uuid:()=> 'generated-'+(++h.seq),now:()=>h.time});
  h.batches=h.newBatches();await h.store.refresh();
  h.review=(ids=['ada','sam','new'])=>h.batches.review({listId:'list',items:ids.map(personId=>({personId,body:' Hello '+personId+' ' }))});
  h.writes=type=>h.calls.filter(c=>c.method==='PUT'&&(!type||c.path.includes('/send/'+type+'/')));
  h.remove=async id=>{const member=h.store.snapshot().state.memberships[membershipKey('list',id)];h.addEvent(operation('remove-'+id,'member.remove',member.membership_id,member.revision,{list_id:'list',person_id:id}));await h.store.refresh();};
  return h;
}

let passed=0,failed=0;
async function test(name,fn){try{await fn();passed++;}catch(error){failed++;console.error('FAIL crm_batches:',name,'\n',error.stack);}}

await test('real model reviews immutable cross-account manifest without writes',async()=>{
  const h=await fixture(),m=await h.review();assert.equal(await verifyReviewedBatch(m),true);assert.equal(m.children.length,2);assert.equal(m.items.length,3);assert.equal(h.writes().length,0);assert.equal(m.items[0].body,'Hello ada');assert.ok(Object.isFrozen(m)&&Object.isFrozen(m.items[0].route));
  assert.equal(m.items.find(i=>i.person_id==='new').route.mode,'request_new_conversation_approval');
});
await test('durable canonical parent precedes exact proposals and isolated hashed child submissions',async()=>{
  const h=await fixture(),m=await h.review(),results=await h.batches.submit(m);
  assert.equal(h.writes()[0].body.type,'batch.review');assert.deepEqual(h.store.snapshot().state.batches[m.batch_id],m);
  assert.equal(h.writes(PROPOSAL).length,3);assert.equal(h.writes(REQUEST).length,2);assert.ok(results.every(r=>r.state==='accepted'));
  for(const request of h.writes(REQUEST)) {
    const child=await createCRMChildEnvelope(m,request.body.child_id);assert.equal(request.body.child_hash,child.child_hash);
    assert.ok(request.body.items.every(i=>i.owner_mxid===child.owner_mxid));
    assert.ok(request.path.includes(encodeURIComponent(h.accounts[child.owner_mxid].proposal_room_id)));
    for(const item of request.body.items){const staged=h.writes(PROPOSAL).find(c=>c.body[ITEM].item_id===item.item_id);assert.equal(staged.body.body,item.body);assert.equal(staged.body[ITEM].child_hash,child.child_hash);assert.equal(staged.body[ITEM].payload_hash,m.payload_hash);assert.ok(h.calls.indexOf(staged)<h.calls.indexOf(request));assert.match(item.proposal_event_id,/^\$/);if(item.route.mode==='direct_existing'){assert.equal(staged.body.target_room,item.route.room_id);assert.equal(staged.body.target_identifier,undefined);}else{assert.equal(staged.body.target_identifier,item.route.network_id);assert.equal(staged.body.target_room,undefined);}}
  }
  assert.equal(results.flatMap(r=>r.items).filter(i=>i.state==='waiting_for_approval').length,1);
  assert.ok(results.flatMap(r=>r.items).every(i=>!['sent','delivered','read'].includes(i.state)));
});
await test('metadata failure never stages or submits anything',async()=>{const h=await fixture(),m=await h.review();h.metadataHook=async()=>{throw Error('metadata offline');};await assert.rejects(h.batches.submit(m),/metadata offline/);assert.equal(h.writes(PROPOSAL).length+h.writes(REQUEST).length,0);});
await test('tampered review hash cannot persist or stage',async()=>{const h=await fixture(),m=clone(await h.review());m.items[0].body='changed';await assert.rejects(h.batches.submit(m),/invalid-manifest-hash/);assert.equal(h.writes().length,0);});
await test('lost accepted parent response is reconciled before first proposal',async()=>{const h=await fixture(),m=await h.review();let lost=false;h.metadataHook=async body=>{if(!lost){lost=true;h.addEvent(body);throw Error('lost parent');}};await assert.rejects(h.batches.submit(m),/lost parent/);assert.equal(h.writes(PROPOSAL).length,0);await h.batches.submit(m);assert.equal(h.writes(OP).length,1);assert.equal(h.writes(PROPOSAL).length,3);});
await test('lost staged proposal reply reuses exact canonical event and never restages it',async()=>{
  const h=await fixture(),m=await h.review(['ada']);let lost=false;
  h.proposalHook=async(body,path,room)=>{if(!lost){lost=true;h.transactions.set(path,'$accepted-proposal');h.timeline[room].push({event_id:'$accepted-proposal',sender:MANAGER,type:PROPOSAL,content:clone(body)});throw Error('lost stage');}};
  assert.equal((await h.batches.submit(m))[0].state,'not_confirmed');assert.equal(h.writes(REQUEST).length,0);
  await h.newBatches().submit(m);assert.equal(h.writes(PROPOSAL).length,1);assert.equal(h.writes(REQUEST)[0].body.items[0].proposal_event_id,'$accepted-proposal');
});
await test('lost accepted child response reopens without another source submit',async()=>{const h=await fixture(),m=await h.review(['ada']);h.batchHook=async(body,path)=>{h.publish(body);h.transactions.set(path,'$accepted');throw Error('lost child');};assert.equal((await h.batches.submit(m))[0].state,'not_confirmed');assert.equal((await h.newBatches().submit(m))[0].state,'accepted');assert.equal(h.writes(REQUEST).length,1);assert.equal(h.writes(PROPOSAL).length,1);});
await test('transport failure before child acceptance retries exact request body and transaction',async()=>{const h=await fixture(),m=await h.review(['ada']);let failed=false;h.batchHook=async()=>{if(!failed){failed=true;throw Error('not accepted');}};await h.batches.submit(m);await h.batches.submit(m);assert.equal(h.writes(REQUEST).length,2);assert.deepEqual(h.writes(REQUEST)[0],h.writes(REQUEST)[1]);assert.equal(h.writes(PROPOSAL).length,1);});
await test('offline Elliot child does not prevent David acceptance',async()=>{const h=await fixture(),m=await h.review();h.roomHook=async room=>{if(room===h.accounts[ELLIOT].proposal_room_id)throw Error('offline Elliot');};const results=await h.batches.submit(m);assert.equal(results[0].state,'not_confirmed');assert.equal(results[1].state,'accepted');assert.equal(h.writes(REQUEST).length,1);assert.equal(h.writes(REQUEST)[0].body.owner_mxid,DAVID);});
await test('missing source acknowledgment is awaiting, never sent or delivered',async()=>{const h=await fixture(),m=await h.review(['ada']);h.batchHook=async(body,path)=>h.transactions.set(path,'$stored-but-pending');const r=(await h.batches.submit(m))[0];assert.deepEqual(r,{child_id:m.children[0].child_id,state:'awaiting_acknowledgment',items:[]});});

for(const [name,mutate] of [
  ['null item',e=>e.content.items[0]=null],['non-text reason',e=>e.content.items[0].reason={}],['sent invalid room',e=>Object.assign(e.content.items[0],{state:'sent',sent_event_id:'$valid',sent_room_id:'foreign'})],
  ['missing item state',e=>delete e.content.items[0].state],['non-text item state',e=>e.content.items[0].state={}],['unknown item state',e=>e.content.items[0].state='magically_delivered'],['sent without exact event',e=>e.content.items[0].state='sent'],
  ['author',e=>e.sender=DAVID],['owner',e=>e.content.owner_mxid=DAVID],['generation',e=>e.content.generation='other'],['epoch',e=>e.content.journal_epoch='other'],['hash',e=>e.content.child_hash='0'.repeat(64)],['batch ID',e=>e.content.batch_id='other'],['duplicate items',e=>e.content.items.push(clone(e.content.items[0]))],['foreign item',e=>e.content.items[0].item_id='foreign'],['missing items',e=>e.content.items=[]],
])await test('reject mismatched source acknowledgment '+name,async()=>{const h=await fixture(),m=await h.review(['ada']);await h.batches.submit(m);mutate(h.results[h.accounts[ELLIOT].proposal_room_id][0]);const r=(await h.batches.statuses(m))[0];assert.equal(r.state,'unavailable');assert.equal(r.items.length,0);});

for(const [name,mutate] of [
  ['unshared contact',h=>h.contacts=[]],['account generation',h=>h.accounts[ELLIOT].generation='changed'],['source batch capability',h=>h.rooms[h.accounts[ELLIOT].proposal_room_id].find(e=>e.type==='com.jkali.crm.capability').content.batch=false],['foreign former member',h=>h.rooms[h.accounts[ELLIOT].proposal_room_id].push(state('m.room.member',{membership:'leave'},'@foreign:master'))],
])await test('review blocks '+name,async()=>{const h=await fixture();mutate(h);await assert.rejects(h.review(['ada']));assert.equal(h.writes().length,0);});

await test('review rejects empty/duplicate/invalid bodies without persistence',async()=>{for(const items of [[],[{personId:'ada',body:''}],[{personId:'ada',body:'x'},{personId:'ada',body:'y'}],[{personId:'ada',body:'x'.repeat(8001)}]]){const h=await fixture();await assert.rejects(h.batches.review({listId:'list',items}));assert.equal(h.writes().length,0);}});
await test('multiple Direct conversations require an explicit chosen route',async()=>{const h=await fixture(),result=h.rooms[h.accounts[ELLIOT].proposal_room_id].find(e=>e.type==='com.jkali.crm.direct_result');result.content.rooms.push({room_id:'!second:local',state:'applied',revision:4});await assert.rejects(h.review(['ada']),/Choose/);const routes=await h.batches.routes('list','ada');const m=await h.batches.review({listId:'list',items:[{personId:'ada',body:'x',route:routes[1].route}]});assert.equal(m.items[0].route.room_id,'!second:local');});
await test('archived or removed list membership cannot be reviewed',async()=>{const h=await fixture();await h.remove('ada');await assert.rejects(h.review(['ada']),/unavailable/);h.addEvent(operation('archive','list.archive','list',1,{}));await assert.rejects(h.review(['sam']),/unavailable/);});
await test('revoked contact after review cannot stage a proposal',async()=>{const h=await fixture(),m=await h.review(['ada']);h.contacts=[];const r=await h.batches.submit(m);assert.equal(r[0].state,'not_confirmed');assert.equal(h.writes(PROPOSAL).length,0);});
await test('list change after review rejects parent before source writes',async()=>{const h=await fixture(),m=await h.review(['ada']);await h.remove('ada');await assert.rejects(h.batches.submit(m),/binding-unavailable|revision-conflict/);assert.equal(h.writes(PROPOSAL).length,0);});
await test('expired review cannot stage while accepted prior child remains reconcilable',async()=>{const h=await fixture(),m=await h.review(['ada']);h.time+=600001;assert.equal((await h.batches.submit(m))[0].state,'not_confirmed');assert.equal(h.writes(PROPOSAL).length,0);});
await test('session invalidation during account point-read aborts before source writes',async()=>{const h=await fixture(),m=await h.review(['ada']),gate=deferred(),reached=deferred();h.roomHook=async()=>{reached.resolve();await gate.promise;};const pending=h.batches.submit(m);await reached.promise;h.current=false;gate.resolve();await assert.rejects(pending,/session changed/);assert.equal(h.writes(PROPOSAL).length+h.writes(REQUEST).length,0);});
await test('foreign staged event never substitutes for exact manager proposal',async()=>{const h=await fixture(),m=await h.review(['ada']);h.proposalHook=async(body,path,room)=>{h.timeline[room].push({event_id:'$foreign',sender:DAVID,type:PROPOSAL,content:clone(body)});h.proposalHook=null;throw Error('not accepted');};await h.batches.submit(m);await h.batches.submit(m);assert.notEqual(h.writes(REQUEST)[0].body.items[0].proposal_event_id,'$foreign');});
await test('staged pagination loop cannot submit incomplete history',async()=>{const h=await fixture(),m=await h.review(['ada']);let page=0;h.pageHook=async room=>room===ROOM?null:{chunk:[],end:++page%2?'a':'b'};const result=await h.batches.submit(m);assert.equal(result[0].state,'not_confirmed');assert.equal(h.writes(PROPOSAL).length+h.writes(REQUEST).length,0);});
await test('proposal retries before acceptance preserve original transaction and content',async()=>{const h=await fixture(),m=await h.review(['ada']);let failed=false;h.proposalHook=async()=>{if(!failed){failed=true;throw Error('pre acceptance');}};await h.batches.submit(m);await h.batches.submit(m);assert.equal(h.writes(PROPOSAL).length,2);assert.deepEqual(h.writes(PROPOSAL)[0],h.writes(PROPOSAL)[1]);});
await test('reopening a source-accepted child does not stage or submit again',async()=>{const h=await fixture(),m=await h.review(['ada']);await h.batches.submit(m);const counts=[h.writes(OP).length,h.writes(PROPOSAL).length,h.writes(REQUEST).length];h.time+=700000;assert.equal((await h.newBatches().submit(m))[0].state,'accepted');assert.deepEqual([h.writes(OP).length,h.writes(PROPOSAL).length,h.writes(REQUEST).length],counts);});
await test('duplicate canonical source acknowledgments fail closed',async()=>{const h=await fixture(),m=await h.review(['ada']);await h.batches.submit(m);const events=h.results[h.accounts[ELLIOT].proposal_room_id];events.push(clone(events[0]));assert.equal((await h.batches.statuses(m))[0].state,'unavailable');});
await test('staged history from a foreign room cannot supply a child event reference',async()=>{
  const h=await fixture(),m=await h.review(['ada']);let lost=false;
  h.proposalHook=async(body,path,room)=>{if(!lost){lost=true;h.timeline[room].push({event_id:'$foreign-room',sender:MANAGER,type:PROPOSAL,room_id:'!foreign:master',content:clone(body)});throw Error('not accepted');}};
  await h.batches.submit(m);await h.batches.submit(m);
  assert.ok(h.writes(REQUEST).every(c=>c.body.items.every(i=>i.proposal_event_id!=='$foreign-room')));
});
await test('one Matrix event ID cannot stand for two differently personalized staged messages',async()=>{
  const h=await fixture();h.seedPerson('bea',ELLIOT);await h.store.refresh();const m=await h.review(['ada','bea']);
  h.proposalHook=async(body,path,room)=>{h.transactions.set(path,'$same-event');h.timeline[room].push({event_id:'$same-event',sender:MANAGER,type:PROPOSAL,room_id:room,content:clone(body)});};
  const r=await h.batches.submit(m);assert.equal(r[0].state,'not_confirmed');assert.equal(h.writes(REQUEST).length,0);
});
await test('caller mutation during parent persistence cannot replace reviewed body with a different valid manifest',async()=>{
  const h=await fixture(),m=clone(await h.review(['ada']));
  h.metadataHook=async()=>{m.items[0].body='Changed after persistence began';const {payload_hash,...payload}=m;m.payload_hash=Buffer.from(await crypto.subtle.digest('SHA-256',new TextEncoder().encode(canonicalCRMJSON(payload)))).toString('hex');};
  await h.batches.submit(m);
  const stored=h.store.snapshot().state.batches[m.batch_id];
  assert.ok(h.writes(PROPOSAL).every(c=>c.body.body===stored.items[0].body));
});

// Final fresh account proof is an await boundary: metadata can change there.
await test('membership removal during final account verification cannot submit a child',async()=>{
  const h=await fixture(),m=await h.review(['ada']);let removed=false;
  h.roomHook=async()=>{if(h.writes(PROPOSAL).length&&!removed){removed=true;await h.remove('ada');}};
  const results=await h.batches.submit(m);assert.equal(results[0].state,'not_confirmed');assert.equal(h.writes(REQUEST).length,0);
});
await test('membership removal during per-item account verification cannot stage a proposal',async()=>{
  const h=await fixture(),m=await h.review(['ada']);let reads=0;
  h.roomHook=async()=>{if(++reads===2)await h.remove('ada');};
  const results=await h.batches.submit(m);assert.equal(results[0].state,'not_confirmed');assert.equal(h.writes(PROPOSAL).length,0);
});

await test('cancellation is canonical before exact frozen source cancel requests',async()=>{
  const h=await fixture(),m=await h.review();await h.batches.submit(m);const r=await h.batches.cancel(m);
  assert.ok(r.every(c=>c.state==='canceled'));const writes=h.writes(),intent=writes.findIndex(c=>c.body.type==='batch.cancel_requested');assert.ok(intent>=0);
  for(const c of h.writes(REQUEST).filter(c=>c.body.phase==='cancel')){assert.ok(writes.indexOf(c)>intent);assert.deepEqual(Object.keys(c.body).sort(),['schema','phase','owner_mxid','generation','journal_epoch','batch_id','child_id','child_hash'].sort());assert.equal(c.body.child_hash,(await createCRMChildEnvelope(m,c.body.child_id)).child_hash);}
  assert.ok(h.store.snapshot().state.batch_cancellations[m.batch_id]);
});
await test('cancellation still reaches original account after contact revocation, archive, expiry and stale discovery',async()=>{
  const h=await fixture(),m=await h.review(['ada']);await h.batches.submit(m);h.contacts=[];h.accounts[ELLIOT]={generation:'replacement',journal_epoch:'replacement',proposal_room_id:'!replacement:master'};h.time+=700000;h.addEvent(operation('archive','list.archive','list',1,{}));
  // Fake source publication deliberately continues on the original room.
  h.batchHook=async(body,path)=>{if(body.phase==='cancel'){h.results[m.children[0].proposal_room_id]=[state(RESULT,{schema:1,...body,state:'canceled',items:[]},body.child_id,ELLIOT)];h.transactions.set(path,'$cancel');}};
  const r=await h.batches.cancel(m);assert.equal(r[0].state,'canceled');assert.ok(h.writes(REQUEST).at(-1).path.includes(encodeURIComponent(m.children[0].proposal_room_id)));
});
await test('failed canonical cancellation intent never sends a source cancel',async()=>{const h=await fixture(),m=await h.review(['ada']);await h.batches.submit(m);h.metadataHook=async body=>{if(body.type==='batch.cancel_requested')throw Error('cancel metadata offline');};await assert.rejects(h.batches.cancel(m),/metadata offline/);assert.equal(h.writes(REQUEST).filter(c=>c.body.phase==='cancel').length,0);});
await test('lost canonical cancellation reply resumes intent without another metadata write',async()=>{const h=await fixture(),m=await h.review(['ada']);await h.batches.submit(m);let lost=false;h.metadataHook=async body=>{if(body.type==='batch.cancel_requested'&&!lost){lost=true;h.addEvent(body);throw Error('lost intent');}};await assert.rejects(h.batches.cancel(m),/lost intent/);assert.equal(h.writes(REQUEST).filter(c=>c.body.phase==='cancel').length,0);assert.equal((await h.newBatches().cancel(m))[0].state,'canceled');assert.equal(h.writes(OP).filter(c=>c.body.type==='batch.cancel_requested').length,1);});
await test('lost source cancellation response resumes exact acknowledgment without repeating cancel',async()=>{const h=await fixture(),m=await h.review(['ada']);await h.batches.submit(m);h.batchHook=async(body,path)=>{if(body.phase==='cancel'){h.publish(body);h.transactions.set(path,'$accepted-cancel');throw Error('lost cancel');}};assert.equal((await h.batches.cancel(m))[0].state,'cancellation_unconfirmed');assert.equal((await h.newBatches().cancel(m))[0].state,'canceled');assert.equal(h.writes(REQUEST).filter(c=>c.body.phase==='cancel').length,1);});
await test('unacknowledged cancellation preserves already sent item and remains visibly pending',async()=>{
  const h=await fixture(),m=await h.review(['ada']);await h.batches.submit(m);const result=h.results[h.accounts[ELLIOT].proposal_room_id][0].content;result.items[0].state='sent';result.items[0].sent_event_id='$sent';result.items[0].sent_room_id='!local-ada:local';
  h.batchHook=async(body,path)=>{if(body.phase==='cancel')h.transactions.set(path,'$stored-no-result');};
  const r=(await h.batches.cancel(m))[0];assert.equal(r.state,'cancellation_requested');assert.equal(r.items[0].state,'sent');assert.equal(r.items[0].sent_event_id,'$sent');
});
await test('cancel tombstone before child acceptance prevents later submit from resurrecting queue',async()=>{
  const h=await fixture(),m=await h.review(['ada']);h.batchHook=async()=>{throw Error('not yet accepted');};await h.batches.submit(m);h.batchHook=null;assert.equal((await h.batches.cancel(m))[0].state,'canceled');const submits=h.writes(REQUEST).filter(c=>c.body.phase==='submit').length;assert.equal((await h.newBatches().submit(m))[0].state,'canceled');assert.equal(h.writes(REQUEST).filter(c=>c.body.phase==='submit').length,submits);
});
await test('cancellation of one offline account cannot hide the other source result',async()=>{const h=await fixture(),m=await h.review();await h.batches.submit(m);h.roomHook=async room=>{if(room===h.accounts[ELLIOT].proposal_room_id)throw Error('Elliot offline');};const r=await h.batches.cancel(m);assert.equal(r[0].state,'cancellation_unconfirmed');assert.equal(r[1].state,'canceled');});
await test('changed source capability generation refuses cancellation through new account authority',async()=>{const h=await fixture(),m=await h.review(['ada']);await h.batches.submit(m);h.rooms[h.accounts[ELLIOT].proposal_room_id].find(e=>e.type==='com.jkali.crm.capability').content.generation='replacement';assert.equal((await h.batches.cancel(m))[0].state,'cancellation_unconfirmed');assert.equal(h.writes(REQUEST).filter(c=>c.body.phase==='cancel').length,0);});
await test('mutable cancellation input cannot change original reviewed source after await',async()=>{const h=await fixture(),m=clone(await h.review(['ada']));await h.batches.submit(m);const hash=m.payload_hash;h.metadataHook=async body=>{if(body.type==='batch.cancel_requested'){m.children[0].proposal_room_id='!foreign:master';m.payload_hash='0'.repeat(64);}};const r=await h.batches.cancel(m);assert.equal(r[0].state,'canceled');const intent=h.writes(OP).find(c=>c.body.type==='batch.cancel_requested');assert.equal(intent.body.payload.payload_hash,hash);assert.equal(h.writes(REQUEST).some(c=>c.path.includes('foreign')),false);});
await test('cancellation committed during proposal authority read stops that proposal',async()=>{const h=await fixture(),m=await h.review(['ada']);let reads=0;h.roomHook=async()=>{if(++reads===2){h.addEvent(operation('other-tab-cancel','batch.cancel_requested',m.batch_id,0,{payload_hash:m.payload_hash}));await h.store.refresh();}};assert.equal((await h.batches.submit(m))[0].state,'not_confirmed');assert.equal(h.writes(PROPOSAL).length,0);});
await test('session changes during cancellation read never emit a source request',async()=>{const h=await fixture(),m=await h.review(['ada']);await h.batches.submit(m);h.roomHook=async()=>{h.current=false;};await assert.rejects(h.batches.cancel(m),/session changed/);assert.equal(h.writes(REQUEST).filter(c=>c.body.phase==='cancel').length,0);});

await test('authenticated source rejection remains explicit and prevents resubmission',async()=>{
  const h=await fixture(),m=await h.review(['ada']);
  h.batchHook=async(body,path)=>{h.publish(body,{state:'rejected',reason:'review-expired',items:[]});h.transactions.set(path,'$rejected');};
  const first=(await h.batches.submit(m))[0];assert.equal(first.state,'rejected');assert.equal(first.reason,'review-expired');
  const count=h.writes(REQUEST).length;const retry=(await h.newBatches().submit(m))[0];
  assert.equal(retry.state,'rejected');assert.equal(h.writes(REQUEST).length,count);assert.equal((await h.batches.statuses(m))[0].reason,'review-expired');
});
await test('malformed or forged source rejection is not a confirmed batch refusal',async()=>{
  for(const change of [{reason:''},{items:[{item_id:'unexpected',state:'sent'}]},{owner_mxid:DAVID}]){
    const h=await fixture(),m=await h.review(['ada']);
    h.batchHook=async(body,path)=>{h.publish(body,{state:'rejected',reason:'refused',items:[],...change});h.transactions.set(path,'$bad-rejection');};
    assert.equal((await h.batches.submit(m))[0].state,'not_confirmed');
  }
});

await test('a definitive rejection arriving during cancellation is not reported as pending',async()=>{
  const h=await fixture(),m=await h.review(['ada']);
  h.batchHook=async(body,path)=>{h.transactions.set(path,'$pending');};
  await h.batches.submit(m);
  h.batchHook=async(body,path)=>{if(body.phase==='cancel'){h.publish(body,{state:'rejected',reason:'review-expired',items:[]});h.transactions.set(path,'$cancel-ack');}};
  const result=(await h.batches.cancel(m))[0];
  assert.equal(result.state,'rejected');assert.equal(result.reason,'review-expired');
});

await test('new shared catalog version updates metadata without replaying Direct permission',async()=>{
  const h=await fixture(),before=clone(h.store.snapshot().state.memberships['list/ada'].direct_request);
  h.contacts[0].contact_version='8';const m=await h.review(['ada']);
  assert.equal(m.items[0].contact_version,'8');assert.equal(h.store.snapshot().state.bindings[m.items[0].binding_id].contact_version,'8');
  assert.deepEqual(h.store.snapshot().state.memberships['list/ada'].direct_request,before);
  assert.deepEqual(h.writes().map(c=>c.body.type),['binding.update']);
  assert.equal(h.writes(PROPOSAL).length+h.writes(REQUEST).length,0);
});
await test('contact catalog changes after frozen review still prevent proposal staging',async()=>{
  const h=await fixture(),m=await h.review(['ada']);h.contacts[0].contact_version='8';
  assert.equal((await h.batches.submit(m))[0].state,'not_confirmed');assert.equal(h.writes(PROPOSAL).length,0);
  assert.equal(h.writes(REQUEST).length,0);assert.equal(m.items[0].contact_version,'7');
});
await test('revocation or relink during version refresh account proof cannot update binding or review',async()=>{
  for(const kind of ['revoked','relinked','new-version']){
    const h=await fixture();h.contacts[0].contact_version='8';let changed=false;
    h.roomHook=async()=>{if(changed)return;changed=true;if(kind==='revoked')h.contacts=[];else if(kind==='relinked')h.accounts[ELLIOT].generation='replacement';else h.contacts[0].contact_version='9';};
    await assert.rejects(h.review(['ada']),/changed|update|not ready/);assert.equal(h.writes().length,0);
  }
});
await test('canonical membership removal during version refresh blocks stale metadata mutation',async()=>{
  const h=await fixture();h.contacts[0].contact_version='8';let changed=false;
  h.roomHook=async()=>{if(!changed){changed=true;await h.remove('ada');}};
  await assert.rejects(h.review(['ada']),/unavailable|changed/);assert.equal(h.writes().length,0);
});
await test('ambiguous duplicate shared identity cannot choose a catalog version arbitrarily',async()=>{
  const h=await fixture();h.contacts.push({...h.contacts[0],contact_version:'8'});
  await assert.rejects(h.review(['ada']),/uniquely shared/);assert.equal(h.writes().length,0);
});
await test('contact revoked during metadata acknowledgment cannot produce an eligible frozen review',async()=>{
  const h=await fixture();h.contacts[0].contact_version='8';h.metadataHook=async body=>{if(body.type==='binding.update')h.contacts=[];};
  await assert.rejects(h.review(['ada']),/shared|changed/);assert.equal(h.writes(PROPOSAL).length+h.writes(REQUEST).length,0);
});
await test('canonical binding race during version refresh cannot overwrite a newer binding',async()=>{
  const h=await fixture();h.contacts[0].contact_version='8';let changed=false;
  h.metadataHook=async body=>{if(body.type==='binding.update'&&!changed){changed=true;h.addEvent(operation('concurrent-binding-version','binding.update',body.entity_id,body.base_revision,{contact_version:'9',availability:'shared'}));}};
  await assert.rejects(h.review(['ada']),/conflict/);assert.equal(h.store.snapshot().state.bindings['binding-ada'].contact_version,'9');
  assert.equal(h.writes(PROPOSAL).length+h.writes(REQUEST).length,0);
});
await test('one history refresh reuses verified account state without mixing separate child results',async()=>{
  const h=await fixture(),a=await h.review(['ada']);await h.batches.submit(a);const first=clone(h.results[h.accounts[ELLIOT].proposal_room_id]);
  const b=await h.review(['ada']);await h.batches.submit(b);h.results[h.accounts[ELLIOT].proposal_room_id].push(...first);
  h.calls=[];const reads=new Map(),ra=await h.batches.statuses(a,reads),rb=await h.batches.statuses(b,reads);
  assert.equal(ra[0].child_id,a.children[0].child_id);assert.equal(rb[0].child_id,b.children[0].child_id);
  assert.notEqual(ra[0].child_id,rb[0].child_id);assert.equal(ra[0].state,'accepted');assert.equal(rb[0].state,'accepted');
  assert.equal(h.calls.filter(c=>c.method==='GET'&&c.path.endsWith('/state')).length,1);assert.equal(h.writes().length,0);
});
await test('shared history account-read cache isolates owners and refresh cycles',async()=>{
  const h=await fixture(),m=await h.review();await h.batches.submit(m);h.calls=[];
  const reads=new Map();await h.batches.statuses(m,reads);await h.batches.statuses(m,reads);
  const first=h.calls.filter(c=>c.method==='GET'&&c.path.endsWith('/state'));
  assert.equal(first.length,2);assert.equal(new Set(first.map(c=>c.path)).size,2);
  await h.batches.statuses(m,new Map());assert.equal(h.calls.filter(c=>c.method==='GET'&&c.path.endsWith('/state')).length,4);
});

await test('existing non-iMessage Direct conversation remains reviewable and stages exact room',async()=>{
  const h=await fixture();h.seedPerson('wa',ELLIOT,false,'whatsapp');await h.store.refresh();
  const m=await h.review(['wa']);assert.equal(m.items[0].route.source,'whatsapp');assert.equal(m.items[0].route.mode,'direct_existing');
  await h.batches.submit(m);assert.equal(h.writes(PROPOSAL)[0].body.target_room,'!local-wa:local');assert.equal(h.writes(PROPOSAL)[0].body.target_identifier,undefined);
});
await test('non-iMessage identifier-only new chat is explicitly excluded before review or staging',async()=>{
  const h=await fixture();h.seedPerson('wa-new',ELLIOT,true,'whatsapp');await h.store.refresh();
  await assert.rejects(h.batches.routes('list','wa-new'),/Starting a new whatsapp conversation from CRM is not supported/);
  await assert.rejects(h.review(['wa-new']),/Starting a new whatsapp conversation from CRM is not supported/);
  assert.equal(h.writes().length,0);assert.equal(Object.keys(h.store.snapshot().state.batches).length,0);
});

await test('unresolved personalized placeholders cannot create a reviewed batch or source writes',async()=>{
  const h=await fixture();await assert.rejects(h.batches.review({listId:'list',items:[{personId:'ada',body:'Hello {{first_name}}'}]}),/Fill every.*placeholder/);
  assert.equal(h.writes().length,0);assert.equal(Object.keys(h.store.snapshot().state.batches).length,0);
});
console.log(`crm_batches: ${passed} independent cases passed; ${failed} failed`);
if(failed)process.exitCode=1;
