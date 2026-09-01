#!/usr/bin/env python3
"""Unit tests for master/enroll.py delete_teammate guards and roster helpers.

The I/O (Synapse deactivate / leave) is stubbed. These cases hold the
authorization + username gate still: manager-only, reserved localpart,
unknown teammate, roster-remove.

Run: python3 tests/unit/enroll_delete_teammate.test.py
"""
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "..", "master"))
import enroll  # noqa: E402

_pass = 0
_fail = 0
_failures = []


def ok(cond, label):
    global _pass, _fail
    if cond:
        _pass += 1
    else:
        _fail += 1
        _failures.append(label)


# ---- _roster_remove is pure -------------------------------------------------
ok(enroll._roster_remove("alice bob trialtop", "trialtop") == "alice bob",
   "roster_remove: drops the name, keeps the rest")
ok(enroll._roster_remove("trialtop", "trialtop") == "",
   "roster_remove: last name leaves empty roster")
ok(enroll._roster_remove("alice bob", "carol") == "alice bob",
   "roster_remove: missing name is a no-op")
ok(enroll._roster_remove("", "alice") == "",
   "roster_remove: empty roster stays empty")
ok(enroll._roster_remove("alice alice", "alice") == "",
   "roster_remove: drops every copy")


# ---- _burn_teammate_codes ---------------------------------------------------
def _with_store(codes):
    fd, path = tempfile.mkstemp(prefix="enroll-del-", suffix=".json")
    os.close(fd)
    os.environ["ENROLL_STORE"] = path
    enroll._save_store({"codes": codes})
    return path


path = _with_store({
    "aaa": {"teammate": "trialtop", "used_at": None},
    "bbb": {"teammate": "jkali", "used_at": None},
    "ccc": {"teammate": "trialtop", "used_at": 1},
})
try:
    enroll._burn_teammate_codes("trialtop")
    leftover = enroll._load_store()["codes"]
    ok(list(leftover.keys()) == ["bbb"],
       "burn_teammate_codes: drops every code for that teammate, keeps others")
finally:
    os.remove(path)
    os.environ.pop("ENROLL_STORE", None)


# ---- delete_teammate: gate before any I/O ----------------------------------
_calls = []


def _no_io(*a, **k):
    _calls.append((a, k))
    raise AssertionError("I/O should not run on a refused delete")


def _stub_gate(manager_mxid="@manager:master", who="@manager:master", known=None):
    enroll._manager_mxid = lambda: manager_mxid
    enroll._whoami = lambda cs_base, token: who
    enroll._cs_base = lambda: "http://127.0.0.1:8018"
    enroll.known_teammates = lambda: list(known or [])
    enroll._try_login = _no_io
    enroll._login = _no_io
    enroll.derive_password = _no_io
    enroll._deactivate_account = _no_io
    enroll._leave_teammate_rooms = _no_io


def expect_enroll_error(username, frag, label, known=None, who="@manager:master"):
    _stub_gate(known=known, who=who)
    try:
        enroll.delete_teammate("tok", username)
        ok(False, "%s: expected EnrollError" % label)
    except enroll.EnrollError as e:
        ok(frag in str(e), "%s: %r" % (label, e))
    except Exception as e:  # noqa: BLE001
        ok(False, "%s: expected EnrollError, got %r" % (label, e))


_stub_gate()
try:
    enroll.delete_teammate(None, "trialtop")
    ok(False, "missing token: expected HttpError")
except enroll.HttpError as e:
    ok(e.status == 401, "missing token is 401")

_stub_gate(who=None)
try:
    enroll.delete_teammate("tok", "trialtop")
    ok(False, "invalid token: expected HttpError")
except enroll.HttpError as e:
    ok(e.status == 401, "invalid token is 401")

_stub_gate(who="@alice:master")
try:
    enroll.delete_teammate("tok", "trialtop")
    ok(False, "teammate caller: expected HttpError")
except enroll.HttpError as e:
    ok(e.status == 403, "teammate caller is 403")

expect_enroll_error("manager", "reserved", "cannot delete the manager localpart",
                    known=["manager"])
expect_enroll_error("Nope!", "invalid username", "charset is closed")
expect_enroll_error("trialtop", "unprovisioned", "unknown teammate refused",
                    known=["jkali"])
ok(len(_calls) == 0, "refused deletes never touch login/deactivate/leave")


print("enroll_delete_teammate: %d passed, %d failed" % (_pass, _fail))
for f in _failures:
    print("  FAIL: " + f)
sys.exit(1 if _fail else 0)
