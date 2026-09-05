import assert from 'node:assert/strict';
import { loadContactPages } from '../../apps/user/contact-pages.js';

const validate = body => body.contacts;
const response = body => ({ ok: true, json: async () => body });
let calls = [];
let rows = await loadContactPages(async cursor => {
  calls.push(cursor);
  return response(cursor ? { contacts: [{key: 'b'}], next_cursor: null }
    : { contacts: [{key: 'a'}], next_cursor: 'second' });
}, validate);
assert.deepEqual(rows.map(r => r.key), ['a', 'b']);
assert.deepEqual(calls, [null, 'second']);
await assert.rejects(() => loadContactPages(async cursor => cursor
  ? {ok: false, status: 503} : response({contacts: [{key: 'a'}], next_cursor: 'later'}), validate), /503/);
await assert.rejects(() => loadContactPages(async () => response({contacts: [], next_cursor: 'same'}), validate), /repeated/);
await assert.rejects(() => loadContactPages(async () => response({notContacts: []}), validate), /invalid page/);
rows = await loadContactPages(async () => response({contacts: [{key: 'legacy'}]}), validate);
assert.equal(rows[0].key, 'legacy');
console.log('PASS contact pagination and later-page failure visibility');
