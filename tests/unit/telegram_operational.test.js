import assert from 'node:assert/strict';
import {ControlCore} from '../../master/control-core.mjs';
import {ControlDomain} from '../../master/control-domain.mjs';

const owner='@owner:test',room='!room:test',native='!native:test',catalog='!contacts:test';
function fixture(){
 const f={now:1700000000000,sent:[],shared:true,version:'1',mode:'direct',owner,validated:[]};
 const d=new ControlDomain({manager:'@manager:test',clock:()=>f.now,roster:async()=>({}),bootstrap:async()=>{},api:async(_,path)=>{
   if(decodeURIComponent(path).includes(catalog))return [{type:'m.room.create',sender:owner,state_key:'',content:{}},{type:'com.jkali.contacts',sender:owner,state_key:'',content:{}},
    {type:'com.jkali.contact',sender:f.owner,state_key:'contact',content:{source:'imessage',network_id:'address',contact_version:f.version,deleted:!f.shared}}];
   return [{type:'m.room.create',sender:owner,state_key:'',content:{'com.jkali.mirror_of':native}},
    {type:'com.jkali.share_level',sender:owner,state_key:'',content:{level:f.mode}}];
 }});
 d.contactRooms=new Map([[owner,[catalog]]]);d.rooms.set(room,{id:room,native,owner,level:'direct'});
 d.members.validate=async p=>f.validated.push(p);d.batches.validate=async p=>f.validated.push(p);
 const binding={binding_id:'binding',owner_mxid:owner,contact:{source:'imessage',network_id:'address'},contact_version:'1'};
 d.state=()=>({bindings:{binding}});d.refresh=async()=>{};d.hydrateAccounts=async()=>{};
 const c=new ControlCore({path:':memory:',clock:()=>f.now,domain:d,telegram:async(_,payload)=>{f.sent.push(payload);f.onSend?.();return {message_id:f.sent.length};},model:async()=>{}});
 c.set('operator',{user:1,chat:1});
 return Object.assign(f,{d,c,binding});
}
{
 const f=fixture(),c=f.c;
 c.setHealth('telegram_poll','Polling unavailable');c.setHealth('monitoring');assert.match(c.status().poll_error,/Polling unavailable/);
 c.setHealth('model','Model unavailable');c.setHealth('telegram_poll');assert.match(c.status().poll_error,/Model unavailable/);
 c.setHealth('model');assert.equal(c.status().poll_error,null);f.now+=90001;
 assert.equal(c.status().health.telegram_poll.stale,true);assert.equal(c.status().health.monitoring.stale,false);
 c.setHealth('telegram_poll');assert.equal(c.status().health.telegram_poll.stale,false);
 c.telegram=async()=>{const error=Error('untrusted error');error.status=403;throw error;};c.enqueue('blocked','Test');await c.flush();
 c.setHealth('monitoring');c.setHealth('telegram_poll');assert.match(c.status().poll_error,/delivery blocked/);assert(!JSON.stringify(c.status()).includes('untrusted error'));
 assert.equal(c.status().delivery_outcomes.find(o=>o.state==='failed').count,1);c.db.close();
}
for(const kind of ['contact','batch'])for(const change of ['delete','version','foreign-author','private-room']){
 if(kind==='contact'&&change==='private-room')continue;
 const f=fixture();
 try{
   const draft={id:'review',kind,created:f.now,...(kind==='contact'?{plan:{binding:f.binding}}:{manifest:{items:[{binding_id:'binding',owner_mxid:owner,route:{mode:'direct_existing',room_id:native}}]}})};
   f.c.draft(draft);await f.d.validateReviewScope(draft);assert.equal(f.validated.length,1);
   if(change==='delete')f.shared=false;if(change==='version')f.version='2';if(change==='foreign-author')f.owner='@forger:test';if(change==='private-room')f.mode='private';
   f.c.enqueue('review','Private review content',{draft:'review',review:true});await f.c.flush();
   assert.equal(f.sent.length,0);assert.equal((await f.c.confirm('review')).state,'review_required');
   assert.equal((await f.c.cancel('review')).state,'review_required');
   assert.equal((await f.c.tool('send_status',{draft_id:'review'})).state,'review_required');
   assert(!f.c.db.prepare('SELECT payload FROM outbox').get().payload.includes('Private review content'));
   assert.deepEqual(JSON.parse(f.c.db.prepare('SELECT payload FROM drafts').get().payload),{id:'review',kind,created:f.now});
 }finally{f.c.db.close();}
}
{
 const f=fixture();try{
   f.c.draft({id:'multipart',kind:'contact',created:f.now,plan:{binding:f.binding}});
   f.c.enqueue('multipart','Sensitive content '.repeat(700),{draft:'multipart',review:true});f.onSend=()=>{f.shared=false;};
   await f.c.flush();assert.equal(f.sent.length,1,'revoke after first part suppresses every remaining part');
   await assert.rejects(f.c.command('/confirm multipart',null,'confirm'),/full review/);
 }finally{f.c.db.close();}
}
{
 const f=fixture();try{
   f.c.draft({id:'delivered-review',kind:'contact',created:f.now,plan:{binding:f.binding}});
   f.c.enqueue('old','Private review content',{draft:'delivered-review',review:true});await f.c.flush();assert.equal(f.sent.length,1);
   f.shared=false;await f.c.invalidateReviews();assert.equal((await f.c.confirm('delivered-review')).state,'review_required');
   f.c.enqueue('history','Previously shared history',{room});f.mode='private';await f.c.flush();assert.equal(f.sent.length,1);
 }finally{f.c.db.close();}
}
console.log('Operational controls: independent health failures, stale polling, delivery errors and access-revoked multipart/contact/batch reviews passed.');
