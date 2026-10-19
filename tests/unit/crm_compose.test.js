import assert from 'node:assert/strict';
import { createCRMCompose } from '../../apps/master/crm-compose.js';

class Element {
  constructor(tag){this.tagName=tag.toUpperCase();this.children=[];this.attributes={};this.listeners={};this._text='';this.disabled=false;this.value='';}
  set textContent(value){this._text=String(value);this.children=[];}
  get textContent(){return this._text+this.children.map(c=>c.textContent).join('');}
  set innerHTML(value){throw Error('HTML sink forbidden');}
  append(...nodes){this.children.push(...nodes);}
  setAttribute(k,v){this.attributes[k]=String(v);}
  getAttribute(k){return this.attributes[k];}
  addEventListener(type,fn){(this.listeners[type]||=[]).push(fn);}
  fire(type){if(this.disabled)return;for(const fn of this.listeners[type]||[])fn({target:this});}
}
const oldDocument=globalThis.document;globalThis.document={createElement:tag=>new Element(tag)};
const flush=()=>new Promise(r=>setImmediate(r));
const deferred=()=>{let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b;});return {promise,resolve,reject};};
const frozen=value=>{if(value&&typeof value==='object'){Object.values(value).forEach(frozen);Object.freeze(value);}return value;};
function fixture(){
  const h={opened:[],calls:[],changed:0,list:{list_id:'list',archived:false},state:{persons:{ada:{label:'Ada'},sam:{label:'Sam'}},bindings:{ba:{owner:'Elliot'},bs:{owner:'David'}},memberships:{'list/ada':{person_id:'ada',binding_id:'ba'},'list/sam':{person_id:'sam',binding_id:'bs'}},batches:{}},routeHook:null,reviewHook:null,submitHook:null};
  h.route=person=>({source:'imessage',network_id:person+'@example.invalid',mode:person==='sam'?'request_new_conversation_approval':'direct_existing',...(person==='sam'?{}:{room_id:'!ada:local'}),consent_revision:0});
  h.service={routes:async(list,person)=>{h.calls.push(['routes',list,person]);return h.routeHook?h.routeHook(list,person):[{label:'Synthetic '+person,route:h.route(person)}];},
    review:async args=>{h.calls.push(['review',args]);return h.reviewHook?h.reviewHook(args):frozen({batch_id:'batch',list_id:args.listId,children:args.items.map(i=>({child_id:'child-'+i.personId})),items:args.items.map(i=>({item_id:'item-'+i.personId,person_id:i.personId,child_id:'child-'+i.personId,binding_id:i.personId==='ada'?'ba':'bs',body:i.body,route:i.route}))});},
    submit:async manifest=>{h.calls.push(['submit',manifest]);return h.submitHook?h.submitHook(manifest):manifest.children.map(c=>({child_id:c.child_id,state:'awaiting_acknowledgment',items:[]}));},
    cancel:async manifest=>{h.calls.push(['cancel',manifest]);h.state.batch_cancellations={[manifest.batch_id]:{payload_hash:'saved-review'}};return h.cancelHook?h.cancelHook(manifest):manifest.children.map(c=>({child_id:c.child_id,state:'canceled',items:[]}));},
    statuses:async manifest=>{h.calls.push(['statuses',manifest]);if(h.statusHook)return h.statusHook(manifest);return manifest.children.map(c=>({child_id:c.child_id,state:'accepted',items:manifest.items.filter(i=>i.child_id===c.child_id).map(i=>({item_id:i.item_id,state:i.person_id==='sam'?'waiting_for_approval':'accepted'}))}));}};
  h.draw=()=>{h.root=new Element('div');h.root.append(h.view.controls(h.list,h.state,false));h.view.render(h.root,h.list,h.state,false);};
  h.view=createCRMCompose({selectList:id=>{h.list=h.state.lists?.[id]||{list_id:id,archived:false};},openMessage:(...args)=>h.opened.push(args),getBatches:()=>h.service,onChange:()=>{h.changed++;h.draw();},describeBinding:b=>({owner:b?.owner||'Unavailable'})});
  h.nodes=()=>{const nodes=[];const visit=n=>{nodes.push(n);n.children.forEach(visit);};visit(h.root);return nodes;};
  h.button=text=>{const n=h.nodes().find(n=>n.tagName==='BUTTON'&&n.textContent===text);assert.ok(n,'button '+text);return n;};
  h.field=label=>{const n=h.nodes().find(n=>n.getAttribute('aria-label')===label);assert.ok(n,'field '+label);return n;};
  h.select=person=>{const cell=h.view.selectionCell(h.list.list_id,{person_id:person},false,person);cell.children[0].checked=true;cell.children[0].fire('change');};
  h.click=async text=>{h.button(text).fire('click');await flush();};
  h.fill=(label,value)=>{const n=h.field(label);n.value=value;n.fire(n.tagName==='SELECT'?'change':'input');};
  h.start=async()=>{h.select('ada');h.select('sam');await h.click('Personalize messages');h.fill('Message to Ada','Hello Ada');h.fill('Message to Sam','Hello Sam');};
  h.draw();return h;
}
let passed=0,failed=0;
async function test(name,fn){try{await fn();passed++;}catch(e){failed++;console.error('FAIL crm_compose:',name,'\n',e.stack);}}
await test('empty selection disables personalization and cannot request routes',async()=>{const h=fixture();assert.equal(h.button('Personalize messages').disabled,true);await h.click('Personalize messages');assert.equal(h.calls.length,0);});
await test('selection personalizes each sender, shows immutable review and only explicit submit dispatches',async()=>{const h=fixture();await h.start();assert.equal(h.calls.filter(c=>c[0]==='submit').length,0);assert.match(h.root.textContent,/Send through Elliot/);assert.match(h.root.textContent,/Send through David/);await h.click('Review batch');assert.equal(h.nodes().filter(n=>n.tagName==='TEXTAREA').length,0);assert.match(h.root.textContent,/Hello Ada/);assert.match(h.root.textContent,/User approval required/);assert.equal(h.calls.filter(c=>c[0]==='submit').length,0);await h.click('Submit reviewed batch');assert.equal(h.calls.filter(c=>c[0]==='submit').length,1);assert.match(h.root.textContent,/awaiting acknowledgment/);assert.doesNotMatch(h.root.textContent,/delivered|read receipt/i);});
await test('edit returns original personalized drafts without creating a send',async()=>{const h=fixture();await h.start();await h.click('Review batch');await h.click('Edit messages');assert.equal(h.field('Message to Ada').value,'Hello Ada');assert.equal(h.field('Message to Sam').value,'Hello Sam');h.fill('Message to Ada','Edited Ada');await h.click('Review batch');assert.match(h.root.textContent,/Edited Ada/);assert.equal(h.calls.filter(c=>c[0]==='submit').length,0);});
await test('multiple route choices remain explicit in review arguments',async()=>{const h=fixture();h.routeHook=async(list,p)=>[{label:'First',route:h.route(p)},{label:'Second',route:{...h.route(p),room_id:'!second:local'}}];h.select('ada');await h.click('Personalize messages');assert.equal(h.field('Conversation for Ada').value,'');h.fill('Conversation for Ada','1');h.fill('Message to Ada','Chosen thread');await h.click('Review batch');assert.equal(h.calls.find(c=>c[0]==='review')[1].items[0].route.room_id,'!second:local');});
await test('route failure shows error and disables review until recheck',async()=>{const h=fixture();h.select('ada');h.routeHook=async()=>{throw Error('Source account unavailable');};await h.click('Personalize messages');assert.match(h.root.textContent,/Source account unavailable/);assert.equal(h.button('Review batch').disabled,true);h.routeHook=null;await h.click('Recheck sending accounts');assert.equal(h.button('Review batch').disabled,false);});
await test('review rejection retains drafts and never submits',async()=>{const h=fixture();await h.start();h.reviewHook=async()=>{throw Error('Contact revoked');};await h.click('Review batch');assert.match(h.root.textContent,/Contact revoked/);assert.equal(h.field('Message to Ada').value,'Hello Ada');assert.equal(h.calls.filter(c=>c[0]==='submit').length,0);});
await test('in-flight source submit prevents double click and preserves exact review object',async()=>{const h=fixture();await h.start();await h.click('Review batch');const gate=deferred();h.submitHook=()=>gate.promise;const button=h.button('Submit reviewed batch');button.fire('click');button.fire('click');assert.equal(h.calls.filter(c=>c[0]==='submit').length,1);assert.equal(h.button('Reconcile submission').disabled,true);gate.resolve([]);await flush();const manifest=h.calls.find(c=>c[0]==='submit')[1];assert.ok(Object.isFrozen(manifest.items[0]));});
await test('source status refresh retains queued versus approval-required distinctions',async()=>{const h=fixture();await h.start();await h.click('Review batch');await h.click('Submit reviewed batch');await h.click('Refresh progress');assert.match(h.root.textContent,/Sending through account/);assert.match(h.root.textContent,/Queued · awaiting user/);assert.doesNotMatch(h.root.textContent,/delivered|read receipt/i);assert.equal(h.calls.filter(c=>c[0]==='submit').length,1);});
await test('changing lists suppresses pending route completion and separates drafts',async()=>{const h=fixture(),gate=deferred();h.select('ada');h.routeHook=()=>gate.promise;h.button('Personalize messages').fire('click');h.list={list_id:'other',archived:false};h.draw();gate.resolve([{label:'Stale',route:h.route('ada')}]);await flush();assert.doesNotMatch(h.root.textContent,/Stale|Personalize messagesChecking/);assert.equal(h.nodes().filter(n=>n.tagName==='TEXTAREA').length,0);assert.equal(h.button('Personalize messages').disabled,true);});
await test('closing during pending review drops response and destroys drafts',async()=>{const h=fixture();await h.start();const gate=deferred();h.reviewHook=()=>gate.promise;h.button('Review batch').fire('click');h.view.close();const changed=h.changed;gate.resolve({items:[],children:[]});await flush();assert.equal(h.changed,changed);assert.equal(h.calls.filter(c=>c[0]==='submit').length,0);});
await test('message and contact strings remain inert text throughout compose and review',async()=>{const h=fixture();h.state.persons.ada.label='<img src=x onerror=evil()>';h.select('ada');await h.click('Personalize messages');h.fill('Message to <img src=x onerror=evil()>','<script>evil()</script>');await h.click('Review batch');assert.match(h.root.textContent,/<script>evil\(\)<\/script>/);assert.equal(h.nodes().filter(n=>['SCRIPT','IMG'].includes(n.tagName)).length,0);});
await test('source submission failure keeps reviewed manifest available for explicit reconciliation',async()=>{const h=fixture();await h.start();await h.click('Review batch');h.submitHook=async()=>{throw Error('Source response lost');};await h.click('Submit reviewed batch');assert.match(h.root.textContent,/Source response lost/);h.submitHook=null;await h.click('Reconcile submission');const calls=h.calls.filter(c=>c[0]==='submit');assert.equal(calls.length,2);assert.equal(calls[0][1],calls[1][1]);});
await test('cancellation is explicit and switches durable retry action away from submission',async()=>{const h=fixture();await h.start();await h.click('Review batch');await h.click('Submit reviewed batch');assert.equal(h.calls.filter(c=>c[0]==='cancel').length,0);await h.click('Cancel unattempted messages');assert.equal(h.calls.filter(c=>c[0]==='cancel').length,1);assert.equal(h.nodes().some(n=>n.tagName==='BUTTON'&&n.textContent==='Reconcile submission'),false);await h.click('Reconcile cancellation');assert.equal(h.calls.filter(c=>c[0]==='cancel').length,2);assert.equal(h.calls.filter(c=>c[0]==='submit').length,1);});
await test('pending cancellation retains already sent evidence and original reviewed text',async()=>{const h=fixture();await h.start();await h.click('Review batch');await h.click('Submit reviewed batch');h.cancelHook=async m=>m.children.map(c=>({child_id:c.child_id,state:'cancellation_requested',items:m.items.filter(i=>i.child_id===c.child_id).map(i=>({item_id:i.item_id,state:'sent'}))}));await h.click('Cancel unattempted messages');assert.match(h.root.textContent,/Hello Ada/);assert.match(h.root.textContent,/sent/i);assert.match(h.root.textContent,/already attempted/i);});
await test('cancellation failure preserves retry action without silently requeuing source sends',async()=>{const h=fixture();await h.start();await h.click('Review batch');await h.click('Submit reviewed batch');h.cancelHook=async()=>{throw Error('Cancellation source offline');};await h.click('Cancel unattempted messages');assert.match(h.root.textContent,/Cancellation source offline/);assert.equal(h.button('Reconcile cancellation').disabled,false);assert.equal(h.calls.filter(c=>c[0]==='submit').length,1);});
await test('pending cancellation cannot be double submitted by repeated old-button clicks',async()=>{const h=fixture();await h.start();await h.click('Review batch');await h.click('Submit reviewed batch');const gate=deferred();h.cancelHook=()=>gate.promise;const button=h.button('Cancel unattempted messages');button.fire('click');button.fire('click');assert.equal(h.calls.filter(c=>c[0]==='cancel').length,1);gate.resolve([]);await flush();});
async function trackedFixture(deliveryState='sending', response={state:'waiting'}) {
  const h=fixture(); await h.start(); await h.click('Review batch');
  h.submitHook=async manifest=>manifest.children.map(c=>({child_id:c.child_id,state:'accepted',items:manifest.items.filter(i=>i.child_id===c.child_id).map(i=>({item_id:i.item_id,state:'sent',sent_event_id:'$sent-'+i.person_id,sent_room_id:'!local:source',tracking:{revision:1,delivery:{sent_event_id:'$sent-'+i.person_id,state:deliveryState,mirror_event_id:'$mirror-'+i.person_id,mirror_room_id:'!mirror:master'},response}}))}));
  await h.click('Submit reviewed batch'); return h;
}
await test('exact source receipt evidence uses the normal gray and blue checks',async()=>{
  for(const [state,css,glyph]of[['sending','sending','✓'],['sent','sending','✓'],['delivered','delivered','✓✓'],['read','read','✓✓']]){
    const h=await trackedFixture(state),checks=h.nodes().filter(n=>n.className==='message-receipt '+css);
    assert.equal(checks.length,2);assert.ok(checks.every(n=>n.textContent===glyph));
  }
});
await test('a reply cannot promote sending to a delivered or read receipt',async()=>{
  const h=await trackedFixture('sending',{state:'replied',incoming_event_id:'$incoming',mirror_event_id:'$reply',mirror_room_id:'!mirror:master'});
  assert.match(h.root.textContent,/Replied to this message/);assert.equal(h.nodes().filter(n=>n.className==='message-receipt sending').length,2);
  assert.equal(h.nodes().filter(n=>/message-receipt (read|delivered)/.test(n.className)).length,0);
  await h.click('Open reply');assert.equal(h.opened.length,1);assert.equal(h.opened[0][2],'response');assert.equal(h.opened[0][1].tracking.response.incoming_event_id,'$incoming');
});
await test('retained uncertain response stays reconciling and opens exact prior link',async()=>{
  const h=await trackedFixture('sent',{state:'reconciling',reason:'<script>unavailable</script>',incoming_event_id:'$prior',mirror_event_id:'$prior-mirror',mirror_room_id:'!mirror:master'});
  assert.match(h.root.textContent,/Response link is reconciling/);assert.doesNotMatch(h.root.textContent,/Replied to this message/);
  await h.click('Open previously linked reply');assert.equal(h.opened[0][1].tracking.response.incoming_event_id,'$prior');
  assert.equal(h.nodes().filter(n=>n.tagName==='SCRIPT').length,0);
});
await test('missing exact send target or invalid tracking revision produces no receipt or message link',async()=>{
  for(const change of [o=>o.tracking.revision=0,o=>o.tracking.revision=1.5,o=>o.tracking.delivery.sent_event_id='$different',o=>o.sent_event_id=null]){
    const h=fixture();await h.start();await h.click('Review batch');h.submitHook=async m=>m.children.map(c=>({child_id:c.child_id,state:'accepted',items:m.items.filter(i=>i.child_id===c.child_id).map(i=>{const o={item_id:i.item_id,state:'outcome_unknown',sent_event_id:'$sent',tracking:{revision:1,delivery:{state:'read',sent_event_id:'$sent'}}};change(o);return o;})}));await h.click('Submit reviewed batch');
    assert.equal(h.nodes().filter(n=>n.className?.startsWith('message-receipt')).length,0);assert.equal(h.nodes().some(n=>n.textContent==='Open message'),false);
  }
});
await test('missing mirror mapping disables exact-message navigation and closed controls are inert',async()=>{
  const h=await trackedFixture('sent',{state:'replied',incoming_event_id:'$reply'});assert.equal(h.button('Open reply').disabled,true);
  const message=h.button('Open message');h.view.close();message.fire('click');assert.equal(h.opened.length,0);
});
await test('unavailable recipient is explicitly excluded while eligible recipient can be reviewed',async()=>{
  const h=fixture();h.routeHook=async(list,p)=>{if(p==='sam')throw Error('David offline');return [{label:'Ada route',route:h.route(p)}];};
  h.select('ada');h.select('sam');await h.click('Personalize messages');
  assert.match(h.root.textContent,/1 included · 1 excluded/);assert.match(h.root.textContent,/Excluded · David offline/);
  assert.equal(h.button('Review batch').disabled,false);h.fill('Message to Ada','Only Ada');await h.click('Review batch');
  assert.deepEqual(h.calls.find(c=>c[0]==='review')[1].items.map(i=>i.personId),['ada']);
  assert.match(h.root.textContent,/Sam · Excluded · David offline/);assert.equal(h.calls.filter(c=>c[0]==='submit').length,0);
});
await test('rechecking excludes revoked contacts and recovers their retained personalized drafts',async()=>{
  const h=fixture();await h.start();h.routeHook=async(list,p)=>{if(p==='ada')throw Error('Contact no longer shared');return [{label:'Sam',route:h.route(p)}];};
  await h.click('Recheck sending accounts');assert.match(h.root.textContent,/AdaSend through ElliotExcluded · Contact no longer shared/);
  assert.equal(h.nodes().some(n=>n.getAttribute('aria-label')==='Message to Ada'),false);
  h.routeHook=null;await h.click('Recheck sending accounts');assert.equal(h.field('Message to Ada').value,'Hello Ada');
  assert.equal(h.field('Message to Sam').value,'Hello Sam');assert.equal(h.calls.filter(c=>c[0]==='submit').length,0);
});
function historyFixture(){
  const h=fixture();h.state.bindings.bd={owner:'David'};
  h.state.lists={list:{list_id:'list',name:'Current',archived:false},old:{list_id:'old',name:'Archived',archived:true}};
  const batch=(id,list,person,binding,owner,time)=>({batch_id:id,payload_hash:id+'-hash',list_id:list,reviewed_at:time,
    children:[{child_id:'child-'+id}],items:[{item_id:'item-'+id,person_id:person,binding_id:binding,owner_mxid:owner,
      child_id:'child-'+id,body:'Message '+id,route:h.route(person)}]});
  h.state.batches={old:batch('old','old','ada','ba','@elliot:master',10),new:batch('new','list','ada','bd','@david:master',30),sam:batch('sam','list','sam','bs','@david:master',20)};
  h.outcomes={};
  for(const [id,b]of Object.entries(h.state.batches))h.outcomes[id]=[{child_id:b.children[0].child_id,state:'accepted',items:[{item_id:b.items[0].item_id,state:'sent',sent_event_id:'$sent-'+id,sent_room_id:'!source:local',tracking:{revision:1,
    delivery:{sent_event_id:'$sent-'+id,state:id==='old'?'read':'sending',mirror_event_id:'$mirror-'+id,mirror_room_id:'!mirror:master'},
    response:id==='old'?{state:'replied',incoming_event_id:'$reply-old',mirror_event_id:'$mirror-reply',mirror_room_id:'!mirror:master'}:{state:'waiting'}}}]}];
  h.statusHook=async b=>structuredClone(h.outcomes[b.batch_id]);
  h.panels=()=>h.nodes().filter(n=>n.tagName==='SECTION'&&n.className==='crm-compose-item');
  return h;
}
await test('central history includes archived and removed people with independent account/message records',async()=>{
  const h=historyFixture();h.state.memberships={};await h.view.openHistory(h.state);
  assert.equal(h.panels().length,3);assert.match(h.root.textContent,/Elliot · imessage · Archived/);
  assert.match(h.root.textContent,/David · imessage · Current/);assert.equal(h.calls.filter(c=>['submit','cancel','routes'].includes(c[0])).length,0);
  assert.equal(h.nodes().filter(n=>n.className==='message-receipt read').length,1);
  await h.click('Open reply');assert.equal(h.opened[0][0].owner_mxid,'@elliot:master');assert.equal(h.opened[0][1].tracking.response.incoming_event_id,'$reply-old');
});
await test('person history retains both original senders and filters never reassign sender',async()=>{
  const h=historyFixture();await h.view.openHistory(h.state,'ada');assert.equal(h.panels().length,2);
  h.fill('Outreach sending account','@elliot:master');assert.equal(h.panels().length,1);assert.match(h.panels()[0].textContent,/Message old/);
  h.fill('Outreach sending account','@david:master');assert.equal(h.panels().length,1);assert.match(h.panels()[0].textContent,/Message new/);
  assert.equal(h.state.memberships['list/ada'].binding_id,'ba');
});
await test('history filters intersect list, owner, delivery and response without any writes',async()=>{
  const h=historyFixture();await h.view.openHistory(h.state);
  h.fill('Outreach list','old');h.fill('Outreach sending account','@elliot:master');h.fill('Outreach message state','read');h.fill('Outreach response','replied');
  assert.equal(h.panels().length,1);h.fill('Outreach response','waiting');assert.equal(h.panels().length,0);assert.match(h.root.textContent,/No outreach matches/);
  assert.equal(h.calls.filter(c=>['submit','cancel','review'].includes(c[0])).length,0);
});
await test('latest reviewed summary uses assigned binding rather than another account newer review',async()=>{
  const h=historyFixture();await h.view.refreshHistory(h.state);
  const cell=h.view.latestCell(h.state,{person_id:'ada',binding_id:'ba'});assert.match(cell.textContent,/readReplied/);
  const other=h.view.latestCell(h.state,{person_id:'ada',binding_id:'bd'});assert.match(other.textContent,/sendingAwaiting reply/);
  const missing=h.view.latestCell(h.state,{person_id:'ada',binding_id:'unknown'});assert.equal(missing.textContent,'No reviewed outreach');
});
await test('history never turns a reply or malformed send correlation into blue checks',async()=>{
  const h=historyFixture();const item=h.outcomes.old[0].items[0];item.tracking.delivery.sent_event_id='$wrong';
  await h.view.openHistory(h.state);assert.equal(h.nodes().filter(n=>n.className==='message-receipt read').length,0);
  assert.doesNotMatch(h.view.latestCell(h.state,{person_id:'ada',binding_id:'ba'}).textContent,/read|Replied/);
});
await test('revoked/unavailable source refresh removes cached message links without hiding history',async()=>{
  const h=historyFixture();await h.view.openHistory(h.state);assert.equal(h.nodes().some(n=>n.textContent==='Open reply'),true);
  h.statusHook=async()=>{throw Error('Contact revoked or account offline');};await h.view.refreshHistory(h.state);
  assert.equal(h.panels().length,3);assert.equal(h.nodes().some(n=>n.textContent==='Open reply'),false);
  assert.equal(h.nodes().filter(n=>n.className?.startsWith('message-receipt')).length,0);assert.match(h.root.textContent,/Contact revoked or account offline/);
});
await test('changed immutable parent hash cannot reuse source results from an earlier review',async()=>{
  const h=historyFixture();await h.view.refreshHistory(h.state);h.state.batches.old.payload_hash='different';
  const cell=h.view.latestCell(h.state,{person_id:'ada',binding_id:'ba'});assert.match(cell.textContent,/Status not loaded/);assert.doesNotMatch(cell.textContent,/read|Replied/);
});
await test('overlapping history refreshes discard obsolete delayed outcomes',async()=>{
  const h=historyFixture(),gate=deferred();let first=true;h.statusHook=async b=>{if(first){first=false;return gate.promise;}return structuredClone(h.outcomes[b.batch_id]);};
  const old=h.view.refreshHistory(h.state);h.outcomes.old[0].items[0].tracking.delivery.state='delivered';await h.view.refreshHistory(h.state);
  const stale=structuredClone(h.outcomes.old);stale[0].items[0].tracking.delivery.state='read';gate.resolve(stale);await old;
  const cell=h.view.latestCell(h.state,{person_id:'ada',binding_id:'ba'});assert.match(cell.textContent,/delivered/);assert.doesNotMatch(cell.textContent,/readReplied/);
});
await test('closed view ignores pending history and old navigation controls',async()=>{
  const h=historyFixture();await h.view.openHistory(h.state);const reply=h.button('Open reply'),gate=deferred();h.statusHook=()=>gate.promise;
  const pending=h.view.refreshHistory(h.state);h.view.close();const changes=h.changed;reply.fire('click');gate.resolve([]);await pending;
  assert.equal(h.changed,changes);assert.equal(h.opened.length,0);
});
await test('newer batch detail outcome is not overwritten by an older in-flight history read',async()=>{
  const h=historyFixture(),gate=deferred();let first=true;
  const stale=structuredClone(h.outcomes.old);stale[0].items[0].tracking.delivery.state='sending';
  h.outcomes.old[0].items[0].tracking.revision=2;
  h.statusHook=async b=>{if(first){first=false;return gate.promise;}return structuredClone(h.outcomes[b.batch_id]);};
  const pending=h.view.openHistory(h.state);
  const old=h.panels().find(p=>p.textContent.includes('Message old'));
  old.children.find(n=>n.tagName==='BUTTON'&&n.textContent==='View batch').fire('click');await flush();
  assert.match(h.view.latestCell(h.state,{person_id:'ada',binding_id:'ba'}).textContent,/readReplied/);
  gate.resolve(stale);await pending;
  assert.match(h.view.latestCell(h.state,{person_id:'ada',binding_id:'ba'}).textContent,/readReplied/);
});
await test('observed-native response label stays qualified and never supplies delivery evidence',async()=>{
  const h=await trackedFixture('sending',{state:'replied',evidence_kind:'observed_native',incoming_event_id:'$observed',mirror_event_id:'$observed-mirror',mirror_room_id:'!mirror:master'});
  assert.match(h.root.textContent,/Replied · observed history/);assert.match(h.root.textContent,/Later history sync may revise/);
  assert.equal(h.nodes().filter(n=>n.className==='message-receipt sending').length,2);
  assert.equal(h.nodes().filter(n=>/message-receipt (read|delivered)/.test(n.className)).length,0);
});
await test('observed-native waiting state never claims complete absence of a reply',async()=>{
  const h=historyFixture();h.outcomes.old[0].items[0].tracking.response={state:'waiting',evidence_kind:'observed_native'};
  await h.view.openHistory(h.state,'ada');assert.match(h.root.textContent,/No reply in observed history/);
  assert.match(h.view.latestCell(h.state,{person_id:'ada',binding_id:'ba'}).textContent,/No reply in observed history/);
});
await test('duplicate normalized destination warns across people and accounts without merging messages',async()=>{
  const h=fixture();h.routeHook=async(list,p)=>[{label:p,route:{...h.route(p),network_id:p==='ada'?' SAME@Example.Invalid ':'same@example.invalid'}}];
  await h.start();await h.click('Review batch');assert.match(h.root.textContent,/Potential duplicate destination: Ada via Elliot, Sam via David/);
  assert.match(h.root.textContent,/Both messages are included/);assert.equal(h.calls.filter(c=>c[0]==='submit').length,0);
  await h.click('Submit reviewed batch');const m=h.calls.find(c=>c[0]==='submit')[1];assert.equal(m.items.length,2);
  assert.deepEqual(m.items.map(i=>i.body),['Hello Ada','Hello Sam']);
});
await test('same identifier on different sources is not reported as duplicate',async()=>{
  const h=fixture();h.routeHook=async(list,p)=>[{label:p,route:{...h.route(p),source:p==='ada'?'imessage':'whatsapp',network_id:'same@example.invalid'}}];
  await h.start();await h.click('Review batch');assert.doesNotMatch(h.root.textContent,/Potential duplicate destination/);
});
await test('unresolved placeholder warning blocks submission and retains exact draft for correction',async()=>{
  const h=fixture();await h.start();const body='Hello {{first_name}}, keep <b>{{company}}</b> literal.';h.fill('Message to Ada',body);
  await h.click('Review batch');assert.match(h.root.textContent,/Ada has an unresolved \{\{placeholder\}\}/);
  assert.equal(h.calls.filter(c=>c[0]==='submit').length,0);assert.equal(h.nodes().filter(n=>n.tagName==='B').length,0);
  assert.equal(h.button('Submit reviewed batch').disabled,true);await h.click('Submit reviewed batch');assert.equal(h.calls.filter(c=>c[0]==='submit').length,0);
  await h.click('Edit messages');assert.equal(h.field('Message to Ada').value,body);
});
globalThis.document=oldDocument;
console.log(`crm_compose: ${passed} independent cases passed; ${failed} failed`);if(failed)process.exitCode=1;
