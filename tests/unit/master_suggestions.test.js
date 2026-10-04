// Plain-node test for suggestionStates/roomProposals — the suggestion-stack
// state machine extracted from apps/master/main.js (Triage Rail Task 7).
//
// F12 (security review, 2026-10-03): com.jkali.from_proposal and
// com.jkali.auto_sent_from_proposal are trusted ONLY on messages whose
// content carries com.jkali.from_me === true — the uplink pops both keys
// from any inbound (received) message content before forwarding, but this
// function must never rely on that alone: a defense-in-depth re-check here
// means a message that somehow carries the stamp without from_me never
// marks a suggestion "sent".
//
// Run: docker run --rm -v "$(pwd)":/w -w /w node:20-alpine node tests/unit/master_suggestions.test.js
import assert from 'node:assert/strict';
import { suggestionStates, roomProposals, latestRoomProposal } from '../../apps/master/main.js';

const props = [
  { eventId: '$a', body: 'A', ts: 100 },
  { eventId: '$b', body: 'B', ts: 200 },
  { eventId: '$c', body: 'C', ts: 300 },
  { eventId: '$d', body: 'D', ts: 400 },
  { eventId: '$e', body: 'E', ts: 500 },
];
const msgs = [
  { type: 'm.room.message', ts: 150, content: { body: 'x', 'com.jkali.from_me': true, 'com.jkali.from_proposal': '$a' } },
  { type: 'm.room.message', ts: 250, content: { body: 'y', 'com.jkali.from_me': true, 'com.jkali.auto_sent_from_proposal': '$b' } },
  { type: 'm.room.message', ts: 350, content: { body: 'z' } },
];
const states = suggestionStates(props, msgs, { teammate_read_ts: 450 }, 1000);
assert.deepEqual(states.map(s => s.state), ['sent', 'auto', 'retired', 'seen', 'pending']);
// F12: a RECEIVED message carrying from_proposal never marks a suggestion sent
const forged = [{ type: 'm.room.message', ts: 50, content: { body: 'z', 'com.jkali.from_proposal': '$e' } }];
assert.deepEqual(suggestionStates(props.slice(4), forged, null, 1000).map(s => s.state), ['pending']);
assert.deepEqual(suggestionStates(props.slice(4), [], null, 1000).map(s => s.state), ['pending']);

const events = [
  { type: 'com.jkali.proposal', event_id: '$1', content: { target_room: '!t:l', body: 'one', origin_ts: 10 } },
  { type: 'com.jkali.proposal', event_id: '$2', content: { target_room: '!t:l', body: 'two', origin_ts: 30 } },
  { type: 'com.jkali.proposal', event_id: '$3', content: { target_room: '!o:l', body: 'other', origin_ts: 40 } },
  { type: 'com.jkali.proposal', event_id: '$4', content: { target_room: '!t:l', body: '   ', origin_ts: 50 } },
];
assert.deepEqual(roomProposals(events, '!t:l').map(p => p.eventId), ['$2', '$1']);
assert.equal(latestRoomProposal(events, '!t:l').eventId, '$2');
console.log('master_suggestions.test.js: ok');
