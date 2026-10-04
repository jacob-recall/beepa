"""agents/uplink/invites.py — the DAEMON's bridge-invite trust predicate.

A byte-for-byte port of apps/user/invites.js. The browser app accepts bridge
invites while it is open; this module lets the uplink do the same while it is
closed, under the SAME decision. Two implementations of one authorization-ish
predicate drift by construction, so:

  * tests/conformance/invites_conformance.py runs the same vectors through BOTH
    real modules and fails on any differing output or any crash. Change one
    file and you change the other IN THE SAME COMMIT, then rerun that harness.
  * The JS file states the rules; they are repeated here only where the Python
    spelling of a JS idiom is non-obvious. Read apps/user/invites.js first.

RULES FOR THIS FILE (same contract as apps/user/invites.js):
  1. PURE LEAF — imports `re` and nothing else. No I/O, no logging, no `self`,
     no module-level state. It must stay importable with the repo absent from
     sys.path beyond this directory.
  2. NO FALLBACK VALUES / NO SENTINEL RETURNS. localpart() returns the
     localpart or None; a predicate refuses when it cannot prove the claim.
  3. RAW COMPARISON. Never sanitize or normalize inside a predicate — a lossy
     transform makes distinct identities compare equal.
  4. re.fullmatch ONLY, never re.match with a trailing `$`: Python's `$` also
     matches before a final newline, JS's does not, and that difference is a
     silently admitted "@bot:localhost\\n".

THE DECISION, in one line: an invite is accepted iff its server-stamped
m.room.create `sender` and the single sender of the invite's own
m.room.member/<self> event are the SAME account, and that account is one of the
code-owned bridge bots. A space invite (create.content.type == 'm.space')
carries a third bind: its name must start with the spaceName of the SAME source
whose bot created it, or be exactly one of that source's childSpaceNames
(Discord's "Direct Messages" is a real m.space and is NOT prefixed with
"Discord" — verified on the live hub).
"""
import re

# A Matrix user id whose localpart is in the Synapse-permitted charset. The
# domain part is deliberately unconstrained; identity is decided by exact-string
# membership in the bot allowlist, not by parsing.
MXID_RE = re.compile(r"@([a-z0-9._=/+-]+):.+")

# Room ids on the teammate's OWN homeserver only (server_name "localhost"),
# matching apps/user/invites.js's ROOM_SHAPE_RE and uplink.ROOMID_RE's intent.
ROOM_SHAPE_RE = re.compile(r"![A-Za-z0-9._=/+-]+:localhost")

# Distinguishes JS `undefined` from JSON `null` for the ONE place the JS
# behaviour depends on it: the first m.room.name wins only when its assignment
# produced a non-null value (`name === null` is the JS re-assignment guard), so
# a first name event whose content.name is missing blocks later ones while one
# whose content.name is null does not. Never returned, never compared outside
# this module.
_UNDEFINED = object()


def localpart(mxid):
    """The localpart of a well-formed mxid, or None. No sentinel, ever."""
    if not isinstance(mxid, str):
        return None
    m = MXID_RE.fullmatch(mxid)
    return m.group(1) if m else None


def _stripped_facts(invite_entry, self_mxid):
    """The stripped m.room.create event (state_key ""), the m.room.name
    content.name, and the DISTINCT senders of every m.room.member invite event
    addressed to `self_mxid`, from one rooms.invite entry. Missing/non-object
    entries yield nulls and an empty sender list — a refusal upstream."""
    events = []
    if isinstance(invite_entry, dict):
        state = invite_entry.get("invite_state")
        if isinstance(state, dict) and isinstance(state.get("events"), list):
            events = state["events"]
    create = None
    name = None
    member_senders = []            # insertion-ordered, de-duplicated (JS Set)
    for e in events:
        if not isinstance(e, dict):
            continue
        if e.get("type") == "m.room.member":
            # The invite addressed to US, whoever stamped it. state_key is the
            # invitee; sender is the inviter. Both must be right.
            content = e.get("content")
            content = content if isinstance(content, dict) else {}
            if (content.get("membership") == "invite"
                    and e.get("state_key") == self_mxid
                    and isinstance(e.get("sender"), str)):
                if e["sender"] not in member_senders:
                    member_senders.append(e["sender"])
            continue
        if e.get("state_key") != "":
            continue
        if e.get("type") == "m.room.create" and create is None:
            create = e
        if e.get("type") == "m.room.name" and name is None:
            content = e.get("content")
            name = (content.get("name", _UNDEFINED) if isinstance(content, dict)
                    else None)
    return create, name, member_senders


def _space_match(spaces, name, creator):
    """JS `spaces.find(...)` for the space name/creator bind: the FIRST source
    whose spaceName prefixes `name` (or whose childSpaceNames contains it
    exactly), and whose botMxid is the room's creator."""
    for s in spaces:
        if not isinstance(s, dict):
            continue
        space_name = s.get("spaceName")
        if not isinstance(space_name, str) or not space_name:
            continue
        children = s.get("childSpaceNames")
        if name.startswith(space_name) or (isinstance(children, list) and name in children):
            return s.get("botMxid") == creator
    return False


def _as_number(v, default):
    """JS `typeof v === 'number' ? v : default`. Python bools are ints; JS
    booleans are not numbers, so they take the default."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return default
    return v


def bridge_invites_to_join(invite_section, bot_mxids, self_mxid, source_spaces, opts=None):
    """Which pending bridge invites the daemon may auto-accept.

    invite_section  the RAW rooms.invite object of a /sync response, ALREADY
                    pre-filtered by the caller to fresh candidates so the
                    max_examine budget is never starved by handled rooms.
    bot_mxids       the code-owned bridge bot mxids (SOURCES[].botMxid). Never
                    a value read off the wire.
    self_mxid       this daemon's own user id (cfg.local_user).
    source_spaces   [{spaceName, botMxid, childSpaceNames}] — used ONLY for the
                    space-invite name/creator bind.
    opts            {"maxExamine": n, "maxJoins": n} backpressure caps.

    Returns {"join": [...], "refusedNonBridge": n, "overCap": n}; `join` is
    deterministic (sorted by room id — ids are ASCII by ROOM_SHAPE_RE, so
    Python's code-point sort equals JS's UTF-16 sort) and every id matches
    ROOM_SHAPE_RE.
    """
    empty = {"join": [], "refusedNonBridge": 0, "overCap": 0}
    # Normalize the allowlist to a set of plain strings. SOURCES[0] ('all') has
    # no botMxid, so non-strings are dropped rather than trusted.
    bots = set()
    if isinstance(bot_mxids, (set, frozenset, list, tuple)):
        for b in bot_mxids:
            if isinstance(b, str) and b:
                bots.add(b)
    if not bots:
        return empty                                   # no allowlist -> join nothing
    if not isinstance(self_mxid, str) or not self_mxid:
        return empty

    spaces = source_spaces if isinstance(source_spaces, list) else []
    o = opts if isinstance(opts, dict) else {}
    max_examine = _as_number(o.get("maxExamine"), 100)
    max_joins = _as_number(o.get("maxJoins"), 30)
    section = invite_section if isinstance(invite_section, dict) else {}

    # Only well-shaped ids are candidates at all; a malformed id is never joined
    # and never consumes the examine budget.
    ids = sorted(rid for rid in section
                 if isinstance(rid, str) and ROOM_SHAPE_RE.fullmatch(rid))
    # JS: Math.min(ids.length, Math.max(0, maxExamine)) with a `i < limit` loop,
    # so a fractional cap examines ceil(limit) entries. Kept float-exact.
    limit = min(len(ids), max(0, max_examine))

    join = []
    refused_non_bridge = 0
    over_cap = 0
    i = 0
    while i < limit:
        rid = ids[i]
        i += 1
        create, name, member_senders = _stripped_facts(section[rid], self_mxid)
        if create is None:
            refused_non_bridge += 1                    # no identity -> refuse
            continue
        creator = create.get("sender")
        if localpart(creator) is None:
            refused_non_bridge += 1                    # unparseable -> refuse
            continue
        # Two-field agreement: EXACTLY ONE inviter, it is the room's creator,
        # and that account is a known bridge bot. Any disagreement or
        # multiplicity (a bot invite plus a ghost invite) fails closed.
        if (len(member_senders) != 1 or member_senders[0] != creator
                or creator not in bots):
            refused_non_bridge += 1
            continue
        content = create.get("content")
        content = content if isinstance(content, dict) else {}
        if content.get("type") == "m.space":
            # Space bind: the name must belong to the SAME source whose bot
            # created the space. An unnamed space, or one named for another
            # bridge, is refused — source attribution is by name alone.
            if not (isinstance(name, str) and _space_match(spaces, name, creator)):
                refused_non_bridge += 1
                continue
        if len(join) >= max_joins:
            over_cap += 1                              # eligible, deferred
            continue
        join.append(rid)
    return {"join": join, "refusedNonBridge": refused_non_bridge, "overCap": over_cap}
