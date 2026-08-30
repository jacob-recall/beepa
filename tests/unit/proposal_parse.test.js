// Plain-node test for parseProposal — the pure, DOM-free discriminator extracted
// from apps/user/proposals.js. It is the teammate SEND leg's gate: a proposal
// event is classified as either an existing-room draft (kind:'room') or a
// person-targeted new-iMessage-chat draft (kind:'identifier'), and anything that
// is neither a valid target_room nor a valid target_identifier is DROPPED (null).
//
// Security-relevant invariants asserted here:
//  - an identifier draft whose handle fails the SC-7 regex (E.164 / strict email)
//    is refused at parse time (null) — the client re-validates again at send, and
//    the daemon is the authoritative gate;
//  - target_room never leaks into an identifier parse (no cross-shape confusion
//    that could redirect a start-chat into a room send).
//
// Run: docker run --rm -v "$(pwd)":/w -w /w node:20-alpine \
//        node tests/unit/proposal_parse.test.js

import { parseProposal } from '../../apps/user/proposals.js';

let pass = 0;
let fail = 0;
const failures = [];
function ok(cond, label) { if (cond) { pass++; } else { fail++; failures.push(label); } }

const mk = (content, event_id = '$e') => ({ type: 'com.jkali.proposal', event_id, content });

// ---- existing-room proposal -> kind:'room', unchanged fields ----
const room = parseProposal(mk({ target_room: '!abc:localhost', body: 'Hi there', origin_ts: 123 }, '$r'));
ok(room !== null, 'room proposal parses');
ok(room && room.kind === 'room', 'room proposal has kind:room');
ok(room && room.targetRoom === '!abc:localhost', 'room targetRoom preserved');
ok(room && room.body === 'Hi there', 'room body preserved');
ok(room && room.eventId === '$r', 'room eventId preserved');
ok(room && room.ts === 123, 'room ts from origin_ts');
ok(room && !('targetIdentifier' in room), 'room parse has NO targetIdentifier key');
ok(room && !('targetSource' in room), 'room parse has NO targetSource key');

// ---- template flag carried through ----
const tmpl = parseProposal(mk({ target_room: '!abc:localhost', body: 'x', template: true }, '$t'));
ok(tmpl && tmpl.template === true, 'template flag carried on room proposal');

// ---- identifier proposal (E.164) -> kind:'identifier', right fields, NO room ----
const idp = parseProposal(mk({
  target_source: 'imessage', target_identifier: '+14155550123',
  target_display: 'Alice', body: 'Hello', origin_ts: 456,
}, '$i'));
ok(idp !== null, 'identifier proposal parses');
ok(idp && idp.kind === 'identifier', 'identifier proposal has kind:identifier');
ok(idp && idp.targetSource === 'imessage', 'targetSource preserved');
ok(idp && idp.targetIdentifier === '+14155550123', 'targetIdentifier preserved');
ok(idp && idp.targetDisplay === 'Alice', 'targetDisplay preserved');
ok(idp && idp.body === 'Hello', 'identifier body preserved');
ok(idp && idp.ts === 456, 'identifier ts from origin_ts');
// THE LEAK TEST: target_room must never appear in an identifier parse.
ok(idp && !('targetRoom' in idp), 'identifier parse has NO targetRoom key');

// ---- identifier proposal (strict email) ----
const em = parseProposal(mk({
  target_source: 'imessage', target_identifier: 'alice@example.com',
  target_display: 'Alice', body: 'Hi',
}, '$e2'));
ok(em && em.kind === 'identifier', 'email identifier parses to kind:identifier');
ok(em && em.targetIdentifier === 'alice@example.com', 'email identifier preserved');
ok(em && !('targetRoom' in em), 'email identifier parse has NO targetRoom key');

// ---- display defaults to the identifier when absent ----
const noDisp = parseProposal(mk({
  target_source: 'imessage', target_identifier: '+14155550123', body: 'hi',
}, '$nd'));
ok(noDisp && noDisp.targetDisplay === '+14155550123', 'targetDisplay defaults to identifier');

// ---- identifier is trimmed before validation ----
const idTrim = parseProposal(mk({
  target_source: 'imessage', target_identifier: '  +14155550123  ', body: 'hi',
}, '$tr'));
ok(idTrim && idTrim.targetIdentifier === '+14155550123', 'identifier trimmed before use');

// ---- a bad handle drops the whole proposal (null) ----
const bads = [
  ['bare handle',            'notaphone'],
  ['phone leading zero',     '+0155555'],
  ['phone too short',        '+1'],
  ['phone too long',         '+1234567890123456'],
  ['phone with letters',     '+1415ABC0123'],
  ['email missing dot',      'a@b'],
  ['email with space',       'a b@c.com'],
  ['email missing @',        'ab.com'],
  ['empty identifier',       ''],
];
for (const [label, handle] of bads) {
  ok(parseProposal(mk({ target_source: 'imessage', target_identifier: handle, body: 'x' })) === null,
    'bad identifier dropped: ' + label);
}
ok(parseProposal(mk({ target_source: 'imessage', target_identifier: null, body: 'x' })) === null,
  'non-string identifier dropped');

// ---- neither a valid room nor a valid identifier -> null ----
ok(parseProposal(mk({ body: 'orphan draft' })) === null, 'no target dropped');
ok(parseProposal(mk({ target_room: '', body: 'x' })) === null, 'empty target_room dropped');
ok(parseProposal(mk({ target_room: '!r:localhost', body: '   ' })) === null, 'whitespace body dropped');
ok(parseProposal(mk({ target_room: '!r:localhost', body: 42 })) === null, 'non-string body dropped');

// ---- wrong / missing envelope -> null ----
ok(parseProposal(null) === null, 'null event dropped');
ok(parseProposal({ type: 'm.room.message', content: { target_room: '!r:localhost', body: 'x' } }) === null,
  'wrong event type dropped');
ok(parseProposal({ type: 'com.jkali.proposal' }) === null, 'missing content dropped');

if (fail) {
  console.error('proposal_parse.test.js: ' + fail + ' FAILED, ' + pass + ' passed');
  for (const f of failures) console.error('  - ' + f);
  process.exitCode = 1;
} else {
  console.log('proposal_parse.test.js: all ' + pass + ' checks passed');
}
