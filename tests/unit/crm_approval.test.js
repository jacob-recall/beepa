import assert from 'node:assert/strict';
import { webcrypto } from 'node:crypto';
import { createCRMApprovalWriter, parseCRMItem } from '../../apps/user/crm-approval.js';
import { parseProposal, partitionProposals, attachDrafts, identifierDrafts } from '../../apps/user/proposals.js';
globalThis.crypto ||= webcrypto;
const clone = value => JSON.parse(JSON.stringify(value));
const annotation = { schema: 1, batch_id: 'batch', child_id: 'child', item_id: 'item', owner_mxid: '@owner:master', generation: 'generation', journal_epoch: 'epoch', payload_hash: 'a'.repeat(64), child_hash: 'b'.repeat(64) };
const user = '@owner:local', policy = '!policy:local', inbox = '!inbox:local';
function fixture() {
  const h = { identity: { token: 'token-one', userId: user }, puts: [], result: null, hook: null, lose: false, calls: [] };
  const event = (type, content, state_key = '') => ({ type, content, state_key, sender: user });
  h.cap = { schema: 1, crm_approval: true, generation: 'generation', journal_epoch: 'epoch', policy_room_id: policy };
  h.state = [event('m.room.create', {}), event('com.jkali.policy_control', { schema: 1, journal_epoch: 'epoch' }),
    event('m.room.member', { membership: 'join' }, user), event('m.room.join_rules', { join_rule: 'invite' }),
    event('m.room.history_visibility', { history_visibility: 'joined' }), event('m.room.guest_access', { guest_access: 'forbidden' }),
    event('m.room.power_levels', { users: { [user]: 100 }, events_default: 100, state_default: 100, invite: 100, kick: 100, ban: 100, redact: 100 })];
  h.original = { type: 'com.jkali.proposal', sender: user, event_id: '$proposal', room_id: inbox,
    content: { body: 'Original text', target_room: '!person:local', 'com.jkali.crm.item': clone(annotation) } };
  h.proposal = { ...parseProposal(h.original), proposalRoomId: inbox };
  h.api = async (method, path, body) => {
    h.calls.push([method, path]); if (h.hook) await h.hook(method, path, body);
    if (path.endsWith('/account_data/com.jkali.crm_control')) return clone(h.cap);
    if (path === '/_matrix/client/v3/rooms/' + encodeURIComponent(policy) + '/state') return clone(h.state);
    if (path.includes('/event/')) return clone(h.original);
    if (method === 'PUT') {
      assert.ok(path.includes('/send/com.jkali.crm.approval_request/'), 'browser must write only a typed approval intent');
      h.puts.push([path, clone(body)]);
      const request_hash = path.split('/').at(-1).slice('crm_approval_'.length);
      h.result = { schema: 1, ...clone(annotation), local_proposal_event_id: '$proposal', state: body.decision === 'decline' ? 'declined' : 'approval_pending',
        approval: { request_hash, decision: body.decision } };
      if (h.lose) { h.lose = false; throw new Error('lost response'); }
      return { event_id: '$approval-intent' };
    }
    if (path.endsWith('/state')) return h.result ? [event('com.jkali.crm.item_status', clone(h.result), 'child/item')] : [];
    throw new Error('Unexpected API ' + method + ' ' + path);
  };
  h.writer = createCRMApprovalWriter({ api: h.api, session: () => h.identity, wait: async () => {}, polls: 2 });
  return h;
}
let passed = 0;
async function test(name, run) { await run(); passed++; }
await test('typed approval retains exact edited text and awaits actual sending', async () => {
  const h = fixture(); const result = await h.writer(h.proposal, 'approve', 'Exactly reviewed edited text');
  assert.equal(result.state, 'approval_pending'); assert.equal(h.puts.length, 1); assert.equal(h.puts[0][1].body, 'Exactly reviewed edited text');
});
await test('lost reply and browser restart reconcile same source claim without another write', async () => {
  const h = fixture(); h.lose = true; await assert.rejects(h.writer(h.proposal, 'approve', 'Approved'));
  const restarted = createCRMApprovalWriter({ api: h.api, session: () => ({ ...h.identity, token: 'token-two' }), polls: 1 });
  assert.equal((await restarted(h.proposal, 'approve', 'Approved')).state, 'approval_pending'); assert.equal(h.puts.length, 1);
});
await test('changed decision or edited body cannot replace first source claim', async () => {
  const h = fixture(); await h.writer(h.proposal, 'approve', 'First text');
  await assert.rejects(h.writer(h.proposal, 'approve', 'Changed text'), /different approval/);
  await assert.rejects(h.writer(h.proposal, 'decline', ''), /different approval/); assert.equal(h.puts.length, 1);
});
await test('decline is an authenticated durable intent with no message text or native write', async () => {
  const h = fixture(); const result = await h.writer(h.proposal, 'decline', 'ignored');
  assert.equal(result.state, 'declined'); assert.equal(h.puts[0][1].body, '');
});
await test('session replacement during verification prevents intent write', async () => {
  const h = fixture(); h.hook = (method, path) => { if (path.includes('/event/')) h.identity = { token: 'replacement', userId: '@other:local' }; };
  await assert.rejects(h.writer(h.proposal, 'approve', 'Text'), /session changed/); assert.equal(h.puts.length, 0);
});
await test('session replacement after PUT never confirms previous session outcome', async () => {
  const h = fixture(); h.hook = method => { if (method === 'PUT') h.identity = { token: 'replacement', userId: user }; };
  await assert.rejects(h.writer(h.proposal, 'approve', 'Text'), /session changed/); assert.equal(h.puts.length, 1);
});
await test('missing capability or wrong generation never falls back to browser send', async () => {
  for (const cap of [{}, { ...fixture().cap, generation: 'other' }, { ...fixture().cap, crm_approval: false }]) {
    const h = fixture(); h.cap = cap; await assert.rejects(h.writer(h.proposal, 'approve', 'Text')); assert.equal(h.puts.length, 0);
  }
});
await test('unsafe control room is refused before intent', async () => {
  for (const mutation of [h => h.state.push({ type: 'm.room.member', state_key: '@other:local', sender: user, content: { membership: 'leave' } }),
    h => h.state.find(e => e.type === 'm.room.history_visibility').content.history_visibility = 'world_readable',
    h => h.state.push({ type: 'm.room.encryption', state_key: '', sender: user, content: {} })]) {
    const h = fixture(); mutation(h); await assert.rejects(h.writer(h.proposal, 'approve', 'Text')); assert.equal(h.puts.length, 0);
  }
});
await test('changed target or forged proposal sender blocks approval', async () => {
  for (const mutate of [h => h.original.sender = '@foreign:local', h => h.original.content.target_room = '!other:local', h => h.original.content.body = 'Changed']) {
    const h = fixture(); mutate(h); await assert.rejects(h.writer(h.proposal, 'approve', 'Text')); assert.equal(h.puts.length, 0);
  }
});
await test('malformed CRM annotation remains tracked and can never take legacy send path', async () => {
  const h = fixture(); h.original.content['com.jkali.crm.item'] = null;
  const parsed = parseProposal(h.original); assert.equal(parsed.crmTracked, true); assert.equal(parsed.crm, null);
  await assert.rejects(h.writer({ ...parsed, proposalRoomId: inbox }, 'approve', 'Text')); assert.equal(h.puts.length, 0);
});
await test('CRM handling classification ignores browser localStorage dismissal', async () => {
  const h = fixture(), handled = new Set(['$proposal']);
  assert.equal(partitionProposals([h.proposal], handled).pending.length, 1);
  const feed = new Map([['!person:local', { id: '!person:local' }]]); attachDrafts([h.proposal], handled, feed); assert.equal(feed.get('!person:local').drafts.length, 1);
  const declined = { ...h.proposal, crmState: 'declined' }; assert.equal(partitionProposals([declined], new Set()).dismissed.length, 1);
  const identifier = { ...h.proposal, kind: 'identifier' }; assert.equal(identifierDrafts([identifier], handled).length, 1);
});
await test('queued approval is not an actionable room draft or a sent message', async () => {
  const h = fixture(), p = { ...h.proposal, crmState: 'approval_pending' }, feed = new Map([['!person:local', { id: '!person:local' }]]);
  attachDrafts([p], new Set(), feed); assert.equal(feed.get('!person:local').drafts.length, 0);
  assert.equal(partitionProposals([p], new Set()).sent.length, 0);
});
await test('sent status without an exact message identity is never confirmed', async () => {
  const h = fixture(); h.result = { ...annotation, local_proposal_event_id: '$proposal', state: 'sent', sent_event_id: 'unverified' };
  await assert.rejects(h.writer(h.proposal, 'approve', 'Approved'), /no verified message identity/);
  assert.equal(h.puts.length, 0);
});
assert.ok(parseCRMItem(annotation));
console.log(`crm_approval: ${passed} browser approval scenarios passed`);
