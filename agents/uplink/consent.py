"""Pure consent resolver — Python port of shared/model/consent.js.

PLAN-MASTER-SYNC.md §4, as amended by the direct-share-level plan (D1).
CONVERSATION SHARING IS EXPLICIT-ONLY: a conversation carries exactly ONE
per-conversation level and nothing else can share it:
  'share'   -> mirrored; manager suggestions wait in the proposal inbox
  'direct'  -> mirrored; the uplink auto-sends a manager proposal into the
               conversation (the auto-send code itself lands in a later slice)
  'private' -> not mirrored (the default)
ABSENT OR ANY UNRECOGNIZED VALUE RESOLVES 'private' — a stated invariant with
its own conformance vector class. A stored override this code does not
recognize must never be able to share a conversation.

There is NO inheritance on the conversation path any more: contact-profile
share-state, the per-source policy and the global standing policy do NOT affect
whether a conversation mirrors. resolve() still ACCEPTS those arguments (call
sites + conformance vectors) and deliberately ignores them. The layered,
most-specific-wins model survives only in the SEPARATE contact-sharing
dimension at the bottom of this file, which keeps its standing policies on
purpose — and, as of the per-contact-share plan, gains a per-CONTACT override
that is more specific than both.

The one-time migration that materializes previously-inherited shares into
explicit 'share' overrides lives in uplink.py (D0) and keeps its own copy of
the old inherit-semantics resolver for that single purpose — deliberately NOT
here, so this file has exactly one model.

This module is PURE: no I/O, no side effects at import. Its output must match
consent.js byte-for-byte on the same inputs — see tests/unit/consent_py.test.py,
which mirrors tests/unit/consent.test.js, and the conformance harness.
"""
import re

SHARE_POLICY_TYPE = "com.jkali.share_policy"      # global user account-data
SHARE_OVERRIDE_TYPE = "com.jkali.share_override"  # per-room account-data
# Model-version marker (D0/F7): written to LOCAL user account-data by the uplink
# once the explicit-levels migration has completed, so the teammate UI knows the
# daemon no longer honors standing policies.
CONSENT_MODEL_TYPE = "com.jkali.consent_model"
CONSENT_MODEL_EXPLICIT = 2

GLOBAL_STATES = {"share-all", "private"}
SOURCE_STATES = {"share-all", "private-all", "inherit"}
# The THREE explicit conversation levels. Anything else (including the old
# 'inherit', an absent event, or junk) is 'private' — see effective_level().
OVERRIDE_STATES = {"share", "direct", "private"}
# Profile share-state. Retained for the contact-profile storage shape ONLY:
# since D1 a profile's share-state has NO effect on conversation mirroring.
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


def effective_level(override):
    """The conversation's explicit level: 'private' | 'share' | 'direct'.

    Accepts the raw account-data content (object form {'state': ...} or a bare
    string) as well as an already-normalized token, so the uplink's fresh
    point-read and the resolver can share one gate. ABSENT OR UNRECOGNIZED =>
    'private' — the invariant this whole model rests on. Mirrors
    effectiveLevel() in consent.js.
    """
    return normalize_override(override) or "private"


def resolve(convo, policy, override, profile=None):
    """Resolve one conversation's effective shared-state AND the reason.

    convo / policy / profile are IGNORED (D1: no inheritance on the
    conversation path); they stay in the signature so existing call sites and
    the conformance vectors keep working. Returns {"shared": bool, "reason":
    str} where reason is one of 'explicit' | 'direct' | 'excluded' | 'private'.
    `reason` is UI-only — never parse it for authorization. Mirrors resolve()
    in consent.js exactly.
    """
    level = effective_level(override)
    if level == "share":
        return {"shared": True, "reason": "explicit"}
    if level == "direct":
        return {"shared": True, "reason": "direct"}
    # Private either way; the reason distinguishes a deliberate exclusion from
    # "never set" purely for the UI's wording.
    return {"shared": False,
            "reason": "excluded" if normalize_override(override) else "private"}


def effective_shared(convo, policy, override, profile=None):
    """The boolean the uplink asks for when deciding whether to mirror a room."""
    return resolve(convo, policy, override, profile)["shared"]


def resolve_all(convos, policy, overrides, profiles=None):
    """Batch resolve. overrides may be a dict keyed by room id, or None.

    policy/profiles are IGNORED (D1). Returns a list of
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

    out = []
    for convo in convos:
        rid = convo.get("id") if isinstance(convo, dict) else None
        r = resolve(convo, policy, get(rid), None)
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
    """A per-room override -> 'share' | 'direct' | 'private' | None.

    Accepts the object form {'state': 'share'} or a bare string. None means
    "no recognized level stored", which the explicit model resolves to PRIVATE
    — never to an inherited share. Mirrors normalizeOverride() in JS.
    """
    if not data:
        return None
    v = data if isinstance(data, str) else (data.get("state") if isinstance(data, dict) else None)
    # The isinstance gate is load-bearing for parity AND for not crashing: JS's
    # Set.has() simply answers false for a non-string, while `v in <set>` raises
    # TypeError on an unhashable value (e.g. {"state": ["share"]}), and a crash
    # in the resolver aborts a whole reconcile pass.
    return v if isinstance(v, str) and v in OVERRIDE_STATES else None


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

# ---- per-contact overrides (per-contact-share plan, C1) --------------------
# A SECOND, more specific level in the contact dimension: one HANDLE
# ('<source>|<network_id>') may be pinned 'share' or 'private', winning over the
# per-source and global standing policies. Unlike the conversation dimension,
# the contact dimension deliberately KEEPS its standing policies — absent means
# inherit, not private.
CONTACT_OVERRIDES_TYPE = "com.jkali.contact_overrides"  # global user account-data
# F5: every WRITE path must refuse before crossing this, and a STORED map above
# it reads as a read-failure (normalize_contact_overrides -> None) so a bloated
# event can never be silently half-honored.
CONTACT_OVERRIDES_CAP = 1024
CONTACT_OVERRIDE_STATES = {"share", "private"}


def contact_override_key(source, network_id):
    """'<source>|<network_id>' for a valid pair, else None (F5/F6 key spec).

    The segment before the FIRST '|' must be a valid source id; the remainder is
    taken VERBATIM and may itself contain '|' (the importer's email charset
    admits it), which is why nothing here ever uses split('|'). The
    _SOURCE_KEY_RE prefix is what makes the composite injective. Mirrors
    contactOverrideKey() in consent.js."""
    if not isinstance(source, str) or not _SOURCE_KEY_RE.fullmatch(source):
        return None
    if not isinstance(network_id, str) or not network_id:
        return None
    return source + "|" + network_id


def split_contact_override_key(key):
    """The inverse: {'source', 'network_id'} or None. Splits ONCE, first '|'."""
    if not isinstance(key, str):
        return None
    i = key.find("|")
    if i <= 0:
        return None
    source, network_id = key[:i], key[i + 1:]
    if not _SOURCE_KEY_RE.fullmatch(source) or not network_id:
        return None
    return {"source": source, "network_id": network_id}


def _contact_override_entries(raw):
    """(stored_entry_count, {key: value}) for a stored overrides event, or None
    for a READ FAILURE. Mirrors contactOverrideEntries() in consent.js."""
    content = _plain(raw)
    if content is None:
        return (0, {})
    # An absent `overrides` field is an empty map; a PRESENT but non-dict one is
    # a READ FAILURE, never {} (F5) — a partially corrupt event must not
    # silently drop a 'private' deny and re-widen the contact to its source.
    if "overrides" not in content:
        return (0, {})
    src = _plain(content.get("overrides"))
    if src is None:
        return None
    out = {}
    keys = list(src.keys())
    for k in keys:
        if split_contact_override_key(k) is None:
            continue  # malformed KEY -> dropped (inherit)
        v = src[k]
        if v == "share" or v == "private":  # unknown VALUE -> dropped (inherit)
            out[k] = v
    return (len(keys), out)


def normalize_contact_overrides(raw):
    """{'<source>|<network_id>': 'share'|'private'} — or None on a READ FAILURE
    (non-dict `overrides` field, or a stored map over CONTACT_OVERRIDES_CAP).

    MUST stay byte-parity with normalizeContactOverrides() in consent.js."""
    e = _contact_override_entries(raw)
    if e is None:
        return None
    count, out = e
    if count > CONTACT_OVERRIDES_CAP:
        return None
    return out


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


def resolve_contact_share(source, policy, override=None):
    """Resolve whether a source's contacts are shared AND the reason.

    override is THIS ONE contact's stored value ('share'|'private'|absent), from
    normalize_contact_overrides keyed by contact_override_key.

    Precedence (most-specific-wins), mirroring resolveContactShare() in JS:
      0. per-contact 'share'      -> shared,     'this contact'
      0. per-contact 'private'    -> not shared, 'this contact private'
      1. per-source 'share-all'   -> shared,     'all <source> contacts'
      2. per-source 'private-all' -> not shared, 'private'
      3. global 'share-all'       -> shared,     'all contacts'
      4. safe default             -> not shared, 'private'
    An unrecognized override VALUE falls through to the source/global levels —
    the contact dimension keeps its standing policies (F5's fall-through is safe
    here only because normalize_contact_overrides drops unknown values on the
    way IN, and a non-dict stored map is a read failure rather than an empty
    one). `==` never raises on an unhashable override; `in <set>` would.
    """
    if override == "share":
        return {"shared": True, "reason": "this contact"}
    if override == "private":
        return {"shared": False, "reason": "this contact private"}

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

    Reads only com.jkali.share_override. Returns
    { room_id: 'share'|'direct'|'private' }; a room whose stored value is
    absent/cleared/unrecognized is OMITTED (a later junk event even deletes an
    earlier valid one — pinned by the unit tests), and an omitted room resolves
    private. Mirrors overridesFromSync() in JS.
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
