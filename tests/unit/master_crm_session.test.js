// Exercise the actual unexported master session adapter with the real CRM store
// and Matrix transport. Extract function declarations only to avoid booting DOM UI.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createCRMStore } from '../../apps/master/crm-store.js';
import { api, setOnUnauthorized, configureMatrixBase } from '../../shared/matrix/client.js';
import { S } from '../../shared/state.js';

const source = readFileSync(new URL('../../apps/master/main.js', import.meta.url), 'utf8');
function actual(name) {
  const match = source.match(new RegExp('function ' + name + '\\([^\\n]*\\) \\{[\\s\\S]*?\\n\\}'));
  assert.ok(match, 'actual master function exists: ' + name);
  return match[0];
}
const buildAdapter = new Function('createCRMStore', 'S', 'api', 'fetch', 'ENROLL_BASE', 'sanitize', 'MS', '$', 'stopTail', 'sessionStorage',
  'let crmView = null, crmStore = null, crmSession = null, notifications = null;\n' + actual('getCRMStore') + '\n' + actual('forgetSession') + '\nreturn { getCRMStore, forgetSession };');
const deferred = () => { let resolve, reject; const promise = new Promise((a, b) => { resolve = a; reject = b; }); return { promise, resolve, reject }; };
const ROOM = '!crm:master', MANAGER = '@manager:master';
const info = user => ({ room_id: ROOM, manager_mxid: user, schema_version: 1 });
const ok = value => ({ status: 200, ok: true, json: async () => value });
const flush = () => new Promise(resolve => setImmediate(resolve));
const savedFetch = globalThis.fetch;
let passed = 0, failures = 0;
function fixture() {
  const f = { requests: [], removed: [], stopped: 0, currentHandler: null };
  const fetcher = (url, options) => {
    f.requests.push({ url, options });
    if (f.currentHandler) return f.currentHandler(url, options);
    return Promise.resolve(ok(url.endsWith('/admin/crm-room') ? info(S.userId) : { chunk: [] }));
  };
  globalThis.fetch = fetcher;
  S.token = 'synthetic-old'; S.userId = MANAGER;
  configureMatrixBase({ csBase: 'http://matrix.fixture', serverName: 'master' });
  f.adapter = buildAdapter(createCRMStore, S, api, fetcher, 'http://enroll.fixture', value => String(value),
    { rooms: {}, byUser: new Map(), feed: [], pollTimer: null }, () => null,
    () => f.stopped++, { removeItem: key => f.removed.push(key) });
  setOnUnauthorized(f.adapter.forgetSession);
  return f;
}
async function test(name, run) {
  try { await run(); passed++; } catch (error) { failures++; console.error('FAIL master_crm_session:', name, '\n', error.stack); }
  finally { S.token = null; S.userId = null; setOnUnauthorized(() => {}); }
}
await test('authentication required and same session reuses one store', async () => {
  const f = fixture(); const first = f.adapter.getCRMStore(); assert.equal(first, f.adapter.getCRMStore());
  await first.refresh();
  assert.equal(f.requests[0].url, 'http://enroll.fixture/admin/crm-room');
  assert.equal(f.requests[0].options.headers.Authorization, 'Bearer synthetic-old');
  assert.equal(f.requests[0].options.body, '{}');
  assert.equal(f.requests[1].options.headers.Authorization, 'Bearer synthetic-old');
  f.adapter.forgetSession(); assert.deepEqual(first.snapshot(), { state: null, ready: false, roomId: null });
  assert.throws(() => f.adapter.getCRMStore(), /Sign in/);
});
await test('different token retires old private store and captures new token', async () => {
  const f = fixture(), first = f.adapter.getCRMStore(); await first.refresh();
  S.token = 'synthetic-new'; const second = f.adapter.getCRMStore(); assert.notEqual(first, second);
  assert.deepEqual(first.snapshot(), { state: null, ready: false, roomId: null });
  await second.refresh(); assert.equal(f.requests.at(-1).options.headers.Authorization, 'Bearer synthetic-new');
});
await test('different signed-in identity retires cached store even with unchanged token', async () => {
  const f = fixture(), first = f.adapter.getCRMStore(); await first.refresh();
  S.userId = '@other:master'; const second = f.adapter.getCRMStore(); assert.notEqual(first, second);
  assert.equal(first.snapshot().state, null); await second.refresh(); assert.equal(second.snapshot().state.manager_mxid, '@other:master');
});
await test('late bootstrap401 cannot sign out or initialize another session', async () => {
  const f = fixture(), pendingFetch = deferred(); f.currentHandler = () => pendingFetch.promise;
  const first = f.adapter.getCRMStore(), request = first.refresh(); await flush();
  S.token = 'synthetic-new'; S.userId = '@other:master';
  pendingFetch.resolve({ status: 401, ok: false, json: async () => ({ error: 'expired' }) });
  await assert.rejects(request, /session changed/); assert.equal(S.token, 'synthetic-new'); assert.equal(S.userId, '@other:master'); assert.equal(f.stopped, 0);
});
await test('identity change during bootstrap body parsing cannot bind old room', async () => {
  const f = fixture(), body = deferred(); f.currentHandler = () => Promise.resolve({ status: 200, ok: true, json: () => body.promise });
  const first = f.adapter.getCRMStore(), request = first.refresh(); await flush(); S.token = 'synthetic-new';
  body.resolve(info(MANAGER)); await assert.rejects(request, /session changed/); assert.equal(first.snapshot().roomId, null); assert.equal(f.stopped, 0);
});
await test('current bootstrap401 clears the actual app session', async () => {
  const f = fixture(); f.currentHandler = () => Promise.resolve({ status: 401, ok: false, json: async () => ({ error: 'expired' }) });
  const store = f.adapter.getCRMStore(); await assert.rejects(store.refresh(), /Sign in again/);
  assert.equal(S.token, null); assert.equal(S.userId, null); assert.equal(store.snapshot().state, null);
  assert.deepEqual(f.removed, ['master_token', 'master_user']); assert.equal(f.stopped, 1);
});
await test('current bootstrap401 clears session even when response body is not JSON', async () => {
  const f = fixture(); f.currentHandler = () => Promise.resolve({ status: 401, ok: false, json: async () => { throw new SyntaxError('HTML response'); } });
  const store = f.adapter.getCRMStore(); await assert.rejects(store.refresh());
  assert.equal(S.token, null); assert.equal(S.userId, null); assert.equal(store.snapshot().state, null); assert.equal(f.stopped, 1);
});
await test('forbidden bootstrap retains session but never creates writable store', async () => {
  const f = fixture(); f.currentHandler = () => Promise.resolve({ status: 403, ok: false, json: async () => ({ error: 'Not the manager' }) });
  const store = f.adapter.getCRMStore(); await assert.rejects(store.refresh(), /Not the manager/);
  assert.equal(S.token, 'synthetic-old'); assert.equal(store.snapshot().ready, false); assert.equal(store.snapshot().roomId, null);
});
await test('late real Matrix401 through old CRM store preserves new app session', async () => {
  const f = fixture(), response = deferred(), reached = deferred();
  f.currentHandler = url => { if (url.endsWith('/admin/crm-room')) return Promise.resolve(ok(info(MANAGER))); reached.resolve(); return response.promise; };
  const store = f.adapter.getCRMStore(), request = store.refresh(); await reached.promise;
  S.token = 'synthetic-new'; S.userId = '@other:master';
  response.resolve({ status: 401 }); await assert.rejects(request, /session expired/);
  assert.equal(S.token, 'synthetic-new'); assert.equal(f.stopped, 0);
});
await test('current real Matrix401 closes CRM store and clears app session', async () => {
  const f = fixture(); f.currentHandler = url => Promise.resolve(url.endsWith('/admin/crm-room') ? ok(info(MANAGER)) : { status: 401 });
  const store = f.adapter.getCRMStore(); await assert.rejects(store.refresh(), /session expired/);
  assert.equal(S.token, null); assert.equal(store.snapshot().state, null); assert.equal(f.stopped, 1);
});
globalThis.fetch = savedFetch;
console.log(`master_crm_session: ${passed} independent adapter scenarios passed; ${failures} failed`);
if (failures) process.exitCode = 1;
