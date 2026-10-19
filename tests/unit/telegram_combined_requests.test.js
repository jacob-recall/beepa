import assert from 'node:assert/strict';
import {randomUUID} from 'node:crypto';
import {mkdtempSync,rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {ControlCore} from '../../master/control-core.mjs';
import {ControlDomain} from '../../master/control-domain.mjs';
import {createCRMState,applyCRMOperation} from '../../shared/model/crm.js';

const ROOM='!gerry:test',OWNER='@jacob:test';
const call=(name,args)=>({id:randomUUID(),type:'function',function:{name,arguments:JSON.stringify(args)}});
const response=(...calls)=>({tool_calls:calls});
const send=()=>call('prepare_send',{room_id:ROOM,body:'A synthetic message'});
const watch=()=>call('prepare_notifications',{room_id:ROOM});
const finish=(remaining=[])=>response(call('finish_request',{remaining}));
function fixture(path=':memory:'){
  let state=createCRMState({workspace_id:'!crm:test',manager_mxid:'@manager:test'});
  const f={steps:[],sent:[],writes:[],submissions:[],now:1700000000000};
  const domain=new ControlDomain({manager:'@manager:test',bootstrap:async()=>{},roster:async()=>({}),api:async()=>{throw Error('Unexpected network request');},clock:()=>f.now});
  domain.rooms.set(ROOM,{id:ROOM,owner:OWNER,title:'Gerry',source:'gmessages',native:'!native:test'});
  domain.state=()=>state;domain.refresh=async()=>{};domain.hydrateAccounts=async()=>{};domain.contextStamp=undefined;
  domain.liveRoom=async id=>domain.room(id);
  domain.store={refresh:async()=>{},append:async op=>{
    const next=applyCRMOperation(state,op);assert(next.outcome.accepted,next.outcome.code);state=next.state;f.writes.push(op);return next.outcome;
  }};
  domain.prepareSend=async({room_id,body})=>({id:randomUUID(),kind:'send',room_id,owner:OWNER,body,created:f.now,conversation:domain.room(room_id),content:{'com.jkali.send_mode':'direct'}});
  domain.validateSend=async()=>{};domain.executeSend=async d=>{f.submissions.push(d);return {proposal_id:'$synthetic',mode:'direct'};};
  const core=new ControlCore({path,domain,clock:()=>f.now,telegram:async(method,p)=>{if(method==='sendMessage'){f.sent.push(p);return {message_id:f.sent.length};}return true;},model:async()=>{assert(f.steps.length,'Unexpected model call');const step=f.steps.shift();if(step instanceof Error)throw step;return step;}});
  core.set('operator',{user:7,chat:7});
  const drafts=()=>core.db.prepare('SELECT * FROM drafts ORDER BY rowid').all().map(r=>({...r,data:JSON.parse(r.payload)}));
  return {...f,domain,core,drafts,state:()=>state,close:()=>core.db.close()};
}

// Both parallel tool calls and sequential model rounds must retain both actions.
for(const parallel of [true,false]){
  const f=fixture();try{
    f.steps.push(...(parallel?[response(send(),watch())]:[response(send()),response(watch())]),finish());
    await f.core.command('Send this to Gerry and notify me when he replies',null,'combined');
    const drafts=f.drafts();assert.equal(drafts.length,2,JSON.stringify(f.core.db.prepare('SELECT payload FROM outbox').all()));assert.equal(f.writes.length,0);assert.equal(f.submissions.length,0);
    await f.core.flush();const reviews=f.sent.filter(p=>p.reply_markup);assert.equal(reviews.length,2);
    assert.notEqual(reviews[0].reply_markup.inline_keyboard[0][0].callback_data,reviews[1].reply_markup.inline_keyboard[0][0].callback_data);
    assert.match(f.sent.at(-1).text,/2 actions prepared.*Nothing has been sent or enabled/);
    await assert.rejects(f.core.command('yes',null,'ambiguous'),/exact draft/);
    for(const d of drafts){f.core.requireTelegramReview(d.id);await f.core.confirm(d.id);await f.core.confirm(d.id);}
    assert.equal(f.submissions.length,1,'confirming either review twice never resends');
    const list=f.state().lists['browser-followed-conversations'];assert(list.monitoring.enabled);assert(list.watches[ROOM].enabled);
    assert.deepEqual(list.watches[ROOM].channels,['browser','telegram']);assert.equal(list.watches[ROOM].all_messages,false);
    assert.equal(f.domain.rules().filter(r=>r.room===ROOM&&r.channels.includes('telegram')&&r.channels.includes('browser')&&!r.all_messages).length,1);
    assert.equal(f.writes.length,3);assert.equal(Object.keys(f.state().memberships).length,0,'monitoring never adds Direct membership');
  }finally{f.close();}
}
// A repeated preparation does not produce duplicate drafts or reviews.
{
  const f=fixture();try{f.steps.push(response(send()),response(send(),watch()),finish());
    await f.core.command('Send and watch',null,'dedup');assert.equal(f.drafts().length,2);
    await f.core.flush();assert.equal(f.sent.filter(p=>p.reply_markup).length,2);
  }finally{f.close();}
}
// Failure after one review must be visible, and never executes that review.
for(const failure of ['model','tool','clarification']){
  const f=fixture();try{
    f.steps.push(response(send()));
    if(failure==='model')f.steps.push(Error('Model timeout'));
    else if(failure==='tool')f.steps.push(response(call('prepare_notifications',{room_id:'!missing:test'})),finish(['Notification setup failed; choose a shared conversation.']));
    else f.steps.push(finish(['Which conversation should receive reply alerts?']));
    await f.core.command('Send and notify',null,'partial');await f.core.flush();
    assert.equal(f.drafts().length,1);assert.equal(f.submissions.length,0);assert.equal(f.writes.length,0);
    assert.match(f.sent.at(-1).text,/Preparation stopped|Still needs attention/);
  }finally{f.close();}
}
// Plans require the exact current list revision and do not
// enable disabled unrelated watches. Interrupted application stays unknown.
for(const scenario of ['disabled-list','stale','revoked','excluded-owner','notifications-off','partial','cancel']){
  const f=fixture();try{
    if(scenario==='disabled-list'){
      await f.domain.apply(f.domain.operation('list.create','browser-followed-conversations',0,{name:'Existing',purpose:'monitoring'}));
    }
    const d=await f.core.tool('prepare_notifications',{room_id:ROOM});
    const before=f.writes.length;
    if(scenario==='stale')await f.domain.apply(f.domain.operation('list.create','browser-followed-conversations',0,{name:'Someone else created this'}));
    if(scenario==='revoked')f.domain.rooms.delete(ROOM);
    if(['excluded-owner','notifications-off'].includes(scenario)){
      const settings=f.domain.settings();f.domain.settings=()=>({...settings,...(scenario==='excluded-owner'?{allowed_accounts:['@someone:test']}:{notifications_enabled:false})});
    }
    if(['stale','revoked','excluded-owner','notifications-off'].includes(scenario)){await assert.rejects(f.core.confirm(d.id));assert.equal(f.writes.length,before+(scenario==='stale'?1:0));}
    else if(scenario==='cancel'){await f.core.cancel(d.id);await f.core.confirm(d.id);assert.equal(f.writes.length,before);}
    else if(scenario==='partial'){
      const apply=f.domain.apply.bind(f.domain);let attempts=0;f.domain.apply=async op=>{if(++attempts===2)throw Error('Connection lost');return apply(op);};
      await assert.rejects(f.core.confirm(d.id),/not confirmed/);assert.equal((await f.core.confirm(d.id)).state,'unknown');assert.equal(attempts,2);
    }else{
      await f.core.confirm(d.id);assert.equal(f.state().lists['browser-followed-conversations'].monitoring,undefined);
      assert(Object.values(f.state().lists).some(l=>l.watches?.[ROOM]?.enabled));
    }
    assert.equal(f.submissions.length,0);
  }finally{f.close();}
}
// Every review is attached to the durable API job. Restart preserves pending
// reviews and quarantines interrupted execution without replaying either action.
{
  const dir=mkdtempSync(join(tmpdir(),'beepa-combined-')),path=join(dir,'control.db'),f=fixture(path);
  let reopened;
  try{
    f.steps.push(response(send(),watch()),finish());
    f.core.enqueueCommand({request_id:'combined-api',text:'Send and notify'});await f.core.processCommand();
    assert.equal(f.core.commandStatus('combined-api').drafts.length,2);
    const drafts=f.drafts();f.core.db.prepare("UPDATE drafts SET state='executing' WHERE id=?").run(drafts[1].id);f.close();
    reopened=new ControlCore({path,domain:f.domain,clock:()=>f.now,telegram:async()=>true,model:async()=>{throw Error('Must not replay');}});
    assert.equal(reopened.commandStatus('combined-api').drafts.length,2);
    assert.equal((await reopened.confirm(drafts[1].id)).state,'unknown');
    assert.equal(f.writes.length,0);assert.equal(f.submissions.length,0);
    assert.equal(reopened.db.prepare('SELECT state FROM drafts WHERE id=?').get(drafts[0].id).state,'pending');
  }finally{reopened?.db.close();rmSync(dir,{recursive:true,force:true});}
}
console.log('Combined Telegram requests: all reviews, sequential/parallel tools, independent confirmation, dedup, partial failures, complete canonical notifications and stale/uncertain safeguards passed.');
