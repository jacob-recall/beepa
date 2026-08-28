// apps/master/invites.js — the master console's identity/trust predicates.
//
// This file is the SINGLE definition of "does the manager trust this room, and
// under whose label?", shared by BOTH gates in apps/master/main.js:
//   * the auto-join gate  (invitesToJoin, over stripped invite_state), and
//   * the render gate     (acceptedSpaces/verifiedChildIds + buildByUser's
//                          per-child creator check, over joined-room state).
// Enforcing the same predicate twice is deliberate: a mislabeled space is
// refused at join AND again at render, so neither gate alone is load-bearing.
//
// RULES FOR THIS FILE (docs/SHARE-LOGIC.md, "The fix"):
//   1. PURE LEAF — zero imports, no DOM, no network, no module-level side
//      effects, no state. It must stay importable by plain node so
//      tests/unit/master_invites.test.js can hold every trust decision still.
//      Never add an import here, and never add a send/join CALL here: this
//      file only ever *decides*; main.js performs.
//   2. NO FALLBACK VALUES. Every predicate returns null when it cannot prove
//      the claim. An earlier draft's `'unknown'` localpart sentinel was a
//      fail-open vulnerability (an unparseable mxid compared equal to a room
//      literally labeled "space:unknown").
//   3. RAW COMPARISON. Never sanitize/normalize inside a predicate — a lossy
//      transform makes distinct identities compare equal (e.g. a zero-width
//      char stripped out of "space:jk<ZWSP>ali"). Sanitizing is a DISPLAY
//      concern and belongs at the call site, after the decision.
//
// The identity source is the room's m.room.create event `sender`: it is
// server-stamped and is one of the few things Matrix's stripped invite state
// reliably carries (verified live: Synapse 1.159.0, room version 11).

// A Matrix user id whose localpart is in the Synapse-permitted charset. The
// domain part is deliberately unconstrained (`.+`) — the console only ever
// compares localparts, and this master is a closed, federation-off server.
const MXID_RE = /^@([a-z0-9._=\/+-]+):.+$/;

// Generic room-id shape (ANY server), for ids this console validates but does
// not own — e.g. a proposal's teammate-local target_room. Ids belonging to the
// master itself are additionally checked against the transport's server-pinned
// ROOMID_RE at the call site.
const ROOM_SHAPE_RE = /^![^:]+:[A-Za-z0-9.\-:]+$/;

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

/**
 * The teammate label a space proves it may use, or null.
 *
 * A space is trusted iff its name is EXACTLY "space:" + the localpart of the
 * account that created it — the identity bind that stops @bob:master from
 * presenting a space named "space:jkali" (which would otherwise inject rows
 * under jkali's label and hijack jkali's proposals-room mapping).
 * Comparison is raw and exact; no trimming, no sanitizing, no case folding.
 * @param {*} name          the m.room.name content.name, as received
 * @param {*} createSender  the m.room.create event's server-stamped sender
 * @returns {string|null}
 */
function spaceLabelFor(name, createSender) {
  const lp = localpart(createSender);
  if (lp === null) return null;
  if (typeof name !== 'string') return null;
  return name === 'space:' + lp ? lp : null;
}

// The stripped m.room.create / m.room.name state events of one invite, or
// nulls. Only state_key "" is accepted (the only valid key for either type).
function strippedCreateAndName(inviteEntry) {
  const events = (inviteEntry && inviteEntry.invite_state
    && Array.isArray(inviteEntry.invite_state.events)) ? inviteEntry.invite_state.events : [];
  let create = null;
  let name = null;
  for (const e of events) {
    if (!e || typeof e !== 'object' || e.state_key !== '') continue;
    if (e.type === 'm.room.create' && create === null) create = e;
    if (e.type === 'm.room.name' && name === null) {
      name = (e.content && typeof e.content === 'object') ? e.content.name : null;
    }
  }
  return { create, name };
}

/**
 * Which pending invites the console may auto-accept.
 *
 * @param {object} inviteSection  the RAW rooms.invite object of a /sync response
 * @param {Map<string,string>} verifiedChildIds  childRoomId -> teammate label,
 *        built from already-JOINED, identity-verified spaces (see below).
 * @param {{maxExamine?:number, maxJoins?:number}} [opts]  backpressure caps
 * @returns {string[]} room ids to join, deterministic (sorted) order
 *
 * An invite is joined iff, from ITS OWN stripped state:
 *   (a) it is a space and spaceLabelFor(name, create.sender) proves the label
 *       (this is how a teammate's space enters on the first pass), or
 *   (c) it is a known child of an identity-verified space AND its own create
 *       sender's localpart equals that space's label AND it is not itself a
 *       space (this joins the mirror rooms + the Proposals room on the pass
 *       after the space was joined and parsed).
 *
 * Rule (c) checks THIS INVITE'S OWN stripped m.room.create sender. It must
 * never be evaluated against parsed joined-room data: a pending invite has no
 * joined-room record at all, so such a check could only ever pass vacuously or
 * fail permanently. (Closing-review blocker.)
 *
 * There is deliberately NO rule that joins on com.jkali.mirror_of shape alone.
 * It was removed in security review as the design's only unauthenticated join
 * path — i.e. its DoS surface — and is redundant: the uplink space-links every
 * legitimate room, so rule (c) covers them within one refresh.
 */
function invitesToJoin(inviteSection, verifiedChildIds, opts) {
  const o = (opts && typeof opts === 'object') ? opts : {};
  const maxExamine = typeof o.maxExamine === 'number' ? o.maxExamine : 50;
  const maxJoins = typeof o.maxJoins === 'number' ? o.maxJoins : 20;
  const known = (verifiedChildIds instanceof Map) ? verifiedChildIds : new Map();
  const section = (inviteSection && typeof inviteSection === 'object') ? inviteSection : {};

  const out = [];
  const ids = Object.keys(section).sort();
  const limit = Math.min(ids.length, Math.max(0, maxExamine));
  for (let i = 0; i < limit; i++) {
    if (out.length >= maxJoins) break;
    const rid = ids[i];
    const { create, name } = strippedCreateAndName(section[rid]);
    if (!create) continue;                                  // no identity -> refuse
    const creator = localpart(create.sender);
    if (creator === null) continue;                         // unparseable id -> refuse
    const content = (create.content && typeof create.content === 'object') ? create.content : {};
    const isSpace = content.type === 'm.space';

    let join = false;
    if (isSpace) {
      join = spaceLabelFor(name, create.sender) !== null;   // rule (a)
    } else {
      join = known.has(rid) && known.get(rid) === creator;  // rule (c)
    }
    if (!join) continue;
    if (!ROOM_SHAPE_RE.test(rid)) continue;                 // malformed id -> never
    out.push(rid);
  }
  return out;
}

/**
 * The joined spaces whose label is proven by their own creator, sorted by room
 * id. An UNVERIFIED space is skipped outright — never given a fallback label
 * (that would let a mislabeled space still occupy the rail).
 *
 * @param {object} parsedRooms  roomId -> parsed room info; each entry needs
 *        {isSpace, name, createSender, children} (see main.js parseSnapshot)
 * @returns {{label:string, space:object}[]}
 */
function acceptedSpaces(parsedRooms) {
  const rooms = (parsedRooms && typeof parsedRooms === 'object') ? parsedRooms : {};
  const out = [];
  for (const id of Object.keys(rooms).sort()) {
    const space = rooms[id];
    if (!space || !space.isSpace) continue;
    const label = spaceLabelFor(space.name, space.createSender);
    if (label === null) continue;
    out.push({ label, space });
  }
  return out;
}

/**
 * childRoomId -> teammate label, over the children of verified spaces.
 *
 * Membership in this map means ONLY "an identity-verified space claims this
 * room as a child". It is deliberately NOT sufficient on its own: both the
 * invite gate (rule (c)) and the render gate re-check the child's OWN creator
 * against the label. Two spaces with the same verified label MERGE; the first
 * claim wins under acceptedSpaces' sorted iteration, so the result is
 * deterministic and a later space cannot re-point an existing child.
 *
 * @param {{label:string, space:object}[]} accepted  output of acceptedSpaces()
 * @returns {Map<string,string>}
 */
function verifiedChildIds(accepted) {
  const map = new Map();
  const list = Array.isArray(accepted) ? accepted : [];
  for (const item of list) {
    if (!item || typeof item.label !== 'string' || !item.space) continue;
    const children = Array.isArray(item.space.children) ? item.space.children : [];
    for (const cid of children) {
      if (typeof cid !== 'string' || !cid) continue;
      if (!map.has(cid)) map.set(cid, item.label);          // first claim wins
    }
  }
  return map;
}

export { localpart, spaceLabelFor, invitesToJoin, acceptedSpaces, verifiedChildIds, ROOM_SHAPE_RE };
