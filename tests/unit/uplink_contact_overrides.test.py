#!/usr/bin/env python3
"""Unit tests for the uplink half of the per-contact-share plan (C2).

The contact mirror is the path that carries ADDRESS-BOOK PII off the machine,
and this slice adds a second, more specific consent input to it. These tests
are the bounds on what a failed read of that input may do:

  C2/F5  a non-404 OVERRIDES read SKIPS the push leg entirely (pushed == 0)
         while tombstones still run — a transient error must never let a
         'private'-overridden contact fall back to a share-all source and push
         its PII; a 404 is "no overrides" and behaves exactly as before;
  P3     the tombstone leg runs off the LAST SUCCESSFULLY-READ map cached in
         state.db, so a read failure does not flap tombstone/re-push churn;
  P3     a non-404 POLICY read aborts the whole pass — collapsing it to
         global-private used to storm a full tombstone sweep on a blip;
  F10    the push leg re-reads the overrides map every OVERRIDE_RECHECK pushes
         and drops rows flipped to 'private' mid-pass;
  F9     no log line carries a network_id or an override key (both are PII).

Run: python3 tests/unit/uplink_contact_overrides.test.py
"""
import hashlib
import logging
import os
import sys
import tempfile
import types
import urllib.error

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "agents", "uplink"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "agents", "contacts"))
import consent          # noqa: E402
import contacts_store   # noqa: E402
import uplink           # noqa: E402

passed = 0
failed = 0
failures = []
LOG_LINES = []


class _Capture(logging.Handler):
    def emit(self, record):
        LOG_LINES.append(record.getMessage())


uplink.log.handlers = [_Capture()]
uplink.log.propagate = False
uplink.log.setLevel(logging.DEBUG)


def check(name, cond):
    global passed, failed
    if cond:
        passed += 1
    else:
        failed += 1
        failures.append(name)


LOCAL_USER = "@jkali:localhost"
ROOM = "!contacts:master"
NUMBERS = ["+15550000001", "+15550000002", "+15550000003"]

READ_404 = object()     # sentinel: absent account-data
READ_ERROR = object()   # sentinel: a non-404 failure


def make_uplink(policy, overrides, profiles=None, handles=NUMBERS,
                mirrored=(), cache=None):
    """A real state.db + a real contacts.db, with stubbed transports."""
    tmp = tempfile.mkdtemp(prefix="uplink-contacts-")
    u = object.__new__(uplink.Uplink)
    u.db_path = os.path.join(tmp, "state.db")
    u.db = uplink.Uplink._open_db(u.db_path)
    u.db.execute("INSERT OR REPLACE INTO meta (k,v) VALUES ('master_contacts_room',?)", (ROOM,))
    if cache is not None:
        u.db.execute("INSERT OR REPLACE INTO meta (k,v) VALUES (?,?)",
                     (uplink.Uplink.CONTACT_OVERRIDES_CACHE, cache))
    for (source, network_id, version) in mirrored:
        u.db.execute("INSERT OR REPLACE INTO contact_mirror "
                     "(source, network_id, mirrored_version, master_state_key) "
                     "VALUES (?,?,?,?)", (source, network_id, version, "sk"))
    u.db.commit()

    db_path = os.path.join(tmp, "contacts.db")
    conn = contacts_store.open_store(db_path)
    contacts_store.upsert_contacts(conn, "imessage", [
        {"network_id": n, "kind": "phone", "display_name": "C%d" % i}
        for i, n in enumerate(handles)])
    conn.close()

    u.cfg = types.SimpleNamespace(local_user=LOCAL_USER, contacts_db=db_path)
    u.reads = []
    u.puts = []

    def nf(path):
        return urllib.error.HTTPError(path, 404, "Not Found", None, None)

    def local(method, path, body=None, query=None, timeout=60):
        if method == "GET" and path.endswith("/account_data/com.jkali.master_link"):
            raise nf(path)  # legacy env pairing, independent of contact policy
        assert method == "GET", path
        if path.endswith("/account_data/" + uplink.CONTACT_PROFILES_TYPE):
            if profiles is READ_ERROR:
                raise urllib.error.URLError("boom")
            if profiles is None:
                raise nf(path)
            return profiles
        if path.endswith("/account_data/" + consent.CONTACT_SHARE_POLICY_TYPE):
            if policy is READ_ERROR:
                raise urllib.error.HTTPError(path, 500, "Server Error", None, None)
            if policy is READ_404:
                raise nf(path)
            return policy
        if path.endswith("/account_data/" + consent.CONTACT_OVERRIDES_TYPE):
            u.reads.append("overrides")
            if overrides is READ_ERROR:
                raise urllib.error.HTTPError(path, 500, "Server Error", None, None)
            if overrides is READ_404:
                raise nf(path)
            return overrides() if callable(overrides) else overrides
        raise AssertionError("unexpected local GET " + path)

    def master(method, path, body=None, query=None, timeout=60):
        assert method == "PUT", path
        u.puts.append((path, body))
        return {}

    u.local = local
    u.master = master
    return u


SHARE_ALL_IMSG = {"global": "private", "sources": {"imessage": "share-all"}}
PRIVATE = {"global": "private", "sources": {}}


def state_key(source, network_id):
    """The same sha1(source|network_id) _put_contact uses; a tombstone body
    carries only {deleted: true}, so the PATH is what identifies the handle."""
    return hashlib.sha1((source + "|" + network_id).encode("utf-8")).hexdigest()


def tombstoned_handles(u, source="imessage"):
    keys = {state_key(source, n): n for n in NUMBERS}
    keys[state_key(source, "gone-handle")] = "gone-handle"
    out = []
    for (path, body) in u.puts:
        if body.get("deleted"):
            out.append(keys.get(path.rsplit("/", 1)[-1], path.rsplit("/", 1)[-1]))
    return out


def pushed_and_tombstoned(u):
    pushes = [b for (_, b) in u.puts if not b.get("deleted")]
    tombs = [b for (_, b) in u.puts if b.get("deleted")]
    return len(pushes), len(tombs)


# --- 404 overrides: today's per-source behavior, unchanged ------------------
u = make_uplink(SHARE_ALL_IMSG, READ_404)
u.mirror_contacts()
check("404 overrides -> per-source backfill unchanged", pushed_and_tombstoned(u) == (3, 0))

# --- override 'private' withholds exactly one contact from a shared source --
u = make_uplink(SHARE_ALL_IMSG,
                {"overrides": {"imessage|" + NUMBERS[1]: "private"}})
u.mirror_contacts()
pushed = [b["network_id"] for (_, b) in u.puts if not b.get("deleted")]
check("override private withholds exactly that contact",
      sorted(pushed) == sorted([NUMBERS[0], NUMBERS[2]]))

# --- override 'share' shares exactly one contact from a private source ------
u = make_uplink(PRIVATE, {"overrides": {"imessage|" + NUMBERS[0]: "share"}})
u.mirror_contacts()
pushed = [b["network_id"] for (_, b) in u.puts if not b.get("deleted")]
check("override share in a private source pushes just that contact",
      pushed == [NUMBERS[0]])

# --- ACCEPTANCE (C2): a non-404 overrides read => pushed == 0, tombstones run
u = make_uplink(SHARE_ALL_IMSG, READ_ERROR,
                mirrored=[("imessage", "gone-handle", 1)])
u.mirror_contacts()
n_push, n_tomb = pushed_and_tombstoned(u)
check("overrides read error -> ZERO pushes that pass", n_push == 0)
check("overrides read error -> tombstones still applied", n_tomb == 1)
check("overrides read error -> the tombstoned mirror row is dropped",
      u.db.execute("SELECT count(*) FROM contact_mirror").fetchone()[0] == 0)

# --- P3: the tombstone leg uses the CACHED map, not an empty one ------------
# A contact overridden 'private' is already tombstoned; with the cache in place
# a failing read must not re-push it, and must not re-tombstone the rest.
u = make_uplink(SHARE_ALL_IMSG, READ_ERROR,
                mirrored=[("imessage", NUMBERS[0], 1), ("imessage", NUMBERS[1], 2)],
                cache='{"imessage|%s": "private"}' % NUMBERS[0])
u.mirror_contacts()
check("cached overrides drive the tombstone leg during a read failure",
      tombstoned_handles(u) == [NUMBERS[0]])

# --- P3: a non-404 POLICY read aborts the pass (no tombstone storm) ---------
u = make_uplink(READ_ERROR, READ_404,
                mirrored=[("imessage", NUMBERS[0], 1), ("imessage", NUMBERS[1], 2)])
u.mirror_contacts()
check("policy read error -> no PUTs at all", u.puts == [])
check("policy read error -> mirror table untouched",
      u.db.execute("SELECT count(*) FROM contact_mirror").fetchone()[0] == 2)
check("policy read error -> overrides are never even read", u.reads == [])

# --- F10: the push leg re-reads mid-pass and drops a mid-pass 'private' -----
uplink.Uplink.OVERRIDE_RECHECK = 1          # one push between samples, for the test
flips = {"n": 0}


def flipping():
    flips["n"] += 1
    if flips["n"] == 1:
        return {"overrides": {}}
    return {"overrides": {"imessage|" + NUMBERS[2]: "private"}}


u = make_uplink(SHARE_ALL_IMSG, flipping)
u.mirror_contacts()
pushed = [b["network_id"] for (_, b) in u.puts if not b.get("deleted")]
check("F10: a mid-pass flip to private drops the remaining row",
      NUMBERS[2] not in pushed and len(pushed) == 2)
uplink.Uplink.OVERRIDE_RECHECK = 50

# --- F9: no PII in any log line --------------------------------------------
joined = "\n".join(LOG_LINES)
check("F9: no network_id in any log line", not any(n in joined for n in NUMBERS))
check("F9: no override key in any log line", "imessage|" not in joined)

print("\n%d passed, %d failed" % (passed, failed))
if failed:
    sys.stderr.write("\nFailures:\n")
    for f in failures:
        sys.stderr.write("  - " + f + "\n")
    sys.exit(1)
