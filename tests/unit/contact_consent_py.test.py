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
print("ok contact_consent_py")
