// Plain-node test for buildIdentifierProposalContent — the pure, person-targeted
// proposal content builder extracted from apps/master/main.js's submitProposal.
//
// It guards the master's send-incapable spine: a PERSON-targeted proposal is
// still just a `com.jkali.proposal` payload (the same event type the master has
// always written), aimed at an inert contact identifier — never a message,
// never a start-chat. The identifier is SHAPE-validated (E.164 phone OR strict
// email) before it can ever become content, and the content carries NO
// target_room (that is the room-targeted branch's field, which stays unchanged).
//
// Run: docker run --rm -v "$(pwd)":/w -w /w node:20-alpine \
//        node tests/unit/proposal_identifier.test.js
// Exits 0 on all-pass, nonzero (via process.exitCode) on any failure.

import { buildIdentifierProposalContent } from '../../apps/master/main.js';

let pass = 0;
let fail = 0;
const failures = [];
function ok(cond, label) { if (cond) { pass++; } else { fail++; failures.push(label); } }

// ---- valid E.164 phone -> content object, and crucially NO target_room ----
const phone = buildIdentifierProposalContent(
  { source: 'imessage', identifier: '+14155550123', display: 'Alice', body: 'Hi Alice' });
ok(phone !== null, 'valid E.164 identifier returns content');
ok(phone && phone.target_identifier === '+14155550123', 'E.164 identifier preserved');
ok(phone && phone.target_source === 'imessage', 'source carried as target_source');
ok(phone && phone.target_display === 'Alice', 'display carried as target_display');
ok(phone && phone.body === 'Hi Alice', 'body carried through');
ok(phone && !('target_room' in phone), 'E.164 content has NO target_room key');
ok(phone && 'created_by' in phone, 'content carries a created_by field');
ok(phone && typeof phone.origin_ts === 'number', 'content carries a numeric origin_ts');

// ---- valid strict email -> content object, no target_room ----
const email = buildIdentifierProposalContent(
  { source: 'imessage', identifier: 'alice@example.com', display: 'Alice', body: 'Hello' });
ok(email !== null, 'valid email identifier returns content');
ok(email && email.target_identifier === 'alice@example.com', 'email identifier preserved');
ok(email && !('target_room' in email), 'email content has NO target_room key');

// ---- trimming: identifier and body are trimmed before use ----
const trimmed = buildIdentifierProposalContent(
  { source: 'imessage', identifier: '+14155550123', display: 'A', body: '   spaced   ' });
ok(trimmed && trimmed.body === 'spaced', 'body is trimmed');
const idTrim = buildIdentifierProposalContent(
  { source: 'imessage', identifier: '  +14155550123  ', display: 'A', body: 'x' });
ok(idTrim && idTrim.target_identifier === '+14155550123', 'identifier is trimmed before validation');

// ---- bad identifier SHAPE -> null ----
const bads = [
  ['bare handle, no + or @',      { source: 'imessage', identifier: 'alice',          display: 'A', body: 'x' }],
  ['phone with leading zero',     { source: 'imessage', identifier: '+0155555',       display: 'A', body: 'x' }],
  ['phone too short',             { source: 'imessage', identifier: '+1',             display: 'A', body: 'x' }],
  ['phone too long (>15 digits)', { source: 'imessage', identifier: '+1234567890123456', display: 'A', body: 'x' }],
  ['phone with letters',          { source: 'imessage', identifier: '+1415ABC0123',   display: 'A', body: 'x' }],
  ['email missing domain dot',    { source: 'imessage', identifier: 'a@b',            display: 'A', body: 'x' }],
  ['email with a space',          { source: 'imessage', identifier: 'a b@c.com',      display: 'A', body: 'x' }],
  ['email missing @',            { source: 'imessage', identifier: 'ab.com',          display: 'A', body: 'x' }],
  ['empty identifier',            { source: 'imessage', identifier: '',               display: 'A', body: 'x' }],
  ['non-string identifier',       { source: 'imessage', identifier: null,             display: 'A', body: 'x' }],
];
for (const [label, arg] of bads) {
  ok(buildIdentifierProposalContent(arg) === null, 'rejects bad identifier: ' + label);
}

// ---- empty / whitespace / non-string body -> null even with a valid identifier ----
ok(buildIdentifierProposalContent(
  { source: 'imessage', identifier: '+14155550123', display: 'A', body: '' }) === null,
  'rejects empty body');
ok(buildIdentifierProposalContent(
  { source: 'imessage', identifier: '+14155550123', display: 'A', body: '   ' }) === null,
  'rejects whitespace-only body');
ok(buildIdentifierProposalContent(
  { source: 'imessage', identifier: '+14155550123', display: 'A', body: null }) === null,
  'rejects non-string body');

// ---- no-arg / empty-arg safety ----
ok(buildIdentifierProposalContent() === null, 'no args returns null');
ok(buildIdentifierProposalContent({}) === null, 'empty object returns null');

if (fail) {
  console.error('proposal_identifier.test.js: ' + fail + ' FAILED, ' + pass + ' passed');
  for (const f of failures) console.error('  - ' + f);
  process.exitCode = 1;
} else {
  console.log('proposal_identifier.test.js: all ' + pass + ' checks passed');
}
