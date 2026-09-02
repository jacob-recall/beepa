#!/usr/bin/env python3
"""Parity test for agents/uplink/consent.py against shared/model/consent.js.

Mirrors tests/unit/consent.test.js case-for-case so the Python resolver the
uplink authorizes with produces byte-identical decisions to the JS resolver the
user app shows. Run: python3 tests/unit/consent_py.test.py  (exit 0 = all pass).

THE EXPLICIT THREE-LEVEL MODEL (direct-share-level plan, D1): a conversation
mirrors on its own per-conversation level and nothing else — 'share' -> shared,
'direct' -> shared, 'private' -> not shared, ABSENT OR ANY UNRECOGNIZED VALUE ->
not shared. The profile / per-source / global inputs are accepted and IGNORED;
the cases that pass a share-all policy or a shared profile next to an unset
override exist to prove exactly that.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "..", "agents", "uplink"))
import consent  # noqa: E402
from consent import (  # noqa: E402
    resolve, effective_shared, effective_level, resolve_all,
    normalize_policy, normalize_override, overrides_from_sync,
)

_pass = 0
_fail = 0
_failures = []


def eq(actual, expected, label):
    global _pass, _fail
    if actual == expected:
        _pass += 1
    else:
        _fail += 1
        _failures.append("%s: expected %r, got %r" % (label, expected, actual))


def convo(source_id, source_label=None):
    return {"id": "!" + source_id + ":local", "sourceId": source_id,
            "sourceLabel": source_label or source_id}


# Every "loud" standing-policy input the old model would have shared on.
LOUD_POLICY = {"global": "share-all", "sources": {"imessage": "share-all"}}
LOUD_PROFILE = {"displayName": "Dana Lewis", "share": "share"}
DENY_POLICY = {"global": "private", "sources": {"imessage": "private-all"}}
DENY_PROFILE = {"displayName": "Dana Lewis", "share": "private"}

# 1. Absent override -> private, whatever any other level says (NO INHERITANCE)
c = convo("imessage", "iMessage")
eq(resolve(c, {}, None), {"shared": False, "reason": "private"}, "unset: no policy at all")
eq(resolve(c, {"global": "private", "sources": {}}, None),
   {"shared": False, "reason": "private"}, "unset: explicit global private")
eq(resolve(c, {"global": "share-all", "sources": {}}, None),
   {"shared": False, "reason": "private"}, "unset: global share-all does NOT share")
eq(resolve(c, {"global": "private", "sources": {"imessage": "share-all"}}, None),
   {"shared": False, "reason": "private"}, "unset: per-source share-all does NOT share")
eq(resolve(c, LOUD_POLICY, None, LOUD_PROFILE),
   {"shared": False, "reason": "private"},
   "unset: shared profile + both share-all levels do NOT share")
eq(effective_shared(c, LOUD_POLICY, None, LOUD_PROFILE), False,
   "unset: effective_shared false under every share-all")

# 2. 'share' -> shared/explicit, whatever any other level says
eq(resolve(c, {}, "share"), {"shared": True, "reason": "explicit"}, "share: empty policy")
eq(resolve(c, DENY_POLICY, "share"), {"shared": True, "reason": "explicit"},
   "share: per-source private-all cannot exclude an explicit share")
eq(resolve(c, DENY_POLICY, "share", DENY_PROFILE), {"shared": True, "reason": "explicit"},
   "share: a private profile cannot exclude an explicit share")
eq(resolve(c, {}, {"state": "share"}), {"shared": True, "reason": "explicit"},
   "share: object form {state} is accepted")
eq(effective_shared(c, DENY_POLICY, "share", DENY_PROFILE), True, "share: effective_shared true")

# 3. 'direct' -> shared, reason 'direct'
eq(resolve(c, {}, "direct"), {"shared": True, "reason": "direct"}, "direct: empty policy")
eq(resolve(c, DENY_POLICY, "direct", DENY_PROFILE), {"shared": True, "reason": "direct"},
   "direct: not excludable by policy or profile")
eq(resolve(c, {}, {"state": "direct"}), {"shared": True, "reason": "direct"},
   "direct: object form {state} is accepted")
eq(effective_shared(c, {}, "direct"), True, "direct: effective_shared true")

# 4. 'private' -> not shared/excluded, whatever any other level says
eq(resolve(c, {}, "private"), {"shared": False, "reason": "excluded"}, "private: empty policy")
eq(resolve(c, LOUD_POLICY, "private", LOUD_PROFILE), {"shared": False, "reason": "excluded"},
   "private: beats shared profile and both share-all levels")
eq(resolve(c, {}, {"state": "private"}), {"shared": False, "reason": "excluded"},
   "private: object form {state} is accepted")
eq(effective_shared(c, LOUD_POLICY, "private", LOUD_PROFILE), False,
   "private: effective_shared false")

# 5. THE UNKNOWN-VALUE INVARIANT (F8): absent or ANY unrecognized value is
#    private — even under the loudest possible share-all policy + a shared
#    profile. Mirrors the same list in consent.test.js (JS `undefined` and
#    `null` both arrive here as None).
UNKNOWN = [
    None, "", "inherit", "junk", "shared", "Share", "SHARE", "share ", " share",
    "Direct", "DIRECT", "direct ", "share-all", "private-all", "auto", "__proto__",
    "constructor", 0, 1, 5, True, False, [], {}, ["share"], ["direct"],
    {"state": "inherit"}, {"state": "junk"}, {"state": None}, {"state": 5},
    {"state": ["share"]},
    {"State": "share"}, {"level": "share"}, {"state": "share-all"},
    # NFKC lookalikes, a zero-width space, a NUL suffix, a NBSP: exact-match
    # canaries against a future strip()/normalize() on one side only.
    "\uff53hare", "sh\u200bare", "share\u0000", "direct\u0000", "private\u00a0",
]
for v in UNKNOWN:
    lab = "unknown-override %r" % (v,)
    eq(resolve(convo("imessage", "iMessage"), LOUD_POLICY, v, LOUD_PROFILE),
       {"shared": False, "reason": "private"}, lab + ": private under every share-all")
    eq(effective_level(v), "private", lab + ": effective_level private")
    eq(effective_shared(convo("imessage"), LOUD_POLICY, v, LOUD_PROFILE), False,
       lab + ": effective_shared false")

# 6. effective_level: the three levels, from both storage forms
eq(effective_level("share"), "share", "effective_level: share")
eq(effective_level("direct"), "direct", "effective_level: direct")
eq(effective_level("private"), "private", "effective_level: private")
eq(effective_level({"state": "share"}), "share", "effective_level: object share")
eq(effective_level({"state": "direct"}), "direct", "effective_level: object direct")
eq(effective_level({"state": "private"}), "private", "effective_level: object private")
eq(effective_level(None), "private", "effective_level: absent -> private")
eq(effective_level({"state": "share", "migrated": True}), "share",
   "effective_level: the migration marker does not disturb the level")

# 7. "share everything except one thread" is now a per-conversation job
policy = {"global": "share-all", "sources": {}}
eq(resolve(convo("imessage", "iMessage"), policy, "private"),
   {"shared": False, "reason": "excluded"}, "except-one: the excluded thread is private")
for cc in (convo("imessage"), convo("linkedin"), convo("whatsapp")):
    eq(resolve(cc, policy, None), {"shared": False, "reason": "private"},
       "except-one: unset thread %s is private, NOT swept in by share-all" % cc["sourceId"])
    eq(resolve(cc, policy, "share"), {"shared": True, "reason": "explicit"},
       "except-one: thread %s shares only when set explicitly" % cc["sourceId"])

# 8. resolve_all: dict overrides, input order; policy/profiles accepted+ignored
convos = [
    {"id": "!a:local", "sourceId": "imessage", "sourceLabel": "iMessage"},
    {"id": "!b:local", "sourceId": "linkedin", "sourceLabel": "LinkedIn"},
    {"id": "!c:local", "sourceId": "linkedin", "sourceLabel": "LinkedIn"},
    {"id": "!d:local", "sourceId": "imessage", "sourceLabel": "iMessage"},
]
res = resolve_all(convos, LOUD_POLICY,
                  {"!a:local": "share", "!c:local": "direct", "!d:local": "private"})
eq([r["shared"] for r in res], [True, False, True, False], "resolve_all: dict overrides")
eq([r["reason"] for r in res], ["explicit", "private", "direct", "excluded"],
   "resolve_all: reasons")
eq([r["convo"]["id"] for r in res], ["!a:local", "!b:local", "!c:local", "!d:local"],
   "resolve_all: preserves input order")
eq([r["shared"] for r in resolve_all(convos, LOUD_POLICY, None)], [False] * 4,
   "resolve_all: no overrides -> all private despite a share-all policy")
eq([r["shared"] for r in resolve_all(convos, LOUD_POLICY, None,
                                     {"!a:local": LOUD_PROFILE, "!b:local": LOUD_PROFILE})],
   [False] * 4, "resolve_all: a shared profiles map shares nothing")
eq(resolve_all(None, LOUD_POLICY, None), [], "resolve_all: non-list convos -> []")
# PARITY REGRESSION (found by the conformance harness): the override key is read
# ONLY from a dict convo. JS's `convo && convo.id` yielded "" for the
# empty-string convo, matched an override stored under "", and shared a
# conversation this enforcer resolved private.
eq([r["shared"] for r in resolve_all([{"id": "", "sourceId": ""}, "", 0, None, [], {"id": 5}],
                                     LOUD_POLICY, {"": "share"})],
   [True, False, False, False, False, False],
   "resolve_all: only a dict convo can match an override key")

# 9. normalize_policy: unchanged (the standing policy still round-trips through
#    storage, it just no longer decides anything on the conversation path)
eq(normalize_policy(None), {"global": "private", "sources": {}}, "normalizePolicy: None")
eq(normalize_policy({}), {"global": "private", "sources": {}}, "normalizePolicy: empty")
eq(normalize_policy({"global": "share-all", "sources": {}}),
   {"global": "share-all", "sources": {}}, "normalizePolicy: valid share-all")
eq(normalize_policy({"global": "bogus", "sources": {}}),
   {"global": "private", "sources": {}}, "normalizePolicy: unknown global -> private")
eq(normalize_policy({"global": "private",
                     "sources": {"a": "share-all", "b": "private-all", "c": "inherit",
                                 "d": "junk", "e": 123}}),
   {"global": "private", "sources": {"a": "share-all", "b": "private-all"}},
   "normalizePolicy: drops inherit/junk")
eq(normalize_policy({"global": "share-all", "sources": None}),
   {"global": "share-all", "sources": {}}, "normalizePolicy: null sources -> {}")
eq(normalize_policy({"global": "private", "sources": ["share-all", "private-all"]}),
   {"global": "private", "sources": {}}, "normalizePolicy: array sources -> {}")

# 10. normalize_override: share/direct/private survive; everything else -> None
eq(normalize_override(None), None, "normalizeOverride: None")
eq(normalize_override({}), None, "normalizeOverride: empty object")
eq(normalize_override("share"), "share", "normalizeOverride: bare share")
eq(normalize_override("direct"), "direct", "normalizeOverride: bare direct")
eq(normalize_override("private"), "private", "normalizeOverride: bare private")
eq(normalize_override("inherit"), None, "normalizeOverride: bare inherit -> None")
eq(normalize_override({"state": "share"}), "share", "normalizeOverride: obj share")
eq(normalize_override({"state": "direct"}), "direct", "normalizeOverride: obj direct")
eq(normalize_override({"state": "private"}), "private", "normalizeOverride: obj private")
eq(normalize_override({"state": "inherit"}), None, "normalizeOverride: obj inherit -> None")
eq(normalize_override({"state": "junk"}), None, "normalizeOverride: obj junk -> None")
eq(normalize_override({"state": "share", "migrated": True}), "share",
   "normalizeOverride: the D0 migration marker rides along untouched")
eq(normalize_override({"state": ["share"]}), None,
   "normalizeOverride: an unhashable state value is None, never a crash")

# 11. overrides_from_sync: carries all three levels; PINNED — a later junk value
#     DELETES an earlier valid one for the same room, and it then resolves private.
ov = lambda content: {"type": "com.jkali.share_override", "content": content}  # noqa: E731
sync = {"rooms": {"join": {
    "!a:local": {"account_data": {"events": [ov({"state": "share"})]}},
    "!b:local": {"account_data": {"events": [ov("private")]}},
    "!c:local": {"account_data": {"events": [ov({"state": "direct"})]}},
    "!d:local": {"account_data": {"events": [ov({"state": "inherit"})]}},
    "!e:local": {"account_data": {"events": [ov({"state": "share", "migrated": True})]}},
    "!f:local": {"account_data": {"events": [{"type": "m.tag", "content": {}}]}},
}}}
eq(overrides_from_sync(sync),
   {"!a:local": "share", "!b:local": "private", "!c:local": "direct", "!e:local": "share"},
   "overridesFromSync: keeps share/direct/private, omits inherit + unrelated types")

clobber = {"rooms": {"join": {
    "!a:local": {"account_data": {"events": [ov({"state": "share"}), ov({"state": "junk"})]}},
    "!b:local": {"account_data": {"events": [ov({"state": "direct"}), ov({})]}},
    "!c:local": {"account_data": {"events": [ov({"state": "share"}), ov({"state": "private"})]}},
}}}
eq(overrides_from_sync(clobber), {"!c:local": "private"},
   "PINNED overridesFromSync: a later junk/empty event deletes the earlier valid key")
eq(resolve({"id": "!a:local", "sourceId": "imessage"}, LOUD_POLICY,
           overrides_from_sync(clobber).get("!a:local"), LOUD_PROFILE),
   {"shared": False, "reason": "private"},
   "PINNED: a key deleted by a junk value resolves private, never inherited-shared")
eq(overrides_from_sync({"rooms": {"join": {"not-a-room": {
    "account_data": {"events": [ov("share")]}}}}}), {},
   "overridesFromSync: non-room-shaped keys never enter the map")
eq(overrides_from_sync(None), {}, "overridesFromSync: junk input -> {}, no throw")

# 12. Reason string exhaustiveness (reason is UI-only, never authorization)
eq(resolve(convo("x"), {}, None)["reason"], "private", 'reason: unset -> "private"')
eq(resolve(convo("x"), {"global": "share-all"}, None)["reason"], "private",
   'reason: global share-all no longer produces an "all <source>" reason')
eq(resolve(convo("x"), {}, "share")["reason"], "explicit", 'reason: share -> "explicit"')
eq(resolve(convo("x"), {}, "direct")["reason"], "direct", 'reason: direct -> "direct"')
eq(resolve(convo("x"), {"global": "share-all"}, "private")["reason"], "excluded",
   'reason: private -> "excluded"')
eq(resolve(convo("x"), {}, None, {"displayName": "Ann", "share": "share"})["reason"], "private",
   'reason: a shared profile no longer produces a "profile: <name>" reason')

# 13. Hostile/degenerate convo shapes never throw and never share
for cc in (None, 5, "imessage", [], {}, {"id": "!r:l", "sourceId": 5},
           {"id": "!r:l", "sourceId": "__proto__"}):
    eq(resolve(cc, LOUD_POLICY, None, LOUD_PROFILE), {"shared": False, "reason": "private"},
       "hostile convo %r: private" % (cc,))
    eq(resolve(cc, DENY_POLICY, "share", DENY_PROFILE), {"shared": True, "reason": "explicit"},
       "hostile convo %r: explicit share still holds" % (cc,))

# The model-version marker constants the uplink writes and apps/user reads must
# stay identical on both sides (F7).
eq(consent.CONSENT_MODEL_TYPE, "com.jkali.consent_model", "marker: account-data type")
eq(consent.CONSENT_MODEL_EXPLICIT, 2, "marker: explicit-model version")
eq(sorted(consent.OVERRIDE_STATES), ["direct", "private", "share"],
   "exactly three conversation levels exist")

print("\n%d passed, %d failed" % (_pass, _fail))
if _fail:
    sys.stderr.write("\nFailures:\n")
    for f in _failures:
        sys.stderr.write("  - " + f + "\n")
    sys.exit(1)
