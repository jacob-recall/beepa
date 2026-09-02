# tests/unit/contact_consent_py.test.py
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "agents", "uplink"))
import consent
def eq(a,b,m):
    assert a==b, m+": "+repr(a)
eq(consent.resolve_contact_share("imessage", consent.normalize_contact_policy(None)), {"shared":False,"reason":"private"}, "default")
eq(consent.resolve_contact_share("imessage", consent.normalize_contact_policy({"global":"share-all"})), {"shared":True,"reason":"all contacts"}, "global")
eq(consent.resolve_contact_share("imessage", consent.normalize_contact_policy({"global":"share-all","sources":{"imessage":"private-all"}})), {"shared":False,"reason":"private"}, "src-private")
eq(consent.resolve_contact_share("imessage", consent.normalize_contact_policy({"global":"private","sources":{"imessage":"share-all"}})), {"shared":True,"reason":"all imessage contacts"}, "src-share")
eq(consent.resolve_contact_share("imessage", consent.normalize_contact_policy({"global":"yolo"})), {"shared":False,"reason":"private"}, "garbage")
# array-shaped sources must be rejected like no sources (parity with JS !Array.isArray guard)
eq(consent.normalize_contact_policy({"global":"private","sources":["share-all","private-all"]}), {"global":"private","sources":{}}, "array sources -> {}")
eq(consent.normalize_contact_policy({"global":"private","sources":["share-all","private-all"]}), consent.normalize_contact_policy({"global":"private"}), "array sources same as no sources")
eq(consent.resolve_contact_share("imessage", consent.normalize_contact_policy({"global":"private","sources":["share-all"]})), {"shared":False,"reason":"private"}, "array sources resolves like empty -> private")
# PINNED (consent-conformance plan, deny-drop decision): a malformed source id
# drops its private-all rule and falls through to a global share-all. Do NOT
# change one side only — see the conformance harness.
eq(consent.resolve_contact_share(5, {"global":"share-all","sources":{"5":"private-all"}}),
   {"shared":True,"reason":"all contacts"},
   "pinned: non-string source drops the private-all rule -> global share-all")

# ---- per-contact overrides (per-contact-share plan, C1) --------------------
# Mirrors tests/unit/contact_consent.test.js case for case.
SHARE_ALL = consent.normalize_contact_policy({"global": "private", "sources": {"imessage": "share-all"}})
PRIVATE_ALL = consent.normalize_contact_policy({"global": "share-all", "sources": {"imessage": "private-all"}})

eq(consent.resolve_contact_share("imessage", PRIVATE_ALL, "share"),
   {"shared": True, "reason": "this contact"}, "override share beats private-all")
eq(consent.resolve_contact_share("imessage", SHARE_ALL, "private"),
   {"shared": False, "reason": "this contact private"}, "override private beats share-all")
eq(consent.resolve_contact_share("imessage", consent.normalize_contact_policy(None), "share"),
   {"shared": True, "reason": "this contact"}, "override share beats the private default")

# an UNRECOGNIZED / absent override inherits — the contact dimension keeps its
# standing policies (unlike the conversation dimension, where unknown = private)
for junk in (None, "", "inherit", "junk", "Share", "share-all", 5, True, {}, [], {"state": "share"}):
    eq(consent.resolve_contact_share("imessage", SHARE_ALL, junk),
       {"shared": True, "reason": "all imessage contacts"}, "unknown override inherits share-all")
    eq(consent.resolve_contact_share("imessage", PRIVATE_ALL, junk),
       {"shared": False, "reason": "private"}, "unknown override inherits private-all")

# ---- key spec (F5/F6): first-'|' split, _SOURCE_KEY_RE prefix --------------
eq(consent.contact_override_key("imessage", "+15551234567"), "imessage|+15551234567", "key built")
eq(consent.contact_override_key("imessage", "a|b@example.com"), "imessage|a|b@example.com",
   "'|' is legal in a network_id")
eq(consent.split_contact_override_key("imessage|a|b@example.com"),
   {"source": "imessage", "network_id": "a|b@example.com"}, "split once, on the FIRST pipe")
eq(consent.contact_override_key("__proto__", "x"), None, "prototype-named source rejected")
eq(consent.contact_override_key("iMessage", "x"), None, "uppercase source rejected")
eq(consent.contact_override_key("imessage", ""), None, "empty network_id rejected")
eq(consent.split_contact_override_key("imessage"), None, "a key with no pipe is invalid")
eq(consent.split_contact_override_key("|x"), None, "an empty source segment is invalid")
eq(consent.split_contact_override_key("imessage|"), None, "an empty network_id segment is invalid")

# ---- normalize_contact_overrides -----------------------------------------
eq(consent.normalize_contact_overrides(None), {}, "absent event -> {}")
eq(consent.normalize_contact_overrides({}), {}, "no overrides field -> {}")
eq(consent.normalize_contact_overrides({"overrides": {"imessage|+15551234567": "share"}}),
   {"imessage|+15551234567": "share"}, "valid entry kept")
eq(consent.normalize_contact_overrides({"overrides": {
    "imessage|+1": "share", "nopipe": "private", "|x": "private", "imessage|": "private",
    "__proto__|x": "private", "imessage|+2": "junk", "imessage|+3": 5, "imessage|+4": "private",
}}), {"imessage|+1": "share", "imessage|+4": "private"}, "malformed keys/values dropped")
for bad in ([], "share", 5, None, True):
    eq(consent.normalize_contact_overrides({"overrides": bad}), None,
       "non-dict overrides field is a read failure")
over_cap = {"imessage|+1%d" % i: "private" for i in range(consent.CONTACT_OVERRIDES_CAP + 1)}
eq(len(over_cap) > consent.CONTACT_OVERRIDES_CAP, True, "fixture is over the cap")
eq(consent.normalize_contact_overrides({"overrides": over_cap}), None,
   "over-cap stored map is a read failure")
at_cap = {"imessage|+1%d" % i: "private" for i in range(consent.CONTACT_OVERRIDES_CAP)}
eq(len(consent.normalize_contact_overrides({"overrides": at_cap})), consent.CONTACT_OVERRIDES_CAP,
   "exactly at the cap still reads")

print("ok contact_consent_py")
