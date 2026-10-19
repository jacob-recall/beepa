import assert from 'node:assert/strict';
import { createOrderedConsentWriter } from '../../apps/user/ordered-consent.js';
const USER = '@user:local', ROOM = '!conversation:local', CONTROL = '!control:local';
const absent = () => Object.assign(new Error('absent'), { status: 404 });
const copy = value => JSON.parse(JSON.stringify(value));
const deferred = () => { let resolve; const promise = new Promise(done => { resolve = done; }); return { promise, resolve }; };
function fixture() {
  const f = { session: { token: 'synthetic', userId: USER }, legacy: 0, calls: [], serial: 0, override: {}, result: null, onPut: null, before: null,
    cap: { schema: 1, manual: true, journal_epoch: 'epoch-1', generation: 'generation-1', policy_room_id: CONTROL } };
  const event = (type, content, state_key = '') => ({ type, state_key, sender: USER, content });
  f.state = [event('m.room.create', { room_version: '11', 'm.federate': false }),
    event('com.jkali.policy_control', { schema: 1, journal_epoch: 'epoch-1' }),
    event('m.room.member', { membership: 'join' }, USER),
    event('m.room.power_levels', { users: { [USER]: 100 }, users_default: 0, events_default: 100, state_default: 100, invite: 100, kick: 100, ban: 100, redact: 100, events: {} }),
    event('m.room.join_rules', { join_rule: 'invite' }), event('m.room.history_visibility', { history_visibility: 'joined' }),
    event('m.room.guest_access', { guest_access: 'forbidden' })];
  f.api = async (method, path, body) => {
    f.calls.push({ method, path, body: body && copy(body) });
    if (f.before) await f.before(method, path, body);
    if (path.endsWith('/account_data/com.jkali.crm_control')) { if (f.cap instanceof Error) throw f.cap; return copy(f.cap); }
    if (path.endsWith('/state')) return copy(f.state);
    if (path.endsWith('/account_data/com.jkali.share_override')) { if (f.override instanceof Error) throw f.override; return copy(f.override); }
    if (path.includes('/send/com.jkali.policy.request/')) { f.last = copy(body); if (f.onPut) await f.onPut(body); return { event_id: '$request' }; }
    if (path.includes('/state/com.jkali.policy.result/')) { if (f.result === null) throw absent(); return copy(f.result); }
    throw new Error('Unexpected endpoint ' + path);
  };
  f.newWriter = () => createOrderedConsentWriter({ api: f.api, session: () => ({ ...f.session }), uuid: () => 'operation-' + ++f.serial, wait: async () => {}, polls: 2 });
  f.writer = f.newWriter(); f.write = (level = 'private') => f.writer(ROOM, level, async () => { f.legacy++; return level; });
  f.puts = () => f.calls.filter(call => call.method === 'PUT');
  f.apply = body => { f.result = { schema: 1, command_id: body.command_id, room_id: ROOM, state: 'applied', operation_id: 'manual-' + body.command_id, revision: 1 };
    f.override = { ...(body.level === 'inherit' ? {} : { state: body.level }), 'com.jkali.policy_journal': { schema: 1, operation_id: f.result.operation_id, revision: 1, generation: f.cap.generation, owner_mxid: '@user:master' } }; };
  return f;
}
let passed = 0, failed = 0;
async function test(name, run) { try { await run(); passed++; } catch (error) { failed++; console.error('FAIL ordered_consent_adversarial:', name, '\n', error.stack); } }
await test('verified preprotocol absence alone allows legacy writer', async () => {
  const f = fixture(); f.cap = absent(); f.override = absent(); assert.equal(await f.write(), 'private'); assert.equal(f.legacy, 1); assert.equal(f.puts().length, 0);
});
await test('fresh browser never downgrades existing journal marker to legacy', async () => {
  const f = fixture(); f.cap = absent(); f.override = { state: 'direct', 'com.jkali.policy_journal': { schema: 1, operation_id: 'prior', revision: 4 } };
  await assert.rejects(f.write()); assert.equal(f.legacy, 0); assert.equal(f.puts().length, 0);
});
await test('malformed capability followed by absence cannot downgrade writer', async () => {
  const f = fixture(); f.cap = {}; await assert.rejects(f.write()); f.cap = absent(); await assert.rejects(f.write()); assert.equal(f.legacy, 0);
});
await test('failed capability or override read never authorizes legacy fallback', async () => {
  for (const which of ['cap', 'override']) { const f = fixture(); f.cap = absent(); f[which] = Object.assign(new Error('offline'), { status: 503 }); await assert.rejects(f.write()); assert.equal(f.legacy, 0); }
});
for (const [name, mutate] of [
  ['public history', f => { f.state.find(e => e.type === 'm.room.history_visibility').content.history_visibility = 'world_readable'; }],
  ['public join', f => { f.state.find(e => e.type === 'm.room.join_rules').content.join_rule = 'public'; }],
  ['guest access', f => { f.state.find(e => e.type === 'm.room.guest_access').content.guest_access = 'can_join'; }],
  ['encryption', f => { f.state.push({ type: 'm.room.encryption', state_key: '', sender: USER, content: { algorithm: 'm.megolm.v1.aes-sha2' } }); }],
  ['mirror room', f => { f.state.find(e => e.type === 'm.room.create').content['com.jkali.mirror_of'] = ROOM; }],
  ['foreign power holder', f => { f.state.find(e => e.type === 'm.room.power_levels').content.users['@other:local'] = 100; }],
  ['former room member', f => { f.state.push({ type: 'm.room.member', state_key: '@other:local', sender: USER, content: { membership: 'leave' } }); }],
]) await test('unsafe control room rejected: ' + name, async () => {
  const f = fixture(); mutate(f); await assert.rejects(f.write()); assert.equal(f.puts().length, 0); assert.equal(f.legacy, 0);
});
await test('applied outcome requires exact operation/revision/level readback', async () => {
  for (const mutate of [f => { f.override['com.jkali.policy_journal'].operation_id = 'another'; }, f => { f.override['com.jkali.policy_journal'].revision = 2; }, f => { f.override.state = 'direct'; }]) {
    const f = fixture(); f.onPut = body => { f.apply(body); mutate(f); }; await assert.rejects(f.write(), /changed again|current setting/); assert.equal(f.legacy, 0);
  }
});
await test('unknown Direct followed by explicit Private queues but never claims success', async () => {
  const f = fixture(); await assert.rejects(f.write('direct'), /awaiting confirmation/);
  const first = f.last; f.onPut = body => { f.result = { schema: 1, command_id: body.command_id, room_id: ROOM, state: 'queued', operation_id: 'new-op' }; };
  await assert.rejects(f.write('private'), /awaiting confirmation/); assert.notEqual(f.last.command_id, first.command_id); assert.equal(f.last.level, 'private');
  assert.equal(f.legacy, 0); assert.ok(f.puts().every(call => call.path.includes('/send/com.jkali.policy.request/')));
});
await test('timeout retry keeps exact command and transaction for same requested level', async () => {
  const f = fixture(); f.onPut = () => { throw new Error('lost HTTP response'); }; await assert.rejects(f.write(), /lost HTTP/);
  const first = f.puts()[0]; f.onPut = f.apply; assert.equal(await f.write(), 'private');
  assert.deepEqual(f.puts()[1], first); assert.equal(f.serial, 1);
});
await test('pending command is not replayed into replacement journal', async () => {
  const f = fixture(); await assert.rejects(f.write()); const count = f.puts().length;
  f.cap.journal_epoch = 'epoch-2'; f.state.find(e => e.type === 'com.jkali.policy_control').content.journal_epoch = 'epoch-2';
  await assert.rejects(f.write(), /changed/); assert.equal(f.puts().length, count); assert.equal(f.legacy, 0);
});
await test('identity change during capability read never posts old user intent', async () => {
  const f = fixture(), waiting = deferred(), reached = deferred();
  f.before = async (method, path) => { if (path.endsWith('/account_data/com.jkali.crm_control')) { reached.resolve(); await waiting.promise; } };
  const attempt = f.write(); await reached.promise; f.session.token = 'new-token'; f.session.userId = '@other:local'; waiting.resolve();
  await assert.rejects(attempt, /session changed/); assert.equal(f.puts().length, 0); assert.equal(f.legacy, 0);
});
await test('identity change during outcome wait never confirms former user setting', async () => {
  const f = fixture(); f.onPut = body => { f.apply(body); f.session.token = 'new-token'; };
  await assert.rejects(f.write(), /session changed/); assert.equal(f.calls.some(call => call.path.includes('/state/com.jkali.policy.result/')), false);
});
await test('result for another room or command never confirms request', async () => {
  for (const mismatch of [{ room_id: '!foreign:local' }, { command_id: 'foreign-command' }]) {
    const f = fixture(); f.onPut = body => { f.apply(body); Object.assign(f.result, mismatch); }; await assert.rejects(f.write(), /did not match/);
  }
});
console.log(`ordered_consent_adversarial: ${passed} independent scenarios passed; ${failed} failed`);
if (failed) process.exitCode = 1;
