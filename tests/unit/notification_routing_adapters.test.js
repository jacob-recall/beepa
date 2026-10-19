import assert from 'node:assert/strict';
import {ControlCore} from '../../master/control-core.mjs';
import {NotificationRouting} from '../../master/notification-routing.mjs';
import {notificationAdapters,telegramNotificationKey} from '../../master/notification-routing-adapters.mjs';

function fixture(){
  const h={now:1700000000010,sent:[],browserCalls:[],browserState:'pending',revoked:false};
  h.rule={list_id:'test',list_name:'Followed',revision:1,room:'!shared:test',owner:'@david:test',title:'Ada',source:'imessage',channels:['browser','telegram'],routing:{primary:'telegram',fallback:true},all_messages:false,preview:false};
  h.rules=[h.rule];h.source={kind:'message',room:h.rule.room,event:'$new',outgoing:false};
  const domain={rules:()=>h.rules,rooms:new Map([[h.rule.room,h.rule]]),refresh:async()=>{},hydrateAccounts:async()=>{},
    liveRoom:async()=>{if(h.revoked)throw Error('Sharing revoked');return h.rule;},updates:async()=>h.updates||[],history:async()=>({messages:[{id:'$new',text:'Fresh preview'}]})};
  h.core=new ControlCore({path:':memory:',domain,clock:()=>h.now,model:async()=>{throw Error('No model in dispatch');},telegram:async(method,payload)=>{h.sent.push({method,payload});if(h.failure)throw h.failure;return {message_id:h.sent.length};}});
  h.core.bindTransport({id:1,is_bot:true,username:'test_bot'});h.core.set('operator',{user:1,chat:1});
  h.router=new NotificationRouting({db:h.core.db,clock:()=>h.now});
  h.adapters=notificationAdapters({core:h.core,sourceFor:id=>h.router.status(id)?.source,browser:async data=>{
    h.browserCalls.push(data);if(h.browserError)throw Error('lost acknowledgment');
    const id=data.ids?.[0]||h.id;
    return {devices:1,outcomes:[{id:h.wrongId?'wrong':id,state:h.browserState,deliveries:[]}]};
  }});
  h.observe=()=>h.id=h.router.observe(h.source,h.rules);
  h.drain=()=>h.router.drain({adapters:h.adapters,validate:async()=>({rules:h.revoked?[]:h.rules,ready:['test']})});
  h.close=()=>h.core.db.close();return h;
}
// Actual Telegram outbox rejection releases browser fallback without native sends.
{
  const h=fixture();try{
    h.observe();await h.drain();assert.equal(h.sent.length,0);assert.equal(h.browserCalls.length,0);
    h.failure=Object.assign(Error('blocked'),{status:403});await h.core.flush();await h.drain();
    assert.equal(h.sent.length,1);assert.equal(h.browserCalls.filter(c=>c.action==='control_enqueue').length,1);
    const entry=h.browserCalls.find(c=>c.action==='control_enqueue').entries[0];
    assert.deepEqual(entry,{room:h.source.room,event:'$new',kind:'message',refs:{outgoing:false,rules:[{id:'test',revision:1}]}});
    await h.drain();assert.equal(h.browserCalls.filter(c=>c.action==='control_enqueue').length,1);
  }finally{h.close();}
}
// Accepted and timeout outcomes from the real outbox never release fallback.
for(const failure of [null,Error('response lost')]){
  const h=fixture();try{
    h.failure=failure;h.observe();await h.drain();await h.core.flush();await h.drain();
    assert.equal(h.sent.length,1);assert.equal(h.browserCalls.length,0);
    assert.equal(await h.adapters.telegram.outcome(h.id),failure?'unknown':'accepted');
  }finally{h.close();}
}
// The outbox owns bounded provider retries. Router does not call send itself or
// treat an explicit retryable response as a failed primary.
{
  const h=fixture();try{
    h.failure=Object.assign(Error('rate limited'),{status:429});h.observe();await h.drain();await h.core.flush();await h.drain();assert.equal(h.browserCalls.length,0);
    h.now+=60001;h.failure=null;await h.core.flush();await h.drain();assert.equal(h.sent.length,2);assert.equal(h.browserCalls.length,0);
  }finally{h.close();}
}
// Unpaired transport is known unavailable before an external delivery attempt.
{
  const h=fixture();try{h.core.disconnect();h.observe();await h.drain();assert.equal(h.sent.length,0);assert.equal(h.browserCalls.filter(c=>c.action==='control_enqueue').length,1);}finally{h.close();}
}
// Adapter reuses old worker IDs, including retired bot outcome records.
for(const retired of [false,true]){
  const h=fixture();try{
    const key=telegramNotificationKey(h.source);h.core.enqueue(key,'Previously delivered');await h.core.flush();
    if(retired){h.core.bindTransport({id:2,is_bot:true,username:'replacement_bot'});h.core.set('operator',{user:1,chat:1});}
    h.observe();await h.drain();await h.core.flush();await h.drain();assert.equal(h.sent.length,1);assert.equal(h.browserCalls.length,0);assert.equal(await h.adapters.telegram.outcome(h.id),'accepted');
  }finally{h.close();}
}
// Changing scope between queue and delivery cancels through existing core checks.
{
  const h=fixture();try{h.observe();await h.drain();h.revoked=true;await h.core.flush();await h.drain();assert.equal(h.sent.length,0);assert.equal(h.browserCalls.length,0);}finally{h.close();}
}
// Existing presentation and fresh-preview behavior remains in the real outbox.
{
  const h=fixture();try{
    h.rule.presentation={mode:'grouped',interval_ms:60000};h.rule.preview=true;h.observe();await h.drain();await h.core.flush();assert.equal(h.sent.length,0);
    h.now+=60000;await h.core.flush();await h.drain();assert.equal(h.sent.length,1);assert.match(h.sent[0].payload.text,/Fresh preview/);assert.equal(h.core.ref(1,1).room,h.rule.room);
  }finally{h.close();}
}
// Browser first: known absence falls back, but malformed/lost adapter responses
// are uncertain. A response for another event is never accepted as evidence.
for(const scenario of ['unavailable','lost','wrong-id']){
  const h=fixture();try{
    h.rule.routing.primary='browser';h.browserState='unavailable';h.browserError=scenario==='lost';h.wrongId=scenario==='wrong-id';
    h.observe();await h.drain();await h.core.flush();await h.drain();assert.equal(h.sent.length,scenario==='unavailable'?1:0);
  }finally{h.close();}
}
// Reminder route retains source and per-list slot; resolved evidence cancels it.
{
  const h=fixture();try{
    h.source={kind:'reminder',room:h.rule.room,event:'$waiting',due:h.now-1000,slot:h.now-1000};h.rule.delivery_slot=h.source.slot;
    h.updates=[{id:h.rule.room,list_id:'test',fresh:true,obligations:[{event_id:'$waiting',due_at:h.source.due,waiting:'waiting_for_us'}]}];
    h.observe();await h.drain();h.updates[0].obligations[0].waiting='answered';await h.core.flush();await h.drain();assert.equal(h.sent.length,0);assert.equal(h.browserCalls.length,0);
  }finally{h.close();}
}
console.log('notification routing adapters: real Telegram outbox, browser contracts, legacy IDs, retries, uncertainty, revoked access, grouping and reminder cancellation passed');
