// Plain-node test for the S2 wire contract (direct-share-level plan, D3/S2
// acceptance): a proposal's `com.jkali.auto_sent` / `com.jkali.send_ambiguous`
// content flags classify it as non-actionable history/ambiguous FROM EVENT
// CONTENT ONLY — never from localStorage — so a fresh browser profile (empty
// localStorage) classifies identically to one with populated state.
//
// Run: docker run --rm -v "$(pwd)":/w -w /w node:20-alpine \
//        node tests/unit/proposal_classification.test.js

import { parseProposal, partitionProposals, pendingForRoom } from '../../apps/user/proposals.js';

let pass = 0;
let fail = 0;
const failures = [];
function ok(cond, label) { if (cond) { pass++; } else { fail++; failures.push(label); } }

const mk = (content, event_id = '$e') => ({ type: 'com.jkali.proposal', event_id, content });

// ---- auto_sent classification from content ----
const autoSent = parseProposal(mk({
  target_room: '!a:localhost', body: 'hi', origin_ts: 10,
  'com.jkali.auto_sent': true, sent_event_id: '$local1',
}, '$as'));
ok(autoSent && autoSent.autoSent === true, 'auto_sent flag parsed true');
ok(autoSent && autoSent.sentEventId === '$local1', 'sent_event_id carried through');
ok(autoSent && autoSent.ambiguous === false, 'auto_sent proposal is not ambiguous');

// ---- ambiguous classification from content ----
const ambiguous = parseProposal(mk({
  target_room: '!b:localhost', body: 'hi2', origin_ts: 20,
  'com.jkali.send_ambiguous': true,
}, '$am'));
ok(ambiguous && ambiguous.ambiguous === true, 'send_ambiguous flag parsed true');
ok(ambiguous && ambiguous.autoSent === false, 'ambiguous proposal is not auto_sent');
ok(ambiguous && ambiguous.sentEventId === null, 'ambiguous proposal carries no sent_event_id');

// ---- a normal draft is neither ----
const normal = parseProposal(mk({ target_room: '!c:localhost', body: 'plain', origin_ts: 30 }, '$n'));
ok(normal && normal.autoSent === false && normal.ambiguous === false, 'plain draft is actionable');

// ---- auto_sent wins over a (malformed) simultaneous ambiguous flag ----
const both = parseProposal(mk({
  target_room: '!d:localhost', body: 'x', 'com.jkali.auto_sent': true,
  sent_event_id: '$local2', 'com.jkali.send_ambiguous': true,
}, '$both'));
ok(both && both.autoSent === true && both.ambiguous === false,
  'auto_sent takes priority over a simultaneous ambiguous flag');

// ---- partitionProposals: auto_sent/ambiguous NEVER pending or dismissed, on
// an EMPTY localStorage (fresh browser profile) AND a POPULATED one — the
// exact same classification either way (F5's localStorage-independence). ----
const all = [autoSent, ambiguous, normal];
const emptyHandled = new Set();
const populatedHandled = new Set([autoSent.eventId, ambiguous.eventId, normal.eventId, 'unrelated-id']);

for (const [label, handled] of [['empty localStorage', emptyHandled], ['populated localStorage', populatedHandled]]) {
  const parts = partitionProposals(all, handled);
  ok(parts.sent.length === 1 && parts.sent[0].eventId === autoSent.eventId,
    'sent bucket holds exactly the auto_sent record (' + label + ')');
  ok(parts.ambiguous.length === 1 && parts.ambiguous[0].eventId === ambiguous.eventId,
    'ambiguous bucket holds exactly the ambiguous record (' + label + ')');
  ok(!parts.pending.some((p) => p.eventId === autoSent.eventId || p.eventId === ambiguous.eventId),
    'pending never includes auto_sent/ambiguous (' + label + ')');
  ok(!parts.dismissed.some((p) => p.eventId === autoSent.eventId || p.eventId === ambiguous.eventId),
    'dismissed never includes auto_sent/ambiguous (' + label + ')');
}
// The plain draft's own pending/dismissed placement is the ONLY thing allowed
// to differ between the two localStorage states.
ok(partitionProposals(all, emptyHandled).pending.some((p) => p.eventId === normal.eventId),
  'plain draft is pending when unhandled (fresh profile)');
ok(partitionProposals(all, populatedHandled).dismissed.some((p) => p.eventId === normal.eventId),
  'plain draft is dismissed once handled locally (populated profile)');

// ---- pendingForRoom: auto_sent/ambiguous never attach to a conversation row --
ok(pendingForRoom(all, emptyHandled, '!a:localhost') === null,
  'pendingForRoom never returns an auto_sent proposal');
ok(pendingForRoom(all, emptyHandled, '!b:localhost') === null,
  'pendingForRoom never returns an ambiguous proposal');
ok(pendingForRoom(all, emptyHandled, '!c:localhost') === normal,
  'pendingForRoom still returns a plain pending draft');

// ---- malformed content is inert, not a crash ----
ok(parseProposal(mk({ target_room: '!e:localhost', body: 'x', 'com.jkali.auto_sent': 'yes' }))
  .autoSent === false, 'non-boolean auto_sent flag is ignored');
ok(parseProposal(mk({ target_room: '!f:localhost', body: 'x', 'com.jkali.auto_sent': true, sent_event_id: 42 }))
  .sentEventId === null, 'non-string sent_event_id is dropped, not passed through');

if (fail) {
  console.error('proposal_classification.test.js: ' + fail + ' FAILED, ' + pass + ' passed');
  for (const f of failures) console.error('  - ' + f);
  process.exitCode = 1;
} else {
  console.log('proposal_classification.test.js: all ' + pass + ' checks passed');
}
