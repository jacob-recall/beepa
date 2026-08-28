// Plain-node test for apps/master/invites.js — no framework.
// Run: node tests/unit/master_invites.test.js  (or via docker, see tests/run.sh)
// Exits 0 on all-pass, nonzero (via process.exitCode) on any failure.
//
// The fixtures below are modeled on the REAL stripped invite_state this Synapse
// (1.159.0, room version 11) sends: m.room.create with full content and a
// server-stamped sender, m.room.name, m.room.join_rules, m.room.member — and
// NOTHING custom. Every case is a trust decision, so read a failure here as
// "the manager console would join/render something it must not", not as a
// cosmetic regression.

import {
  localpart, spaceLabelFor, invitesToJoin, acceptedSpaces, verifiedChildIds, ROOM_SHAPE_RE,
} from '../../apps/master/invites.js';

let pass = 0;
let fail = 0;
const failures = [];

function eq(actual, expected, label) {
  const a = JSON.stringify(actual);
  const e = JSON.stringify(expected);
  if (a === e) {
    pass++;
  } else {
    fail++;
    failures.push(`${label}: expected ${e}, got ${a}`);
  }
}

// ---- stripped-state fixture builders (shapes copied from the live payload) --
const JK = '@jkali:master';
const BOB = '@bob:master';

function createEv(sender, content) {
  const e = { type: 'm.room.create', state_key: '', content };
  if (sender !== undefined) e.sender = sender;
  return e;
}
function nameEv(sender, name) {
  return { type: 'm.room.name', state_key: '', sender, content: { name } };
}
function boilerplate(sender) {
  return [
    { type: 'm.room.join_rules', state_key: '', sender, content: { join_rule: 'invite' } },
    { type: 'm.room.member', state_key: sender, sender, content: { membership: 'join' } },
    { type: 'm.room.member', state_key: '@manager:master', sender, content: { membership: 'invite' } },
  ];
}
function inv(events) { return { invite_state: { events } }; }

// space:  create{type:'m.space', room_version:'11'} + name "space:<label>"
function spaceInvite(sender, name) {
  return inv([createEv(sender, { room_version: '11', type: 'm.space' }),
    nameEv(sender, name), ...boilerplate(sender || JK)]);
}
// mirror: create{'com.jkali.mirror_of':'!x:localhost', room_version:'11'} + name
function mirrorInvite(sender, name, mirrorOf) {
  return inv([createEv(sender, { 'com.jkali.mirror_of': mirrorOf, room_version: '11' }),
    nameEv(sender, name), ...boilerplate(sender || JK)]);
}
// proposals: BARE create{room_version:'11'} + name "Proposals" (no marker at all
// — the defect this module fixes was gating on a marker that never arrives).
function proposalsInvite(sender) {
  return inv([createEv(sender, { room_version: '11' }),
    nameEv(sender, 'Proposals'), ...boilerplate(sender || JK)]);
}

// The live defect: 1 space + 6 mirrors + 1 Proposals, all invited by @jkali.
const SPACE_ID = '!CaGopMtXAJnSpace:master';
const MIRROR_IDS = [
  '!GYNsCaVEWXYmirror1:master', '!HJgwCEumKnkmirror2:master', '!RUrMZZuxXTmmirror3:master',
  '!RnwSFlxSZQwmirror4:master', '!UtNQQvUmFJymirror5:master', '!mapfhoPdFbamirror6:master',
];
const PROPOSALS_ID = '!wEWxKBziQYvProposals:master';

function eightInvites() {
  const out = {};
  out[SPACE_ID] = spaceInvite(JK, 'space:jkali');
  MIRROR_IDS.forEach((id, i) => {
    out[id] = mirrorInvite(JK, 'Conversation ' + i, '!local' + i + ':localhost');
  });
  out[PROPOSALS_ID] = proposalsInvite(JK);
  return out;
}
const CHILD_IDS = MIRROR_IDS.concat([PROPOSALS_ID]).slice().sort();

// ---------------------------------------------------------------------------
// 1. Pass 1 — nothing verified yet: ONLY the identity-proven space is joined.
//    The mirrors and the Proposals room are not joinable until their parent
//    space has been joined and parsed (that is the whole two-pass design).
// ---------------------------------------------------------------------------
{
  eq(invitesToJoin(eightInvites(), new Map(), {}), [SPACE_ID], 'pass1: only the space');
}

// ---------------------------------------------------------------------------
// 2. Pass 2 — the space is joined (so it is gone from rooms.invite) and its
//    children are known. All 7 children join. CLOSING-REVIEW FIX: the creator
//    check runs against each invite's OWN stripped m.room.create sender, so it
//    works even though none of these 7 rooms exists in any parsed joined-room
//    map yet (a pending invite has no joined-room record at all).
// ---------------------------------------------------------------------------
{
  const invites = eightInvites();
  delete invites[SPACE_ID];                       // joined on pass 1
  const vch = new Map(CHILD_IDS.map(id => [id, 'jkali']));
  eq(invitesToJoin(invites, vch, {}), CHILD_IDS, 'pass2: all 6 mirrors + proposals');
}

// ---------------------------------------------------------------------------
// 3. Cross-teammate space spoof: @bob names a space "space:jkali". Refused —
//    without this bind, bob could inject rows under jkali's label AND hijack
//    jkali's proposals-room mapping (the manager's suggestions would land in a
//    bob-controlled room).
// ---------------------------------------------------------------------------
{
  const invites = { '!spoof:master': spaceInvite(BOB, 'space:jkali') };
  eq(invitesToJoin(invites, new Map(), {}), [], 'space named for another teammate is refused');
  // ...and bob's OWN correctly-labeled space is still fine.
  eq(invitesToJoin({ '!bobspace:master': spaceInvite(BOB, 'space:bob') }, new Map(), {}),
    ['!bobspace:master'], "bob's own space:bob is accepted");
}

// ---------------------------------------------------------------------------
// 4. Child capture: a room created by @bob is space-linked under jkali's
//    verified space. In the map, but its own creator disagrees -> refused.
// ---------------------------------------------------------------------------
{
  const cid = '!bobchild:master';
  const invites = { [cid]: mirrorInvite(BOB, 'Bob thread', '!lb:localhost') };
  eq(invitesToJoin(invites, new Map([[cid, 'jkali']]), {}), [],
    'child whose creator is another teammate is refused');
  eq(invitesToJoin(invites, new Map([[cid, 'bob']]), {}), [cid],
    'same child under its own creator label is accepted');
}

// ---------------------------------------------------------------------------
// 5. No identity at all: create event without a sender, name "space:unknown".
//    Must refuse — localpart() returns null and there is NO sentinel value for
//    it to match (the earlier 'unknown' fallback was a fail-open hole).
// ---------------------------------------------------------------------------
{
  const invites = { '!nosender:master': spaceInvite(undefined, 'space:unknown') };
  eq(invitesToJoin(invites, new Map(), {}), [], 'create without a sender is refused');
  eq(localpart(undefined), null, 'localpart(undefined) is null, not a sentinel');
  eq(spaceLabelFor('space:unknown', undefined), null, 'no sentinel label for a missing sender');
  // ...and an invite with no m.room.create event at all.
  const bare = { '!nocreate:master': inv([nameEv(JK, 'space:jkali'), ...boilerplate(JK)]) };
  eq(invitesToJoin(bare, new Map(), {}), [], 'invite without m.room.create is refused');
}

// ---------------------------------------------------------------------------
// 6. Lossy-compare defense: a zero-width space inside the label. A predicate
//    that sanitized before comparing would treat this as "space:jkali".
// ---------------------------------------------------------------------------
{
  const sneaky = 'space:jk​ali';
  eq(spaceLabelFor(sneaky, JK), null, 'zero-width char in the name breaks the match (raw compare)');
  eq(invitesToJoin({ '!zw:master': spaceInvite(JK, sneaky) }, new Map(), {}), [],
    'zero-width-name space is refused');
}

// ---------------------------------------------------------------------------
// 7. A SPACE reachable through rule (c): being a known child does not let a
//    space in through the child rule — a space must prove its own label.
// ---------------------------------------------------------------------------
{
  const cid = '!childspace:master';
  const invites = { [cid]: spaceInvite(JK, 'Proposals') };   // space-typed, unproven name
  eq(invitesToJoin(invites, new Map([[cid, 'jkali']]), {}), [],
    'space-typed child is refused by rule (c)');
}

// ---------------------------------------------------------------------------
// 8. acceptedSpaces / verifiedChildIds over PARSED (joined) rooms.
// ---------------------------------------------------------------------------
{
  const mkSpace = (id, name, createSender, children) =>
    ({ id, name, createSender, isSpace: true, children });
  const rooms = {
    '!a:master': mkSpace('!a:master', 'space:jkali', BOB, ['!x:master']),      // mislabeled
    '!b:master': mkSpace('!b:master', 'space:jkali', JK, ['!x:master', '!y:master']),
    '!c:master': mkSpace('!c:master', 'space:jkali', JK, ['!y:master', '!z:master']),
    '!d:master': { id: '!d:master', name: 'not a space', createSender: JK, isSpace: false, children: [] },
  };
  const accepted = acceptedSpaces(rooms);
  eq(accepted.map(a => [a.space.id, a.label]), [['!b:master', 'jkali'], ['!c:master', 'jkali']],
    'acceptedSpaces: mislabeled space skipped, no fallback label');
  const vch = verifiedChildIds(accepted);
  eq([...vch.entries()].sort(),
    [['!x:master', 'jkali'], ['!y:master', 'jkali'], ['!z:master', 'jkali']],
    'verifiedChildIds: same-label spaces merge (union), no overwrite');
  // The mislabeled space contributes nothing at all, not even its child.
  eq(verifiedChildIds(acceptedSpaces({ '!a:master': rooms['!a:master'] })).size, 0,
    'verifiedChildIds: unverified space contributes no children');
  eq(acceptedSpaces(undefined), [], 'acceptedSpaces: tolerates a missing map');
  eq([...verifiedChildIds(undefined).entries()], [], 'verifiedChildIds: tolerates a missing list');
}

// ---------------------------------------------------------------------------
// 9. Backpressure caps + determinism.
// ---------------------------------------------------------------------------
{
  // 60 invites, ONLY the last 10 (sorted) eligible -> nothing is returned,
  // proving no more than maxExamine (50) invites were looked at.
  const late = {};
  for (let i = 0; i < 60; i++) {
    const id = '!r' + String(i).padStart(2, '0') + ':master';
    late[id] = i >= 50 ? spaceInvite(JK, 'space:jkali') : spaceInvite(JK, 'unlabeled room');
  }
  eq(invitesToJoin(late, new Map(), {}), [], 'maxExamine: eligible invites past #50 are not seen');

  // 60 eligible invites -> exactly maxJoins (20), the 20 smallest ids, in order.
  const many = {};
  const ids = [];
  for (let i = 0; i < 60; i++) {
    const id = '!r' + String(i).padStart(2, '0') + ':master';
    ids.push(id);
    many[id] = spaceInvite('@u' + String(i).padStart(2, '0') + ':master',
      'space:u' + String(i).padStart(2, '0'));
  }
  const got = invitesToJoin(many, new Map(), {});
  eq(got.length, 20, 'maxJoins: at most 20 joins per pass');
  eq(got, ids.slice().sort().slice(0, 20), 'deterministic: sorted-by-room-id order');
  // Explicit caps are honored.
  eq(invitesToJoin(many, new Map(), { maxJoins: 3 }).length, 3, 'maxJoins override');
  eq(invitesToJoin(many, new Map(), { maxExamine: 2 }).length, 2, 'maxExamine override');
}

// ---------------------------------------------------------------------------
// 10. Malformed room ids are never returned, however well-formed the state is.
// ---------------------------------------------------------------------------
{
  const bad = {
    'CaGopMtXAJn:master': spaceInvite(JK, 'space:jkali'),   // no leading '!'
    '!noserver': spaceInvite(JK, 'space:jkali'),            // no ':server'
    '!x:mas ter': spaceInvite(JK, 'space:jkali'),           // space in the server part
    '!y:mas/ter': spaceInvite(JK, 'space:jkali'),           // '/' is not a server char
  };
  eq(invitesToJoin(bad, new Map(), {}), [], 'malformed room ids are never joined');
  const badChild = { '!z:mas ter': mirrorInvite(JK, 'c', '!l:localhost') };
  eq(invitesToJoin(badChild, new Map([['!z:mas ter', 'jkali']]), {}), [],
    'malformed child id is never joined even when known');
  eq(ROOM_SHAPE_RE.test('!ok:master'), true, 'ROOM_SHAPE_RE accepts a plain id');
  eq(ROOM_SHAPE_RE.test('!ok:localhost'), true, 'ROOM_SHAPE_RE accepts a foreign-server id');
  eq(ROOM_SHAPE_RE.test('ok:master'), false, 'ROOM_SHAPE_RE rejects a missing !');
  eq(invitesToJoin(undefined, undefined, undefined), [], 'invitesToJoin tolerates missing args');
}

// ---------------------------------------------------------------------------
// 11. The two primitives, exhaustively.
// ---------------------------------------------------------------------------
{
  eq(localpart('@bob:master'), 'bob', 'localpart: plain');
  eq(localpart('@bob.smith_1=/+-:master.example.org:8448'), 'bob.smith_1=/+-',
    'localpart: full permitted charset, server with a port');
  eq(localpart('@bob!smith:master'), null, 'localpart: charset is closed (no "!")');
  eq(localpart('@bob smith:master'), null, 'localpart: charset is closed (no space)');
  eq(localpart('bob:master'), null, 'localpart: missing @');
  eq(localpart('@bob'), null, 'localpart: missing server');
  eq(localpart('@bob:'), null, 'localpart: empty server');
  eq(localpart('@:master'), null, 'localpart: empty localpart');
  eq(localpart('@Bob:master'), null, 'localpart: uppercase is not a valid localpart');
  eq(localpart(''), null, 'localpart: empty string');
  eq(localpart(null), null, 'localpart: null');
  eq(localpart(42), null, 'localpart: non-string');
  eq(localpart({ toString: () => '@bob:master' }), null, 'localpart: object is not coerced');

  eq(spaceLabelFor('space:jkali', '@jkali:master'), 'jkali', 'spaceLabelFor: match');
  eq(spaceLabelFor('space:jkali', '@bob:master'), null, 'spaceLabelFor: wrong sender');
  eq(spaceLabelFor('space:jkali', 'jkali'), null, 'spaceLabelFor: malformed sender');
  eq(spaceLabelFor('space:jkali', undefined), null, 'spaceLabelFor: missing sender');
  eq(spaceLabelFor('space:jkali ', '@jkali:master'), null, 'spaceLabelFor: trailing space is not trimmed');
  eq(spaceLabelFor(' space:jkali', '@jkali:master'), null, 'spaceLabelFor: leading space is not trimmed');
  eq(spaceLabelFor('space:JKALI', '@jkali:master'), null, 'spaceLabelFor: no case folding');
  eq(spaceLabelFor('jkali', '@jkali:master'), null, 'spaceLabelFor: missing prefix');
  eq(spaceLabelFor('space:jkali:extra', '@jkali:master'), null, 'spaceLabelFor: suffix');
  eq(spaceLabelFor(undefined, '@jkali:master'), null, 'spaceLabelFor: missing name');
  eq(spaceLabelFor(null, '@jkali:master'), null, 'spaceLabelFor: null name');
  eq(spaceLabelFor(123, '@jkali:master'), null, 'spaceLabelFor: non-string name');
}

// ---------------------------------------------------------------------------
console.log(`\n${pass} passed, ${fail} failed`);
if (fail > 0) {
  console.error('\nFailures:');
  for (const f of failures) console.error('  - ' + f);
  process.exitCode = 1;
}
