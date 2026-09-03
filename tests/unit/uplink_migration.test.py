#!/usr/bin/env python3
"""Unit tests for D0 — the one-time migration to explicit per-conversation levels.

Conversation sharing became EXPLICIT-ONLY (no inheritance from a contact
profile / per-source policy / global Share-All). Without a migration, the first
reconcile after the upgrade would resolve every standing-policy-shared room
private and REVOKE it. D0 materializes those rooms as explicit 'share'
overrides first. The ordering is the whole point, so it is what these tests
assert hardest:

  - S1 acceptance: a pre-S1 state.db with a standing-policy-shared, currently
    mirrored room ends up with an explicit `migrated: true` 'share' override AND
    the SAME master_room_id in mirror_rooms — no delete/re-create, and ZERO
    delete_mirror calls observed anywhere in the pass;
  - the migration runs once (flag in state.db) and is idempotent;
  - an existing explicit override is never rewritten;
  - a room that is NOT currently mirrored is not migrated (D0 materializes what
    is actually being shared, it does not widen);
  - a failed account-data write aborts the pass BEFORE the flag is set and
    BEFORE any deletion is evaluated;
  - F8 pin: under the OLD resolver a 'direct' override behaves as INHERIT, not
    as private — so a partial rollback to old code is caught by a red test
    rather than by a surprise in production.

Run: python3 tests/unit/uplink_migration.test.py
"""
import os
import sys
import tempfile
import types
import urllib.error

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "agents", "uplink"))
import consent   # noqa: E402
import uplink    # noqa: E402

passed = 0
failed = 0


def check(name, cond):
    global passed, failed
    if cond:
        passed += 1
    else:
        failed += 1
        print("FAIL: " + name)


SPACE = "!space:localhost"
LOCAL_USER = "@jkali:localhost"
OVERRIDE_PATH_FRAG = "/account_data/" + consent.SHARE_OVERRIDE_TYPE
MODEL_PATH_FRAG = "/account_data/" + consent.CONSENT_MODEL_TYPE


def make_sync(room_ids, overrides=None):
    """A /sync snapshot: one iMessage source space with `room_ids` as children."""
    overrides = overrides or {}
    events = [{"type": "m.room.name", "content": {"name": "iMessage"}}]
    for rid in room_ids:
        events.append({"type": "m.space.child", "state_key": rid,
                       "content": {"via": ["localhost"]}})
    join = {SPACE: {"state": {"events": events}}}
    for rid in room_ids:
        ad = []
        if rid in overrides:
            ad.append({"type": consent.SHARE_OVERRIDE_TYPE,
                       "content": {"state": overrides[rid]}})
        join[rid] = {"state": {"events": [{"type": "m.room.name",
                                           "content": {"name": "conversation"}}]},
                     "account_data": {"events": ad}}
    return {"rooms": {"join": join}}


def make_uplink(mirrors=(), policy=None, profiles=None, sync=None, migrated=False,
                fail_override_write=False):
    """A real state.db (pre-S1 schema — S1 adds no tables) + a fake transport."""
    u = object.__new__(uplink.Uplink)
    path = os.path.join(tempfile.mkdtemp(prefix="uplink-mig-"), "state.db")
    u.db = uplink.Uplink._open_db(path)
    for local_id, master_id in mirrors:
        u.db.execute("INSERT INTO mirror_rooms (local_room_id, master_room_id, source, "
                     "last_synced_pos) VALUES (?,?,?,?)", (local_id, master_id, "imessage", None))
    if migrated:
        u.db.execute("INSERT INTO meta (k,v) VALUES (?,?)", (uplink.MIGRATED_FLAG, "1"))
    u.db.commit()
    u.cfg = types.SimpleNamespace(local_user=LOCAL_USER)
    u.self_mxids = set()
    u._last_sourceless = None
    u.puts = []          # (path, body) for every account-data write
    u.deleted = []       # every delete_mirror target — must stay empty pre-flag
    u.created = []
    u.synced = []
    u.stamped = []       # D2b share-level (re-)stamps; master-side, stubbed here

    def local(method, path, body=None, query=None, timeout=60):
        if method == "GET":
            if path.endswith("/account_data/" + consent.SHARE_POLICY_TYPE):
                if policy is None:
                    raise urllib.error.HTTPError(path, 404, "Not Found", None, None)
                return policy
            if path.endswith("/account_data/" + uplink.CONTACT_PROFILES_TYPE):
                if profiles is None:
                    raise urllib.error.HTTPError(path, 404, "Not Found", None, None)
                return profiles
            if path.endswith("/account_data/" + uplink.SELF_IDENTITIES_TYPE):
                raise urllib.error.HTTPError(path, 404, "Not Found", None, None)
            if path.endswith("/sync"):
                return sync or {"rooms": {"join": {}}}
            raise urllib.error.HTTPError(path, 404, "Not Found", None, None)
        if method == "PUT":
            if fail_override_write and OVERRIDE_PATH_FRAG in path:
                raise urllib.error.HTTPError(path, 403, "Forbidden", None, None)
            u.puts.append((path, body))
            return {}
        raise AssertionError("unexpected %s %s" % (method, path))

    u.local = local
    # Master-side effects are stubbed: this slice must not touch the master at
    # all during the migration, and `deleted` staying empty is the acceptance.
    u.create_mirror = lambda rid, *a, **kw: u.created.append(rid)
    u.delete_mirror = lambda rid: u.deleted.append(rid)
    u.stamp_share_level = lambda rid, level: u.stamped.append((rid, level))
    u.sync_mirror_name = lambda rid, name: None
    u.sync_room = lambda rid: u.synced.append(rid)
    return u


def override_puts(u):
    return [(p, b) for p, b in u.puts if OVERRIDE_PATH_FRAG in p]


def mirror_row(u, rid):
    return u.db.execute("SELECT master_room_id FROM mirror_rooms WHERE local_room_id=?",
                        (rid,)).fetchone()


# ---------------------------------------------------------------------------
# S1 ACCEPTANCE: standing-policy-shared + currently mirrored -> explicit
# migrated 'share', same mirror, zero delete_mirror calls.
# ---------------------------------------------------------------------------
CONV = "!conv1:localhost"
MASTER = "!master1:master"

u = make_uplink(mirrors=[(CONV, MASTER)],
                policy={"global": "share-all", "sources": {}},
                sync=make_sync([CONV]))
u.reconcile()

check("acceptance: zero delete_mirror calls in the migrating pass", u.deleted == [])
check("acceptance: mirror not re-created", u.created == [])
check("acceptance: the SAME master_room_id survives", mirror_row(u, CONV) == (MASTER,))
ops = override_puts(u)
check("acceptance: exactly one override written", len(ops) == 1)
check("acceptance: written into THIS room's account-data",
      ops and "/rooms/%21conv1%3Alocalhost" + OVERRIDE_PATH_FRAG in ops[0][0])
check("acceptance: content is an explicit share stamped migrated:true",
      ops and ops[0][1] == {"state": "share", "migrated": True})
check("acceptance: consent-model marker written",
      any(MODEL_PATH_FRAG in p and b == {"version": 2} for p, b in u.puts))
check("acceptance: migration flag set", u.meta_get(uplink.MIGRATED_FLAG) == "1")
check("acceptance: the kept room is still tailed", u.synced == [CONV])

# The migrated override is what keeps the room shared under the NEW resolver.
check("acceptance: the materialized override resolves shared under the new model",
      consent.effective_shared({"id": CONV, "sourceId": "imessage"},
                               {"global": "private", "sources": {}}, "share") is True)

# ---------------------------------------------------------------------------
# Idempotency: a second pass migrates nothing and still deletes nothing.
# ---------------------------------------------------------------------------
before = len(u.puts)
u.local = (lambda method, path, body=None, query=None, timeout=60:
           make_sync([CONV], {CONV: "share"}) if path.endswith("/sync")
           else (_ for _ in ()).throw(urllib.error.HTTPError(path, 404, "nf", None, None)))
u.reconcile()
check("idempotent: no further account-data writes", len(u.puts) == before)
check("idempotent: still zero delete_mirror calls", u.deleted == [])
check("idempotent: mirror still intact", mirror_row(u, CONV) == (MASTER,))

# ---------------------------------------------------------------------------
# An EXISTING explicit override is the teammate's own decision: never rewritten
# (and never stamped migrated:true, so it stays out of the review list).
# ---------------------------------------------------------------------------
u = make_uplink(mirrors=[(CONV, MASTER)],
                policy={"global": "share-all", "sources": {}},
                sync=make_sync([CONV], {CONV: "share"}))
u.reconcile()
check("existing explicit override is not rewritten", override_puts(u) == [])
check("existing explicit override: still no deletions", u.deleted == [])
check("existing explicit override: marker still written",
      any(MODEL_PATH_FRAG in p for p, b in u.puts))

# ---------------------------------------------------------------------------
# NOT currently mirrored -> not migrated. D0 materializes what is actually
# being shared; it must not widen a standing policy into new explicit shares.
# ---------------------------------------------------------------------------
OTHER = "!conv2:localhost"
u = make_uplink(mirrors=[(CONV, MASTER)],
                policy={"global": "share-all", "sources": {}},
                sync=make_sync([CONV, OTHER]))
u.reconcile()
paths = [p for p, _ in override_puts(u)]
check("unmirrored room is NOT migrated",
      len(paths) == 1 and "%21conv2" not in paths[0])
check("unmirrored room gets no new mirror either (explicit model: private)",
      u.created == [])

# ---------------------------------------------------------------------------
# The profile level counted under the OLD rules, so a room shared only via a
# shared contact profile is migrated too.
# ---------------------------------------------------------------------------
u = make_uplink(mirrors=[(CONV, MASTER)],
                policy={"global": "private", "sources": {}},
                profiles={"profiles": [{"id": "p1", "displayName": "D",
                                        "roomIds": [CONV], "share": "share"}]},
                sync=make_sync([CONV]))
u.reconcile()
check("profile-shared room is migrated to an explicit share",
      len(override_puts(u)) == 1
      and override_puts(u)[0][1] == {"state": "share", "migrated": True})
check("profile-shared room: no deletions", u.deleted == [])

# ---------------------------------------------------------------------------
# A mirrored room the OLD resolver does NOT share is not migrated — and IS
# deleted, but only AFTER the migration completed (deletions under the new
# rules are still correct; they just never race the migration).
# ---------------------------------------------------------------------------
u = make_uplink(mirrors=[(CONV, MASTER)],
                policy={"global": "private", "sources": {"imessage": "private-all"}},
                sync=make_sync([CONV]))
u.reconcile()
check("not-shared-under-old-rules room is not migrated", override_puts(u) == [])
check("not-shared-under-old-rules room is revoked after the flag is set",
      u.deleted == [CONV] and u.meta_get(uplink.MIGRATED_FLAG) == "1")

# ---------------------------------------------------------------------------
# A failed override write aborts the pass BEFORE the flag and BEFORE any
# deletion — the fail-closed ordering the whole design rests on.
# ---------------------------------------------------------------------------
u = make_uplink(mirrors=[(CONV, MASTER)],
                policy={"global": "share-all", "sources": {}},
                sync=make_sync([CONV]),
                fail_override_write=True)
raised = False
try:
    u.reconcile()
except urllib.error.HTTPError:
    raised = True
check("write failure propagates out of reconcile", raised)
check("write failure: migration flag NOT set", u.meta_get(uplink.MIGRATED_FLAG) is None)
check("write failure: zero delete_mirror calls", u.deleted == [])
check("write failure: mirror untouched", mirror_row(u, CONV) == (MASTER,))
check("write failure: no model marker written (UI keeps the old surface)",
      not any(MODEL_PATH_FRAG in p for p, _ in u.puts))

# ---------------------------------------------------------------------------
# write_share_override input gates
# ---------------------------------------------------------------------------
u = make_uplink()
for bad in ("not-a-room", "", None, "@user:localhost"):
    try:
        u.write_share_override(bad, "share")
        check("write_share_override refuses room id %r" % (bad,), False)
    except ValueError:
        check("write_share_override refuses room id %r" % (bad,), True)
for bad in ("share-all", "inherit", "", None, "SHARE"):
    try:
        u.write_share_override(CONV, bad)
        check("write_share_override refuses level %r" % (bad,), False)
    except ValueError:
        check("write_share_override refuses level %r" % (bad,), True)
u.write_share_override(CONV, "direct")
check("write_share_override accepts the three levels and stamps nothing extra",
      u.puts and u.puts[-1][1] == {"state": "direct"})

# ---------------------------------------------------------------------------
# F8 PIN — the retained OLD resolver's treatment of 'direct'.
# Old code recognized only 'share'/'private', so a stored 'direct' fell through
# to profile/source/global (INHERIT), it was NOT private. That is exactly what a
# partial rollback to pre-S1 code would do, and the Rollback section of the plan
# depends on it being true. If someone "fixes" the legacy helper to treat
# 'direct' as private, this test goes red before the surprise ships.
# ---------------------------------------------------------------------------
C = {"id": CONV, "sourceId": "imessage", "sourceLabel": "iMessage"}
leg = uplink.legacy_effective_shared
check("OLD model: 'direct' inherits a global share-all (NOT private)",
      leg(C, {"global": "share-all", "sources": {}}, "direct") is True)
check("OLD model: 'direct' inherits a global private",
      leg(C, {"global": "private", "sources": {}}, "direct") is False)
check("OLD model: 'direct' inherits a per-source share-all",
      leg(C, {"global": "private", "sources": {"imessage": "share-all"}}, "direct") is True)
check("OLD model: 'direct' inherits a per-source private-all",
      leg(C, {"global": "share-all", "sources": {"imessage": "private-all"}}, "direct") is False)
check("OLD model: 'direct' inherits a shared profile",
      leg(C, {"global": "private", "sources": {}}, "direct",
          {"displayName": "D", "share": "share"}) is True)
check("OLD model: 'direct' inherits a private profile even under share-all",
      leg(C, {"global": "share-all", "sources": {}}, "direct",
          {"displayName": "D", "share": "private"}) is False)
# ... while the NEW model says 'direct' is shared no matter what else is set:
check("NEW model: 'direct' is shared regardless of policy/profile",
      consent.effective_shared(C, {"global": "private", "sources": {"imessage": "private-all"}},
                               "direct", {"displayName": "D", "share": "private"}) is True)
check("the two models genuinely disagree on 'direct' (rollback caveat is real)",
      leg(C, {"global": "private", "sources": {}}, "direct") is not
      consent.effective_shared(C, {"global": "private", "sources": {}}, "direct"))
# 'share'/'private' mean the same thing in both models — that is what makes the
# materialized overrides safe to leave behind on a rollback.
for st, want in (("share", True), ("private", False)):
    check("both models agree on %r" % st,
          leg(C, {"global": "share-all", "sources": {"imessage": "private-all"}}, st) is want
          and consent.effective_shared(C, {"global": "share-all",
                                           "sources": {"imessage": "private-all"}}, st) is want)
# An UNRECOGNIZED value inherits under the old model but is private under the
# new one — the direction that matters (new model never widens on junk).
check("OLD model inherits on junk; NEW model is private on junk",
      leg(C, {"global": "share-all", "sources": {}}, "junk") is True
      and consent.effective_shared(C, {"global": "share-all", "sources": {}}, "junk") is False)

print("%d passed, %d failed" % (passed, failed))
sys.exit(1 if failed else 0)
