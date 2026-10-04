// Plain-node test for parseReadState (shared/ui/account-data.js): the server's
// notification_count is the only unread source, and "the other party read" is
// the newest m.receipt from a sender that is not us, not our ghost, not a bot.
// Run: docker run --rm -v "$(pwd)":/w -w /w node:20-alpine node tests/unit/read_state.test.js
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const src = fs.readFileSync(new URL('../../shared/ui/account-data.js', import.meta.url), 'utf8')
  .replace(/^import .*;\n/gm, '').replace(/^export .*;\n/gm, '');
const ctx = vm.createContext({ S: {}, SOURCES: [{ id: 'whatsapp', botMxid: '@whatsappbot:localhost' }], setTimeout });
vm.runInContext(src, ctx);
const self = new Set(['@me:localhost', '@whatsapp_123:localhost']);

const room = {
  unread_notifications: { notification_count: 3, highlight_count: 0 },
  ephemeral: { events: [{ type: 'm.receipt', content: {
    '$e1': { 'm.read': { '@me:localhost': { ts: 900 }, '@whatsapp_555:localhost': { ts: 700 } } },
    '$e2': { 'm.read': { '@whatsapp_555:localhost': { ts: 800 }, '@whatsappbot:localhost': { ts: 999 } } },
  } }] },
};
const rs = ctx.parseReadState(room, self);
assert.equal(rs.unread, 3);
assert.equal(rs.remoteReadTs, 800, 'newest receipt from a non-self, non-bot sender');
// objects cross a vm realm, so compare fields (deepStrictEqual checks prototypes)
const flat = (o) => ({ unread: o.unread, remoteReadTs: o.remoteReadTs });
assert.deepEqual(flat(ctx.parseReadState({}, self)), { unread: 0, remoteReadTs: 0 });
assert.deepEqual(flat(ctx.parseReadState({ unread_notifications: { notification_count: 'x' } }, self)), { unread: 0, remoteReadTs: 0 });
assert.equal(ctx.parseReadState({ ephemeral: { events: [{ type: 'm.receipt', content: { '$a': { 'm.read': { '@me:localhost': { ts: 5 } } } } }] } }, self).remoteReadTs, 0);
console.log('read_state.test.js: ok');
