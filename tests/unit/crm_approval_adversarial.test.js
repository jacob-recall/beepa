import assert from 'node:assert/strict';
import { createCRMApprovalWriter, crmItemStatus } from '../../apps/user/crm-approval.js';
import { parseProposal, partitionProposals, attachDrafts } from '../../apps/user/proposals.js';
const clone=v=>JSON.parse(JSON.stringify(v));
const USER='@elliot:local', POLICY='!policy:local', INBOX='!inbox:local';
const item={schema:1,batch_id:'batch',child_id:'child',item_id:'item',owner_mxid:'@elliot:master',generation:'generation',journal_epoch:'epoch',payload_hash:'a'.repeat(64),child_hash:'b'.repeat(64)};
const event=(type,content,key='')=>({type,content,state_key:key,sender:USER});
function fixture(){
  const h={identity:{token:'synthetic',userId:USER},puts:[],status:null,hook:null,pollPending:false};
  h.cap={schema:1,crm_approval:true,generation:'generation',journal_epoch:'epoch',policy_room_id:POLICY};
  h.state=[event('m.room.create',{}),event('com.jkali.policy_control',{schema:1,journal_epoch:'epoch'}),event('m.room.member',{membership:'join'},USER),event('m.room.join_rules',{join_rule:'invite'}),event('m.room.history_visibility',{history_visibility:'joined'}),event('m.room.guest_access',{guest_access:'forbidden'}),event('m.room.power_levels',{users:{[USER]:100},events_default:100,state_default:100,invite:100,kick:100,ban:100,redact:100})];
  h.source={type:'com.jkali.proposal',sender:USER,event_id:'$proposal',room_id:INBOX,content:{body:'Original',target_room:'!person:local','com.jkali.crm.item':clone(item)}};
  h.proposal={...parseProposal(h.source),proposalRoomId:INBOX};
  h.result=(state='approval_pending')=>({...clone(item),local_proposal_event_id:'$proposal',state});
  h.statusEvents=()=>h.status?[event('com.jkali.crm.item_status',h.status,'child/item')]:[];
  h.api=async(method,path,body)=>{
    if(h.hook)await h.hook(method,path,body);
    if(path.includes('/account_data/'))return clone(h.cap);
    if(path==='/_matrix/client/v3/rooms/'+encodeURIComponent(POLICY)+'/state')return clone(h.state);
    if(path.includes('/event/'))return clone(h.source);
    if(method==='PUT'){
      assert.ok(path.includes('/send/com.jkali.crm.approval_request/'),'typed intent only');h.puts.push({path,body:clone(body)});
      if(!h.pollPending)h.status={...h.result(),approval:{request:clone(body),request_hash:path.split('/').at(-1).slice('crm_approval_'.length)}};
      return {event_id:'$intent'};
    }
    return clone(h.statusEvents());
  };
  h.writer=createCRMApprovalWriter({api:h.api,session:()=>h.identity,polls:2,wait:async()=>{}});
  return h;
}
let passed=0,failed=0;
async function test(name,fn){try{await fn();passed++;}catch(e){failed++;console.error('FAIL crm_approval_adversarial:',name,'\n',e.stack);}}
for(const [name,mutate] of [
  ['duplicate marker',s=>s.push(clone(s.find(e=>e.type==='com.jkali.policy_control')))],
  ['wrong marker epoch',s=>s.find(e=>e.type==='com.jkali.policy_control').content.journal_epoch='old'],
  ['former foreign member',s=>s.push(event('m.room.member',{membership:'leave'},'@foreign:local'))],
  ['low ordinary message power',s=>s.find(e=>e.type==='m.room.power_levels').content.events={'m.room.message':50}],
  ['forged creator',s=>s.find(e=>e.type==='m.room.create').sender='@other:local'],
  ['foreign power author',s=>s.find(e=>e.type==='m.room.power_levels').sender='@other:local'],
  ['tombstone',s=>s.push(event('m.room.tombstone',{replacement_room:'!other:local'}))],
])await test('private approval room rejects '+name,async()=>{const h=fixture();mutate(h.state);await assert.rejects(h.writer(h.proposal,'approve','Approved'));assert.equal(h.puts.length,0);});
await test('read-only pending source cannot be displayed as sent after intent PUT',async()=>{const h=fixture();h.pollPending=true;await assert.rejects(h.writer(h.proposal,'approve','Approved'),/awaiting.*no send has been confirmed/);assert.equal(h.puts.length,1);assert.deepEqual(Object.keys(h.puts[0].body).sort(),['schema','child_id','item_id','local_proposal_event_id','journal_epoch','decision','body'].sort());});
await test('retry after unacknowledged intent uses the exact same hash transaction and body',async()=>{const h=fixture();h.pollPending=true;await assert.rejects(h.writer(h.proposal,'approve','Approved'));await assert.rejects(h.writer(h.proposal,'approve','Approved'));assert.deepEqual(h.puts[0],h.puts[1]);});
await test('source acknowledgment author, context and multiplicity are independently verified',async()=>{
  for(const mutate of [e=>e.sender='@other:local',e=>e.content.child_hash='f'.repeat(64),e=>e.content.local_proposal_event_id='$other',e=>e.content.owner_mxid='@david:master',e=>e.content.journal_epoch='other']){
    const h=fixture(),e=event('com.jkali.crm.item_status',h.result(),'child/item');mutate(e);assert.throws(()=>crmItemStatus([e],h.proposal,USER));
  }
  const h=fixture(),e=event('com.jkali.crm.item_status',h.result(),'child/item');assert.throws(()=>crmItemStatus([e,clone(e)],h.proposal,USER));
});
await test('session token rotation during status reconciliation cannot write approval',async()=>{const h=fixture();h.hook=(method,path)=>{if(path.endsWith(encodeURIComponent(INBOX)+'/state'))h.identity={...h.identity,token:'changed'};};await assert.rejects(h.writer(h.proposal,'approve','Approved'),/session changed/);assert.equal(h.puts.length,0);});
await test('invalid annotation stays outside legacy actionable drafts and cannot approve',async()=>{for(const annotation of [null,{},'bad',{...item,extra:true},{...item,schema:2}]){const h=fixture();h.source.content['com.jkali.crm.item']=annotation;const p={...parseProposal(h.source),proposalRoomId:INBOX};assert.equal(p.crmTracked,true);assert.equal(p.crm,null);await assert.rejects(h.writer(p,'approve','Approved'));assert.equal(h.puts.length,0);}});
await test('unknown native acceptance remains ambiguous and never enters sent partition',async()=>{const h=fixture(),p={...h.proposal,crmState:'outcome_unknown',crmNativeStatus:'accepted'};const parts=partitionProposals([p],new Set());assert.equal(parts.sent.length,0);const rooms=new Map([['!person:local',{id:'!person:local'}]]);attachDrafts([p],new Set(),rooms);assert.equal(rooms.get('!person:local').drafts.length,0);});
for(const state of ['sent','declined','canceled','refused'])await test('already terminal source item '+state+' is not newly approved by a stale browser',async()=>{
  const h=fixture();h.status=h.result(state);if(state==='sent')h.status.sent_event_id='$already-sent';
  try{await h.writer(h.proposal,'approve','Approved');}catch{}
  assert.equal(h.puts.length,0,'terminal source outcome must prevent a fresh approval intent');
});
console.log(`crm_approval_adversarial: ${passed} independent cases passed; ${failed} failed`);if(failed)process.exitCode=1;
