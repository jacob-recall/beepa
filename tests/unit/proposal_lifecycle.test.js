import assert from 'node:assert/strict';
import { createProposalDecisions } from '../../apps/user/proposal-decisions.js';
import { proposalDecision, proposalLabel, proposalHistory, PROPOSAL_DECISION, PROPOSAL_CLAIM } from '../../shared/model/proposal-lifecycle.js';
import { parseProposal, attachDrafts } from '../../apps/user/proposals.js';
import { pendingDrafts } from '../../shared/model/attention.js';
const identity = { userId: '@self:local', token: 'synthetic' }, state = [], writes = [];
const props = [1,2,3].map(n => ({ ...parseProposal({ type: 'com.jkali.proposal', event_id: '$' + n, origin_server_ts: n,
  content: { target_room: '!chat:local', body: 'Message ' + n } }), proposalRoomId: '!proposals:local' }));
const api = async (method, path, body) => {
  if (method === 'GET' && path.endsWith('/state')) return structuredClone(state);
  if (method === 'GET' && path.includes('/event/')) {
    const p = props.find(p => path.endsWith(encodeURIComponent(p.eventId)));
    return { event_id: p.eventId, room_id: p.proposalRoomId, sender: identity.userId, type: 'com.jkali.proposal', content: { body:p.body, target_room:p.targetRoom } };
  }
  assert.equal(method, 'PUT'); assert.ok(path.includes('/state/' + PROPOSAL_DECISION)); writes.push(body);
  const i = state.findIndex(e => e.type === PROPOSAL_DECISION && e.state_key === body.proposal_event_id);
  const e = { type: PROPOSAL_DECISION, state_key:body.proposal_event_id, sender:identity.userId, content:structuredClone(body) };
  if(i < 0) state.push(e); else state[i] = e;
  const eventId = '$decision-' + writes.length;
  if (!state.some(e => e.type === PROPOSAL_CLAIM && e.state_key === body.proposal_event_id)) state.push({type:PROPOSAL_CLAIM,state_key:body.proposal_event_id,sender:identity.userId,content:{...structuredClone(body),decision_event_id:eventId}});
  return {event_id:eventId};
};
let sends = 0;
const decide = createProposalDecisions({ api, session: () => identity });
await decide(props[0], 'accepted', async () => { sends++; return true; });
await decide(props[1], 'declined', async () => { throw new Error('decline must never send'); });
const restored = props.map(p => ({ ...p, decision:proposalDecision(state,p.eventId,identity.userId) }));
assert.equal(proposalLabel(restored[0].decision), 'Accepted · sent');
assert.equal(proposalLabel(restored[1].decision), 'Declined');
const feed = new Map([['!chat:local',{id:'!chat:local', lastTs:999}]]);
attachDrafts(restored,new Set(),feed);
assert.deepEqual(pendingDrafts(feed.get('!chat:local').drafts,999).map(d=>d.eventId), ['$3'], 'later activity leaves the third suggestion queued');
await assert.rejects(decide(restored[0],'accepted',async()=>{sends++;return true;}),/already has a decision/);
assert.equal(sends,1);
assert.equal(proposalDecision(state,'$1','@another:local'),null);
await assert.rejects(decide({...props[2],direct:true},'accepted',async()=>true),/not an actionable/);
assert.equal(proposalLabel({state:'accepted'}),'Sending through account','source queue acceptance is never user acceptance');
assert.equal(proposalLabel({state:'sent',approval:{decision:'approve'}}),'Accepted · sent');
await decide(props[2],'accepted',async()=>false);
assert.equal(proposalLabel(props[2].decision),'Accepted · send unconfirmed');
let pages=0;
const history = await proposalHistory(async()=>++pages===1?{chunk:[{event_id:'$a'}],end:'next'}:{chunk:[{event_id:'$b'}]},'!p:local');
assert.equal(history.length,2);
await assert.rejects(proposalHistory(async()=>({chunk:[{}],end:'same'}),'!p:local'),/cursor repeated/);
console.log('proposal_lifecycle: independent durable decisions, refusal, uncertainty, identity and paginated history pass');

let conflictSent = 0;
const conflictProposal = {...props[0], decision:null};
const conflict = createProposalDecisions({session:()=>identity,wait:async()=>{},polls:2,api:async(method,path,body)=>{
  if (method==='PUT') return {event_id:'$loser'};
  if(path.includes('/event/'))return api(method,path,body);
  if(!conflict.checked){conflict.checked=true;return [];}
  return [{type:PROPOSAL_CLAIM,state_key:conflictProposal.eventId,sender:identity.userId,content:{schema:1,proposal_event_id:conflictProposal.eventId,decision:'declined',send_state:'none',decision_event_id:'$winner'}}];
}});
await assert.rejects(conflict(conflictProposal,'accepted',async()=>{conflictSent++;return true;}),/Another session/);
assert.equal(conflictSent,0);
