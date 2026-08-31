"""Pure consent resolver — Python port of shared/model/consent.js.

PLAN-MASTER-SYNC.md §4 + §12 phase 5. Layered, most-specific-wins:
  1. per-conversation override  : 'share' | 'private'          (absent = inherit)
  2. profile (contact profile)  : 'share' | 'private'          ('inherit' = fall through)
  3. per-source policy          : 'share-all' | 'private-all'  (absent = inherit)
  4. global standing policy     : 'share-all' | 'private'      (default 'private')
Safe default: PRIVATE. Nothing is shared unless a level explicitly says so.

The profile level lets a whole contact profile (one person's conversations
across sources) share or hide together, while a per-conversation override still
wins over it. resolve() takes only the resolved {displayName, share} for the
conversation's profile so it stays pure and byte-identical to consent.js.

This module is PURE: no I/O, no side effects at import. Its output must match
consent.js byte-for-byte on the same inputs — see tests/unit/consent_py.test.py,
which mirrors tests/unit/consent.test.js.
"""
import re

SHARE_POLICY_TYPE = "com.jkali.share_policy"      # global user account-data
SHARE_OVERRIDE_TYPE = "com.jkali.share_override"  # per-room account-data

GLOBAL_STATES = {"share-all", "private"}
SOURCE_STATES = {"share-all", "private-all", "inherit"}
OVERRIDE_STATES = {"share", "private"}
# Profile share-state (from a contact profile). 'inherit' means "no opinion —
# fall through to the per-source/global levels".
PROFILE_STATES = {"share", "private", "inherit"}

# ---------------------------------------------------------------------------
# INPUT CANONICALISATION — identical gates in shared/model/consent.js (see
# docs/superpowers/plans/2026-08-30-consent-conformance.md's table; the
# conformance harness tests/conformance/consent_conformance.py proves parity
# on ~84k vectors every run). Anything failing a gate is treated as ABSENT
# (the fall-through value) — note "absent" is safe only relative to the
# more-specific levels: the per-source level carries a deny (private-all),
# so a dropped malformed rule can fall through to a global share-all. That
# reconciles the UI up to what this enforcer already answered; the curated
# tests pin it. All regexes are matched with END-OF-STRING semantics
# (re.fullmatch here, ^…$ in JS — Python's `$` also matches before a
# trailing newline, JS's does not; never switch back to .match with `$`).
# ---------------------------------------------------------------------------

# A per-source policy key / contact source id. Shape-based (a new bridge id
# needs no change here); deliberately excludes __proto__-style names. Never
# tighten it to something a real source id could fail — dropping a key drops
# a private-all too.
_SOURCE_KEY_RE = re.compile(r"[a-z][a-z0-9]{0,31}")
# Room-id shape for overrides_from_sync output keys. Static and server-name
# agnostic ON PURPOSE — never reuse a server-bound or runtime-configured
# room-id regex here (JS mirrors this literal as CONSENT_ROOMID_RE).
_CONSENT_ROOMID_RE = re.compile(r"![^:]+:[A-Za-z0-9.\-:]+")


def _plain(x):
    """The dict itself, or None — the shared 'plain object' gate."""
    return x if isinstance(x, dict) else None


def _nonempty_str(x):
    return x if isinstance(x, str) and x else None


def _source_rule(sources, source_id):
    """'share-all' | 'private-all' | None for a per-source rule lookup.

    The consent gate for the per-source level: the container must be a dict,
    the key a valid source id (regex above), present, with an exactly-valid
    value — anything else is inherit."""
    if not isinstance(sources, dict):
        return None
    if not isinstance(source_id, str) or not _SOURCE_KEY_RE.fullmatch(source_id):
        return None
    v = sources.get(source_id)
    return v if v in ("share-all", "private-all") else None


def _source_label_of(convo):
    """A non-empty-string sourceLabel, else non-empty-string sourceId, else
    'source' — never throws, never coerces a non-string into a reason."""
    c = _plain(convo) or {}
    return _nonempty_str(c.get("sourceLabel")) or _nonempty_str(c.get("sourceId")) or "source"


def resolve(convo, policy, override, profile=None):
    """Resolve one conversation's effective shared-state AND the reason.

    profile is {"displayName", "share"} for the room's contact profile, or None.
    Returns {"shared": bool, "reason": str} where reason is one of
    'all <source>' | 'explicit' | 'excluded' | 'profile: <name>' | 'private'.
    Mirrors resolve() in consent.js exactly.
    """
    pol = _plain(policy) or {}
    sources = pol.get("sources")
    c = _plain(convo) or {}
    source_id = _nonempty_str(c.get("sourceId"))

    # 1. Per-conversation override wins over everything (most specific).
    #    Only the exact strings count; any other shape is inherit.
    if override == "share":
        return {"shared": True, "reason": "explicit"}
    if override == "private":
        return {"shared": False, "reason": "excluded"}

    # 2. Profile level: a shared/private contact profile shares or hides all its
    #    members together, but only 'share'/'private' take effect — 'inherit'
    #    (a non-object profile, or an absent one) falls through.
    prof = _plain(profile)
    if prof:
        pname = "profile: " + (_nonempty_str(prof.get("displayName")) or "profile")
        if prof.get("share") == "share":
            return {"shared": True, "reason": pname}
        if prof.get("share") == "private":
            return {"shared": False, "reason": pname}

    # 3. Per-source standing policy (gated: valid key, own entry, exact value).
    src = _source_rule(sources, source_id)
    if src == "share-all":
        return {"shared": True, "reason": "all " + _source_label_of(convo)}
    if src == "private-all":
        return {"shared": False, "reason": "private"}
    # (inherit / absent / malformed -> fall through to global)

    # 4. Global standing policy.
    if pol.get("global") == "share-all":
        return {"shared": True, "reason": "all " + _source_label_of(convo)}

    # 5. Safe default: private.
    return {"shared": False, "reason": "private"}


def effective_shared(convo, policy, override, profile=None):
    """The boolean the uplink asks for when deciding whether to mirror a room."""
    return resolve(convo, policy, override, profile)["shared"]


def resolve_all(convos, policy, overrides, profiles=None):
    """Batch resolve. overrides/profiles may be dicts keyed by room id, or None.

    profiles maps room id -> {"displayName", "share"}. Returns a list of
    {"convo", "shared", "reason"} in input order.
    """
    if not isinstance(convos, list):
        return []

    def get(room_id):
        # container must be a dict, the key a string (a junk convo id can be
        # unhashable; JS gates the same way and additionally accepts a Map)
        if not isinstance(overrides, dict) or not isinstance(room_id, str):
            return None
        return overrides.get(room_id)

    def get_profile(room_id):
        if not isinstance(profiles, dict) or not isinstance(room_id, str):
            return None
        return profiles.get(room_id)

    out = []
    for convo in convos:
        rid = convo.get("id") if isinstance(convo, dict) else None
        r = resolve(convo, policy, get(rid), get_profile(rid))
        out.append({"convo": convo, "shared": r["shared"], "reason": r["reason"]})
    return out


def normalize_policy(p):
    """Coerce stored/incoming policy into { 'global', 'sources' }.

    Unknown global -> 'private'. Only 'share-all'/'private-all' source states
    survive; 'inherit' and junk are dropped. Mirrors normalizePolicy() in JS.
    """
    src = p.get("sources") if isinstance(p, dict) else None
    if not isinstance(src, dict):
        src = {}
    g = p.get("global") if isinstance(p, dict) else None
    global_ = "share-all" if g == "share-all" else "private"  # == never throws; Set-membership on an unhashable dict would
    sources = {}
    for k, v in src.items():
        # key must be a valid source id (drops __proto__-style and junk keys —
        # same gate as _source_rule, so normalize+resolve agree with JS)
        if not isinstance(k, str) or not _SOURCE_KEY_RE.fullmatch(k):
            continue
        if v == "share-all" or v == "private-all":
            sources[k] = v
    return {"global": global_, "sources": sources}


def normalize_override(data):
    """A per-room override -> 'share' | 'private' | None (None == inherit).

    Accepts the object form {'state': 'share'} or a bare string. Mirrors
    normalizeOverride() in JS.
    """
    if not data:
        return None
    v = data if isinstance(data, str) else (data.get("state") if isinstance(data, dict) else None)
    return v if v in OVERRIDE_STATES else None


# ===========================================================================
# CONTACT-SHARE — a SEPARATE consent dimension from conversation sharing above.
# Decides whether a teammate's address-book contacts (per source) leave their
# machine for the manager. Own policy, own account-data key, own default:
# PRIVATE (absent policy => not shared). MUST stay byte-parity with
# shared/model/consent.js's resolveContactShare/normalizeContactPolicy.
# ===========================================================================

CONTACT_SHARE_POLICY_TYPE = "com.jkali.contact_share_policy"  # global user account-data
CONTACT_GLOBAL_STATES = {"share-all", "private"}
CONTACT_SOURCE_STATES = {"share-all", "private-all", "inherit"}


def normalize_contact_policy(raw):
    """Coerce a contact-share policy into { 'global', 'sources' }.

    Unknown global -> 'private' (safe default). Only 'share-all'/'private-all'
    source states survive; 'inherit' (source omitted == inherit) and junk are
    dropped. Mirrors normalizeContactPolicy() in consent.js.
    """
    src = raw.get("sources") if isinstance(raw, dict) else None
    if not isinstance(src, dict):
        src = {}
    g = raw.get("global") if isinstance(raw, dict) else None
    global_ = "share-all" if g == "share-all" else "private"  # == never throws; Set-membership on an unhashable dict would
    sources = {}
    for k, v in src.items():
        if not isinstance(k, str) or not _SOURCE_KEY_RE.fullmatch(k):
            continue  # same key gate as the conversation dimension
        if v == "share-all" or v == "private-all":
            sources[k] = v
    return {"global": global_, "sources": sources}


def resolve_contact_share(source, policy):
    """Resolve whether a source's contacts are shared AND the reason.

    Precedence (most-specific-wins), mirroring resolveContactShare() in JS:
      1. per-source 'share-all'   -> shared,     'all <source> contacts'
      2. per-source 'private-all' -> not shared, 'private'
      3. global 'share-all'       -> shared,     'all contacts'
      4. safe default             -> not shared, 'private'
    """
    pol = _plain(policy) or {}

    # same gated lookup as the conversation dimension: valid source id, own
    # entry, exact value — anything else is inherit
    src = _source_rule(pol.get("sources"), source)
    if src == "share-all":
        return {"shared": True, "reason": "all " + source + " contacts"}
    if src == "private-all":
        return {"shared": False, "reason": "private"}
    # (inherit / absent / malformed -> fall through to global)

    if pol.get("global") == "share-all":
        return {"shared": True, "reason": "all contacts"}

    return {"shared": False, "reason": "private"}


def overrides_from_sync(sync_data):
    """Extract per-room overrides from a /sync response's room account-data.

    Reads only com.jkali.share_override. Returns { room_id: 'share'|'private' }
    (rooms set to inherit omitted). Mirrors overridesFromSync() in JS.
    """
    out = {}
    rooms = sync_data.get("rooms") if isinstance(sync_data, dict) else None
    join = rooms.get("join") if isinstance(rooms, dict) else None
    if not isinstance(join, dict):
        return out
    for rid, room in join.items():
        # output keys are gated by the STATIC room-id shape (parity with JS's
        # CONSENT_ROOMID_RE; a junk/"__proto__" key never enters the map)
        if not isinstance(rid, str) or not _CONSENT_ROOMID_RE.fullmatch(rid):
            continue
        ad = room.get("account_data") if isinstance(room, dict) else None
        events = ad.get("events") if isinstance(ad, dict) else None
        if not isinstance(events, list):
            continue
        for e in events:
            if isinstance(e, dict) and e.get("type") == SHARE_OVERRIDE_TYPE:
                v = normalize_override(e.get("content"))
                if v:
                    out[rid] = v
                elif rid in out:
                    del out[rid]
    return out
