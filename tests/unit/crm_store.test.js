import assert from 'node:assert/strict';
import { createCRMStore } from '../../apps/master/crm-store.js';

const ROOM = '!crm:master', MANAGER = '@manager:master', TYPE = 'com.jkali.crm.operation';
const op = (op_id, type = 'list.create', entity_id = op_id, payload = { name: op_id }, base_revision = 0) => ({ schema: 1, workspace_id: ROOM, op_id, type, entity_id, base_revision, payload });
const event = (content, event_id = '$' + content.op_id) => ({ type: TYPE, event_id, sender: MANAGER, room_id: ROOM, content });
const clone = value => JSON.parse(JSON.stringify(value));
const deferred = () => { let resolve, reject; const promise = new Promise((a, b) => { resolve = a; reject = b; }); return { promise, resolve, reject }; };
function harness(initial = []) {
  const h = { events: clone(initial), calls: [], publications: [], current: true, getHook: null, putHook: null, bootstrapHook: null, transactions: new Map() };
  h.bootstrap = async () => h.bootstrapHook ? h.bootstrapHook() : ({ room_id: ROOM, manager_mxid: MANAGER, schema_version: 1 });
  h.api = async (method, path, body) => {
    h.calls.push({ method, path, body: body && clone(body) });
    if (method === 'GET') {
      if (h.getHook) { const response = await h.getHook(path); if (response !== undefined) return response; }
      const url = new URL(path, 'http://fixture.invalid');
      assert.equal(url.pathname, '/_matrix/client/v3/rooms/' + encodeURIComponent(ROOM) + '/messages');
      assert.equal(url.searchParams.get('dir'), 'f');
      const start = Number((url.searchParams.get('from') || 'p0').slice(1));
      const chunk = clone(h.events.slice(start, start + 100));
      return { start: 'p' + start, end: 'p' + (start + chunk.length), chunk };
    }
    assert.equal(method, 'PUT');
    assert.ok(path.startsWith('/_matrix/client/v3/rooms/' + encodeURIComponent(ROOM) + '/send/' + TYPE + '/crm_'));
    if (h.putHook) { const response = await h.putHook(path, body); if (response !== undefined) return response; }
    if (!h.transactions.has(path)) { const e = event(clone(body), '$put-' + h.transactions.size); h.transactions.set(path, e.event_id); h.events.push(e); }
    return { event_id: h.transactions.get(path) };
  };
  h.newStore = () => createCRMStore({ api: h.api, bootstrap: h.bootstrap, managerMxid: MANAGER, isCurrent: () => h.current, onChange: value => h.publications.push(value) });
  h.store = h.newStore();
  h.puts = () => h.calls.filter(c => c.method === 'PUT');
  return h;
}
let failures = 0, passed = 0;
async function test(name, run) { try { await run(); passed++; } catch (error) { failures++; console.error('FAIL crm_store:', name, '\n', error.stack); } }

await test('canonical order decides conflicts, not operation timestamps or entity names', async () => {
  const h = harness([event(op('create', 'list.create', 'list')), event(op('z-first', 'list.rename', 'list', { name: 'first' }, 1)), event(op('a-second', 'list.rename', 'list', { name: 'second' }, 1))]);
  await h.store.refresh(); const { state, ready } = h.store.snapshot();
  assert.equal(ready, true); assert.equal(state.lists.list.name, 'first'); assert.equal(state.operations['a-second'].outcome.code, 'revision-conflict');
});
await test('duplicates across events and two tabs converge without another write', async () => {
  const create = op('create'); const h = harness([event(create), event(create, '$duplicate')]); const second = h.newStore();
  await Promise.all([h.store.refresh(), second.refresh()]);
  assert.equal((await h.store.append(create)).duplicate, true); assert.equal((await second.append(create)).duplicate, true); assert.equal(h.puts().length, 0);
  const rename = op('rename', 'list.rename', 'create', { name: 'renamed' }, 1);
  assert.equal((await second.append(rename)).accepted, true); await h.store.refresh();
  assert.equal(h.store.snapshot().state.lists.create.name, 'renamed'); assert.equal(h.puts().length, 1);
});
await test('operation ID collisions never write a changed payload', async () => {
  const h = harness([event(op('same'))]); await h.store.refresh();
  const result = await h.store.append(op('same', 'list.create', 'other'));
  assert.equal(result.code, 'operation-id-collision'); assert.equal(h.puts().length, 0);
});
for (const [name, mutation] of [
  ['sender', e => { e.sender = '@intruder:master'; }],
  ['state key', e => { e.state_key = ''; }],
  ['room', e => { e.room_id = '!foreign:master'; }],
  ['event ID', e => { e.event_id = 'not-an-event'; }],
]) await test('reject hostile operation ' + name, async () => {
  const e = event(op('hostile')); mutation(e); const h = harness([e]);
  await assert.rejects(h.store.refresh()); assert.equal(h.store.snapshot().ready, false); assert.equal(Object.keys(h.store.snapshot().state.lists).length, 0);
});
await test('failed page does not commit partial history and retry resumes safely', async () => {
  const h = harness(); let reads = 0;
  h.getHook = async () => { if (++reads === 1) return { chunk: [event(op('partial'))], end: 'p1' }; throw new Error('offline'); };
  await assert.rejects(h.store.refresh(), /offline/); assert.equal(h.store.snapshot().ready, false); assert.equal(h.store.snapshot().state.lists.partial, undefined);
  await assert.rejects(h.store.append(op('cannot-write')), /offline/); assert.equal(h.puts().length, 0);
  h.getHook = null; h.events = [event(op('partial'))]; await h.store.refresh(); assert.ok(h.store.snapshot().state.lists.partial);
});
await test('empty filtered pages still advance and cursor loops fail closed', async () => {
  const h = harness(); let n = 0;
  h.getHook = async () => ++n === 1 ? { chunk: [], end: 'p1' } : n === 2 ? { chunk: [event(op('after-empty'))], end: 'p2' } : { chunk: [], end: 'p2' };
  await h.store.refresh(); assert.ok(h.store.snapshot().state.lists['after-empty']);
  const looping = harness(); let page = 0;
  looping.getHook = async () => ({ chunk: [], end: ++page % 2 ? 'p1' : 'p2' });
  await assert.rejects(looping.store.refresh(), /cursor loop/); assert.equal(looping.store.snapshot().ready, false);
});
await test('page budget leaves writes disabled', async () => {
  const h = harness(); let n = 0; h.getHook = async () => ({ chunk: [], end: 'p' + ++n });
  await h.store.refresh(); assert.equal(h.store.snapshot().ready, false);
  await assert.rejects(h.store.append(op('too-early')), /still loading/); assert.equal(h.puts().length, 0);
});
await test('session invalidation during bootstrap or GET never publishes stale data', async () => {
  for (const stage of ['bootstrap', 'GET']) {
    const h = harness([event(op('private'))]), wait = deferred();
    if (stage === 'bootstrap') h.bootstrapHook = () => wait.promise; else h.getHook = () => wait.promise;
    const pending = h.store.refresh(); await new Promise(resolve => setImmediate(resolve)); h.current = false;
    wait.resolve(stage === 'bootstrap' ? { room_id: ROOM, manager_mxid: MANAGER, schema_version: 1 } : { chunk: h.events });
    await assert.rejects(pending, /session changed/); assert.equal(h.publications.length, 0); assert.equal(h.store.snapshot().state?.lists.private, undefined);
  }
});
await test('close during PUT clears state and never acknowledges stale success', async () => {
  const h = harness(), wait = deferred(), reached = deferred(); await h.store.refresh();
  h.putHook = () => { reached.resolve(); return wait.promise; };
  const pending = h.store.append(op('late')); await reached.promise; h.store.close(); wait.resolve({ event_id: '$late' });
  await assert.rejects(pending, /session changed/); assert.deepEqual(h.store.snapshot(), { state: null, ready: false, roomId: null });
});
await test('fresh bootstrap rejects changed workspace before writing', async () => {
  const h = harness(); await h.store.refresh(); h.bootstrapHook = async () => ({ room_id: '!replacement:master', manager_mxid: MANAGER, schema_version: 1 });
  await assert.rejects(h.store.append(op('wrong-room')), /workspace identity changed/); assert.equal(h.puts().length, 0);
});
await test('malformed batch digest cannot become accepted or written', async () => {
  const h = harness([event(op('bad-digest', 'batch.review', 'batch', { batch_id: 'batch', payload_hash: '0'.repeat(64), children: [] }))]);
  await h.store.refresh(); assert.equal(h.store.snapshot().state.batches.batch, undefined);
  const result = await h.store.append(h.events[0].content); assert.equal(result.accepted, false); assert.equal(result.code, 'invalid-manifest-hash'); assert.equal(h.puts().length, 0);
});
await test('incremental redaction rebuild removes previously applied original', async () => {
  const original = event(op('sensitive')); const h = harness([original]); await h.store.refresh(); assert.ok(h.store.snapshot().state.lists.sensitive);
  h.events[0] = { ...original, content: {}, unsigned: { redacted_because: { event_id: '$redaction' } } };
  h.events.push({ event_id: '$redaction', type: 'm.room.redaction', sender: MANAGER, room_id: ROOM, content: { redacts: original.event_id } });
  await h.store.refresh(); assert.equal(h.store.snapshot().ready, true); assert.equal(h.store.snapshot().state.lists.sensitive, undefined);
});
await test('redaction arriving during initial pagination rebuilds already applied operation', async () => {
  const original = event(op('sensitive')), redaction = { event_id: '$redaction', type: 'm.room.redaction', sender: MANAGER, room_id: ROOM, content: { redacts: original.event_id } };
  const h = harness(); let page = 0;
  h.getHook = async () => {
    if (++page === 1) return { chunk: [original], end: 'p1' };
    if (page === 2) { h.events = [{ ...original, content: {}, unsigned: { redacted_because: redaction } }, redaction]; h.getHook = null; return { chunk: [redaction], end: 'p2' }; }
  };
  await h.store.refresh(); assert.equal(h.store.snapshot().ready, true); assert.equal(h.store.snapshot().state.lists.sensitive, undefined);
});
await test('hostile redaction scope is rejected before rebuild', async () => {
  const h = harness([{ type: 'm.room.redaction', event_id: '$bad-redaction', sender: '@intruder:master', room_id: '!foreign:master', state_key: '', content: {} }]);
  await assert.rejects(h.store.refresh(), /Unverified/); assert.equal(h.store.snapshot().ready, false);
});
await test('lost write response is reconciled with same operation identity, never blindly resent', async () => {
  const h = harness(); const original = op('ambiguous'); let attempted = false;
  h.putHook = async (path, body) => { if (!attempted) { attempted = true; h.events.push(event(body, '$committed')); throw new Error('response lost'); } };
  await assert.rejects(h.store.append(original), /response lost/); assert.equal(h.puts().length, 1);
  const retry = await h.store.append(original); assert.equal(retry.duplicate, true); assert.equal(h.puts().length, 1); assert.ok(h.store.snapshot().state.lists.ambiguous);
});
await test('queued append freezes caller intent while waiting', async () => {
  const h = harness(), wait = deferred(), reached = deferred(); let first = true;
  h.bootstrapHook = async () => { if (first) { first = false; reached.resolve(); await wait.promise; } return { room_id: ROOM, manager_mxid: MANAGER, schema_version: 1 }; };
  const submitted = op('frozen'); const pending = h.store.append(submitted); await reached.promise; submitted.payload.name = 'mutated'; wait.resolve();
  await pending; assert.equal(h.puts()[0].body.payload.name, 'frozen'); assert.equal(h.store.snapshot().state.lists.frozen.name, 'frozen');
});

await test('a competing tab winning during PUT changes canonical result to conflict', async () => {
  const h = harness([event(op('seed', 'list.create', 'list'))]);
  h.putHook = async () => { h.events.push(event(op('rival', 'list.rename', 'list', { name: 'rival' }, 1))); h.putHook = null; };
  const result = await h.store.append(op('mine', 'list.rename', 'list', { name: 'mine' }, 1));
  assert.equal(result.accepted, false); assert.equal(result.code, 'revision-conflict');
  assert.equal(h.store.snapshot().state.lists.list.name, 'rival'); assert.equal(h.puts().length, 1);
});
await test('retry after a failure before acceptance preserves exact transaction ID and body', async () => {
  const h = harness(); h.putHook = async () => { h.putHook = null; throw new Error('transport failed before acceptance'); };
  const original = op('retry-same-id'); await assert.rejects(h.store.append(original), /before acceptance/);
  assert.equal((await h.store.append(original)).accepted, true); assert.equal(h.puts().length, 2);
  assert.equal(h.puts()[0].path, h.puts()[1].path); assert.deepEqual(h.puts()[0].body, h.puts()[1].body);
});
await test('continually changing redaction history stops with writes disabled', async () => {
  const original = event(op('sensitive')), redaction = { event_id: '$redaction', type: 'm.room.redaction', sender: MANAGER, room_id: ROOM, content: { redacts: original.event_id } };
  const h = harness([original, redaction]);
  await assert.rejects(h.store.refresh(), /changed during replay/); assert.equal(h.store.snapshot().ready, false);
  assert.equal(h.store.snapshot().state.lists.sensitive, undefined); assert.equal(h.puts().length, 0);
});

console.log(`crm_store: ${passed} independent scenarios passed; ${failures} failed`);
if (failures) process.exitCode = 1;
