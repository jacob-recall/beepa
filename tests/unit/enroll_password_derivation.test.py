#!/usr/bin/env python3
"""Unit tests for master/enroll.py's password handling: derivation,
manager override resolution/migration, key-file failure modes, and the
password-change request shape (logout_devices MUST be False).

Run: python3 tests/unit/enroll_password_derivation.test.py  (exit 0 = pass).
"""
import json
import os
import re
import sys
import tempfile
from unittest.mock import patch

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

# --- Manager override stays local to the manager and survives provisioning.
# Every file and credential below is synthetic; transports are stubbed.
with tempfile.TemporaryDirectory() as override_dir:
    secrets_path = os.path.join(override_dir, "secrets.local")
    override_path = os.path.join(override_dir, "manager-password.local")
    with open(secrets_path, "w") as f:
        f.write("TEAMMATE_PASSWORD_KEY='%s'\n" % KEY_A.decode("ascii"))
    with patch.multiple(enroll, SECRETS_FILE=secrets_path,
                        MANAGER_PASSWORD_FILE=override_path,
                        STATE_FILE=os.path.join(override_dir, "state.local")):
        manager_derived = enroll.derive_password("manager", "manager")
        teammate_derived = enroll.derive_password("teammate", "alice")
        check(enroll.account_password("manager", "manager") == manager_derived,
              "missing manager override retains derived password")

        override_password = "custom 'quoted' \\ password"
        with open(override_path, "w") as f:
            json.dump(override_password, f)
        check(enroll.account_password("manager", "manager") == override_password,
              "manager override preserves the exact JSON string")
        check(enroll.account_password("teammate", "alice") == teammate_derived,
              "manager override does not change teammate password")
        check(enroll.derive_password("manager", "manager") == manager_derived,
              "pure derivation remains available for migration")
        raises(lambda: enroll.account_password("manager", "alice"),
               "manager override does not bypass localpart validation")
        with patch.object(enroll, "SECRETS_FILE", os.path.join(override_dir, "missing")):
            raises(lambda: enroll.account_password("manager", "manager"),
                   "manager override does not bypass missing authority key")

        for invalid in ["not JSON", '""', "null", "42", "false", "[]", "{}"]:
            with open(override_path, "w") as f:
                f.write(invalid)
            raises(lambda: enroll.account_password("manager", "manager"),
                   "invalid manager override is refused: " + invalid)
        check(enroll.account_password("teammate", "alice") == teammate_derived,
              "malformed manager override does not block teammate password")

        with open(override_path, "w") as f:
            json.dump(override_password, f)
        server = {"password": manager_derived}
        attempts, registrations, rotations = [], [], []

        def fake_register(cs_base, secret, localpart, password):
            registrations.append((localpart, password))
            return False  # The account already exists.

        def fake_try_login(cs_base, localpart, password):
            attempts.append(password)
            return "synthetic-manager-token" if password == server["password"] else None

        def fake_login(cs_base, localpart, password):
            token = fake_try_login(cs_base, localpart, password)
            if token is None:
                raise enroll.EnrollError("synthetic password login refused")
            return token

        def fake_change(cs_base, token, localpart, old_password, new_password):
            check(old_password == server["password"],
                  "migration authenticates with the current server password")
            rotations.append((localpart, old_password, new_password))
            server["password"] = new_password

        with patch.multiple(enroll,
                            _cs_base=lambda: "http://synthetic.invalid",
                            _shared_secret=lambda: "synthetic-shared-secret",
                            _server_name=lambda: "synthetic",
                            _register_account=fake_register,
                            _try_login=fake_try_login,
                            _login=fake_login,
                            _change_password=fake_change):
            first_provision = enroll.provision_account("manager", manager=True)
            check(first_provision == {"mxid": "@manager:synthetic",
                                      "token": "synthetic-manager-token",
                                      "migrated": True},
                  "derived manager account migrates to configured password")
            check(attempts == [override_password, manager_derived, override_password],
                  "migration tries configured then current derived, and verifies new password")
            check(rotations == [("manager", manager_derived, override_password)],
                  "manager migration changes only to the configured password")
            check(registrations == [("manager", override_password)],
                  "registration uses configured manager password")

            attempts.clear()
            second_provision = enroll.provision_account("manager", manager=True)
            check(second_provision["migrated"] is False,
                  "repeated manager provisioning reports no migration")
            check(attempts == [override_password] and len(rotations) == 1,
                  "repeated provisioning keeps custom password without another rotation")

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
