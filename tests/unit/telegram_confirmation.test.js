import assert from 'node:assert/strict';
import {mkdtempSync,rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {ControlCore} from '../../master/control-core.mjs';
import {ControlDomain} from '../../master/control-domain.mjs';

const owner='@david:fixture',room='!ada:fixture',native='!native:fixture';
const event=(id,text,at=1700000000001)=>({event_id:'$'+id,type:'m.room.message',sender:owner,
  origin_server_ts:at,content:{msgtype:'m.text',body:text,'com.jkali.from_me':false,'com.jkali.origin_ts':at}});
function fixture(path=':memory:'){
  const h={now:1700000000100,sent:[],writes:[],proposals:[],events:[event('incoming','Tuesday?')],mode:'direct',stateReads:0};
  const state=()=>[
    {type:'m.room.create',state_key:'',sender:owner,content:{'com.jkali.mirror_of':native}},
    {type:'com.jkali.share_level',state_key:'',sender:owner,content:{level:h.mode,revision:1}},
  ];
  h.domain=new ControlDomain({manager:'@manager:fixture',roster:async()=>({}),bootstrap:async()=>{},clock:()=>h.now,
    api:async(method,path,body)=>{
      if(path.endsWith('/state')){h.stateReads++;h.onState?.();return state();}
      if(path.includes('/messages?')){const from=Number(new URL('http://fixture'+path).searchParams.get('from')||0),events=decodeURIComponent(path).includes('!proposals:fixture')?h.proposals:h.events;return {start:String(from),end:String(events.length),chunk:events.slice(from)};}
      assert.equal(method,'PUT');assert(path.includes('/send/com.jkali.proposal/'),'only the source proposal route may be written');h.writes.push({path,body});
      const id='$submitted-'+h.writes.length;h.proposals.push({event_id:id,type:'com.jkali.proposal',sender:'@manager:fixture',content:body});
      if(h.loseReply)throw Error('Response lost');return {event_id:id};
    }});
  h.domain.rooms.set(room,{id:room,owner,native,title:'Ada',source:'imessage',level:'direct',consent:{level:'direct',revision:1},state:state()});
  h.domain.accounts.set(owner,{generation:'generation',journal_epoch:'epoch',proposal_room_id:'!proposals:fixture'});
  h.domain.account=async()=>({state:[]});h.domain.refresh=async()=>{};h.domain.hydrateAccounts=async()=>{};
  h.core=new ControlCore({path,domain:h.domain,clock:()=>h.now,telegram:async(method,data)=>{h.sent.push({method,...data});return {message_id:h.sent.length};},model:async()=>{throw Error('Model unavailable');}});
  h.core.set('operator',{user:7,chat:7});
  h.prepare=body=>h.core.tool('prepare_send',{room_id:room,body:body||'Tuesday works.'});
  h.review=async draft=>{h.core.enqueue('review-'+draft.id,h.core.review(draft),{draft:draft.id,room,review:true});await h.core.flush();};
  h.close=()=>h.core.db.close();return h;
}

// Lost acknowledgment reconciles the exact draft identity in source proposal
// history. It never retries a native or proposal write or matches just text.
{
 const h=fixture();try{
  const d=await h.prepare();h.loseReply=true;await assert.rejects(h.core.confirm(d.id),/not confirmed/);assert.equal(h.writes.length,1);
  const status=await h.core.tool('send_status',{draft_id:d.id});assert.equal(status.state,'submitted');assert.equal(status.result.proposal_id,'$submitted-1');
  await h.core.confirm(d.id);assert.equal(h.writes.length,1);
 }finally{h.close();}
}
for(const collision of ['same-text','wrong-author','duplicate-id']){
 const h=fixture();try{
  const d=await h.prepare();h.loseReply=true;await assert.rejects(h.core.confirm(d.id));
  if(collision==='same-text')h.proposals[0].content={...d.content,'com.jkali.control_draft_id':'another-draft'};
  if(collision==='wrong-author')h.proposals[0].sender='@other:fixture';
  if(collision==='duplicate-id')h.proposals.push({...h.proposals[0],event_id:'$duplicate'});
  if(collision==='duplicate-id')await assert.rejects(h.core.tool('send_status',{draft_id:d.id}),/Conflicting proposal/);
  else assert.equal((await h.core.tool('send_status',{draft_id:d.id})).state,'unknown');
  assert.equal(h.writes.length,1);
 }finally{h.close();}
}

// Actual domain version includes text, edits, redactions and imported history,
// rather than treating an unchanged latest event ID as unchanged context.
for(const change of ['edit','flat-mirrored-edit','redaction','backfill','consent','generation','expiry']){
  const h=fixture();try{
    const draft=await h.prepare();assert.match(h.core.review(draft),/Latest message: Tuesday\?/);assert.match(h.core.review(draft),/Send exactly:\nTuesday works\./);
    if(change==='edit')h.events.push({...event('edit','edited'),content:{'m.relates_to':{rel_type:'m.replace',event_id:'$incoming'},'m.new_content':{msgtype:'m.text',body:'Actually Wednesday?'}}});
    if(change==='flat-mirrored-edit')h.events.push({...event('edit','edited'),content:{'m.relates_to':{rel_type:'m.replace',event_id:'$incoming'},msgtype:'m.text',body:'Actually Wednesday?'}});
    if(change==='redaction')h.events.push({...event('delete',''),type:'m.room.redaction',content:{redacts:'$incoming'}});
    if(change==='backfill')h.events.push(event('older','An older message imported late',1699999999000));
    if(change==='consent')h.mode='share';
    if(change==='generation')h.domain.accounts.set(owner,{generation:'replacement',journal_epoch:'epoch',proposal_room_id:'!proposals:fixture'});
    if(change==='expiry')h.now+=600001;
    await assert.rejects(h.core.confirm(draft.id),/changed|expired/);assert.equal(h.writes.length,0,change);
  }finally{h.close();}
}
{
 const h=fixture();try{
  const draft=await h.prepare();h.onState=()=>{if(h.stateReads>=3)h.mode='private';};
  await assert.rejects(h.core.confirm(draft.id),/not confirmed/);assert.equal(h.writes.length,0,'execution rechecks live consent after confirmation preflight');
 }finally{h.close();}
}

// A history quote, unknown quote or wrong draft ID cannot turn a bare yes into
// approval. Explicit IDs and the complete delivered review remain usable.
{
 const h=fixture();try{
  const d=await h.prepare();await h.review(d);
  for(const ref of [{room,cursor:'history'},{unknown_quote:true}])await assert.rejects(h.core.command('yes',ref,'bad-'+JSON.stringify(ref)),/not a draft/);
  await assert.rejects(h.core.command('/confirm another-id',{draft:d.id},'bad-id'),/does not match/);
  assert.equal(h.writes.length,0);
  await h.core.command('/confirm '+d.id,null,'confirmed');await h.core.command('/confirm '+d.id,null,'repeated');assert.equal(h.writes.length,1);
  assert.equal(h.writes[0].body['com.jkali.send_mode'],'direct');
 }finally{h.close();}
}
{
 const h=fixture();try{
  const a=await h.prepare('First'),b=await h.prepare('Second');await h.review(a);await h.review(b);
  await assert.rejects(h.core.command('yes',null,'ambiguous'),/exact draft/);assert.equal(h.writes.length,0);
  await h.core.command('yes',{draft:b.id},'second');assert.equal(h.writes.length,1);assert.equal(h.writes[0].body.body,'Second');
 }finally{h.close();}
}
{
 const h=fixture();try{
  const d=await h.prepare();h.core.enqueue('multipart','review '.repeat(15000),{draft:d.id,room,review:true});await h.core.flush();
  await assert.rejects(h.core.command('yes',{draft:d.id},'incomplete-review'),/full review/);assert.equal(h.writes.length,0);
  for(let i=0;i<4;i++)await h.core.flush();
  await h.core.command('yes',{draft:d.id},'complete-review');assert.equal(h.writes.length,1);
 }finally{h.close();}
}

// Unknown Telegram quote references survive parsing as unknown, not as an
// unquoted command; two interleaved alert replies cannot switch destinations.
{
 const h=fixture();try{
  const d=await h.prepare();await h.review(d);
  h.core.ingest([{update_id:1,message:{message_id:10,chat:{id:7,type:'private'},from:{id:7},text:'yes',reply_to_message:{message_id:999}}}]);
  await h.core.process();assert.equal(h.writes.length,0);
  let calls=0;h.core.model=async()=>++calls===1?{role:'assistant',tool_calls:[{id:'wrong-room',type:'function',function:{name:'prepare_send',arguments:JSON.stringify({room_id:'!other:fixture',body:'Hello'})}}]}:{role:'assistant',content:'Please start a new request to switch conversations.'};
  await h.core.command('Tell her hello',{room},'quoted-alert');assert.equal(h.core.status().drafts.length,1,'wrong-account draft was not created');
 }finally{h.close();}
}

// Deterministic cancel works without the model or even Matrix reads for a
// pending draft. Submitted individual messages are never falsely recalled.
{
 const h=fixture();try{
  const d=await h.prepare();h.domain.refresh=async()=>{throw Error('Matrix offline');};
  await h.core.command('/cancel '+d.id,null,'cancel');assert.equal((await h.core.confirm(d.id)).state,'canceled');assert.equal(h.writes.length,0);
  h.domain.refresh=async()=>{};const sent=await h.prepare();await h.core.confirm(sent.id);
  await assert.rejects(h.core.cancel(sent.id),/cannot be recalled/);assert.equal(h.writes.length,1);
 }finally{h.close();}
}
{
 const h=fixture();try{
  const manifest={batch_id:'batch',items:[],children:[]},calls=[];
  h.core.draft({id:'batch-draft',kind:'batch',created:h.now,manifest,contexts:[]});
  h.core.db.prepare("UPDATE drafts SET state='submitted' WHERE id='batch-draft'").run();
  h.domain.batches={cancel:async frozen=>{calls.push(frozen);return [{child_id:'david',state:'canceled'},{child_id:'elliot',state:calls.length===1?'cancellation_unconfirmed':'canceled'}];},statuses:async()=>[{child_id:'david',state:'canceled'}]};
  const plan=await h.core.tool('prepare_cancel',{draft_id:'batch-draft'});assert.equal(calls.length,0,'model preparation does not cancel');
  await h.core.confirm(plan.id);assert.deepEqual(calls,[manifest]);
  assert.equal((await h.core.tool('send_status',{draft_id:'batch-draft'})).cancellation,'cancellation_requested');
  await h.core.cancel('batch-draft');await h.core.cancel('batch-draft');assert.equal(calls.length,2,'terminal cancellation is idempotent');assert.equal(h.writes.length,0);
 }finally{h.close();}
}
{
 const dir=mkdtempSync(join(tmpdir(),'beepa-cancel-')),path=join(dir,'test.db');
 try{
  const h=fixture(path);h.core.db.prepare("INSERT INTO cancellations VALUES('draft','canceling',NULL)").run();h.close();
  const next=fixture(path);assert.equal(next.core.db.prepare('SELECT state FROM cancellations').get().state,'cancellation_unconfirmed');assert.equal(next.writes.length,0);next.close();
 }finally{rmSync(dir,{force:true,recursive:true});}
}

// The public tool schema is enforced in the service, not merely suggested to
// the model. Forged confirmations, extra fields and malformed calls do nothing.
{
 const h=fixture();try{
  await assert.rejects(h.core.tool('confirm',{id:'anything'}),/Unknown tool/);
  for(const args of [null,[],{room_id:room,body:'Hi',confirmed:true},{room_id:room,body:42}])await assert.rejects(h.core.tool('prepare_send',args),/Invalid|Unknown/);
  for(const response of [null,{tool_calls:{}},{tool_calls:[null]},{tool_calls:[{id:'x',type:'function',function:{name:'prepare_send',arguments:{}}}]}]){
    h.core.model=async()=>response;await assert.rejects(h.core.command('Prepare a reply',null,'invalid'),/Invalid model/);
  }
  assert.equal(h.core.status().drafts.length,0);assert.equal(h.writes.length,0);
 }finally{h.close();}
}
console.log('Telegram confirmations: context edits, revocation, quote isolation, complete reviews, ID controls, batch cancellation, restart and model argument gates passed');
