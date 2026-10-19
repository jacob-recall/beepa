import assert from 'node:assert/strict';
import { createOrderedConsentWriter } from '../../apps/user/ordered-consent.js';

const missing = () => Object.assign(new Error('not found'), { status: 404 });
function fixture() {
  const f = { identity: { token: 'synthetic', userId: '@user:local' }, calls: [], count: 0,
    cap: { schema: 1, manual: true, journal_epoch: 'journal-1', generation: 'generation-1', policy_room_id: '!control:local' },
    override: {}, result: null, legacy: 0 };
  const event = (type, content, key = '') => ({ type, state_key: key, sender: '@user:local', content });
  f.state = [event('m.room.create', {}), event('com.jkali.policy_control', { schema: 1, journal_epoch: 'journal-1' }),
    event('m.room.member', { membership: 'join' }, '@user:local'),
    event('m.room.join_rules', { join_rule: 'invite' }), event('m.room.history_visibility', { history_visibility: 'joined' }),
    event('m.room.guest_access', { guest_access: 'forbidden' }),
    event('m.room.power_levels', { users: { '@user:local': 100 }, events_default: 100, state_default: 100, invite: 100, kick: 100, ban: 100, redact: 100 })];
  const api = async (method, path, body) => {
    f.calls.push({ method, path, body });
    if (path.endsWith('/account_data/com.jkali.crm_control')) {
      if (f.cap instanceof Error) throw f.cap; return f.cap;
    }
    if (path.endsWith('/state')) return f.state;
    if (path.endsWith('/com.jkali.share_override')) return f.override;
    if (path.includes('/send/com.jkali.policy.request/')) {
      f.last = body;
      if (f.onPut) await f.onPut(body);
      return { event_id: '$request' };
    }
    if (path.includes('/state/com.jkali.policy.result/')) { if (!f.result) throw missing(); return f.result; }
    throw new Error('Unexpected path ' + path);
  };
  f.writer = createOrderedConsentWriter({ api, session: () => ({ ...f.identity }), uuid: () => 'cmd-' + ++f.count, wait: async () => {}, polls: 2 });
  f.write = (level = 'private') => f.writer('!chat:local', level, async () => { f.legacy++; return level; });
  f.apply = () => {
    f.onPut = body => {
      f.result = { schema: 1, command_id: body.command_id, room_id: body.room_id, state: 'applied', operation_id: 'op-1', revision: 1 };
      f.override = { ...(body.level === 'inherit' ? {} : { state: body.level }),
        'com.jkali.policy_journal': { operation_id: 'op-1', revision: 1 } };
    };
  };
  return f;
}

let f = fixture(); f.cap = missing(); assert.equal(await f.write(), 'private'); assert.equal(f.legacy, 1);
for (const cap of [null, {}, { schema: 1 }, Object.assign(new Error('offline'), { status: 503 })]) {
  f = fixture(); f.cap = cap; await assert.rejects(f.write()); assert.equal(f.legacy, 0); assert.equal(f.calls.some(c => c.method === 'PUT'), false);
}
for (const level of ['private', 'share', 'direct', null]) {
  f = fixture(); f.apply(); assert.equal(await f.write(level), level); assert.equal(f.legacy, 0);
  assert.equal(f.last.level, level || 'inherit'); assert.equal(f.last.expected_revision, 0);
  assert.ok(f.calls.filter(c => c.method === 'PUT').every(c => c.path.includes('/send/com.jkali.policy.request/')));
}
f = fixture(); await assert.rejects(f.write(), /awaiting confirmation/);
const first = f.last;
f.apply(); await f.write(); assert.deepEqual(f.last, first); assert.equal(f.count, 1, 'retry keeps exact intent and transaction');
f = fixture(); await assert.rejects(f.write()); f.cap = missing(); await assert.rejects(f.write(), /unavailable/); assert.equal(f.legacy, 0);
f = fixture(); await assert.rejects(f.write()); f.cap.generation = 'replacement'; await assert.rejects(f.write(), /changed/); assert.equal(f.count, 1);
f = fixture(); f.apply(); f.onPut = body => { f.result = { schema: 1, command_id: body.command_id, room_id: body.room_id, state: 'rejected', reason: 'revision-conflict' }; };
await assert.rejects(f.write(), /revision-conflict/); f.apply(); await f.write(); assert.equal(f.count, 2, 'terminal rejection allows a new deliberate change');
f = fixture(); f.apply(); const apply = f.onPut; f.onPut = body => { apply(body); f.override.state = 'direct'; };
await assert.rejects(f.write(), /changed again/);
f = fixture(); f.state.push({ type: 'm.room.member', state_key: '@other:local', content: { membership: 'leave' } });
await assert.rejects(f.write(), /Cannot verify/); assert.equal(f.calls.some(c => c.method === 'PUT'), false);
f = fixture(); f.onPut = () => { f.identity.token = 'new-session'; }; await assert.rejects(f.write(), /session changed/);
assert.equal(f.calls.some(c => c.path.includes('/state/com.jkali.policy.result/')), false);
f = fixture(); f.apply(); f.state.push({ type: 'com.jkali.policy.revision', state_key: '!chat:local', sender: '@user:local',
  content: { schema: 1, journal_epoch: 'journal-1', room_id: '!chat:local', revision: 7 } });
await f.write(); assert.equal(f.last.expected_revision, 7, 'published revision covers external account-data changes');
console.log('ordered_consent: absence-only legacy, typed writes, confirmed outcomes, epochs, sessions, exact retries and revision races pass');
