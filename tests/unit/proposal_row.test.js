// Plain-node test for proposal row gestures — how a pending suggestion on a
// conversation row should act: Enter/✓ send, ✕ reject, click opens the chat
// with the draft prefilled. Identifier drafts must not be treated as a room
// open (no targetRoom leak into the composer path).
//
// Run: docker run --rm -v "$(pwd)":/w -w /w node:20-alpine \
//        node tests/unit/proposal_row.test.js

import { pendingForRoom, rowGesture } from '../../apps/user/proposals.js';

let pass = 0;
let fail = 0;
const failures = [];
function ok(cond, label) { if (cond) { pass++; } else { fail++; failures.push(label); } }

const roomA = { kind: 'room', eventId: '$a', targetRoom: '!a:localhost', body: 'hello', ts: 10 };
const roomANewer = { kind: 'room', eventId: '$a2', targetRoom: '!a:localhost', body: 'newer', ts: 20 };
const roomB = { kind: 'room', eventId: '$b', targetRoom: '!b:localhost', body: 'other', ts: 15 };
const ident = {
  kind: 'identifier', eventId: '$i', targetSource: 'imessage',
  targetIdentifier: '+14155550123', targetDisplay: 'Alice', body: 'hi', ts: 30,
};

const handled = new Set();
const all = [roomA, roomANewer, roomB, ident];

ok(pendingForRoom(all, handled, '!a:localhost') === roomANewer,
  'pendingForRoom: newest unhandled room proposal for that room');
ok(pendingForRoom(all, handled, '!b:localhost') === roomB,
  'pendingForRoom: other room is independent');
ok(pendingForRoom(all, handled, '!missing:localhost') === null,
  'pendingForRoom: unknown room is null');
ok(pendingForRoom(all, handled, '') === null, 'pendingForRoom: empty room id is null');
ok(pendingForRoom(all, handled, null) === null, 'pendingForRoom: non-string room id is null');

const handledA2 = new Set(['$a2']);
ok(pendingForRoom(all, handledA2, '!a:localhost') === roomA,
  'pendingForRoom: skips handled, falls back to older pending');
ok(pendingForRoom(all, new Set(['$a', '$a2']), '!a:localhost') === null,
  'pendingForRoom: all handled for that room is null');
ok(pendingForRoom(all, handled, ident.targetIdentifier) === null,
  'pendingForRoom: identifier drafts never attach to a room row');

ok(rowGesture(roomA, 'enter').action === 'send', 'Enter on a room draft sends');
ok(rowGesture(roomA, 'accept').action === 'send', '✓ on a room draft sends');
ok(rowGesture(roomA, 'reject').action === 'reject', '✕ on a room draft rejects');

const clickRoom = rowGesture(roomA, 'click');
ok(clickRoom.action === 'open', 'click on a room draft opens the conversation');
ok(clickRoom.prefill === 'hello', 'click on a room draft prefills the composer');

const clickIdent = rowGesture(ident, 'click');
ok(clickIdent.action === 'detail', 'click on an identifier draft stays on the detail pane');
ok(!('prefill' in clickIdent) || !clickIdent.prefill,
  'identifier click does not prefill a conversation composer');
ok(rowGesture(ident, 'enter').action === 'send', 'Enter on an identifier draft still means send');
ok(rowGesture(null, 'enter').action === 'none', 'no proposal: Enter is a no-op for this helper');
ok(rowGesture(roomA, 'nope').action === 'none', 'unknown gesture is a no-op');

if (fail) {
  console.error('proposal_row.test.js: ' + fail + ' FAILED, ' + pass + ' passed');
  for (const f of failures) console.error('  - ' + f);
  process.exitCode = 1;
} else {
  console.log('proposal_row.test.js: all ' + pass + ' checks passed');
}
