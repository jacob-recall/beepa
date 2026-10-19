// CRM approval is a typed intent. Only the source's serialized item journal may
// dispatch it; Matrix transaction IDs alone do not deduplicate different tokens.
import { canonicalCRMJSON } from '../../shared/model/crm.js';
const enc = encodeURIComponent;
const ROOM = /^![^\s:]+:[^\s]+$/, EVENT = /^\$[^\s]{1,255}$/, ID = /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/;
const HASH = /^[a-f0-9]{64}$/;
const ITEM = 'com.jkali.crm.item', STATUS = 'com.jkali.crm.item_status';
const fail = text => { throw new Error(text); };
export function parseCRMItem(value) {
  const keys = ['schema', 'batch_id', 'child_id', 'item_id', 'owner_mxid', 'generation', 'journal_epoch', 'payload_hash', 'child_hash'];
  if (!value || typeof value !== 'object' || Array.isArray(value) || Object.keys(value).sort().join() !== keys.sort().join()
      || value.schema !== 1 || !['batch_id', 'child_id', 'item_id', 'journal_epoch'].every(k => typeof value[k] === 'string' && ID.test(value[k]))
      || typeof value.owner_mxid !== 'string' || !/^@[^\s:]+:[^\s]+$/.test(value.owner_mxid)
      || typeof value.generation !== 'string' || !value.generation || value.generation.length > 128
      || !HASH.test(value.payload_hash) || !HASH.test(value.child_hash)) return null;
  return JSON.parse(JSON.stringify(value));
}
export function crmItemStatus(events, proposal, userId) {
  if (!proposal.crm || !Array.isArray(events)) return null;
  const marker = proposal.crm;
  const matches = events.filter(e => e.type === STATUS && e.state_key === marker.child_id + '/' + marker.item_id);
  if (!matches.length) return null;
  const event = matches[0], value = event.content;
  if (matches.length !== 1 || event.sender !== userId || !value || value.schema !== 1
      || !['batch_id', 'child_id', 'item_id', 'owner_mxid', 'generation', 'journal_epoch', 'child_hash'].every(k => value[k] === marker[k])
      || value.local_proposal_event_id !== proposal.eventId) fail('CRM outcome belongs to another proposal or account');
  if (value.state === 'sent' && (typeof value.sent_event_id !== 'string' || !EVENT.test(value.sent_event_id))) fail('CRM sent outcome has no verified message identity');
  return value;
}
async function hash(value) {
  const bytes = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(canonicalCRMJSON(value)));
  return [...new Uint8Array(bytes)].map(v => v.toString(16).padStart(2, '0')).join('');
}
export function createCRMApprovalWriter({ api, session, wait = ms => new Promise(resolve => setTimeout(resolve, ms)), polls = 40 }) {
  return async function decide(proposal, decision, shownBody = '') {
    const item = parseCRMItem(proposal?.crm), captured = { ...session() };
    if (!captured.token || !captured.userId) fail('Sign in to review this message');
    if (!item || !ROOM.test(proposal.proposalRoomId) || !EVENT.test(proposal.eventId)) fail('CRM proposal cannot be verified. Refresh before approving it.');
    if (!['approve', 'decline'].includes(decision)) fail('Invalid approval decision');
    if (decision === 'approve' && (typeof shownBody !== 'string' || !shownBody.trim() || shownBody.length > 8000 || shownBody.trimStart().startsWith('!'))) fail('Enter a message that does not begin with a command');
    const check = () => { const now = session(); if (now.token !== captured.token || now.userId !== captured.userId) fail('Approval session changed'); };
    const request = async (method, path, body) => { check(); const value = await api(method, path, body); check(); return value; };
    const cap = await request('GET', '/_matrix/client/v3/user/' + enc(captured.userId) + '/account_data/com.jkali.crm_control');
    if (cap?.schema !== 1 || cap.crm_approval !== true || cap.journal_epoch !== item.journal_epoch
        || cap.generation !== item.generation || !ROOM.test(cap.policy_room_id)) fail('This account needs the CRM approval sync update');
    const policyPath = '/_matrix/client/v3/rooms/' + enc(cap.policy_room_id);
    const state = await request('GET', policyPath + '/state');
    if (!Array.isArray(state)) fail('Cannot verify private approval controls');
    const one = (type, key = '') => { const all = state.filter(e => e.type === type && e.state_key === key); return all.length === 1 ? all[0] : null; };
    const create = one('m.room.create'), marker = one('com.jkali.policy_control'), power = one('m.room.power_levels');
    if (create?.sender !== captured.userId || create.content?.type === 'm.space' || Object.hasOwn(create?.content || {}, 'com.jkali.mirror_of')
        || marker?.sender !== captured.userId || marker.content?.schema !== 1 || marker.content.journal_epoch !== item.journal_epoch
        || one('m.room.member', captured.userId)?.content?.membership !== 'join'
        || state.some(e => e.type === 'm.room.member' && e.state_key !== captured.userId)
        || state.some(e => ['uk.half-shot.bridge', 'm.bridge', 'com.jkali.mirror_of', 'm.room.encryption', 'm.room.tombstone'].includes(e.type))
        || power?.sender !== captured.userId || power.content?.users?.[captured.userId] !== 100 || Object.keys(power.content.users).length !== 1
        || ['events_default', 'state_default', 'invite', 'kick', 'ban', 'redact'].some(k => power.content[k] !== 100)
        || Object.values(power.content.events || {}).some(v => !Number.isInteger(v) || v < 100)
        || one('m.room.join_rules')?.content?.join_rule !== 'invite' || one('m.room.history_visibility')?.content?.history_visibility !== 'joined'
        || one('m.room.guest_access')?.content?.guest_access !== 'forbidden') fail('Cannot verify private approval controls');
    const proposalPath = '/_matrix/client/v3/rooms/' + enc(proposal.proposalRoomId);
    const current = await request('GET', proposalPath + '/event/' + enc(proposal.eventId));
    if (current?.sender !== captured.userId || current.type !== 'com.jkali.proposal' || Object.hasOwn(current, 'state_key')
        || current.event_id !== proposal.eventId || current.room_id !== proposal.proposalRoomId
        || canonicalCRMJSON(current.content?.[ITEM]) !== canonicalCRMJSON(item) || current.content.body !== proposal.body
        || (proposal.kind === 'room' ? current.content.target_room !== proposal.targetRoom
          : current.content.target_identifier !== proposal.targetIdentifier || current.content.target_source !== proposal.targetSource)) fail('This proposal changed. Refresh before approving it.');
    const body = { schema: 1, child_id: item.child_id, item_id: item.item_id, local_proposal_event_id: proposal.eventId,
      journal_epoch: item.journal_epoch, decision, body: decision === 'approve' ? shownBody : '' };
    const requestHash = await hash(body); check();
    const read = async () => {
      const result = crmItemStatus(await request('GET', proposalPath + '/state'), { ...proposal, crm: item }, captured.userId);
      if (!result) return null;
      if (!result.approval) {
        if (['sent', 'declined', 'canceled', 'refused', 'outcome_unknown', 'attempted'].includes(result.state)) return result;
        return null;
      }
      if (result.approval.request_hash !== requestHash
          || (result.approval.request ? canonicalCRMJSON(result.approval.request) !== canonicalCRMJSON(body) : result.approval.decision !== decision)) fail('This message already has a different approval or decline decision. Refresh its status.');
      return result;
    };
    let result = await read();
    if (result) return result;
    await request('PUT', policyPath + '/send/com.jkali.crm.approval_request/' + enc('crm_approval_' + requestHash), body);
    for (let n = 0; n < polls; n++) {
      result = await read(); if (result) return result;
      if (n + 1 < polls) await wait(500);
      check();
    }
    fail('Approval is awaiting the sync service. Retry to check the same decision; no send has been confirmed.');
  };
}
