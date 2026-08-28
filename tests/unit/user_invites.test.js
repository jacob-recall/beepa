// Plain-node test for apps/user/invites.js — no framework.
// Run: node tests/unit/user_invites.test.js  (or via docker, see tests/run.sh)
// Exits 0 on all-pass, nonzero (via process.exitCode) on any failure.
//
// The three REAL_* fixtures below are captured VERBATIM from this hub's live
// /sync rooms.invite (Synapse 1.159.0, room version 11, the same filter
// shared/ui/account-data.js's fetchSnapshot uses); only display data
// (contact/account names) is renamed. Everything structural — event order,
// state_keys, the com.beeper.* content keys, the absence of any custom type —
// is exactly what the bridges send. Every case here is a trust decision: read a
// failure as "the hub would auto-join a room it must not", not as cosmetic.

import { localpart, bridgeInvitesToJoin, ROOM_SHAPE_RE } from '../../apps/user/invites.js';

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

// ---- the live identities (shared/ui/sources.js SOURCES + this hub's user) ---
const SELF = '@jkali:localhost';
const GMSG_BOT = '@gmessagesbot:localhost';
const WA_BOT = '@whatsappbot:localhost';
const IMSG_BOT = '@imessagebot:localhost';
const BOTS = [GMSG_BOT, WA_BOT, IMSG_BOT, '@instagrambot:localhost',
  '@linkedinbot:localhost', '@twitterbot:localhost'];
const SPACES = [
  { spaceName: 'WhatsApp', botMxid: WA_BOT },
  { spaceName: 'iMessage', botMxid: IMSG_BOT },
  { spaceName: 'Google Messages', botMxid: GMSG_BOT },
  { spaceName: 'Instagram', botMxid: '@instagrambot:localhost' },
  { spaceName: 'LinkedIn', botMxid: '@linkedinbot:localhost' },
  { spaceName: 'Twitter', botMxid: '@twitterbot:localhost' },
];
const go = (section, bots = BOTS, self = SELF, spaces = SPACES, opts = {}) =>
  bridgeInvitesToJoin(section, bots, self, spaces, opts);

// ===========================================================================
// REAL captured invite payloads (display names renamed, structure untouched).
// ===========================================================================

// A Google Messages DM portal invite — the shape 26 of this hub's 27 pending
// invites have. Note: NO m.room.name, NO is_direct, NO custom state type.
const REAL_GMSG_DM = { invite_state: { events: [
  { content: { room_version: '11' }, sender: GMSG_BOT, state_key: '', type: 'm.room.create' },
  { content: { join_rule: 'invite' }, sender: GMSG_BOT, state_key: '', type: 'm.room.join_rules' },
  { content: { 'com.beeper.exclude_from_timeline': true, topic: '' }, sender: GMSG_BOT,
    state_key: '', type: 'm.room.topic' },
  { content: { avatar_url: 'mxc://maunium.net/yGOdcrJcwqARZqdzbfuxfhzb',
    displayname: 'Google Messages bridge bot', membership: 'join' },
    sender: GMSG_BOT, state_key: GMSG_BOT, type: 'm.room.member' },
  { content: { displayname: 'jkali', membership: 'invite' },
    event_id: '$-5Pwx1CljnF703g0Mertv06wOxXBjfMkqpQQaSgc5jA',
    origin_server_ts: 1787780898034, sender: GMSG_BOT, state_key: SELF,
    type: 'm.room.member', unsigned: { age: 160754299 } },
] } };

// The Google Messages SOURCE SPACE invite (the only m.space among the 27).
const REAL_GMSG_SPACE = { invite_state: { events: [
  { content: { room_version: '11', type: 'm.space' }, sender: GMSG_BOT, state_key: '', type: 'm.room.create' },
  { content: { join_rule: 'invite' }, sender: GMSG_BOT, state_key: '', type: 'm.room.join_rules' },
  { content: { url: 'mxc://maunium.net/yGOdcrJcwqARZqdzbfuxfhzb' }, sender: GMSG_BOT,
    state_key: '', type: 'm.room.avatar' },
  { content: { name: 'Google Messages (user@example.com)' }, sender: GMSG_BOT,
    state_key: '', type: 'm.room.name' },
  { content: { topic: 'Your Google Messages bridged chats - user@example.com' },
    sender: GMSG_BOT, state_key: '', type: 'm.room.topic' },
  { content: { avatar_url: 'mxc://maunium.net/yGOdcrJcwqARZqdzbfuxfhzb',
    displayname: 'Google Messages bridge bot', membership: 'join' },
    sender: GMSG_BOT, state_key: GMSG_BOT, type: 'm.room.member' },
  { content: { displayname: 'jkali', membership: 'invite' },
    event_id: '$wRWc44oAA8sr4N6V4Vx4CnZGG9jqeaXvTk8s8CVi82E',
    origin_server_ts: 1787780899154, sender: GMSG_BOT, state_key: SELF,
    type: 'm.room.member', unsigned: { age: 160753179 } },
] } };

// A WhatsApp DM portal invite (the 27th) — a NAMED, non-space room.
const REAL_WA_DM = { invite_state: { events: [
  { content: { room_version: '11' }, sender: WA_BOT, state_key: '', type: 'm.room.create' },
  { content: { join_rule: 'invite' }, sender: WA_BOT, state_key: '', type: 'm.room.join_rules' },
  { content: { 'com.beeper.exclude_from_timeline': true, name: 'Contact One' },
    sender: WA_BOT, state_key: '', type: 'm.room.name' },
  { content: { 'com.beeper.exclude_from_timeline': true, topic: 'WhatsApp private chat' },
    sender: WA_BOT, state_key: '', type: 'm.room.topic' },
  { content: { avatar_url: 'mxc://maunium.net/NeXNQarUbrlYBiPCpprYsRqr',
    displayname: 'WhatsApp bridge bot', membership: 'join' },
    sender: WA_BOT, state_key: WA_BOT, type: 'm.room.member' },
  { content: { 'com.beeper.exclude_from_timeline': true, displayname: 'jkali', membership: 'invite' },
    event_id: '$Gw2T1Ftm8Q2mvL6cMLLx11RPVeAIJxDJlFzdtiz27jQ',
    origin_server_ts: 1787782241971, sender: WA_BOT, state_key: SELF,
    type: 'm.room.member', unsigned: { age: 159410362 } },
] } };

const GMSG_DM_ID = '!AzbbJwkTFwmNBWwNWB:localhost';
const GMSG_SPACE_ID = '!twUmELsqxnTPpCQpSR:localhost';
const WA_DM_ID = '!uLuKqUWBiAFhDUNwgR:localhost';

// Rebuild one of the real payloads with a different create/invite sender, so a
// spoof fixture is structurally identical to the genuine one in every other way.
function reshape(fixture, { createSender, inviteSender, name, spaceType } = {}) {
  const events = JSON.parse(JSON.stringify(fixture.invite_state.events));
  for (const e of events) {
    if (e.type === 'm.room.create' && createSender !== undefined) {
      if (createSender === null) delete e.sender; else e.sender = createSender;
      if (spaceType === false) delete e.content.type;
      if (spaceType === true) e.content.type = 'm.space';
    }
    if (e.type === 'm.room.member' && e.state_key === SELF && inviteSender !== undefined) {
      e.sender = inviteSender;
    }
    if (e.type === 'm.room.name' && name !== undefined) e.content.name = name;
  }
  return { invite_state: { events } };
}
function dropType(fixture, type) {
  return { invite_state: { events: fixture.invite_state.events.filter(e => e.type !== type) } };
}

// ---------------------------------------------------------------------------
// 1. The real bot invite: create.sender === invite sender === a known bot bot.
// ---------------------------------------------------------------------------
{
  eq(go({ [GMSG_DM_ID]: REAL_GMSG_DM }),
    { join: [GMSG_DM_ID], refusedNonBridge: 0, overCap: 0 }, 'real gmessages DM invite is joined');
  eq(go({ [WA_DM_ID]: REAL_WA_DM }),
    { join: [WA_DM_ID], refusedNonBridge: 0, overCap: 0 }, 'real WhatsApp DM invite is joined');
  // Both DMs AND groups: there is no is_direct filter, and the real payload
  // carries no is_direct at all — a group portal has the identical shape.
  eq(go({ [GMSG_DM_ID]: REAL_GMSG_DM, [WA_DM_ID]: REAL_WA_DM }).join.sort(),
    [GMSG_DM_ID, WA_DM_ID].sort(), 'DMs and groups alike, in sorted order');
}

// ---------------------------------------------------------------------------
// 2. A bridge GHOST (not the bot) as both create and invite sender -> refused.
//    Ghosts are the accounts a remote contact controls the content of; they
//    must never be able to get a room auto-joined.
// ---------------------------------------------------------------------------
{
  const ghost = '@gmessages_abc:localhost';
  eq(go({ [GMSG_DM_ID]: reshape(REAL_GMSG_DM, { createSender: ghost, inviteSender: ghost }) }),
    { join: [], refusedNonBridge: 1, overCap: 0 }, 'ghost-created invite is refused');
}

// ---------------------------------------------------------------------------
// 3. The user themself as sender -> refused (self-invite is not a bridge).
// ---------------------------------------------------------------------------
{
  eq(go({ [GMSG_DM_ID]: reshape(REAL_GMSG_DM, { createSender: SELF, inviteSender: SELF }) }),
    { join: [], refusedNonBridge: 1, overCap: 0 }, 'self-created invite is refused');
}

// ---------------------------------------------------------------------------
// 4. An unknown local account -> refused.
// ---------------------------------------------------------------------------
{
  const evil = '@evil:localhost';
  eq(go({ [GMSG_DM_ID]: reshape(REAL_GMSG_DM, { createSender: evil, inviteSender: evil }) }),
    { join: [], refusedNonBridge: 1, overCap: 0 }, 'unknown-sender invite is refused');
}

// ---------------------------------------------------------------------------
// 5. No m.room.create event at all -> refused (no identity to bind to).
// ---------------------------------------------------------------------------
{
  eq(go({ [GMSG_DM_ID]: dropType(REAL_GMSG_DM, 'm.room.create') }),
    { join: [], refusedNonBridge: 1, overCap: 0 }, 'invite without m.room.create is refused');
  eq(go({ [GMSG_DM_ID]: reshape(REAL_GMSG_DM, { createSender: null }) }),
    { join: [], refusedNonBridge: 1, overCap: 0 }, 'create without a sender is refused');
  eq(go({ [GMSG_DM_ID]: {} }), { join: [], refusedNonBridge: 1, overCap: 0 },
    'invite with no invite_state at all is refused');
}

// ---------------------------------------------------------------------------
// 6. The invite is addressed to someone ELSE (state_key !== self) -> refused.
//    Without the state_key check, any room whose stripped state happens to
//    carry SOME bot-stamped invite would be joinable.
// ---------------------------------------------------------------------------
{
  const other = { invite_state: { events: REAL_GMSG_DM.invite_state.events.map(e =>
    (e.type === 'm.room.member' && e.state_key === SELF)
      ? { ...e, state_key: '@someoneelse:localhost' } : e) } };
  eq(go({ [GMSG_DM_ID]: other }), { join: [], refusedNonBridge: 1, overCap: 0 },
    'invite addressed to another user is refused');
  // ...and the same payload with no member events at all.
  eq(go({ [GMSG_DM_ID]: dropType(REAL_GMSG_DM, 'm.room.member') }),
    { join: [], refusedNonBridge: 1, overCap: 0 }, 'no invite member event -> refused');
}

// ---------------------------------------------------------------------------
// 7. TWO invite member events for us with DIFFERENT senders -> refused.
//    Multiplicity is ambiguity: fail closed rather than pick the convenient one.
// ---------------------------------------------------------------------------
{
  const doubled = { invite_state: { events: REAL_GMSG_DM.invite_state.events.concat([
    { content: { membership: 'invite' }, sender: '@evil:localhost', state_key: SELF,
      type: 'm.room.member' },
  ]) } };
  eq(go({ [GMSG_DM_ID]: doubled }), { join: [], refusedNonBridge: 1, overCap: 0 },
    'two different invite senders -> refused (multiplicity)');
  // A duplicate of the SAME bot-stamped invite is one distinct sender: still fine.
  const dupSame = { invite_state: { events: REAL_GMSG_DM.invite_state.events.concat([
    { content: { membership: 'invite' }, sender: GMSG_BOT, state_key: SELF, type: 'm.room.member' },
  ]) } };
  eq(go({ [GMSG_DM_ID]: dupSame }).join, [GMSG_DM_ID], 'duplicate identical invite sender is fine');
}

// ---------------------------------------------------------------------------
// 8. Cross-bridge laundering: created by one bot, invited by ANOTHER bot. Both
//    senders are allowlisted, so a one-field check would pass this.
// ---------------------------------------------------------------------------
{
  eq(go({ [GMSG_DM_ID]: reshape(REAL_GMSG_DM, { createSender: WA_BOT, inviteSender: GMSG_BOT }) }),
    { join: [], refusedNonBridge: 1, overCap: 0 }, 'create bot != invite bot -> refused');
  eq(go({ [GMSG_DM_ID]: reshape(REAL_GMSG_DM, { createSender: GMSG_BOT, inviteSender: WA_BOT }) }),
    { join: [], refusedNonBridge: 1, overCap: 0 }, 'invite bot != create bot -> refused');
}

// ---------------------------------------------------------------------------
// 9. SPACE invites carry a third bind: the name must belong to the same source
//    as the creating bot (buildConvos picks a source's space by name prefix
//    alone, so a mislabeled space would redirect a whole source tab).
// ---------------------------------------------------------------------------
{
  eq(go({ [GMSG_SPACE_ID]: REAL_GMSG_SPACE }),
    { join: [GMSG_SPACE_ID], refusedNonBridge: 0, overCap: 0 },
    'real Google Messages space invite is joined');
  // A WhatsApp-named space really created by the WhatsApp bot: joined.
  eq(go({ [GMSG_SPACE_ID]: reshape(REAL_GMSG_SPACE,
    { createSender: WA_BOT, inviteSender: WA_BOT, name: 'WhatsApp (+15551234567)' }) }).join,
    [GMSG_SPACE_ID], 'WhatsApp-named space from the WhatsApp bot is joined');
  // The SAME name, created+invited by the Google Messages bot: refused.
  eq(go({ [GMSG_SPACE_ID]: reshape(REAL_GMSG_SPACE, { name: 'WhatsApp (+15551234567)' }) }),
    { join: [], refusedNonBridge: 1, overCap: 0 },
    'WhatsApp-named space from the wrong bot is refused');
  // An unnamed space cannot prove which source it is: refused.
  eq(go({ [GMSG_SPACE_ID]: dropType(REAL_GMSG_SPACE, 'm.room.name') }),
    { join: [], refusedNonBridge: 1, overCap: 0 }, 'unnamed space is refused');
  // A space whose name merely CONTAINS a source name (not a prefix): refused.
  eq(go({ [GMSG_SPACE_ID]: reshape(REAL_GMSG_SPACE, { name: 'Fake Google Messages' }) }),
    { join: [], refusedNonBridge: 1, overCap: 0 }, 'space name must be a prefix match');
  // Non-space rooms skip the bind entirely: a DM named "WhatsApp ..." created
  // by the Google Messages bot is still a normal portal and is joined.
  eq(go({ [WA_DM_ID]: reshape(REAL_WA_DM,
    { createSender: GMSG_BOT, inviteSender: GMSG_BOT, name: 'WhatsApp (+15551234567)' }) }).join,
    [WA_DM_ID], 'non-space invites skip the space-name bind');
  // No sourceSpaces supplied -> every space is refused (fail closed).
  eq(go({ [GMSG_SPACE_ID]: REAL_GMSG_SPACE }, BOTS, SELF, []),
    { join: [], refusedNonBridge: 1, overCap: 0 }, 'no sourceSpaces -> spaces refused');
}

// ---------------------------------------------------------------------------
// 10. Backpressure caps + determinism.
// ---------------------------------------------------------------------------
{
  const mk = (n) => {
    const out = {};
    for (let i = 0; i < n; i++) out['!r' + String(i).padStart(3, '0') + ':localhost'] = REAL_GMSG_DM;
    return out;
  };
  const forty = go(mk(40));
  eq([forty.join.length, forty.overCap, forty.refusedNonBridge], [30, 10, 0],
    '40 eligible -> 30 joined, 10 over cap, 0 refused');
  eq(forty.join, Object.keys(mk(40)).sort().slice(0, 30), 'deterministic: sorted-by-room-id order');

  // 120 candidates, only the LAST 20 (sorted) eligible -> none is even seen.
  const many = mk(120);
  for (const id of Object.keys(many).slice(0, 100)) {
    many[id] = reshape(REAL_GMSG_DM, { createSender: '@evil:localhost', inviteSender: '@evil:localhost' });
  }
  const big = go(many);
  eq([big.join.length, big.refusedNonBridge, big.overCap], [0, 100, 0],
    'maxExamine: at most 100 candidates examined');
  eq(go(mk(40), BOTS, SELF, SPACES, { maxJoins: 3 }).join.length, 3, 'maxJoins override');
  eq(go(mk(40), BOTS, SELF, SPACES, { maxExamine: 2 }).join.length, 2, 'maxExamine override');
  eq(go(mk(40), BOTS, SELF, SPACES, { maxExamine: 0 }),
    { join: [], refusedNonBridge: 0, overCap: 0 }, 'maxExamine 0 examines nothing');
}

// ---------------------------------------------------------------------------
// 11. Malformed ids; botMxids as Array or Set; empty/absent inputs.
// ---------------------------------------------------------------------------
{
  const bad = {
    'AzbbJwkTFwmNBWwNWB:localhost': REAL_GMSG_DM,   // no leading '!'
    '!noserver': REAL_GMSG_DM,                      // no ':server'
    '!x:example.org': REAL_GMSG_DM,                 // foreign server (federation is off)
    '!y:local host': REAL_GMSG_DM,                  // space in the server part
    '!z:localhost:8008': REAL_GMSG_DM,              // port is not part of this server_name
  };
  eq(go(bad), { join: [], refusedNonBridge: 0, overCap: 0 },
    'malformed / foreign room ids are never candidates');
  eq(go({ ...bad, [GMSG_DM_ID]: REAL_GMSG_DM }).join, [GMSG_DM_ID],
    'a good id among malformed ones still joins');

  eq(go({ [GMSG_DM_ID]: REAL_GMSG_DM }, new Set(BOTS)).join, [GMSG_DM_ID], 'botMxids as a Set');
  eq(go({ [GMSG_DM_ID]: REAL_GMSG_DM }, [GMSG_BOT]).join, [GMSG_DM_ID], 'botMxids as a 1-element Array');
  eq(go({ [GMSG_DM_ID]: REAL_GMSG_DM }, [WA_BOT]), { join: [], refusedNonBridge: 1, overCap: 0 },
    'a bot outside the passed allowlist is refused');
  // SOURCES[0] ('all') has no botMxid: non-strings are dropped, not trusted.
  eq(go({ [GMSG_DM_ID]: REAL_GMSG_DM }, [undefined, null, 42, {}, GMSG_BOT]).join, [GMSG_DM_ID],
    'non-string entries in botMxids are ignored');
  const EMPTY = { join: [], refusedNonBridge: 0, overCap: 0 };
  eq(go({ [GMSG_DM_ID]: REAL_GMSG_DM }, []), EMPTY, 'empty botMxids -> nothing');
  eq(go({ [GMSG_DM_ID]: REAL_GMSG_DM }, [undefined, null, 42]), EMPTY, 'all-invalid botMxids -> nothing');
  eq(go({ [GMSG_DM_ID]: REAL_GMSG_DM }, BOTS, ''), EMPTY, 'empty selfMxid -> nothing');
  eq(bridgeInvitesToJoin({ [GMSG_DM_ID]: REAL_GMSG_DM }, BOTS, undefined, SPACES, {}), EMPTY,
    'missing selfMxid -> nothing');
  eq(go({ [GMSG_DM_ID]: REAL_GMSG_DM }, BOTS, 42), EMPTY, 'non-string selfMxid -> nothing');
  eq(go({ [GMSG_DM_ID]: REAL_GMSG_DM }, BOTS, '@someoneelse:localhost'),
    { join: [], refusedNonBridge: 1, overCap: 0 }, 'a different self mxid matches no invite');
  eq(bridgeInvitesToJoin(undefined, undefined, undefined, undefined, undefined), EMPTY,
    'tolerates missing arguments');
  eq(go(undefined), EMPTY, 'tolerates a missing invite section');
  eq(go({}), EMPTY, 'no invites -> nothing');
  eq(go({ [GMSG_DM_ID]: REAL_GMSG_DM }, BOTS, SELF, undefined).join, [GMSG_DM_ID],
    'missing sourceSpaces still admits non-space invites');

  eq(ROOM_SHAPE_RE.test('!ok:localhost'), true, 'ROOM_SHAPE_RE accepts a local id');
  eq(ROOM_SHAPE_RE.test('!ok:master'), false, 'ROOM_SHAPE_RE rejects a foreign server');
  eq(ROOM_SHAPE_RE.test('ok:localhost'), false, 'ROOM_SHAPE_RE rejects a missing !');
}

// ---------------------------------------------------------------------------
// 12. localpart(), exhaustively. No sentinel value, ever.
// ---------------------------------------------------------------------------
{
  eq(localpart('@gmessagesbot:localhost'), 'gmessagesbot', 'localpart: plain');
  eq(localpart('@bob.smith_1=/+-:localhost:8008'), 'bob.smith_1=/+-',
    'localpart: full permitted charset, server with a port');
  eq(localpart('@bob!smith:localhost'), null, 'localpart: charset is closed (no "!")');
  eq(localpart('@bob smith:localhost'), null, 'localpart: charset is closed (no space)');
  eq(localpart('bob:localhost'), null, 'localpart: missing @');
  eq(localpart('@bob'), null, 'localpart: missing server');
  eq(localpart('@bob:'), null, 'localpart: empty server');
  eq(localpart('@:localhost'), null, 'localpart: empty localpart');
  eq(localpart('@Bob:localhost'), null, 'localpart: uppercase is not a valid localpart');
  eq(localpart(''), null, 'localpart: empty string');
  eq(localpart(null), null, 'localpart: null');
  eq(localpart(undefined), null, 'localpart: undefined');
  eq(localpart(42), null, 'localpart: non-string');
  eq(localpart({ toString: () => '@bob:localhost' }), null, 'localpart: object is not coerced');
  // A create sender whose id is unparseable is refused even if it were somehow
  // in the allowlist — the shape check runs first.
  eq(go({ [GMSG_DM_ID]: reshape(REAL_GMSG_DM, { createSender: 'notanmxid', inviteSender: 'notanmxid' }) },
    ['notanmxid']), { join: [], refusedNonBridge: 1, overCap: 0 },
    'unparseable sender is refused even if allowlisted');
}

// ---------------------------------------------------------------------------
console.log(`\n${pass} passed, ${fail} failed`);
if (fail > 0) {
  console.error('\nFailures:');
  for (const f of failures) console.error('  - ' + f);
  process.exitCode = 1;
}
