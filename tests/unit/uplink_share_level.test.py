#!/usr/bin/env python3
"""Unit tests for D2b — per-mirror com.jkali.share_level stamping (S3).

`desired_shared()` now carries the per-room LEVEL ('private'|'share'|'direct')
instead of a bool, so reconcile can stamp each mirror with the level the master
console reads (D4: 'direct' labels its affordance "Send", everything else
"Propose"). Two things must stay true:

  - the MIRRORING decision is unchanged. It is still exactly "level is a
    sharing level", still sourced from consent.effective_shared(), and a
    non-level string ('private' included, since it is a non-empty string and
    would be truthy) must never read as shared;
  - the stamp is a DIFF, not a per-pass write: it is re-PUT when a kept
    mirror's level flips in EITHER direction (promotion share->direct and
    demotion direct->share), plus once to backfill a mirror created before the
    stamp existed, and never otherwise.

The stamp is cosmetic on the master side — the authorization for an auto-send
is the teammate-side point-read in D2.5, never this state event — so a stale or
missing stamp may only ever under-promise.

Run: python3 tests/unit/uplink_share_level.test.py
"""
import logging
import os
import sys
import tempfile
import types
import urllib.error

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "agents", "uplink"))
import consent     # noqa: E402
import reconcile   # noqa: E402
import uplink      # noqa: E402

passed = 0
failed = 0

# The daemon's expected warnings (a deliberately failing re-stamp below) are
# not test output — swallow them rather than print them mid-run.
uplink.log.handlers = [logging.NullHandler()]
uplink.log.propagate = False


def check(name, cond):
    global passed, failed
    if cond:
        passed += 1
    else:
        failed += 1
        print("FAIL: " + name)


SPACE = "!space:localhost"
LOCAL_USER = "@jkali:localhost"
A = "!a:localhost"
B = "!b:localhost"

# ---------------------------------------------------------------------------
# level_is_shared — the fail-closed replacement for a bare truthiness test
# ---------------------------------------------------------------------------
for level, want in (("share", True), ("direct", True), ("private", False),
                    ("", False), ("SHARE", False), ("junk", False), ("inherit", False),
                    (True, True), (False, False), (None, False)):
    check("level_is_shared(%r) is %s" % (level, want),
          reconcile.level_is_shared(level) is want)
check("level_is_shared agrees with the consent resolver on every level",
      all(reconcile.level_is_shared(lv)
          is consent.effective_shared({"id": A, "sourceId": "imessage"},
                                      {"global": "private", "sources": {}}, lv)
          for lv in ("share", "direct", "private", "junk")))

# ---------------------------------------------------------------------------
# reconcile_decisions over LEVELS
# ---------------------------------------------------------------------------
plan = reconcile.reconcile_decisions(
    {A: "direct", B: "private", "!c:localhost": "share"}, [B, "!c:localhost"])
check("levels: 'direct' creates", plan["create"] == [A])
check("levels: 'private' deletes an existing mirror", plan["delete"] == [B])
check("levels: 'share' keeps", plan["keep"] == ["!c:localhost"])
check("levels: a junk level is NOT shared (deletes rather than keeps)",
      reconcile.reconcile_decisions({A: "junk"}, [A])["delete"] == [A])

# ---------------------------------------------------------------------------
# plan_level_restamp — pure diff, both directions
# ---------------------------------------------------------------------------
prs = reconcile.plan_level_restamp
check("restamp: promotion share -> direct",
      prs({A: "direct"}, {A: "share"}, [A]) == [(A, "direct")])
check("restamp: demotion direct -> share",
      prs({A: "share"}, {A: "direct"}, [A]) == [(A, "share")])
check("restamp: unchanged level writes nothing",
      prs({A: "direct"}, {A: "direct"}, [A]) == [])
check("restamp: a mirror with no stamp yet is backfilled once",
      prs({A: "share"}, {A: None}, [A]) == [(A, "share")])
check("restamp: only KEPT mirrors are considered",
      prs({A: "direct", B: "direct"}, {A: "share", B: "share"}, [B]) == [(B, "direct")])
check("restamp: a room resolving private is never stamped",
      prs({A: "private"}, {A: "direct"}, [A]) == [])
check("restamp: a junk level is never stamped", prs({A: "junk"}, {A: None}, [A]) == [])
check("restamp: deterministic order",
      prs({A: "direct", B: "direct"}, {}, [B, A]) == [(A, "direct"), (B, "direct")])

# ---------------------------------------------------------------------------
# End to end through a real reconcile(): create stamps, flips re-stamp, and a
# steady state writes nothing.
# ---------------------------------------------------------------------------


def make_sync(levels):
    """A /sync snapshot: one iMessage source space whose children carry the
    given per-room share_override levels."""
    events = [{"type": "m.room.name", "content": {"name": "iMessage"}}]
    for rid in levels:
        events.append({"type": "m.space.child", "state_key": rid,
                       "content": {"via": ["localhost"]}})
    join = {SPACE: {"state": {"events": events}}}
    for rid, level in levels.items():
        join[rid] = {"state": {"events": [{"type": "m.room.name",
                                           "content": {"name": "conversation"}}]},
                     "account_data": {"events": [
                         {"type": consent.SHARE_OVERRIDE_TYPE,
                          "content": {"state": level}}]}}
    return {"rooms": {"join": join}}


def make_uplink(levels, path=None, mirrors=()):
    u = object.__new__(uplink.Uplink)
    u.db_path = path or os.path.join(tempfile.mkdtemp(prefix="uplink-level-"), "state.db")
    u.db = uplink.Uplink._open_db(u.db_path)
    for lid, mid, stamped in mirrors:
        u.db.execute("INSERT OR REPLACE INTO mirror_rooms (local_room_id, master_room_id, "
                     "source, last_synced_pos, stamped_level) VALUES (?,?,?,?,?)",
                     (lid, mid, "imessage", None, stamped))
    u.db.execute("INSERT OR REPLACE INTO meta (k,v) VALUES (?,'1')", (uplink.MIGRATED_FLAG,))
    u.db.commit()
    u.cfg = types.SimpleNamespace(local_user=LOCAL_USER, master_user="@alice:master",
                                  manager_mxid="@manager:master", master_space="!space:master")
    u.self_mxids = set()
    u._last_sourceless = None
    u.sync_data = make_sync(levels)
    u.state_puts = []      # (path, body) for every master state PUT
    u.created = []

    def local(method, path, body=None, query=None, timeout=60):
        if method == "GET":
            if path.endswith("/sync"):
                return u.sync_data
            raise urllib.error.HTTPError(path, 404, "Not Found", None, None)
        raise AssertionError("unexpected local %s %s" % (method, path))

    def master(method, path, body=None, query=None, timeout=60):
        if method == "PUT" and "/state/" in path:
            u.state_puts.append((path, body))
            return {}
        raise AssertionError("unexpected master %s %s" % (method, path))

    u.local = local
    u.master = master
    u.sync_room = lambda rid: None
    u.delete_mirror = lambda rid: None
    return u


def level_puts(u):
    return [(p, b) for p, b in u.state_puts if "/state/" + uplink.SHARE_LEVEL_TYPE in p]


def stamped(u, rid):
    r = u.db.execute("SELECT stamped_level FROM mirror_rooms WHERE local_room_id=?",
                     (rid,)).fetchone()
    return r[0] if r else None


# desired_shared carries levels, not bools.
u = make_uplink({A: "direct", B: "private"})
desired, _, _, _ = u.desired_shared(u.sync_data)
check("desired_shared returns the per-room LEVEL", desired == {A: "direct", B: "private"})
u = make_uplink({A: "junk"})
desired, _, _, _ = u.desired_shared(u.sync_data)
check("desired_shared collapses an unrecognized level to private",
      desired == {A: "private"})

# create stamps the level at room creation (initial_state, not a second PUT).
u = make_uplink({A: "direct"})
created = {}


def fake_create(rid, source, name, profile=None, level=None):
    created["level"] = level
    u.db.execute("INSERT OR REPLACE INTO mirror_rooms (local_room_id, master_room_id, "
                 "source, last_synced_pos, stamped_level) VALUES (?,?,?,?,?)",
                 (rid, "!m:master", source, None, level))
    u.db.commit()


u.create_mirror = fake_create
u.reconcile()
check("create_mirror is handed the resolved level", created.get("level") == "direct")
check("a freshly created mirror needs no separate stamp PUT", level_puts(u) == [])
check("the created mirror records its stamped level", stamped(u, A) == "direct")

# A kept mirror at the same level writes nothing.
u2 = make_uplink({A: "direct"}, path=u.db_path)
u2.reconcile()
check("steady state: no re-stamp for an unchanged level", level_puts(u2) == [])

# Demotion direct -> share re-stamps.
u3 = make_uplink({A: "share"}, path=u.db_path)
u3.reconcile()
check("demotion direct -> share re-stamps exactly once", len(level_puts(u3)) == 1)
path, body = level_puts(u3)[0]
check("the re-stamp is a state event on the MIRROR room with an empty state_key",
      path.endswith("/state/" + uplink.SHARE_LEVEL_TYPE) and "%21m%3Amaster" in path)
check("the re-stamp content is {'level': ...}", body == {"level": "share"})
check("the new level is recorded in state.db", stamped(u3, A) == "share")

# Promotion share -> direct re-stamps too.
u4 = make_uplink({A: "direct"}, path=u.db_path)
u4.reconcile()
check("promotion share -> direct re-stamps exactly once",
      len(level_puts(u4)) == 1 and level_puts(u4)[0][1] == {"level": "direct"})
check("promotion is recorded", stamped(u4, A) == "direct")

# A pre-D2b mirror (no stamp) is backfilled once, then goes quiet.
u5 = make_uplink({A: "share"}, mirrors=((A, "!m:master", None),))
u5.reconcile()
check("a pre-D2b mirror is backfilled with its level once",
      len(level_puts(u5)) == 1 and level_puts(u5)[0][1] == {"level": "share"})
u6 = make_uplink({A: "share"}, path=u5.db_path)
u6.reconcile()
check("the backfill does not repeat", level_puts(u6) == [])

# stamp_share_level's own gates.
u7 = make_uplink({A: "share"}, mirrors=((A, "!m:master", "share"),))
for bad in ("private", "inherit", "", None, "SHARE"):
    try:
        u7.stamp_share_level(A, bad)
        check("stamp_share_level refuses level %r" % (bad,), False)
    except ValueError:
        check("stamp_share_level refuses level %r" % (bad,), True)
u7.stamp_share_level("!nomirror:localhost", "direct")
check("stamp_share_level is a no-op for a room with no mirror", u7.state_puts == [])

# A failed master PUT must not record the new level (the next pass retries).
u8 = make_uplink({A: "direct"}, mirrors=((A, "!m:master", "share"),))


def failing_master(method, path, body=None, query=None, timeout=60):
    raise urllib.error.HTTPError(path, 403, "Forbidden", None, None)


u8.master = failing_master
u8.reconcile()
check("a failed re-stamp leaves the last stamped level unchanged",
      stamped(u8, A) == "share")

print("%d passed, %d failed" % (passed, failed))
sys.exit(1 if failed else 0)
