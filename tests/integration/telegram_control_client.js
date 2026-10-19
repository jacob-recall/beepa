// Only generated disposable Synapse ports and credentials, supplied on stdin.
import fs from 'node:fs';
import assert from 'node:assert/strict';
import {ControlDomain} from '../../master/control-domain.mjs';
import {ControlCore} from '../../master/control-core.mjs';
import {createControlService} from '../../master/telegram-service.mjs';
import {notificationIdentity} from '../../master/notification-routing.mjs';
const c=JSON.parse(fs.readFileSync(0,'utf8'));
for(const base of [c.base,c.enrollBase]){const u=new URL(base);assert.equal(u.hostname,'127.0.0.1');assert(![8008,8011,8018,8019,8023].includes(Number(u.port)));assert(Number(u.port)>1024);}
async function request(base,method,path,data,token=c.token){const r=await fetch(base+path,{method,headers:{Authorization:'Bearer '+token,'Content-Type':'application/json'},...(data===undefined?{}:{body:JSON.stringify(data)})});assert.equal(r.status,200,method+' '+path);return r.json();}
let cancelResponder=false;const canceledMemberships=[];
const api=async(method,path,data)=>{
 const result=await request(c.base,method,path,data);
 if(cancelResponder&&method==='PUT'&&path.includes('/send/com.jkali.crm.direct_request/')&&data.phase==='cancel'){
  canceledMemberships.push(data);
  await request(c.base,'PUT','/_matrix/client/v3/rooms/'+encodeURIComponent(c.proposals)+'/state/com.jkali.crm.direct_result/'+encodeURIComponent(data.command_id),
   {schema:1,command_id:data.command_id,owner_mxid:c.owner,generation:data.generation,journal_epoch:data.journal_epoch,phase:'cancel',state:'canceled',safe_to_change_membership:true},c.ownerToken);
 }
 return result;
};
const domain=new ControlDomain({api,bootstrap:()=>request(c.enrollBase,'POST','/admin/crm-room',{}),manager:c.manager,roster:async()=>({[c.owner]:c.space}),syncDiscovery:true});
const delivered=[];const core=new ControlCore({path:':memory:',domain,telegram:async(method,data)=>{delivered.push(data);return {message_id:delivered.length};},model:async()=>{throw Error('Model not needed');}});
core.set('operator',{user:1,chat:1});await domain.refresh();assert(domain.rooms.has(c.room));
// Health is owned by the source account, not inferred from mirror traffic.
const pingNow=Date.now(),pingHealth={connected:true,updated_at:pingNow,last_ingestion:pingNow,last_delivery:pingNow-86400000,pending_events:0,receipts_pending:0,stage_errors:[],imessage:{updated_at:pingNow,poll_ok:true,inbound_pending:0}};
await request(c.base,'PUT','/_matrix/client/v3/rooms/'+encodeURIComponent(c.space)+'/state/com.jkali.uplink_health/',pingHealth,c.ownerToken);
await domain.refresh();let ping=await core.tool('account_health',{});
assert.equal(ping.accounts[0].master.status,'ok');assert.equal(ping.accounts[0].imessage.status,'ok');
await request(c.base,'PUT','/_matrix/client/v3/rooms/'+encodeURIComponent(c.space)+'/state/com.jkali.uplink_health/',{...pingHealth,updated_at:pingNow-600000},c.ownerToken);
await domain.refresh();assert.equal((await core.ping()).accounts[0].master.status,'stale');
await request(c.base,'PUT','/_matrix/client/v3/rooms/'+encodeURIComponent(c.space)+'/state/com.jkali.uplink_health/',pingHealth,c.ownerToken);
await domain.refresh();
await domain.refresh();assert(domain.discoveryStatus().cache_hits>0,'unchanged real Synapse state is reused after a fresh sync');
await request(c.base,'PUT','/_matrix/client/v3/rooms/'+encodeURIComponent(c.room)+'/state/m.room.name/',{name:'Changed via state-only delta'},c.ownerToken);
await domain.refresh();assert.equal(domain.room(c.room).title,'Changed via state-only delta');assert(domain.discoveryStatus().state_reads>0,'a state-only sync change invalidates the full-state cache');
let draft=await core.tool('prepare_list_change',{operation:'create',name:'Integration monitoring'});await core.confirm(draft.id);const list_id=draft.operation.entity_id;
draft=await core.tool('prepare_list_change',{operation:'watch',list_id,room_id:c.room});await core.confirm(draft.id);
const scheduleNow=Date.now(),minute=new Date(scheduleNow).getUTCHours()*60+new Date(scheduleNow).getUTCMinutes();
const quiet={timezone:'UTC',mode:'quiet',days:[1,2,3,4,5,6,7],start_minute:minute,end_minute:(minute+1)%1440};
draft=await core.tool('prepare_list_change',{operation:'monitoring',list_id,enabled:true,channels:['telegram','browser'],schedule:quiet,repeat_ms:null,presentation:{mode:'digest',interval_ms:60000}});await core.confirm(draft.id);core.clock=()=>scheduleNow;
await core.monitor();await core.flush();assert.equal(delivered.length,0,'skip backlog');
const scopePage=await api('GET','/_matrix/client/v3/rooms/'+encodeURIComponent(c.proposals)+'/messages?dir=b&limit=1&filter='+encodeURIComponent(JSON.stringify({types:['com.jkali.monitoring_scope']})));
assert.equal(scopePage.chunk[0].sender,c.manager);assert.deepEqual(scopePage.chunk[0].content.rooms,['!native:fixture']);
const path='/_matrix/client/v3/rooms/'+encodeURIComponent(c.room);
let incoming=await request(c.base,'PUT',path+'/send/m.room.message/control-incoming',{msgtype:'m.text',body:'Synthetic new reply','com.jkali.from_me':false},c.ownerToken);
await core.monitor();await core.flush();assert.equal(delivered.length,0,'canonical quiet hours defer the durable event');assert.equal(core.status().pending,1);
core.clock=()=>scheduleNow+60000;await core.flush();assert.equal(delivered.length,1);assert.equal(core.ref(1,1).event,incoming.event_id);core.clock=Date.now;
await core.monitor();await core.flush();assert.equal(delivered.length,1,'no repeated notification');
assert.equal((await domain.history(c.room)).messages.at(-1).text,'Synthetic new reply');
core.clock=()=>scheduleNow+120000;
for(let i=0;i<2;i++)incoming=await request(c.base,'PUT',path+'/send/m.room.message/control-group-'+i,{msgtype:'m.text',body:'Synthetic new reply','com.jkali.from_me':false},c.ownerToken);
await core.monitor();await core.flush();assert.equal(delivered.length,1,'digest waits for its collection boundary');
core.clock=()=>scheduleNow+180000;await core.flush();assert.equal(delivered.length,2);assert.match(delivered[1].text,/2 messages in 1 conversation/);assert.equal(core.ref(1,2).bundle[0].events.length,2);core.clock=Date.now;
// Source-owned evidence can drive message-level list settings; a mirror arrival
// alone is not an unanswered-message proof. No native transport is involved.
assert.equal((await domain.updates(list_id))[0].waiting,'unknown');
const account=domain.accounts.get(c.owner),latest=(await domain.history(c.room)).messages.at(-1);
await request(c.base,'PUT',path+'/state/com.jkali.conversation_tracking/',{
 schema:1,state:'ready',owner_mxid:c.owner,generation:account.generation,journal_epoch:account.journal_epoch,
 bridge_epoch:'synthetic-native-epoch',room_id:'!native:fixture',mirror_room_id:c.room,source:domain.room(c.room).source,
 updated_ts:Date.now(),coverage:'native_suffix',complete:false,
 messages:[{event_id:incoming.event_id,incoming:true,order:'100',timestamp:latest.ts,answered_by:null}]},c.ownerToken);
await domain.refresh();assert.equal((await domain.updates(list_id))[0].waiting,'waiting_for_us');
draft=await core.tool('prepare_list_change',{operation:'watch',list_id,room_id:c.room,waiting_us_ms:3600000,schedule:null,repeat_ms:7200000});await core.confirm(draft.id);
assert.equal(domain.rules()[0].schedule,null);assert.equal(domain.rules()[0].repeat_ms,7200000);
assert.equal((await domain.updates(list_id))[0].obligations[0].due_at,latest.ts+3600000);
draft=await core.tool('prepare_list_change',{operation:'watch',list_id,room_id:c.room,inherit_monitoring:true});await core.confirm(draft.id);
assert.deepEqual(domain.rules()[0].schedule,quiet);assert.equal((await domain.updates(list_id))[0].obligations[0].due_at,null);
const trackingDraft=await core.tool('prepare_obligation',{list_id,room_id:c.room,event_id:incoming.event_id,resolved:true,due_at:null});
await core.confirm(trackingDraft.id);assert.equal((await domain.updates(list_id))[0].obligations[0].waiting,'resolved');
const send=await core.tool('prepare_send',{room_id:c.room,body:'Synthetic reviewed outgoing'});const sent=await core.confirm(send.id);assert.equal(sent.mode,'direct');
const proposal=await request(c.base,'GET','/_matrix/client/v3/rooms/'+encodeURIComponent(c.proposals)+'/event/'+encodeURIComponent(sent.proposal_id));
assert.equal(proposal.type,'com.jkali.proposal');assert.equal(proposal.content.target_room,'!native:fixture');assert.equal(proposal.content['com.jkali.send_mode'],'direct');
assert.equal((await core.confirm(send.id)).proposal_id,sent.proposal_id);
const stale=await core.tool('prepare_send',{room_id:c.room,body:'Must not submit'});
await request(c.base,'PUT',path+'/send/m.room.message/control-intervening',{msgtype:'m.text',body:'Intervening response'},c.ownerToken);
await assert.rejects(core.confirm(stale.id),/changed/);
// The actual uplink uses flat replacement content. An unchanged original ID
// must not keep a reply review valid after that message's text changes.
const edited=await core.tool('prepare_send',{room_id:c.room,body:'Review before edit'});
await request(c.base,'PUT',path+'/send/m.room.message/control-edit',{
 msgtype:'m.text',body:'Changed question','m.relates_to':{rel_type:'m.replace',event_id:edited.latest}},c.ownerToken);
await assert.rejects(core.confirm(edited.id),/changed/);
assert.equal((await domain.history(c.room)).messages.at(-1).text,'Changed question');
// Simulate losing the HTTP response after Synapse accepted one typed proposal.
// Status must find the immutable control draft ID and perform no second write.
const uncertain=await core.tool('prepare_send',{room_id:c.room,body:'Lost response fixture'}),originalApi=domain.api;
let proposalWrites=0;
domain.api=async(method,p,data)=>{const result=await originalApi(method,p,data);if(method==='PUT'&&p.includes('/send/com.jkali.proposal/')){proposalWrites++;throw Error('Lost response');}return result;};
await assert.rejects(core.confirm(uncertain.id),/not confirmed/);
const reconciled=await core.tool('send_status',{draft_id:uncertain.id});assert.equal(reconciled.state,'submitted');assert(reconciled.result.proposal_id);
await core.confirm(uncertain.id);assert.equal(proposalWrites,1);domain.api=originalApi;
const restricted=await core.tool('prepare_send',{room_id:c.room,body:'Must be blocked by control settings'});
// Short followups retain actual discovered identifiers across fresh Synapse
// reads. Replayed canonical state must have a stable access fingerprint.
let followupStep=0;
core.model=async messages=>{
 followupStep++;
 if(followupStep===1)return {role:'assistant',tool_calls:[{id:'lookup',type:'function',function:{name:'search_conversations',arguments:JSON.stringify({query:domain.room(c.room).title})}}]};
 if(followupStep===2)return {role:'assistant',content:'Show the latest messages in this account’s conversation?'};
 const prior=messages.find(m=>m.content?.startsWith('Recent exchanges'))?.content;
 assert(prior?.includes(c.room),'fresh discovery retains exact selected room');
 return {role:'assistant',tool_calls:[{id:'history',type:'function',function:{name:'history',arguments:JSON.stringify({room_id:c.room})}}]};
};
await core.command('Find the synthetic conversation',null,'update:followup-1');await core.flush();
await core.command('show the messages',null,'update:followup-2');await core.flush();assert.equal(followupStep,3);
assert(delivered.at(-1).text.includes('Changed question'));
await domain.apply(domain.operation('control.settings','control',0,{allow_model_context:false,sending_mode:'read_only',allowed_accounts:[],notifications_enabled:false,retention_days:7}));
await assert.rejects(core.confirm(restricted.id),/read-only/);assert.equal(domain.rooms.size,0);assert.equal(domain.accounts.size,0);
await assert.rejects(core.command('Summarize my messages',null,'model-disabled'),/disabled/);
await core.invalidateReviews();assert.equal((await core.confirm(restricted.id)).state,'review_required');
await domain.apply(domain.operation('control.settings','control',1,{allow_model_context:true,sending_mode:'draft_confirm',allowed_accounts:null,notifications_enabled:true,retention_days:7}));await domain.refresh();
assert(domain.rooms.has(c.room));assert.equal((await core.confirm(restricted.id)).state,'review_required','restoring access never revives an old review');
core.model=async messages=>{assert(!messages.some(m=>m.content?.startsWith('Recent exchanges')),'disabled access clears conversation memory');return {role:'assistant',content:'Name the account and conversation again.'};};
await core.command('that one',null,'update:followup-3');await core.flush();
// A summary queued before actual source sharing revocation must not leave the
// worker, even though it is unquoted and the model has already read the history.
let summaryStep=0;
core.model=async()=>++summaryStep===1?{role:'assistant',tool_calls:[{id:'read-summary',type:'function',function:{name:'history',arguments:JSON.stringify({room_id:c.room,display:false})}}]}:{content:'Synthetic confidential summary'};
await core.command('Summarize the synthetic conversation',null,'update:queued-summary');
const sharing=domain.room(c.room).consent,deliveredBeforeRevoke=delivered.length;
await request(c.base,'PUT',path+'/state/com.jkali.share_level/',{...sharing,level:'private'},c.ownerToken);
await core.flush();assert.equal(delivered.length,deliveredBeforeRevoke);
assert.equal(core.db.prepare('SELECT state FROM outbox WHERE id=?').get('update:queued-summary:0').state,'canceled');
assert(!core.db.prepare('SELECT payload FROM outbox WHERE id=?').get('update:queued-summary:0').payload.includes('Synthetic confidential summary'));
await core.flush();assert.match(delivered.at(-1).text,/Shared context changed/);
await request(c.base,'PUT',path+'/state/com.jkali.share_level/',sharing,c.ownerToken);await domain.refresh();
draft=await core.tool('prepare_list_change',{operation:'monitoring',list_id,enabled:false});await core.confirm(draft.id);assert.equal(domain.rules().length,0);
const reopened=new ControlDomain({api,bootstrap:()=>request(c.enrollBase,'POST','/admin/crm-room',{}),manager:c.manager,roster:async()=>({[c.owner]:c.space})});await reopened.refresh();assert.equal(reopened.list(list_id).monitoring.enabled,false);
assert.equal(reopened.settings().retention_days,7);assert.equal(reopened.state().control.revision,2);
assert.deepEqual(reopened.list(list_id).monitoring.presentation,{mode:'digest',interval_ms:60000});
// The production HTTP/domain runtime keeps canonical lists available while
// Telegram itself is unavailable; authentication still uses real Synapse.
const service=createControlService({config:{manager:c.manager,roster:{[c.owner]:c.space},model:'fixture'},domain:reopened,core,
 telegram:async()=>{throw Error('Simulated Telegram outage');},authenticate:token=>request(c.base,'GET','/_matrix/client/v3/account/whoami',undefined,token),
 host:'127.0.0.1',port:0,monitorInterval:60000,retryInterval:60000});
const address=await service.start(),controlUrl='http://127.0.0.1:'+address.port+'/control';
const controlRequest=async(action,token=c.token)=>{const r=await fetch(controlUrl,{method:'POST',headers:{Authorization:'Bearer '+token},body:JSON.stringify(typeof action==='string'?{action}:action)});return {status:r.status,data:await r.json()};};
try{
 let status;
 for(let i=0;i<100;i++){status=(await controlRequest('status')).data;if(status.domain_ready)break;await new Promise(r=>setTimeout(r,100));}
 assert.equal(status.domain_ready,true);assert.equal(status.transport_ready,false);
 assert.equal((await controlRequest('lists',c.ownerToken)).status,403);
 assert.equal((await controlRequest('lists','invalid')).status,401);
 const listing=await controlRequest('lists');assert.equal(listing.status,200);assert(listing.data.lists.some(l=>l.list_id===list_id));
 assert.equal((await controlRequest('rules')).status,200);assert.match((await controlRequest('pair')).data.error,/Telegram is reconnecting/);
 const command={action:'command',request_id:'offline-job',text:'/help',session_id:'fixture-session'};
 assert.equal((await controlRequest(command,c.ownerToken)).status,403);
 assert.equal((await controlRequest(command)).data.state,'queued');
 assert.equal((await controlRequest(command)).data.state,'queued');
 assert.equal((await controlRequest({...command,text:'different'})).status,400);
 assert.equal((await controlRequest({action:'command_status',request_id:'offline-job'})).data.state,'queued');
 assert.equal((await controlRequest({action:'command_cancel',request_id:'offline-job'})).data.state,'canceled');
 assert.equal((await core.tool('send_status',{draft_id:send.id})).state,'submitted');
}finally{await service.stop();}
// Real scoped history enters a synthetic held model request. Authenticated
// list/rule reads must finish during that wait; a real sharing revocation must
// discard the answer when the continuation reacquires its execution lease.
let heldModel,releaseModel,modelTurn=0;
core.model=async()=>{
 if(++modelTurn===1)return {tool_calls:[{id:'held-history',type:'function',function:{name:'history',arguments:JSON.stringify({room_id:c.room,display:false})}}]};
 heldModel=true;await new Promise(resolve=>{releaseModel=resolve;});return {content:'Summary from revoked source'};
};
const concurrent=createControlService({config:{manager:c.manager,roster:{[c.owner]:c.space},model:'fixture'},domain,core,
 telegram:async method=>{if(method==='getMe')return {id:991,is_bot:true,username:'fixture_bot'};if(method==='getWebhookInfo')return {url:''};if(method==='getUpdates'){await new Promise(r=>setTimeout(r,10));return [];}return true;},
 authenticate:token=>request(c.base,'GET','/_matrix/client/v3/account/whoami',undefined,token),
 host:'127.0.0.1',port:0,monitorInterval:60000,commandInterval:5});
const concurrentAddress=await concurrent.start();
const concurrentRequest=async data=>{
 const r=await fetch('http://127.0.0.1:'+concurrentAddress.port+'/control',{method:'POST',signal:AbortSignal.timeout(3000),headers:{Authorization:'Bearer '+c.token},body:JSON.stringify(data)});
 assert.equal(r.status,200);return r.json();
};
const waitUntil=async fn=>{for(let i=0;i<200;i++){if(await fn())return;await new Promise(r=>setTimeout(r,25));}throw Error('Concurrent integration did not reach expected state');};
try{
 await waitUntil(async()=>(await concurrentRequest({action:'status'})).ready);core.set('operator',{user:1,chat:1});
 const beforeHeld=delivered.length;
 await concurrentRequest({action:'command',request_id:'held-source',text:'Summarize this conversation'});await waitUntil(()=>heldModel);
 for(const action of ['lists','rules'])await concurrentRequest({action});
 await request(c.base,'PUT',path+'/state/com.jkali.share_level/',{...sharing,level:'private'},c.ownerToken);
 await concurrentRequest({action:'lists'});assert(!domain.rooms.has(c.room));
 releaseModel();await waitUntil(async()=>(await concurrentRequest({action:'command_status',request_id:'held-source'})).state==='failed');
 assert.equal(delivered.length,beforeHeld);assert.equal(core.db.prepare("SELECT count(*) AS n FROM outbox WHERE id LIKE 'api:held-source:%'").get().n,0);
}finally{releaseModel?.();await concurrent.stop();}
await request(c.base,'PUT',path+'/state/com.jkali.share_level/',sharing,c.ownerToken);await domain.refresh();
// Real source discovery and canonical routing, actual browser adapter HTTP,
// and mocked Telegram rejection. No native transport or push vendor is used.
core.browser=data=>request(c.enrollBase,'POST','/admin/notifications',data);
await domain.refresh();await domain.apply(domain.preparePolicy({list_id,enabled:true,schedule:null,presentation:null,channels:['browser','telegram'],routing:{primary:'telegram',fallback:true}}));
await core.monitor();assert.equal(core.status().routing_ready,true);
const routed=await request(c.base,'PUT',path+'/send/m.room.message/routing-fallback',{msgtype:'m.text',body:'Synthetic routing fallback','com.jkali.from_me':false},c.ownerToken);
core.telegram=async()=>{const e=Error('Simulated definitive block');e.status=403;throw e;};
await core.monitor();await core.flush();await core.monitor();
const routedId=notificationIdentity({kind:'message',room:c.room,event:routed.event_id,outgoing:false});
const routingState=core.notificationRouter.status(routedId);assert(routingState);assert.equal(routingState.rules[0].chosen,'browser');
const outcomes=await core.browser({action:'control_outcomes',ids:[routedId]});assert.equal(outcomes.outcomes[0].state,'pending');assert.equal(outcomes.outcomes[0].deliveries.length,1);
await core.monitor();assert.equal((await core.browser({action:'control_outcomes',ids:[routedId]})).outcomes[0].deliveries.length,1);
const handoff=await core.browser({action:'control_dispatcher'});assert.equal(handoff.id,core.value('notification_handoff'));
const media=await request(c.base,'PUT',path+'/send/m.room.message/context-media',{msgtype:'m.image',body:'Exact synthetic caption\nSecond line','com.jkali.from_me':false},c.ownerToken);
const replay=await domain.history(c.room);assert.equal(replay.messages.at(-1).id,media.event_id);assert.equal(replay.messages.at(-1).text,'[m.image]\nExact synthetic caption\nSecond line');
assert.equal(replay.availability.mirror_caught_up,true);assert.equal(replay.availability.native_history_complete,false);
core.enqueueHistory('context-media-replay',replay);
const rendered=core.db.prepare("SELECT payload FROM outbox WHERE id LIKE 'context-media-replay:%' ORDER BY rowid").all().map(row=>JSON.parse(row.payload).text).join('');
assert(rendered.includes('account ·'));assert(!rendered.includes('native completeness'));assert(!rendered.includes(media.event_id));assert(rendered.includes('Exact synthetic caption\nSecond line'));
// Canonical explicit contact-by task for a shared catalog entry with no room.
// This seeds metadata only; no Direct activation or native send is requested.
const catalog=(await request(c.base,'POST','/_matrix/client/v3/createRoom',{preset:'private_chat',invite:[c.manager],
 power_level_content_override:{users:{[c.owner]:100,[c.manager]:0},events_default:50},
 initial_state:[{type:'com.jkali.contacts',state_key:'',content:{schema:1}}]},c.ownerToken)).room_id;
await request(c.base,'POST','/_matrix/client/v3/rooms/'+encodeURIComponent(catalog)+'/join',{});
await request(c.base,'PUT','/_matrix/client/v3/rooms/'+encodeURIComponent(c.space)+'/state/m.space.child/'+encodeURIComponent(catalog),{via:['master']},c.ownerToken);
const contact={source:'imessage',network_id:'uncontacted@example.test',display_name:'Synthetic uncontacted person',person_id:'contact-profile',contact_version:'$contact-v1'};
const contactPath='/_matrix/client/v3/rooms/'+encodeURIComponent(catalog)+'/state/com.jkali.contact/synthetic';
await request(c.base,'PUT',contactPath,contact,c.ownerToken);await domain.refresh();
await domain.apply(domain.operation('person.create','contact-person',0,{label:'Synthetic uncontacted person'}));
await domain.apply(domain.operation('binding.create','contact-binding',0,{person_id:'contact-person',owner_mxid:c.owner,generation:domain.accounts.get(c.owner).generation,
 contact:{source:contact.source,network_id:contact.network_id},contact_version:contact.contact_version}));
await domain.apply(domain.operation('list.create','contact-list',0,{name:'Contact-by fixture'}));
await domain.apply(domain.operation('member.set','contact-member',0,{list_id:'contact-list',person_id:'contact-person',binding_id:'contact-binding'}));
let contactRows=await domain.contactUpdates('contact-list');assert.equal(contactRows.length,1);assert.equal(contactRows[0].due_at,null);assert.equal(contactRows[0].history_known,false);
const contactDraft=await core.tool('prepare_contact_obligation',{list_id:'contact-list',binding_id:'contact-binding',due_at:Date.now()+60000});
await core.confirm(contactDraft.id);const contactDate=contactDraft.operation.payload.due_at;
const contactReload=new ControlDomain({api,bootstrap:()=>request(c.enrollBase,'POST','/admin/crm-room',{}),manager:c.manager,roster:async()=>({[c.owner]:c.space})});
await contactReload.refresh();assert.equal((await contactReload.contactUpdates('contact-list'))[0].due_at,contactDate);
// Exercise the authenticated contact editor and actual browser delivery callback.
await request(c.base,'PUT','/_matrix/client/v3/rooms/'+encodeURIComponent(c.space)+'/state/com.jkali.uplink_health/',{connected:true,updated_at:Date.now()},c.ownerToken);
await domain.refresh();await domain.apply(domain.preparePolicy({list_id,enabled:false}));
await domain.apply(domain.preparePolicy({list_id:'contact-list',enabled:true,channels:['browser'],routing:null}));
const contactService=createControlService({config:{manager:c.manager,roster:{[c.owner]:c.space},model:'fixture'},domain,core,
 telegram:async()=>{throw Error('Synthetic Telegram offline');},authenticate:token=>request(c.base,'GET','/_matrix/client/v3/account/whoami',undefined,token),host:'127.0.0.1',port:0,monitorInterval:60000,retryInterval:60000});
const contactAddress=await contactService.start(),contactUrl='http://127.0.0.1:'+contactAddress.port;
const editContact=async(data,token=c.token)=>{const r=await fetch(contactUrl+'/control',{method:'POST',headers:{Authorization:'Bearer '+token},body:JSON.stringify(data)});return {status:r.status,body:await r.json()};};
try{
 await waitUntil(async()=>(await editContact({action:'status'})).body.domain_ready);
 assert.equal((await editContact({action:'ping'},c.ownerToken)).status,403);
 const apiPing=await editContact({action:'ping',account:c.owner});assert.equal(apiPing.status,200);assert.equal(apiPing.body.accounts[0].owner,c.owner);
 fs.writeFileSync(c.callbackPort,String(contactAddress.port),{mode:0o600});
 const edit={action:'contact_obligation',list_id:'contact-list',binding_id:'contact-binding',revision:domain.list('contact-list').revision,changes:{due_at:Date.now()-1}};
 assert.equal((await editContact(edit,c.ownerToken)).status,403);
 assert.equal((await editContact({...edit,changes:{...edit.changes,owner_mxid:c.owner}})).status,400);
 assert.equal((await editContact(edit)).status,200);assert.equal((await editContact(edit)).status,400,'stale revisions cannot overwrite');
 await core.monitor();
 const contactRoute=core.db.prepare('SELECT id,source FROM notification_routes ORDER BY rowid DESC LIMIT 1').get();assert.equal(JSON.parse(contactRoute.source).kind,'contact_reminder');
 assert.equal((await core.browser({action:'control_outcomes',ids:[contactRoute.id]})).outcomes[0].state,'pending');
 const sourceConnected=async connected=>request(c.base,'PUT','/_matrix/client/v3/rooms/'+encodeURIComponent(c.space)+'/state/com.jkali.uplink_health/',{connected,updated_at:Date.now()},c.ownerToken);
 await sourceConnected(false);await core.browser({action:'sync'});
 assert.equal((await core.browser({action:'control_outcomes',ids:[contactRoute.id]})).outcomes[0].state,'pending','contact callback defers stale source');
 await sourceConnected(true);await core.browser({action:'sync'});
 assert.equal((await core.browser({action:'control_outcomes',ids:[contactRoute.id]})).outcomes[0].state,'accepted');
 const record=await core.browser({action:'delivery',id:contactRoute.id});assert.equal(record.room,'');assert.equal(record.event,'');assert.equal(record.kind,'contact_reminder');
 await core.browser({action:'sync'});assert.equal((await core.browser({action:'delivery',id:contactRoute.id})).deliveries[0].attempts,1);
 // A queued message reminder can be snoozed and delivered in its new cycle
 // through the real coordinator, authenticated browser API and callback.
 const latestNative=(await domain.history(c.room)).messages.at(-1),currentAccount=domain.accounts.get(c.owner);
 await request(c.base,'PUT',path+'/state/com.jkali.conversation_tracking/',{
  schema:1,state:'ready',owner_mxid:c.owner,generation:currentAccount.generation,journal_epoch:currentAccount.journal_epoch,
  bridge_epoch:'snooze-native-epoch',room_id:'!native:fixture',mirror_room_id:c.room,source:domain.room(c.room).source,
  updated_ts:Date.now(),coverage:'native_suffix',complete:false,messages:[{event_id:latestNative.id,incoming:true,order:'1000',timestamp:latestNative.ts,answered_by:null}]},c.ownerToken);
 await domain.refresh();await domain.apply(domain.operation('list.create','snooze-list',0,{name:'Message snooze fixture',purpose:'monitoring'}));
 await domain.apply(domain.prepareWatch({list_id:'snooze-list',room_id:c.room}));
 await domain.apply(domain.preparePolicy({list_id:'snooze-list',enabled:true,channels:['browser'],activity_enabled:false}));
 const editMessage=async changes=>{
  const review=await editContact({action:'tool',name:'prepare_obligation',arguments:{list_id:'snooze-list',room_id:c.room,event_id:latestNative.id,...changes}});
  assert.equal(review.status,200);assert.equal((await editContact({action:'confirm',id:review.body.id})).status,200);return review.body.operation.payload;
 };
 assert.deepEqual((await editContact({action:'updates',room_id:'!unshared:fixture'})).body.updates,[],'exact-room API scope cannot return another conversation');
 const originalDue=Date.now()-1;assert.equal((await editMessage({due_at:new Date(originalDue).toISOString()})).due_at,originalDue,'ISO deadline API persists canonical milliseconds');await core.monitor();
 const oldReminder=notificationIdentity({kind:'reminder',room:c.room,event:latestNative.id,due:originalDue,slot:originalDue});
 assert.equal((await core.browser({action:'control_outcomes',ids:[oldReminder]})).outcomes[0].state,'pending');
 const snoozedMessage=await editMessage({snooze_until:Date.now()+3000});assert.equal(snoozedMessage.due_at,originalDue);assert(snoozedMessage.episode_at);
 await core.browser({action:'sync'});assert.equal((await core.browser({action:'control_outcomes',ids:[oldReminder]})).outcomes[0].state,'canceled');
 await new Promise(resolve=>setTimeout(resolve,Math.max(0,snoozedMessage.snooze_until-Date.now()+20)));
 await core.monitor();const newReminder=notificationIdentity({kind:'reminder',room:c.room,event:latestNative.id,due:originalDue,slot:originalDue,episode:snoozedMessage.episode_at});
 assert.notEqual(newReminder,oldReminder);
 await sourceConnected(false);await core.browser({action:'sync'});
 assert.equal((await core.browser({action:'control_outcomes',ids:[newReminder]})).outcomes[0].state,'pending','message reminder waits for fresh source');
 await sourceConnected(true);await core.browser({action:'sync'});
 assert.equal((await core.browser({action:'control_outcomes',ids:[newReminder]})).outcomes[0].state,'accepted');
 await core.monitor();await core.browser({action:'sync'});assert.equal((await core.browser({action:'delivery',id:newReminder})).deliveries[0].attempts,1);
 // Routine Telegram/API list editing uses the same canonical store and source
 // cancellation adapter as the browser. Only the synthetic source acknowledges.
 const newList=(await editContact({action:'tool',name:'prepare_list_change',arguments:{operation:'create',name:'API outreach',purpose:'outreach'}})).body;
 assert.equal((await editContact({action:'confirm',id:newList.id})).status,200);const newListId=newList.operation.entity_id;
 assert.equal(domain.list(newListId).purpose,'outreach');
 await domain.apply(domain.operation('member.set','api-member',0,{list_id:newListId,person_id:'contact-person',binding_id:'contact-binding'}));
 const proposalState=await request(c.base,'GET','/_matrix/client/v3/rooms/'+encodeURIComponent(c.proposals)+'/state');
 const capability=proposalState.find(e=>e.type==='com.jkali.crm.capability'&&e.state_key==='').content;
 await request(c.base,'PUT','/_matrix/client/v3/rooms/'+encodeURIComponent(c.proposals)+'/state/com.jkali.crm.capability/',{...capability,direct_cancel:true},c.ownerToken);
 cancelResponder=true;
 const removal=(await editContact({action:'tool',name:'prepare_member_removal',arguments:{list_id:newListId,binding_id:'contact-binding'}})).body;
 assert(removal.description.includes(c.owner));assert.equal(canceledMemberships.length,0);
 assert.equal((await editContact({action:'confirm',id:removal.id})).status,200);assert.equal((await editContact({action:'confirm',id:removal.id})).status,200);
 assert.equal(canceledMemberships.length,1);assert.equal(canceledMemberships[0].binding_id,'contact-binding');assert.equal(canceledMemberships[0].owner_mxid,c.owner);
 assert.equal(domain.lists().find(l=>l.list_id===newListId).members.length,0);assert.equal(domain.lists().find(l=>l.list_id==='contact-list').members.length,1);
 const archive=(await editContact({action:'tool',name:'prepare_list_change',arguments:{operation:'archive',list_id:newListId}})).body;
 assert.equal((await editContact({action:'confirm',id:archive.id})).status,200);
 assert(!(await editContact({action:'lists'})).body.lists.some(l=>l.list_id===newListId));
 assert((await editContact({action:'lists',include_archived:true})).body.lists.some(l=>l.list_id===newListId&&l.archived));
 const restore=(await editContact({action:'tool',name:'prepare_list_change',arguments:{operation:'restore',list_id:newListId}})).body;
 assert.equal((await editContact({action:'confirm',id:restore.id})).status,200);assert.equal(domain.list(newListId).archived,false);
 const oldSettings=domain.settings();
 const toggle=(await editContact({action:'tool',name:'prepare_global_notifications',arguments:{enabled:false}})).body;
 assert.equal((await editContact({action:'confirm',id:toggle.id})).status,200);assert.deepEqual(domain.settings(),{...oldSettings,notifications_enabled:false});
 assert.equal((await editContact({action:'tool',name:'prepare_global_notifications',arguments:{enabled:true,allow_model_context:true}})).status,400);
 const readSettings=(await editContact({action:'tool',name:'settings'})).body;assert.equal(readSettings.settings.notifications_enabled,false);
}finally{fs.unlinkSync(c.callbackPort);await contactService.stop();}
const pendingContact=await core.tool('prepare_contact_obligation',{list_id:'contact-list',binding_id:'contact-binding',resolved:true});
await request(c.base,'PUT',contactPath,{...contact,deleted:true},c.ownerToken);await domain.refresh();
assert.deepEqual(await domain.contactUpdates('contact-list'),[]);await assert.rejects(core.confirm(pendingContact.id),/unavailable/);
core.db.close();console.log('Telegram real Synapse: canonical lists, alerts, dedup, exact history, typed proposals, scoped confirmation, source revocation, async commands, dispatcher handoff and real browser fallback enqueue passed. No native transport used.');
