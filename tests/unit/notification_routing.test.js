import assert from 'node:assert/strict';
import {DatabaseSync} from 'node:sqlite';
import {mkdtempSync,rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {NotificationRouting,notificationIdentity,notificationRouting} from '../../master/notification-routing.mjs';

const source=(event='$message')=>({room:'!shared:test',event,kind:'message',outgoing:false});
const rule=(id='list',routing={primary:'browser',fallback:true})=>({room:'!shared:test',list_id:id,revision:1,channels:['browser','telegram'],routing});
function fixture(path=':memory:'){
  const h={db:new DatabaseSync(path),now:1700000000000,rules:[rule()],ready:['list'],calls:[],outcomes:new Map(),results:{browser:'pending',telegram:'accepted'}};
  h.router=new NotificationRouting({db:h.db,clock:()=>h.now});
  h.adapters=Object.fromEntries(['browser','telegram'].map(channel=>[channel,{
    async enqueue(job){h.calls.push(job);if(h.before)await h.before(job);if(h.results[channel] instanceof Error)throw h.results[channel];h.outcomes.set(job.id+channel,h.results[channel]);return h.results[channel];},
    async outcome(id){return h.outcomes.get(id+channel)||'missing';},
  }]));
  h.validate=async()=>({rules:h.rules,ready:h.ready});
  h.observe=s=>h.router.observe(s||source(),h.rules);
  h.drain=options=>h.router.drain({validate:h.validate,adapters:h.adapters,...options});
  h.close=()=>h.db.close();return h;
}
// Defaults preserve both-channel behavior; preferences are explicit and strict.
assert.deepEqual(notificationRouting({channels:['telegram','browser']}),{channels:['browser','telegram'],primary:null,fallback:false});
for(const r of [{channels:['sms']},{channels:['browser','browser']},rule('x',{primary:'sms',fallback:true}),{channels:['browser'],routing:{primary:'browser',fallback:true}},rule('x',{primary:'browser',fallback:true,other:1})])assert.throws(()=>notificationRouting(r));
for(const s of [{...source(),body:'must never persist message text'},{...source(),room:'not a room'},{...source(),outgoing:1},{kind:'reminder',room:'!shared:test',event:'uncontacted',due:100,slot:99}])assert.throws(()=>notificationIdentity(s));

// Pending, in-flight, accepted, and uncertain delivery never enable fallback.
for(const outcome of ['pending','in_flight','unknown','accepted',new Error('response lost')]){
  const h=fixture();try{
    h.results.browser=outcome;const id=h.observe();await h.drain();await h.drain();
    assert.deepEqual(h.calls.map(j=>j.channel),['browser']);
    assert.equal(h.router.status(id).attempts[0].state,outcome instanceof Error?'unknown':outcome);
  }finally{h.close();}
}
// Only definitive negative evidence triggers the other channel, once.
for(const outcome of ['failed','expired','unavailable']){
  const h=fixture();try{
    h.results.browser=outcome;const id=h.observe();await h.drain();await h.drain();
    assert.deepEqual(h.calls.map(j=>j.channel),['browser','telegram']);
    assert.equal(h.router.status(id).rules[0].chosen,'telegram');
    h.outcomes.set(id+'browser','pending');await h.drain();assert.equal(h.calls.length,2);
  }finally{h.close();}
}
{
  const h=fixture();try{
    h.rules=[rule('list',{primary:'telegram',fallback:false})];h.results.telegram='failed';h.observe();await h.drain();await h.drain();assert.deepEqual(h.calls.map(j=>j.channel),['telegram']);
  }finally{h.close();}
}
// Cancellation/revocation is not transport failure and must never fall through.
{
  const h=fixture();try{h.results.browser='canceled';h.observe();await h.drain();await h.drain();assert.equal(h.calls.length,1);}finally{h.close();}
}
// Acknowledgment loss reconciles by ID without enqueueing again. No endpoint,
// message body, preview, name, or credential belongs in the routing ledger.
{
  const h=fixture();try{
    h.results.browser=new Error('lost');const id=h.observe();await h.drain();
    h.outcomes.set(id+'browser','accepted');await h.drain();assert.equal(h.calls.length,1);
    h.router.reconcile(id,'browser','failed');assert.equal(h.router.status(id).attempts[0].state,'accepted');
    assert.deepEqual(h.calls[0].refs,{outgoing:false,rules:[{id:'list',revision:1}]});
    assert(!JSON.stringify(h.router.status(id)).includes('body'));
  }finally{h.close();}
}
// Overlapping preferred/both lists share one attempt per destination, carrying
// the exact policies that requested that attempt. Their decisions stay separate.
{
  const h=fixture();try{
    h.rules=[rule('preferred'),rule('both',null)];h.ready=['preferred','both'];h.results.browser='failed';const id=h.observe();await h.drain();await h.drain();
    assert.deepEqual(h.calls.map(j=>j.channel),['browser','telegram']);
    assert.deepEqual(h.calls[1].refs.rules.map(r=>r.id),['both','preferred']);
    assert.equal(h.router.status(id).rules.find(r=>r.list_id==='preferred').chosen,'telegram');
  }finally{h.close();}
}
// Rules do not get added to an already observed source event by replaying it.
{
  const h=fixture();try{
    const id=h.observe();h.rules.push(rule('new'));h.ready.push('new');assert.equal(h.observe(),id);
    assert.equal(h.observe({outgoing:false,event:'$message',room:'!shared:test',kind:'message'}),id,'object key order does not change source identity');
    await h.drain();assert.equal(h.calls[0].refs.rules.length,1);
    assert.throws(()=>h.observe({...source(),outgoing:true}),/identity changed/);
  }finally{h.close();}
}
// Schedule/digest deferral retains pending policy. Removed/revised rules cancel
// irreversibly; temporary validation outages do not cancel or send.
{
  const h=fixture();try{
    const id=h.observe();h.ready=[];await h.drain();assert.equal(h.calls.length,0);assert.equal(h.router.status(id).rules[0].state,'active');
    h.ready=['list'];h.validate=async()=>{throw Error('offline');};await h.drain();assert.equal(h.calls.length,0);
    h.validate=async()=>({rules:h.rules,ready:h.ready});h.rules[0].revision=2;await h.drain();assert.equal(h.router.status(id).rules[0].state,'canceled');
    h.rules[0].revision=1;await h.drain();assert.equal(h.calls.length,0);
  }finally{h.close();}
}
// Source authorization is revalidated after primary rejection before fallback.
{
  const h=fixture();try{
    h.results.browser='failed';h.before=async()=>{h.rules=[];};h.observe();await h.drain();assert.equal(h.calls.length,1);
  }finally{h.close();}
}
// One rejected list does not cancel another valid overlapping list.
{
  const h=fixture();try{
    h.rules=[rule('removed'),rule('kept',null)];h.ready=['removed','kept'];h.observe();h.rules=h.rules.slice(1);await h.drain();assert.equal(h.calls.length,2);assert.deepEqual(h.calls[0].refs.rules.map(r=>r.id),['kept']);
  }finally{h.close();}
}
// Single consumer + atomic claims protect against concurrent drain calls.
{
  const h=fixture();try{
    let release,entered;const gate=new Promise(r=>release=r),start=new Promise(r=>entered=r);h.before=async()=>{entered();await gate;};
    h.observe();const first=h.drain();await start;await h.drain();release();await first;assert.equal(h.calls.length,1);
  }finally{h.close();}
}
// Pending adapter evidence cannot starve newer source events behind the limit.
{
  const h=fixture();try{
    for(let n=0;n<5;n++)h.observe(source('$'+n));
    for(let n=0;n<5;n++)await h.drain({limit:1});assert.equal(h.calls.length,5);
  }finally{h.close();}
}
// Reminder identity matches browser enqueue and keeps each policy's repeat slot.
{
  const h=fixture();try{
    const s={kind:'reminder',room:'!shared:test',event:'$original',due:100,slot:200};h.rules[0].delivery_slot=200;const id=h.observe(s);await h.drain();
    assert.equal(id,notificationIdentity(s));assert.deepEqual(h.calls[0].refs,{due:100,slot:200,rules:[{id:'list',revision:1,slot:200}]});
    const earlier=h.router.observe({...s,slot:100},[{...h.rules[0],delivery_slot:100}]);assert.notEqual(earlier,id);
  }finally{h.close();}
}
// Validate the entire observation before inserting any durable state.
{
  const h=fixture();try{assert.throws(()=>h.router.observe(source(),[rule(),rule()]));assert.equal(h.db.prepare('SELECT count(*) n FROM notification_routes').get().n,0);}finally{h.close();}
}
// Crash after durable claim: restart preserves uncertainty; adapter absence is
// not failure. Late definitive outcome can release fallback without replay.
{
  const dir=mkdtempSync(join(tmpdir(),'beepa-routing-')),path=join(dir,'state.db');let h=fixture(path);
  try{
    const id=h.observe(),job=h.router.plan(id,{rules:h.rules,ready:h.ready})[0];assert(h.router.claim(job));assert(!h.router.claim(job));h.close();h=fixture(path);
    await h.drain();assert.equal(h.calls.length,0);assert.equal(h.router.status(id).attempts[0].state,'unknown');
    h.outcomes.set(id+'browser','failed');await h.drain();assert.deepEqual(h.calls.map(j=>j.channel),['telegram']);
    h.close();h=fixture(path);await h.drain();assert.equal(h.calls.length,0);
  }finally{h.close();rmSync(dir,{recursive:true,force:true});}
}
console.log('notification routing: preferences, overlap, uncertainty, reconciliation, revocation, scheduling, fairness, and restart passed');
