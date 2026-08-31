#!/usr/bin/env python3
"""Unit tests for master/enroll.py's derived-password scheme (no stored
passwords): derivation determinism/separation, key-file failure modes, and
the migration password-change request shape (logout_devices MUST be False).

Run: python3 tests/unit/enroll_password_derivation.test.py  (exit 0 = pass).
"""
import json
import os
import re
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "..", "master"))
import enroll  # noqa: E402

_pass = 0
_fail = 0
_failures = []


def check(cond, label):
    global _pass, _fail
    if cond:
        _pass += 1
    else:
        _fail += 1
        _failures.append(label)


def raises(fn, label):
    try:
        fn()
    except enroll.EnrollError:
        check(True, label)
    except Exception as e:
        check(False, label + " (wrong exception: %r)" % e)
    else:
        check(False, label + " (no exception)")


KEY_A = b"A" * 48
KEY_B = b"B" * 48

# --- derivation: deterministic, 32 url-safe chars, separated by user/kind/key
p1 = enroll.derive_password("teammate", "alice", key=KEY_A)
check(p1 == enroll.derive_password("teammate", "alice", key=KEY_A), "deterministic")
check(len(p1) == 32 and re.fullmatch(r"[A-Za-z0-9_-]{32}", p1) is not None,
      "32 url-safe chars")
check(p1 != enroll.derive_password("teammate", "bob", key=KEY_A), "differs per localpart")
check(p1 != enroll.derive_password("teammate", "alice", key=KEY_B), "differs per key")
check(enroll.derive_password("manager", "manager", key=KEY_A)
      != enroll.derive_password("teammate", "alice", key=KEY_A), "kind domains differ")

# --- localpart validation: reject, never normalise
raises(lambda: enroll.derive_password("teammate", "Alice", key=KEY_A), "uppercase rejected")
raises(lambda: enroll.derive_password("teammate", "alice\n", key=KEY_A), "trailing newline rejected")
raises(lambda: enroll.derive_password("teammate", "jo.smith", key=KEY_A), "punctuation rejected")
raises(lambda: enroll.derive_password("teammate", "", key=KEY_A), "empty rejected")
raises(lambda: enroll.derive_password("teammate", None, key=KEY_A), "None rejected")
raises(lambda: enroll.derive_password("teammate", "a" * 65, key=KEY_A), "over-long rejected")
# 'manager' is reserved: never a teammate, and the manager kind only for 'manager'
raises(lambda: enroll.derive_password("teammate", "manager", key=KEY_A), "manager reserved")
raises(lambda: enroll.derive_password("manager", "alice", key=KEY_A), "manager kind only for manager")
raises(lambda: enroll.derive_password("junk", "alice", key=KEY_A), "unknown kind rejected")

# --- key file: missing/short key raises (never an empty-HMAC-key password)
orig_secrets = enroll.SECRETS_FILE
d = tempfile.mkdtemp()
try:
    enroll.SECRETS_FILE = os.path.join(d, "missing.local")
    raises(lambda: enroll._password_keys(), "missing secrets file raises")
    raises(lambda: enroll.derive_password("teammate", "alice"), "missing key raises via derive")

    short = os.path.join(d, "short.local")
    with open(short, "w") as f:
        f.write("TEAMMATE_PASSWORD_KEY='tooshort'\n")
    enroll.SECRETS_FILE = short
    raises(lambda: enroll._password_keys(), "short key raises")

    good = os.path.join(d, "good.local")
    with open(good, "w") as f:
        f.write("TEAMMATE_PASSWORD_KEY='%s'\n" % ("k" * 48))
    enroll.SECRETS_FILE = good
    cur, prev = enroll._password_keys()
    check(cur == b"k" * 48 and prev is None, "key = ASCII bytes; no _PREV -> None")

    both = os.path.join(d, "both.local")
    with open(both, "w") as f:
        f.write("TEAMMATE_PASSWORD_KEY='%s'\nTEAMMATE_PASSWORD_KEY_PREV='%s'\n"
                % ("k" * 48, "p" * 48))
    enroll.SECRETS_FILE = both
    cur, prev = enroll._password_keys()
    check(prev == b"p" * 48, "_PREV read when present and long enough")
finally:
    enroll.SECRETS_FILE = orig_secrets

# --- _change_password request shape: logout_devices False, full UIA with the
# OLD password. Stub the transport; capture every request.
calls = []


def fake_request(method, url, headers=None, data=None, timeout=30):
    calls.append({"method": method, "url": url, "headers": headers or {},
                  "body": json.loads(data) if data else None})
    if len(calls) == 1:
        return 401, json.dumps({"session": "s1", "flows": []}).encode()
    return 200, b"{}"


orig_request = enroll._request
enroll._request = fake_request
try:
    enroll._change_password("http://cs", "tok123", "alice", "oldpw", "newpw")
finally:
    enroll._request = orig_request

check(len(calls) == 2, "UIA: initial POST then resubmit")
first, second = calls
check(first["url"].endswith("/_matrix/client/v3/account/password"), "endpoint")
check(first["headers"].get("Authorization") == "Bearer tok123", "bearer token used")
check(first["body"] == {"new_password": "newpw", "logout_devices": False},
      "first body: new_password + logout_devices FALSE, no auth")
check(second["body"].get("logout_devices") is False, "second body keeps logout_devices False")
check(second["body"].get("new_password") == "newpw", "second body keeps new_password")
check(second["body"].get("auth") == {
    "type": "m.login.password",
    "identifier": {"type": "m.id.user", "user": "alice"},
    "password": "oldpw",
    "session": "s1",
}, "UIA auth: OLD password + server session")

# refusal path: non-401 error is surfaced, not retried
calls = []


def fake_refuse(method, url, headers=None, data=None, timeout=30):
    calls.append(1)
    return 403, b'{"error":"no"}'


enroll._request = fake_refuse
try:
    raises(lambda: enroll._change_password("http://cs", "t", "alice", "o", "n"),
           "non-401 refusal raises")
finally:
    enroll._request = orig_request
check(len(calls) == 1, "refusal not retried")

print("%d passed, %d failed" % (_pass, _fail))
if _fail:
    for f in _failures:
        sys.stderr.write("  FAIL: %s\n" % f)
    sys.exit(1)
