#!/usr/bin/env python3
"""Unit test for master/enroll.py's _require_manager authorization guard.

Regression for AUDIT-FINDINGS F4 / SIMPLIFICATION-PLAN P5: the guard used to be
`if who != manager or who != "@manager:master":`, a tautology true for every
`who` — so it rejected everyone the moment the manager was renamed, and only
appeared to work under the default naming. The guard must accept EXACTLY the
configured manager mxid and reject every other caller.

_require_manager itself does I/O (whoami over the CS API, reading tokens.local),
so we stub the two seams it depends on — `_whoami` (the caller's resolved mxid)
and `_manager_mxid` (the configured manager) — and assert the pure decision.

Run: python3 tests/unit/enroll_require_manager.test.py  (exit 0 = all pass).
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "..", "master"))
import enroll  # noqa: E402

_pass = 0
_fail = 0
_failures = []


def _stub(manager_mxid, caller_mxid):
    """Point _require_manager at a fixed configured manager + a fixed caller."""
    enroll._manager_mxid = lambda: manager_mxid
    enroll._whoami = lambda cs_base, token: caller_mxid
    # keep _cs_base cheap and offline
    enroll._cs_base = lambda: "http://127.0.0.1:8018"


def expect_accept(manager_mxid, caller_mxid, label):
    global _pass, _fail
    _stub(manager_mxid, caller_mxid)
    try:
        got = enroll._require_manager("tok")
        if got == manager_mxid:
            _pass += 1
        else:
            _fail += 1
            _failures.append("%s: returned %r, expected %r" % (label, got, manager_mxid))
    except Exception as e:  # noqa: BLE001
        _fail += 1
        _failures.append("%s: expected accept, raised %r" % (label, e))


def expect_reject(manager_mxid, caller_mxid, status, label):
    global _pass, _fail
    _stub(manager_mxid, caller_mxid)
    try:
        enroll._require_manager("tok")
        _fail += 1
        _failures.append("%s: expected reject, was accepted" % label)
    except enroll.HttpError as e:
        if e.status == status:
            _pass += 1
        else:
            _fail += 1
            _failures.append("%s: expected HTTP %d, got %d" % (label, status, e.status))
    except Exception as e:  # noqa: BLE001
        _fail += 1
        _failures.append("%s: expected HttpError, got %r" % (label, e))


# --- the real manager is accepted, at the default AND a renamed identity ------
expect_accept("@manager:master", "@manager:master", "default manager accepted")
expect_accept("@boss:corp.example", "@boss:corp.example",
              "renamed manager accepted (the F4 regression)")

# --- everyone else is rejected 403 --------------------------------------------
expect_reject("@manager:master", "@alice:master", 403, "teammate rejected")
expect_reject("@boss:corp.example", "@manager:master", 403,
              "old default mxid rejected once manager renamed")
expect_reject("@manager:master", "@manager:evil", 403, "look-alike server rejected")

# --- an unresolvable / invalid token is 401 (whoami returns None) -------------
_stub("@manager:master", None)
try:
    enroll._require_manager("tok")
    _fail += 1
    _failures.append("invalid token: expected reject, was accepted")
except enroll.HttpError as e:
    if e.status == 401:
        _pass += 1
    else:
        _fail += 1
        _failures.append("invalid token: expected HTTP 401, got %d" % e.status)

# --- a missing bearer token is 401 --------------------------------------------
try:
    enroll._require_manager(None)
    _fail += 1
    _failures.append("missing token: expected reject, was accepted")
except enroll.HttpError as e:
    if e.status == 401:
        _pass += 1
    else:
        _fail += 1
        _failures.append("missing token: expected HTTP 401, got %d" % e.status)


print("enroll_require_manager: %d passed, %d failed" % (_pass, _fail))
for f in _failures:
    print("  FAIL: " + f)
sys.exit(1 if _fail else 0)
