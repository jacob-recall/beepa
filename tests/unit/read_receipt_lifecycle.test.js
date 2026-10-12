import fs from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';

const source = fs.readFileSync(new URL('../../shared/ui/chat.js', import.meta.url), 'utf8')
  .replace(/^import .*;\n/gm, '').replace(/^export .*;\n/gm, '');
const S = { token: 'one', openRoomId: '!room:local', joinedSet: new Set(['!room:local']) };
const rec = { unread: 2, lastTs: 100 };
const requests = [];
let enabled = true;
const ctx = vm.createContext({ S, ROOMID_RE: /^![^:]+:.+$/, feedModel: new Map([['!room:local', rec]]),
  localStorage: { getItem: () => enabled ? null : '0' }, scheduleFeedRender() {},
  api: (method, path, body) => new Promise((resolve, reject) => requests.push({ method, path, body, resolve, reject })),
});
vm.runInContext(source, ctx);
enabled = false;
await ctx.markRead('!room:local', '$message');
assert.equal(requests.length, 0, 'disabled receipts do not write');
enabled = true;
await ctx.markRead('!other:local', '$message');
await ctx.markRead('!room:local', 'bad');
assert.equal(requests.length, 0, 'invalid or unopened rooms do not write');
let pending = ctx.markRead('!room:local', '$message');
requests.at(-1).reject(new Error('offline')); await pending;
assert.equal(rec.unread, 2, 'failure preserves unread');
pending = ctx.markRead('!room:local', '$message');
const request = requests.at(-1);
assert.equal(request.method, 'POST');
assert.ok(request.path.endsWith('/read_markers'));
assert.equal(request.body['m.fully_read'], '$message');
assert.equal(request.body['m.read'], '$message');
request.resolve({}); await pending;
assert.equal(rec.unread, 0, 'success clears the badge');
rec.unread = 2;
pending = ctx.markRead('!room:local', '$message');
rec.lastTs = 200; rec.unread = 3;
requests.at(-1).resolve({}); await pending;
assert.equal(rec.unread, 3, 'delayed receipt response cannot clear a newer message');
pending = ctx.markRead('!room:local', '$new-message');
S.token = 'another-session';
requests.at(-1).resolve({}); await pending;
assert.equal(rec.unread, 3, 'old-session receipt cannot mutate new-session state');
console.log('read_receipt_lifecycle: gates, failure, success, message race and session race pass');
