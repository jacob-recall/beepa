// Real production store against sandbox-owned Synapse; credentials only on stdin.
import fs from 'node:fs';
import assert from 'node:assert/strict';
import { createCRMStore } from '../../apps/master/crm-store.js';

const input = JSON.parse(fs.readFileSync(0, 'utf8'));
for (const address of [input.base, input.enrollBase]) {
  const url = new URL(address);
  assert.equal(url.hostname, '127.0.0.1');
  assert.ok(Number(url.port) >= 1024 && ![8008, 8009, 8010, 8011, 8017, 8018, 8019, 8028].includes(Number(url.port)), 'sandbox ports only');
}
const observations = [];
let writes = 0, barrier = null;
async function request(base, method, path, body) {
  const response = await fetch(base + path, {
    method, headers: { Authorization: 'Bearer ' + input.token, 'Content-Type': 'application/json' },
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
    signal: AbortSignal.timeout(10000),
  });
  assert.equal(response.status, 200, 'synthetic request accepted: ' + method + ' ' + path);
  return response.json();
}
async function api(method, path, body) {
  if (method === 'PUT') {
    writes++;
    if (barrier) {
      const gate = barrier;
      gate.count++;
      if (gate.count === 2) gate.release();
      await gate.wait;
    }
  }
  const result = await request(input.base, method, path, body);
  if (path.includes('/messages?')) observations.push({ count: result.chunk.length, start: result.start, end: result.end });
  return result;
}
const bootstrap = () => request(input.enrollBase, 'POST', '/admin/crm-room', {});
const make = () => createCRMStore({ api, bootstrap, managerMxid: input.managerMxid });
const operation = (id, type, revision, payload) => ({ schema: 1, op_id: id, workspace_id: input.roomId,
  type, entity_id: 'integration-list', base_revision: revision, payload });
const first = make();
await first.refresh();
assert.equal(first.snapshot().ready, true, 'forward history reaches a writable end');
const create = operation('integration-create', 'list.create', 0, { name: 'Integrated list' });
assert.equal((await first.append(create)).accepted, true);
assert.equal(first.snapshot().state.lists['integration-list'].name, 'Integrated list');
first.close();
const second = make();
await second.refresh();
assert.equal(second.snapshot().ready, true);
assert.equal(second.snapshot().state.lists['integration-list'].name, 'Integrated list', 'canonical replay survives reopening');
const before = writes;
assert.equal((await second.append(create)).duplicate, true);
assert.equal(writes, before, 'operation replay does not send a second write');
const otherTab = make();
await otherTab.refresh();
let release;
const wait = new Promise(resolve => { release = resolve; });
barrier = { count: 0, wait, release };
const timeout = setTimeout(() => release(), 10000);
const results = await Promise.all([
  second.append(operation('integration-rename-a', 'list.rename', 1, { name: 'Rename from A' })),
  otherTab.append(operation('integration-rename-b', 'list.rename', 1, { name: 'Rename from B' })),
]);
clearTimeout(timeout);
assert.equal(barrier.count, 2, 'two tabs passed preflight before writes raced');
barrier = null;
assert.equal(results.filter(result => result.accepted).length, 1, 'one canonical winner');
assert.equal(results.filter(result => result.code === 'revision-conflict').length, 1, 'one visible stale conflict');
await Promise.all([second.refresh(), otherTab.refresh()]);
assert.deepEqual(second.snapshot().state, otherTab.snapshot().state, 'tabs converge on canonical replay');
assert.equal(second.snapshot().state.lists['integration-list'].revision, 2);
const final = make();
await final.refresh();
assert.deepEqual(final.snapshot().state, second.snapshot().state, 'fresh store rebuild includes rejected operation evidence');
for (const store of [second, otherTab, final]) store.close();
console.log(JSON.stringify({ pass: true, writes, historyPages: observations.length,
  initialPage: observations[0], finalPage: observations.at(-1),
  checks: 'reopen, duplicate id, concurrent conflict, full canonical convergence' }));
