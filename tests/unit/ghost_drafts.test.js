// Plain-node test for the ghost-draft data layer in apps/user/proposals.js:
// attachDrafts() writes each room's unhandled room-targeted drafts onto the
// feed record (auto-sent/ambiguous/dismissed excluded), pending vs retired is
// decided by attention.js at render time, and parseProposal() never trusts a
// manager-chosen origin_ts past the server stamp (F17).
// Run: docker run --rm -v "$(pwd)":/w -w /w node:20-alpine node tests/unit/ghost_drafts.test.js
import assert from 'node:assert/strict';
import { parseProposal, attachDrafts, identifierDrafts } from '../../apps/user/proposals.js';
import { pendingDrafts, retiredDrafts, draftPending } from '../../shared/model/attention.js';

const mk = (c, id, serverTs) => parseProposal({ type: 'com.jkali.proposal', event_id: id, origin_server_ts: serverTs, content: c });
const props = [
  mk({ target_room: '!a:l', body: 'one', origin_ts: 100 }, '$1', 100),
  mk({ target_room: '!a:l', body: 'two', origin_ts: 300 }, '$2', 300),
  mk({ target_room: '!a:l', body: 'auto', origin_ts: 400, 'com.jkali.auto_sent': true, sent_event_id: '$x' }, '$3', 400),
  mk({ target_room: '!b:l', body: 'dismissed', origin_ts: 500 }, '$4', 500),
  mk({ target_identifier: '+14155550142', target_source: 'imessage', body: 'new chat', origin_ts: 600 }, '$5', 600),
];
const feed = new Map([
  ['!a:l', { id: '!a:l', lastTs: 200, drafts: [] }],
  ['!b:l', { id: '!b:l', lastTs: 10, drafts: [{ eventId: 'stale' }] }],
]);
const handled = new Set(['$4']);
assert.equal(attachDrafts(props, handled, feed), 1);
assert.deepEqual(feed.get('!a:l').drafts.map(d => d.eventId), ['$2', '$1'], 'auto-sent excluded, newest first');
assert.deepEqual(feed.get('!b:l').drafts, [], 'dismissed removed; stale cleared');
assert.deepEqual(pendingDrafts(feed.get('!a:l').drafts, 200).map(d => d.eventId), ['$2']);
assert.deepEqual(retiredDrafts(feed.get('!a:l').drafts, 200, 1000).map(d => d.eventId), ['$1']);
assert.deepEqual(identifierDrafts(props, handled).map(p => p.eventId), ['$5']);

// F17: a far-future manager origin_ts is clamped to the server stamp
const now = Date.now();
const future = mk({ target_room: '!a:l', body: 'f', origin_ts: now + 1e12 }, '$f', now - 5000);
assert.equal(future.ts, now - 5000);
assert.equal(draftPending(future.ts, now), false, 'cannot stay pending past real activity');
// server stamp missing (old test fixtures): the claimed ts still works
assert.equal(mk({ target_room: '!a:l', body: 'g', origin_ts: 42 }, '$g').ts, 42);
console.log('ghost_drafts.test.js: ok');
