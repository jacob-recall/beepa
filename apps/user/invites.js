// apps/user/invites.js — the teammate app's bridge-invite trust predicate.
//
// New conversations from the six message bridges arrive on the LOCAL homeserver
// as room INVITES (only Google Messages' double-puppeting auto-joins). This file
// is the SINGLE definition of "may this app accept this invite?", used by
// apps/user/main.js's joinBridgeInvites(). It decides; main.js performs.
//
// RULES FOR THIS FILE (same contract as apps/master/invites.js):
//   1. PURE LEAF — zero imports, no DOM, no network, no module-level side
//      effects, no state. It must stay importable by plain node so
//      tests/unit/user_invites.test.js can hold every trust decision still.
//      Never add an import here, and never add a join/send CALL here.
//   2. NO FALLBACK VALUES. A predicate returns null / refuses when it cannot
//      prove the claim (an 'unknown' localpart sentinel was a fail-open hole in
//      the master console's earlier draft — do not reintroduce one here).
//   3. RAW COMPARISON. Never sanitize/normalize inside a predicate — a lossy
//      transform makes distinct identities compare equal. Sanitizing is a
//      DISPLAY concern and belongs at the call site, after the decision.
//
// THE DECISION, in one line: an invite is accepted iff its server-stamped
// m.room.create `sender` and the single sender of the invite's own
// m.room.member/<self> event are the SAME account, and that account is one of
// the six code-owned bridge bots (SOURCES[].botMxid). Two independent fields
// must agree, so a bridge GHOST (@gmessages_abc:localhost), the user, or any
// other local account cannot get a room auto-joined by inviting the user to a
// room some bot created, or by creating a room and having someone else invite.
//
// A SPACE invite (create.content.type === 'm.space') carries a third bind: its
// name must start with the space name of the SAME source whose bot created it,
// so one bridge's bot cannot present a space that the app would then read as
// another bridge's source space (shared/ui/account-data.js's buildConvos picks
// the source space purely by name prefix).
//
// Both DMs and groups are accepted — there is deliberately no `is_direct`
// filter. `is_direct` is not carried in stripped invite state anyway, and the
// hub is meant to show every bridged conversation.

// A Matrix user id whose localpart is in the Synapse-permitted charset. The
// domain part is deliberately unconstrained (`.+`); identity here is decided by
// exact-string membership in the bot allowlist, not by parsing.
const MXID_RE = /^@([a-z0-9._=\/+-]+):.+$/;

// Room ids on THIS app's homeserver only (server_name "localhost"), matching
// shared/matrix/client.js's ROOMID_RE. Federation is off, so an invite to a
// foreign-server room is out of shape by construction.
const ROOM_SHAPE_RE = /^![A-Za-z0-9._=/+-]+:localhost$/;

/**
 * The localpart of a well-formed mxid, or null. No sentinel, ever.
 * @param {*} mxid
 * @returns {string|null}
 */
function localpart(mxid) {
  if (typeof mxid !== 'string') return null;
  const m = MXID_RE.exec(mxid);
  return m ? m[1] : null;
}

// The stripped m.room.create event (state_key ""), the m.room.name content.name,
// and the DISTINCT senders of every m.room.member invite event addressed to
// `selfMxid`, from one rooms.invite entry. Missing/!object entries yield nulls
// and an empty sender list — i.e. a refusal upstream, never a pass.
function strippedFacts(inviteEntry, selfMxid) {
  const events = (inviteEntry && inviteEntry.invite_state
    && Array.isArray(inviteEntry.invite_state.events)) ? inviteEntry.invite_state.events : [];
  let create = null;
  let name = null;
  const memberSenders = new Set();
  for (const e of events) {
    if (!e || typeof e !== 'object') continue;
    if (e.type === 'm.room.member') {
      // The invite addressed to US, whoever stamped it. state_key is the
      // invitee; sender is the inviter. Both must be right.
      const content = (e.content && typeof e.content === 'object') ? e.content : {};
      if (content.membership === 'invite' && e.state_key === selfMxid
          && typeof e.sender === 'string') {
        memberSenders.add(e.sender);
      }
      continue;
    }
    if (e.state_key !== '') continue;
    if (e.type === 'm.room.create' && create === null) create = e;
    if (e.type === 'm.room.name' && name === null) {
      name = (e.content && typeof e.content === 'object') ? e.content.name : null;
    }
  }
  return { create, name, memberSenders: [...memberSenders] };
}

/**
 * Which pending bridge invites this app may auto-accept.
 *
 * @param {object} inviteSection  the RAW rooms.invite object of a /sync response,
 *        ALREADY pre-filtered by the caller to fresh candidates (not joined, not
 *        memoized-failed) so the maxExamine budget is never starved by rooms
 *        that are already handled.
 * @param {Set<string>|string[]} botMxids  the six code-owned bridge bot mxids
 *        (SOURCES[].botMxid). Never a value read off the wire.
 * @param {string} selfMxid  this session's own user id (S.userId).
 * @param {{spaceName:string, botMxid:string}[]} sourceSpaces  used ONLY for the
 *        space-invite name/creator bind below.
 * @param {{maxExamine?:number, maxJoins?:number}} [opts]  backpressure caps
 * @returns {{join:string[], refusedNonBridge:number, overCap:number}}
 *          `join` is deterministic (sorted by room id) and every id matches
 *          ROOM_SHAPE_RE; `refusedNonBridge` counts examined candidates refused
 *          on identity grounds; `overCap` counts eligible rooms left for a
 *          later pass because of maxJoins.
 */
function bridgeInvitesToJoin(inviteSection, botMxids, selfMxid, sourceSpaces, opts) {
  const empty = { join: [], refusedNonBridge: 0, overCap: 0 };
  // Normalize the allowlist to a Set of plain strings. SOURCES[0] ('all') has
  // no botMxid, so non-strings are dropped rather than trusted.
  const bots = new Set();
  const rawBots = (botMxids instanceof Set) ? [...botMxids] : (Array.isArray(botMxids) ? botMxids : []);
  for (const b of rawBots) if (typeof b === 'string' && b) bots.add(b);
  if (bots.size === 0) return empty;                       // no allowlist -> join nothing
  if (typeof selfMxid !== 'string' || !selfMxid) return empty;

  const spaces = Array.isArray(sourceSpaces) ? sourceSpaces : [];
  const o = (opts && typeof opts === 'object') ? opts : {};
  const maxExamine = typeof o.maxExamine === 'number' ? o.maxExamine : 100;
  const maxJoins = typeof o.maxJoins === 'number' ? o.maxJoins : 30;
  const section = (inviteSection && typeof inviteSection === 'object') ? inviteSection : {};

  // Only well-shaped ids are candidates at all; a malformed id is never joined
  // and never consumes the examine budget.
  const ids = Object.keys(section).filter(id => ROOM_SHAPE_RE.test(id)).sort();
  const limit = Math.min(ids.length, Math.max(0, maxExamine));

  const join = [];
  let refusedNonBridge = 0;
  let overCap = 0;
  for (let i = 0; i < limit; i++) {
    const rid = ids[i];
    const { create, name, memberSenders } = strippedFacts(section[rid], selfMxid);
    if (!create) { refusedNonBridge++; continue; }          // no identity -> refuse
    const creator = create.sender;
    if (localpart(creator) === null) { refusedNonBridge++; continue; }  // unparseable -> refuse
    // Two-field agreement: EXACTLY ONE inviter, and it is the room's creator,
    // and that account is a known bridge bot. Any disagreement or multiplicity
    // (e.g. a bot invite plus a ghost invite) fails closed.
    if (memberSenders.length !== 1 || memberSenders[0] !== creator || !bots.has(creator)) {
      refusedNonBridge++;
      continue;
    }
    const content = (create.content && typeof create.content === 'object') ? create.content : {};
    if (content.type === 'm.space') {
      // Space bind: the name must belong to the SAME source whose bot created
      // the space. An unnamed space, or one named for another bridge, is
      // refused — buildConvos() selects a source's space by name prefix alone.
      const match = (typeof name === 'string')
        ? spaces.find(s => s && typeof s.spaceName === 'string' && s.spaceName
            && name.startsWith(s.spaceName))
        : undefined;
      if (!match || match.botMxid !== creator) { refusedNonBridge++; continue; }
    }
    if (join.length >= maxJoins) { overCap++; continue; }   // eligible, deferred (not a refusal)
    join.push(rid);
  }
  return { join, refusedNonBridge, overCap };
}

export { localpart, bridgeInvitesToJoin, ROOM_SHAPE_RE };
