#!/usr/bin/env python3
"""Parity test for agents/uplink/consent.py against shared/model/consent.js.

Mirrors tests/unit/consent.test.js case-for-case so the Python resolver the
uplink authorizes with produces byte-identical decisions to the JS resolver the
user app shows. Run: python3 tests/unit/consent_py.test.py  (exit 0 = all pass).
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "..", "agents", "uplink"))
import consent  # noqa: E402
from consent import (  # noqa: E402
    resolve, effective_shared, resolve_all, normalize_policy, normalize_override,
    overrides_from_sync,
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


# 1. Global default -> private
c = convo("imessage", "iMessage")
eq(resolve(c, {}, None), {"shared": False, "reason": "private"}, "default: no policy")
eq(resolve(c, {"global": "private", "sources": {}}, None),
   {"shared": False, "reason": "private"}, "default: explicit global private")
eq(effective_shared(c, {}, None), False, "default: effectiveShared false")

# 2. Global share-all -> shared for every source
policy = {"global": "share-all", "sources": {}}
eq(resolve(convo("imessage", "iMessage"), policy, None),
   {"shared": True, "reason": "all iMessage"}, "global share-all: iMessage")
eq(resolve(convo("linkedin", "LinkedIn"), policy, None),
   {"shared": True, "reason": "all LinkedIn"}, "global share-all: LinkedIn")
eq(resolve({"id": "!x:local", "sourceId": "whatsapp"}, policy, None),
   {"shared": True, "reason": "all whatsapp"}, "global share-all: label falls back to sourceId")
eq(resolve({"id": "!x:local"}, policy, None),
   {"shared": True, "reason": "all source"}, "global share-all: generic fallback label")

# 3. Per-source share-all (standing policy), global stays private
policy = {"global": "private", "sources": {"imessage": "share-all"}}
eq(resolve(convo("imessage", "iMessage"), policy, None),
   {"shared": True, "reason": "all iMessage"}, "per-source share-all: matching source shared")
eq(resolve(convo("linkedin", "LinkedIn"), policy, None),
   {"shared": False, "reason": "private"}, "per-source share-all: other source private")
eq(resolve(convo("imessage", "iMessage"), policy, None),
   {"shared": True, "reason": "all iMessage"}, "per-source share-all: standing covers later arrival")

# 4. Per-source private-all overrides global share-all
policy = {"global": "share-all", "sources": {"linkedin": "private-all"}}
eq(resolve(convo("linkedin", "LinkedIn"), policy, None),
   {"shared": False, "reason": "private"}, "per-source private-all beats global share-all")
eq(resolve(convo("imessage", "iMessage"), policy, None),
   {"shared": True, "reason": "all iMessage"}, "per-source private-all: others still via global")

# per-source 'inherit' falls through to global (both directions)
eq(resolve(convo("imessage"), {"global": "share-all", "sources": {"imessage": "inherit"}}, None),
   {"shared": True, "reason": "all imessage"}, "per-source inherit -> global share-all")
eq(resolve(convo("imessage"), {"global": "private", "sources": {"imessage": "inherit"}}, None),
   {"shared": False, "reason": "private"}, "per-source inherit -> global private")

# 5. Per-conv 'private' excludes despite any higher share-all
eq(resolve(convo("imessage"), {"global": "share-all", "sources": {}}, "private"),
   {"shared": False, "reason": "excluded"}, "per-conv private excludes despite global share-all")
eq(resolve(convo("imessage"), {"global": "private", "sources": {"imessage": "share-all"}}, "private"),
   {"shared": False, "reason": "excluded"}, "per-conv private excludes despite per-source share-all")
eq(resolve(convo("imessage"), {"global": "share-all", "sources": {"imessage": "share-all"}}, "private"),
   {"shared": False, "reason": "excluded"}, "per-conv private excludes despite BOTH")

# 6. Per-conv 'share' includes despite default-private / per-source private-all
eq(resolve(convo("imessage"), {"global": "private", "sources": {}}, "share"),
   {"shared": True, "reason": "explicit"}, "per-conv share despite default-private")
eq(resolve(convo("imessage"), {"global": "private", "sources": {"imessage": "private-all"}}, "share"),
   {"shared": True, "reason": "explicit"}, "per-conv share despite per-source private-all")
eq(resolve(convo("imessage"), {}, "share"),
   {"shared": True, "reason": "explicit"}, "per-conv share despite empty policy")

# share-everything-except-one
policy = {"global": "share-all", "sources": {}}
eq(resolve(convo("imessage", "iMessage"), policy, "private"),
   {"shared": False, "reason": "excluded"}, "except-one: excluded thread")
for c in (convo("imessage", "iMessage"), convo("linkedin", "LinkedIn"), convo("whatsapp", "WhatsApp")):
    eq(resolve(c, policy, None), {"shared": True, "reason": "all " + c["sourceLabel"]},
       "except-one: other " + c["sourceId"] + " shared")

# all-imessage-not-linkedin
policy = {"global": "private", "sources": {"imessage": "share-all", "linkedin": "private-all"}}
eq(resolve(convo("imessage", "iMessage"), policy, None),
   {"shared": True, "reason": "all iMessage"}, "imsg-not-li: iMessage shared")
eq(resolve(convo("linkedin", "LinkedIn"), policy, None),
   {"shared": False, "reason": "private"}, "imsg-not-li: LinkedIn private")
eq(resolve(convo("whatsapp", "WhatsApp"), policy, None),
   {"shared": False, "reason": "private"}, "imsg-not-li: unrelated default private")

# effective_shared across four levels
eq(effective_shared(convo("imessage"), {"global": "share-all", "sources": {}}, None), True, "es: global share-all")
eq(effective_shared(convo("imessage"), {"global": "private", "sources": {"imessage": "share-all"}}, None), True, "es: per-source share-all")
eq(effective_shared(convo("imessage"), {"global": "share-all", "sources": {"imessage": "private-all"}}, None), False, "es: per-source private-all beats global")
eq(effective_shared(convo("imessage"), {"global": "share-all"}, "private"), False, "es: per-conv private beats global")
eq(effective_shared(convo("imessage"), {}, "share"), True, "es: per-conv share beats default")

# resolve_all: dict overrides, input order
policy = {"global": "private", "sources": {"imessage": "share-all"}}
convos = [
    {"id": "!a:local", "sourceId": "imessage", "sourceLabel": "iMessage"},
    {"id": "!b:local", "sourceId": "linkedin", "sourceLabel": "LinkedIn"},
    {"id": "!c:local", "sourceId": "linkedin", "sourceLabel": "LinkedIn"},
]
res = resolve_all(convos, policy, {"!c:local": "share"})
eq([r["shared"] for r in res], [True, False, True], "resolve_all: shape")
eq([r["reason"] for r in res], ["all iMessage", "private", "explicit"], "resolve_all: reasons")
eq([r["convo"]["id"] for r in res], ["!a:local", "!b:local", "!c:local"], "resolve_all: order")
eq([r["shared"] for r in resolve_all(convos, policy, {"!c:local": "private"})],
   [True, False, False], "resolve_all: private override")
eq([r["shared"] for r in resolve_all(convos, policy, None)], [True, False, False], "resolve_all: no overrides")
eq(resolve_all(None, policy, None), [], "resolve_all: non-list -> []")

# normalize_policy
eq(normalize_policy(None), {"global": "private", "sources": {}}, "normalizePolicy: None")
eq(normalize_policy({}), {"global": "private", "sources": {}}, "normalizePolicy: empty")
eq(normalize_policy({"global": "share-all", "sources": {}}), {"global": "share-all", "sources": {}}, "normalizePolicy: valid share-all")
eq(normalize_policy({"global": "bogus", "sources": {}}), {"global": "private", "sources": {}}, "normalizePolicy: unknown global -> private")
eq(normalize_policy({"global": "private", "sources": {"a": "share-all", "b": "private-all", "c": "inherit", "d": "junk", "e": 123}}),
   {"global": "private", "sources": {"a": "share-all", "b": "private-all"}}, "normalizePolicy: drops inherit/junk")
eq(normalize_policy({"global": "share-all", "sources": None}), {"global": "share-all", "sources": {}}, "normalizePolicy: null sources -> {}")

# normalize_override
eq(normalize_override(None), None, "normalizeOverride: None")
eq(normalize_override({}), None, "normalizeOverride: empty")
eq(normalize_override("share"), "share", "normalizeOverride: bare share")
eq(normalize_override("private"), "private", "normalizeOverride: bare private")
eq(normalize_override("inherit"), None, "normalizeOverride: bare inherit -> None")
eq(normalize_override({"state": "share"}), "share", "normalizeOverride: obj share")
eq(normalize_override({"state": "private"}), "private", "normalizeOverride: obj private")
eq(normalize_override({"state": "inherit"}), None, "normalizeOverride: obj inherit -> None")
eq(normalize_override({"state": "junk"}), None, "normalizeOverride: obj junk -> None")

# reason exhaustiveness
eq(resolve(convo("x"), {}, None)["reason"], "private", "reason: default private")
eq(resolve(convo("x"), {"global": "share-all"}, None)["reason"], "all x", "reason: global share-all interpolates")
eq(resolve(convo("x"), {"global": "private", "sources": {"x": "share-all"}}, None)["reason"], "all x", "reason: per-source share-all interpolates")
eq(resolve(convo("x"), {"global": "share-all", "sources": {"x": "private-all"}}, None)["reason"], "private", "reason: per-source private-all -> private")
eq(resolve(convo("x"), {}, "share")["reason"], "explicit", "reason: per-conv share -> explicit")
eq(resolve(convo("x"), {"global": "share-all"}, "private")["reason"], "excluded", "reason: per-conv private -> excluded")

# overrides_from_sync
sync = {"rooms": {"join": {
    "!a:local": {"account_data": {"events": [{"type": "com.jkali.share_override", "content": {"state": "share"}}]}},
    "!b:local": {"account_data": {"events": [{"type": "com.jkali.share_override", "content": "private"}]}},
    "!c:local": {"account_data": {"events": [{"type": "com.jkali.share_override", "content": {"state": "inherit"}}]}},
    "!d:local": {"account_data": {"events": [{"type": "m.tag", "content": {}}]}},
}}}
eq(overrides_from_sync(sync), {"!a:local": "share", "!b:local": "private"}, "overridesFromSync: only valid overrides")

print("\n%d passed, %d failed" % (_pass, _fail))
if _fail:
    sys.stderr.write("\nFailures:\n")
    for f in _failures:
        sys.stderr.write("  - " + f + "\n")
    sys.exit(1)
