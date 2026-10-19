import assert from 'node:assert/strict';
import { createCRMState, applyCRMOperation, applyVerifiedCRMOperation, membershipKey, canonicalCRMJSON, createReviewedBatch, createCRMChildEnvelope, verifyReviewedBatch, attributeResponses, CRM_MAX_BATCH_ITEMS } from '../../shared/model/crm.js';

const workspace_id = '!crm:master', manager_mxid = '@manager:master';
let state = createCRMState({ workspace_id, manager_mxid }), sequence = 0;
const operation = (type, entity_id, payload, base_revision = 0, op_id = 'op-' + ++sequence) => ({ schema: 1, workspace_id, type, entity_id, payload, base_revision, op_id });
function run(op, code = 'accepted') {
  const result = applyCRMOperation(state, op);
  assert.equal(result.outcome.code, code, JSON.stringify(op)); state = result.state; return result;
}
run(operation('person.create', 'maya', { label: 'Maya' }));
run(operation('person.create', 'noah', { label: 'Noah' }));
const binding = (person_id, owner_mxid, network_id) => ({ person_id, owner_mxid, generation: 'enrollment-1', contact: { source: 'imessage', network_id }, contact_version: '$shared:master' });
run(operation('binding.create', 'maya-elliot', binding('maya', '@elliot:master', 'maya@example.com')));
run(operation('binding.create', 'maya-david', binding('maya', '@david:master', 'maya@example.com')));
run(operation('binding.create', 'noah-david', binding('noah', '@david:master', 'noah@example.com')));
run(operation('binding.create', 'fake-link', binding('noah', '@elliot:master', 'maya@example.com')), 'binding-already-linked');
run(operation('list.create', 'founders', { name: 'Founder introductions' }));
const addMaya = operation('member.set', 'member-maya', { list_id: 'founders', person_id: 'maya', binding_id: 'maya-elliot' });
run(addMaya);
run(operation('member.set', 'member-noah', { list_id: 'founders', person_id: 'noah', binding_id: 'noah-david' }));
assert.equal(state.memberships[membershipKey('founders', 'maya')].binding_id, 'maya-elliot');
assert.ok(Object.isFrozen(state));
const replay = run({ ...addMaya, payload: { binding_id: 'maya-elliot', person_id: 'maya', list_id: 'founders' } });
assert.equal(replay.outcome.duplicate, true, 'key order does not change identity');
run({ ...addMaya, payload: { ...addMaya.payload, binding_id: 'maya-david' } }, 'operation-id-collision');
run(operation('member.set', 'member-maya', { ...addMaya.payload, binding_id: 'noah-david' }, 1), 'binding-unavailable');
run(operation('member.remove', 'member-maya', { list_id: 'founders', person_id: 'maya' }, 1));
assert.equal(state.memberships['founders/maya'].removed, true);
assert.equal(state.bindings['maya-elliot'].availability, 'shared', 'removal never mutates contact/Direct permissions');
run(operation('member.set', 'member-maya', addMaya.payload, 1), 'revision-conflict');
assert.equal(run(addMaya).outcome.duplicate, true);
assert.equal(state.memberships['founders/maya'].removed, true, 'old add never resurrects removal');
run(operation('member.set', 'member-maya', addMaya.payload, 2));
const staleRename = operation('list.rename', 'founders', { name: 'Stale' }, 0);
run(staleRename, 'revision-conflict');
run(operation('list.rename', 'founders', { name: 'Founders' }, 1));
assert.equal(run(staleRename, 'revision-conflict').outcome.duplicate, true);
const before = state;
run({ ...operation('list.create', 'intruder', { name: 'Bad' }), workspace_id: '!other:master' }, 'invalid-operation');
assert.equal(state, before);
run(operation('list.create', '__proto__', { name: 'Bad' }), 'invalid-operation');
run(operation('list.create', 'badname', { name: 'bad\u0000name' }), 'invalid-payload');
run(operation('m.room.message', 'send', { body: 'no' }), 'unknown-operation');
run(operation('list.create', 'extra', { name: 'Bad', arbitrary_room: '!r:evil' }), 'invalid-payload');
run(operation('member.remove', 'wrong-id', { list_id: 'founders', person_id: 'maya' }, 3), 'revision-conflict');
assert.throws(() => canonicalCRMJSON({ x: undefined }));
assert.throws(() => { const x = {}; x.self = x; canonicalCRMJSON(x); }, /payload-too-deep/);

const route = (network_id, room_id) => ({ mode: 'direct_existing', source: 'imessage', network_id, room_id, consent_revision: 4 });
const items = [
  { item_id: 'item-maya', person_id: 'maya', proposal_room_id: '!elliot-proposals:master', journal_epoch: 'journal-1', route: route('maya@example.com', '!maya:master'), body: 'Hi Maya\nMeet Tuesday?' },
  { item_id: 'item-noah', person_id: 'noah', proposal_room_id: '!david-proposals:master', journal_epoch: 'journal-2', route: { mode: 'request_new_conversation_approval', source: 'imessage', network_id: 'noah@example.com', consent_revision: 0 }, body: 'Hi Noah' },
];
const review = async (overrides = {}) => createReviewedBatch(state, { batch_id: 'batch-1', list_id: 'founders', reviewed_at: 1000, items, ...overrides });
const batch = await review();
assert.equal(await verifyReviewedBatch(batch), true);
assert.equal(batch.children.length, 2);
const childEnvelope = await createCRMChildEnvelope(batch, batch.children[0].child_id);
const stagedEnvelope = await createCRMChildEnvelope(batch, batch.children[0].child_id, { 'item-maya': '$proposal:master' });
assert.equal(stagedEnvelope.child_hash, childEnvelope.child_hash);
assert.equal(stagedEnvelope.items[0].proposal_event_id, '$proposal:master');
assert.equal(childEnvelope.journal_epoch, 'journal-1');
assert.equal(batch.items[0].owner_mxid, '@elliot:master');
assert.equal(batch.items[1].route.mode, 'request_new_conversation_approval');
assert.deepEqual(batch, await review(), 'same reviewed input yields identical durable children and hash');
assert.ok(Object.isFrozen(batch.items[0].route));
items[0].body = 'changed draft';
assert.equal(batch.items[0].body, 'Hi Maya\nMeet Tuesday?', 'review is detached from draft');
const altered = JSON.parse(JSON.stringify(batch)); altered.items[0].body = 'unreviewed';
assert.equal(await verifyReviewedBatch(altered), false);
assert.equal((await applyVerifiedCRMOperation(state, operation('batch.review', 'batch-1', altered))).outcome.code, 'invalid-manifest-hash');
run(operation('batch.review', 'batch-1', batch));
run(operation('batch.review', 'batch-1', { ...batch, reviewed_at: 2000 }), 'revision-conflict');
assert.deepEqual(state.batches['batch-1'], batch);
await assert.rejects(review({ items: [items[0], { ...items[0], item_id: 'second' }] }), /duplicate-recipient/);
await assert.rejects(review({ items: [ { ...items[0], route: route('foreign@example.com', '!other:master') } ] }), /route-mismatch/);
await assert.rejects(review({ items: Array(CRM_MAX_BATCH_ITEMS + 1).fill(items[0]) }), /batch-size/);
await assert.rejects(review({ items: [{ ...items[0], body: '😀'.repeat(8001) }] }), /invalid-body/);
const stale = await review({ batch_id: 'batch-stale' });
run(operation('member.set', 'member-maya', { ...addMaya.payload, binding_id: 'maya-david' }, 3));
run(operation('batch.review', 'batch-stale', stale), 'revision-conflict');
assert.equal(state.batches['batch-1'].items[0].owner_mxid, '@elliot:master', 'history never follows reassigned sender');
run(operation('binding.update', 'maya-david', { contact_version: '$revoked:master', availability: 'tombstone' }, 1));
await assert.rejects(review(), /binding-unavailable/);
run(operation('binding.update', 'maya-david', { contact_version: '$revoked:master', availability: 'shared' }, 2), 'binding-tombstoned');
run(operation('binding.update', 'maya-david', { contact_version: '$revived:master', availability: 'shared' }, 2));
assert.equal(state.bindings['maya-david'].availability, 'shared');
run(operation('list.archive', 'founders', {}, 2));
run(operation('list.rename', 'founders', { name: 'Resurrect' }, 3), 'list-archived');
run(operation('member.set', 'member-maya', addMaya.payload, 4), 'list-archived');
run(operation('list.restore', 'founders', {}, 2), 'revision-conflict');
run(operation('list.restore', 'founders', {}, 3));
assert.equal(state.lists.founders.archived, false);
const largeItems = [];
for (let n = 0; n < 5; n++) {
  const person = 'bulk-' + n, network = person + '@example.com';
  run(operation('person.create', person, { label: person }));
  run(operation('binding.create', person + '-binding', binding(person, '@elliot:master', network)));
  run(operation('member.set', person + '-membership', { list_id: 'founders', person_id: person, binding_id: person + '-binding' }));
  largeItems.push({ item_id: person + '-item', person_id: person, proposal_room_id: '!elliot-proposals:master', journal_epoch: 'journal-1', route: route(network, '!' + person + ':master'), body: '😀'.repeat(3000) });
}
await assert.rejects(review({ batch_id: 'bulk', items: largeItems }), /payload-too-large/, 'bound total UTF8 bytes, not only each body or recipient count');
console.log('crm_model: identity, canonical replay, stale edits, tombstones, frozen cross-account review and child isolation pass');

// Frozen permission requests remain exact across later binding updates.
const frozenMember = operation('member.set', state.memberships['founders/noah'].membership_id,
  { list_id: 'founders', person_id: 'noah', binding_id: 'noah-david' }, 1);
frozenMember.payload.direct_request = { schema: 1, command_id: frozenMember.op_id, membership_operation_id: frozenMember.op_id,
  owner_mxid: '@david:master', generation: 'enrollment-1', journal_epoch: 'journal-1', binding_id: 'noah-david',
  contact: { source: 'imessage', network_id: 'noah@example.com' }, contact_version: '$shared:master', proposal_room_id: '!david-proposals:master' };
run({ ...frozenMember, op_id: 'invalid-frozen-request' }, 'direct-request-mismatch');
run(frozenMember);
run(operation('binding.update', 'noah-david', { contact_version: '$new-version:master', availability: 'shared' }, 1));
assert.equal(state.memberships['founders/noah'].direct_request.contact_version, '$shared:master');
assert.ok(Object.isFrozen(state.memberships['founders/noah'].direct_request.contact));
const activeMembers = Object.values(state.memberships).filter(member => member.list_id === 'founders' && !member.removed).map(member => member.operation_id).sort();
run(operation('member.set', frozenMember.entity_id, { list_id: 'founders', person_id: 'noah', binding_id: 'noah-david' }, 2));
assert.equal(state.memberships['founders/noah'].direct_request, undefined, 'new membership never inherits old grant');
run(operation('list.archive', 'founders', { membership_operations: activeMembers }, 4), 'membership-conflict');
const currentMembers = Object.values(state.memberships).filter(member => member.list_id === 'founders' && !member.removed).map(member => member.operation_id).sort();
run(operation('list.archive', 'founders', { membership_operations: currentMembers }, 4));

const message = (source_id, source_order, direction, extra = {}) => ({ trusted: true, owner_mxid: '@elliot:master', generation: 'enrollment-1', conversation_id: '!maya:local', source_id, source_order, source_ts: source_order * 100, direction, kind: 'text', confirmed: true, outreach: direction === 'outgoing', ...extra });
const o1 = message('$o1', 1, 'outgoing'), o2 = message('$o2', 2, 'outgoing'), i3 = message('$i3', 3, 'incoming'), i4 = message('$i4', 4, 'incoming');
const expected = [['$i3', '$o2'], ['$i4', '$o1']];
const links = result => result.links.map(l => [l.incoming_id, l.outgoing_id]);
assert.deepEqual(links(attributeResponses([i4, o2, i3, o1])), expected);
assert.deepEqual(links(attributeResponses([i4, i4, o2, i3, o1, o2])), expected, 'native/mirror duplicate source identities dedup');
assert.equal(attributeResponses([o1, message('$foreign', 2, 'incoming', { owner_mxid: '@david:master' })]).links.length, 0);
assert.equal(attributeResponses([o1, message('$foreign', 2, 'incoming', { generation: 'enrollment-2' })]).links.length, 0);
assert.equal(attributeResponses([o1, message('$foreign', 2, 'incoming', { conversation_id: '!other:local' })]).links.length, 0);
for (const exclusion of [{ trusted: false }, { kind: 'reaction' }, { kind: 'receipt' }, { kind: 'typing' }, { edit: true }, { self_echo: true }]) {
  assert.equal(attributeResponses([o1, message('$excluded', 2, 'incoming', exclusion)]).links.length, 0, JSON.stringify(exclusion));
}
assert.equal(attributeResponses([o1, message('$backfill', 0, 'incoming')]).links.length, 0);
assert.equal(attributeResponses([o1, message('$old-time', 2, 'incoming', { source_ts: 50 })]).links.length, 0);
assert.equal(attributeResponses([{ ...o1, confirmed: false }, i3]).links.length, 0, 'uncertain send has no response');
assert.equal(attributeResponses([{ ...o1, outreach: false }, i3]).links.length, 0, 'ordinary message is not campaign outreach');
let result = attributeResponses([o1, { ...i3, source_order: null }]);
assert.equal(result.links.length, 0); assert.equal(result.pending.length, 2);
result = attributeResponses([o1, o2, { ...i3, source_order: 2 }]);
assert.equal(result.links.length, 0); assert.equal(result.pending.length, 3, 'ambiguous cursor blocks scope, never arbitrary event ID ordering');
result = attributeResponses([o1, i3, { ...i3, source_order: 5 }]);
assert.equal(result.links.length, 0); assert.equal(result.pending.length, 2, 'conflicting duplicate blocks scope');
result = attributeResponses([o1, { ...o2, source_order: null }, i3]);
assert.equal(result.links.length, 0); assert.equal(result.pending.length, 3, 'uncertain newest outgoing never falls back to old outreach');
result = attributeResponses([o1, { ...o2, source_ts: 400 }, i3]);
assert.equal(result.links.length, 0); assert.equal(result.pending.length, 3, 'source time inversion defers scope');
result = attributeResponses([o1, o2, i3], { complete: false });
assert.equal(result.links.length, 0); assert.equal(result.pending.length, 3, 'unresolved history gap prevents attribution');
result = attributeResponses([o1, { ...o2, source_id: '' }, i3]);
assert.equal(result.links.length, 0); assert.equal(result.reconciling_scopes.length, 1, 'trusted record missing source identity blocks guessing');
const partial = attributeResponses([o1, o2, i4]);
assert.deepEqual(links(partial), [['$i4', '$o2']]);
const incomplete = attributeResponses([o1, o2], { complete: false, previousLinks: partial.links });
assert.deepEqual(links(incomplete), links(partial)); assert.equal(incomplete.links[0].reconciling, true);
assert.equal(incomplete.corrections.length, 0, 'partial replay cannot delete existing linkage');
const inverted = attributeResponses([{ ...o1, source_ts: 400 }, { ...i3, source_ts: 300 }, { ...i4, source_ts: 500 }], { previousLinks: [{ ...partial.links[0], incoming_id: '$i3', outgoing_id: '$o1' }] });
assert.deepEqual(links(inverted), [['$i3', '$o1']]);
assert.equal(inverted.links[0].reconciling, true); assert.equal(inverted.reconciling_scopes.length, 1);
result = attributeResponses([o1, o2, i3, i4], { previousLinks: partial.links });
assert.deepEqual(links(result), expected);
assert.deepEqual(result.corrections.map(c => [c.previous_outgoing_id, c.outgoing_id]), [['$o2', '$o1']], 'late earlier incoming produces explicit correction');
assert.equal(attributeResponses([o1, o2, i3, i4], { previousLinks: result.links }).corrections.length, 0, 'rebuild idempotent');
assert.deepEqual(links(attributeResponses([{ ...o1, source_ts: 100 }, { ...o2, source_ts: 100 }, { ...i3, source_ts: 100 }])), [['$i3', '$o2']], 'equal times use trusted source order');
assert.equal(Object.hasOwn(result.links[0], 'delivered'), false, 'response attribution cannot synthesize receipt evidence');
console.log('crm_model: source ordering, latest unanswered, account/generation isolation, exclusions, ambiguous ordering and correction replay pass');

// Cancellation records intent against the immutable reviewed parent, even when
// the current contact/list can no longer be used for another send.
const uncanceledState = state, originalBatch = canonicalCRMJSON(state.batches['batch-1']);
const cancelOp = operation('batch.cancel_requested', 'batch-1', { payload_hash: batch.payload_hash }, 0, 'cancel-batch-1');
assert.equal(state.lists.founders.archived, true);
for (const [op, code] of [
  [operation('batch.cancel_requested', 'missing-batch', { payload_hash: batch.payload_hash }), 'batch-cancel-mismatch'],
  [{ ...cancelOp, op_id:'cancel-wrong-hash', payload:{payload_hash:'0'.repeat(64)} }, 'batch-cancel-mismatch'],
  [{ ...cancelOp, op_id:'cancel-wrong-revision', base_revision:1 }, 'batch-cancel-mismatch'],
  [{ ...cancelOp, op_id:'cancel-extra', payload:{...cancelOp.payload,state:'canceled'} }, 'invalid-payload'],
]) {
  const rejected = await applyVerifiedCRMOperation(state, op);
  assert.equal(rejected.outcome.code, code);
  assert.deepEqual(rejected.state.batch_cancellations, uncanceledState.batch_cancellations);
  assert.equal(canonicalCRMJSON(rejected.state.batches['batch-1']), originalBatch);
}
let canceled = await applyVerifiedCRMOperation(state, cancelOp);
assert.equal(canceled.outcome.accepted, true);
assert.deepEqual(canceled.state.batch_cancellations['batch-1'], {batch_id:'batch-1',payload_hash:batch.payload_hash,operation_id:'cancel-batch-1'});
assert.equal(canonicalCRMJSON(canceled.state.batches['batch-1']), originalBatch);
assert.equal(Object.hasOwn(canceled.state.batch_cancellations['batch-1'],'state'),false,'intent cannot assert the remote queue stopped');
assert.equal((await applyVerifiedCRMOperation(canceled.state,cancelOp)).outcome.duplicate,true);
const secondCancel = await applyVerifiedCRMOperation(canceled.state,{...cancelOp,op_id:'other-tab-cancel'});
assert.equal(secondCancel.outcome.accepted,true);
assert.equal(secondCancel.state.batch_cancellations['batch-1'].operation_id,'cancel-batch-1','first canonical cancellation survives another tab');
assert.equal((await applyVerifiedCRMOperation(canceled.state,{...cancelOp,payload:{payload_hash:'0'.repeat(64)}})).outcome.code,'operation-id-collision');
assert.equal(Object.hasOwn(uncanceledState.batch_cancellations,'batch-1'),false,'immutable older state is unchanged');
assert.equal(canonicalCRMJSON((await applyVerifiedCRMOperation(uncanceledState,cancelOp)).state),canonicalCRMJSON(canceled.state),'replay rebuilds identical durable intent');
console.log('crm_model: cancellation scope, replay, immutable history and archived-list cancellation pass');
