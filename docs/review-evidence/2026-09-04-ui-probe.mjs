// Offline reproduction of a stale conversation-open completion blocking the
// currently selected conversation's live watch. No DOM, credentials, or network.
import fs from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';

const source = fs.readFileSync(new URL('../../shared/ui/chat.js', import.meta.url), 'utf8');
const start = source.indexOf('async function startConvoWatch(roomId)');
const end = source.indexOf('// CV-S1 / CV-2:', start);
assert.ok(start >= 0 && end > start);
const requests = [];
const S = { token: 'synthetic', convoRunning: false, openRoomId: '!B:localhost' };
const context = vm.createContext({
  S, api: async (...args) => { requests.push(args); throw new Error('Unexpected API call'); },
});
vm.runInContext(source.slice(start, end), context);
// Open A's pending /messages returns after B was selected, before B's returns.
await context.startConvoWatch('!A:localhost');
// B's own /messages now returns and tries to start B's live watch.
await context.startConvoWatch('!B:localhost');
assert.equal(S.convoRunning, true);
assert.equal(requests.length, 0);
console.log(JSON.stringify({
  probe: 'stale_open_blocks_current_live_watch',
  selected: S.openRoomId, running_flag: S.convoRunning, sync_requests: requests.length,
}, null, 2));
