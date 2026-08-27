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

SHARE_POLICY_TYPE = "com.jkali.share_policy"      # global user account-data
SHARE_OVERRIDE_TYPE = "com.jkali.share_override"  # per-room account-data

GLOBAL_STATES = {"share-all", "private"}
SOURCE_STATES = {"share-all", "private-all", "inherit"}
OVERRIDE_STATES = {"share", "private"}
# Profile share-state (from a contact profile). 'inherit' means "no opinion —
# fall through to the per-source/global levels".
PROFILE_STATES = {"share", "private", "inherit"}


def _source_label_of(convo):
    """convo.sourceLabel || convo.sourceId || 'source' — never throws."""
    if not isinstance(convo, dict):
        return "source"
    return convo.get("sourceLabel") or convo.get("sourceId") or "source"


def resolve(convo, policy, override, profile=None):
    """Resolve one conversation's effective shared-state AND the reason.

    profile is {"displayName", "share"} for the room's contact profile, or None.
    Returns {"shared": bool, "reason": str} where reason is one of
    'all <source>' | 'explicit' | 'excluded' | 'profile: <name>' | 'private'.
    Mirrors resolve() in consent.js exactly.
    """
    pol = policy if isinstance(policy, dict) else {}
    raw_sources = pol.get("sources")
    sources = raw_sources if isinstance(raw_sources, dict) else {}
    source_id = convo.get("sourceId") if isinstance(convo, dict) else None

    # 1. Per-conversation override wins over everything (most specific).
    if override == "share":
        return {"shared": True, "reason": "explicit"}
    if override == "private":
        return {"shared": False, "reason": "excluded"}

    # 2. Profile level: a shared/private contact profile shares or hides all its
    #    members together, but only 'share'/'private' take effect — 'inherit'
    #    (or an absent profile) falls through to the source/global levels.
    if profile:
        pname = "profile: " + ((profile.get("displayName") if isinstance(profile, dict) else None) or "profile")
        if profile.get("share") == "share":
            return {"shared": True, "reason": pname}
        if profile.get("share") == "private":
            return {"shared": False, "reason": pname}

    # 3. Per-source standing policy.
    src = sources.get(source_id) if source_id else None
    if src == "share-all":
        return {"shared": True, "reason": "all " + _source_label_of(convo)}
    if src == "private-all":
        return {"shared": False, "reason": "private"}
    # (src == 'inherit' or absent -> fall through to global)

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
        if not overrides or room_id is None:
            return None
        return overrides.get(room_id)

    def get_profile(room_id):
        if not profiles or room_id is None:
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
    global_ = "share-all" if (g in GLOBAL_STATES and g == "share-all") else "private"
    sources = {}
    for k, v in src.items():
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


def overrides_from_sync(sync_data):
    """Extract per-room overrides from a /sync response's room account-data.

    Reads only com.jkali.share_override. Returns { room_id: 'share'|'private' }
    (rooms set to inherit omitted). Mirrors overridesFromSync() in JS.
    """
    out = {}
    rooms = sync_data.get("rooms") if isinstance(sync_data, dict) else None
    join = (rooms or {}).get("join") if isinstance(rooms, dict) else None
    for rid, room in (join or {}).items():
        ad = room.get("account_data") if isinstance(room, dict) else None
        for e in ((ad or {}).get("events") or []):
            if isinstance(e, dict) and e.get("type") == SHARE_OVERRIDE_TYPE:
                v = normalize_override(e.get("content"))
                if v:
                    out[rid] = v
                elif rid in out:
                    del out[rid]
    return out
