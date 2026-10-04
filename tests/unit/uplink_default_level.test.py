#!/usr/bin/env python3
"""Unit tests for the ACCOUNT DEFAULT LEVEL in the uplink (roadmap §6).

`com.jkali.share_policy` gained `default_level`: a conversation with NO
explicit per-room override takes it. That makes the standing policy a SECOND
authorization input the daemon reads, and the security review's P0 findings are
all about what happens when that read fails:

  1. read_policy() is lenient on 404 ONLY. Any other failure PROPAGATES, so the
     pass aborts and run_stage backs off. Collapsing a transient 500 into "no
     default" would look exactly like "the holder revoked their default" and
     mass-revoke every defaulted room — a read we could not perform is never a
     revocation.
  2. archive_level() — the per-write consent recheck every master write goes
     through — resolves with the SAME two inputs, 404-lenient on both, and
     raises on anything else (pause, never revoke).
  3. tail_once()'s override-change revocation resolves against the CURRENT
     policy, read ONCE per call: clearing an override under a 'direct' default
     is not a revocation, and must not flap the room in and out.
  4. read_room_level() (D2-5, the fresh point-read that authorizes an
     auto-send) reads BOTH inputs INSIDE its blanket fail-closed except: there,
     an unreadable policy must resolve 'private', never authorize.

Run: python3 tests/unit/uplink_default_level.test.py
"""
import logging
import os
import sys
import tempfile
import types
import urllib.error
import urllib.parse

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "agents", "uplink"))
import consent     # noqa: E402
import uplink      # noqa: E402

uplink.log.handlers = [logging.NullHandler()]
uplink.log.propagate = False

passed = 0
failed = 0


def check(name, cond):
    global passed, failed
    if cond:
        passed += 1
    else:
        failed += 1
        print("FAIL: " + name)


def raises(fn, exc=Exception):
    try:
        fn()
    except exc:
        return True
    except Exception:
        return False
    return False


LOCAL_USER = "@jkali:localhost"
SPACE = "!space:localhost"
A = "!a:localhost"
B = "!b:localhost"
POLICY_PATH = "/account_data/" + consent.SHARE_POLICY_TYPE
OVERRIDE_PATH = "/account_data/" + consent.SHARE_OVERRIDE_TYPE


def http(code):
    return urllib.error.HTTPError("fixture", code, "fixture", {}, None)


def make(policy=None, overrides=None, policy_error=None, override_error=None,
         mirrors=(), sync=None):
    """A bare Uplink with a scripted LOCAL transport.

    policy / policy_error   -> what GET share_policy answers (or raises)
    overrides               -> {room_id: stored content}; absent room = 404
    override_error          -> raised by EVERY override GET instead
    """
    u = object.__new__(uplink.Uplink)
    u.db_path = os.path.join(tempfile.mkdtemp(prefix="uplink-default-"), "state.db")
    u.db = uplink.Uplink._open_db(u.db_path)
    for lid, mid in mirrors:
        u.db.execute("INSERT OR REPLACE INTO mirror_rooms (local_room_id, master_room_id, "
                     "source, last_synced_pos, stamped_level) VALUES (?,?,?,?,?)",
                     (lid, mid, "imessage", None, "share"))
    u.db.execute("INSERT OR REPLACE INTO meta (k,v) VALUES (?,'1')", (uplink.MIGRATED_FLAG,))
    u.db.commit()
    u.cfg = types.SimpleNamespace(local_user=LOCAL_USER, master_user="@alice:master",
                                  manager_mxid="@manager:master", master_space="!space:master",
                                  sync_timeout=0, direct_send_cap=5)
    u.self_mxids = set()
    u._last_sourceless = None
    u.policy_reads = 0
    u.sync_data = sync
    u.enqueued = []
    u.puts = []

    def local(method, path, body=None, query=None, timeout=60):
        if method == "PUT":
            u.puts.append((path, body))
            return {}
        if path.endswith("/sync"):
            return u.sync_data or {}
        if path.endswith(POLICY_PATH):
            u.policy_reads += 1
            if policy_error is not None:
                raise policy_error
            if policy is None:
                raise http(404)
            return policy
        if OVERRIDE_PATH in path:
            if override_error is not None:
                raise override_error
            rid = urllib.parse.unquote(path.split("/rooms/")[1].split("/account_data/")[0])
            if not overrides or rid not in overrides:
                raise http(404)
            return overrides[rid]
        raise AssertionError("unexpected local %s %s" % (method, path))

    u.local = local
    u.master = lambda *a, **k: {}
    u.sync_room = lambda rid: None
    u.delete_mirror = lambda rid: None
    u.enqueue_events = lambda lid, mid, evs: u.enqueued.append((lid, len(evs)))
    u.active_link_for_dispatch = lambda: True
    u.schedule_history = lambda *a, **k: None
    return u

# ---------------------------------------------------------------------------
# 1 (P0). read_policy: 404-only leniency
# ---------------------------------------------------------------------------
check("read_policy: 404 -> the safe default",
      make().read_policy() == {"global": "private", "sources": {}})
check("read_policy: a valid default_level survives normalization",
      make(policy={"default_level": "direct"}).read_policy().get("default_level") == "direct")
check("read_policy: a junk default_level is dropped (reads back private)",
      consent.policy_default_level(
          make(policy={"default_level": "Direct"}).read_policy()) == "private")
for code in (401, 403, 429, 500, 502, 503):
    check("read_policy: HTTP %d PROPAGATES (never a fabricated default)" % code,
          raises(make(policy_error=http(code)).read_policy, urllib.error.HTTPError))
check("read_policy: a transport failure PROPAGATES",
      raises(make(policy_error=urllib.error.URLError("down")).read_policy,
             urllib.error.URLError))
check("read_policy: a timeout PROPAGATES",
      raises(make(policy_error=TimeoutError("slow")).read_policy, TimeoutError))
check("read_policy: an OSError PROPAGATES",
      raises(make(policy_error=OSError("socket")).read_policy, OSError))

# ---------------------------------------------------------------------------
# 2. pass_policy: one read per stage pass, cleared between passes
# ---------------------------------------------------------------------------
u = make(policy={"default_level": "share"})
u.pass_policy(); u.pass_policy(); u.pass_policy()
check("pass_policy: reads the policy once per pass", u.policy_reads == 1)
u.run_stage("fixture", lambda: u.pass_policy())
check("pass_policy: run_stage clears the cache, so the next stage re-reads",
      u.policy_reads == 2)
u2 = make(policy_error=http(500))
check("pass_policy: a failing read propagates out of the stage (which backs off)",
      u2.run_stage("fixture", lambda: u2.pass_policy()) is False)

# ---------------------------------------------------------------------------
# 3 (P0). archive_level — the per-write consent recheck, now default-aware
# ---------------------------------------------------------------------------
check("archive_level: no override + direct default -> 'direct'",
      make(policy={"default_level": "direct"}).archive_level(A) == "direct")
check("archive_level: no override + share default -> 'share'",
      make(policy={"default_level": "share"}).archive_level(A) == "share")
check("archive_level: no override + no default -> 'private' (unchanged behaviour)",
      make().archive_level(A) == "private")
check("archive_level: an EXPLICIT private beats a direct default",
      make(policy={"default_level": "direct"},
           overrides={A: {"state": "private"}}).archive_level(A) == "private")
check("archive_level: an explicit share under a private default still shares",
      make(overrides={A: {"state": "share"}}).archive_level(A) == "share")
check("archive_level: a junk override degrades to the default, it does not share",
      make(overrides={A: {"state": "junk"}}).archive_level(A) == "private")
check("archive_level: an unreadable OVERRIDE raises (pause, never revoke)",
      raises(lambda: make(override_error=http(503)).archive_level(A),
             urllib.error.HTTPError))
check("archive_level: an unreadable POLICY raises (pause, never revoke)",
      raises(lambda: make(policy_error=http(500)).archive_level(A),
             urllib.error.HTTPError))
check("archive_level: a policy transport failure raises too",
      raises(lambda: make(policy_error=urllib.error.URLError("down")).archive_level(A),
             urllib.error.URLError))
u = make(policy={"default_level": "direct"})
u.archive_level(A); u.archive_level(B); u.archive_level(A)
check("archive_level: shares ONE policy read across a stage pass", u.policy_reads == 1)

# ---------------------------------------------------------------------------
# 4 (P0). tail_once — an override CHANGE resolves against the CURRENT policy
# ---------------------------------------------------------------------------
def tail_sync(content):
    """A /sync with one joined, mirrored room whose share_override just changed."""
    return {"next_batch": "s2", "rooms": {"join": {A: {
        "account_data": {"events": [{"type": consent.SHARE_OVERRIDE_TYPE,
                                     "content": content}]},
        "timeline": {"events": []},
    }}}}


def tail(policy, content):
    u = make(policy=policy, mirrors=[(A, "!m:master")], sync=tail_sync(content))
    u.tail_once()
    return u


check("tail_once: clearing an override under a DIRECT default is NOT a revocation",
      tail({"default_level": "direct"}, {}).mirror_status(A) != "revoking")
check("tail_once: clearing an override under a SHARE default is NOT a revocation",
      tail({"default_level": "share"}, {}).mirror_status(A) != "revoking")
check("tail_once: clearing an override with NO default IS a revocation",
      tail(None, {}).mirror_status(A) == "revoking")
check("tail_once: an EXPLICIT private revokes even under a direct default",
      tail({"default_level": "direct"}, {"state": "private"}).mirror_status(A) == "revoking")
check("tail_once: a junk override under a direct default is not a revocation "
      "(junk is 'no override', and the default shares)",
      tail({"default_level": "direct"}, {"state": "junk"}).mirror_status(A) != "revoking")
check("tail_once: a junk override with no default IS a revocation",
      tail(None, {"state": "junk"}).mirror_status(A) == "revoking")
check("tail_once: a kept room is still forwarded",
      tail({"default_level": "direct"}, {}).enqueued == [(A, 0)])
u = make(policy={"default_level": "direct"}, mirrors=[(A, "!m:master"), (B, "!m2:master")],
         sync={"next_batch": "s2", "rooms": {"join": {
             A: {"account_data": {"events": [{"type": consent.SHARE_OVERRIDE_TYPE,
                                              "content": {}}]},
                 "timeline": {"events": []}},
             B: {"account_data": {"events": [{"type": consent.SHARE_OVERRIDE_TYPE,
                                              "content": {}}]},
                 "timeline": {"events": []}}}}})
u.tail_once()
check("tail_once: ONE policy read for the whole tail, not one per room",
      u.policy_reads == 1)
check("tail_once: a policy read failure aborts the tail rather than revoking",
      raises(lambda: make(policy_error=http(500), mirrors=[(A, "!m:master")],
                          sync=tail_sync({})).tail_once(), urllib.error.HTTPError))

# ---------------------------------------------------------------------------
# 5 (D2-5). read_room_level — fail CLOSED, including on the policy read
# ---------------------------------------------------------------------------
check("read_room_level: an explicit direct is still direct",
      make(overrides={A: {"state": "direct"}}).read_room_level(A) == "direct")
check("read_room_level: no override + direct default -> direct",
      make(policy={"default_level": "direct"}).read_room_level(A) == "direct")
check("read_room_level: no override + share default -> share (never auto-sends)",
      make(policy={"default_level": "share"}).read_room_level(A) == "share")
check("read_room_level: an EXPLICIT private beats a direct default",
      make(policy={"default_level": "direct"},
           overrides={A: {"state": "private"}}).read_room_level(A) == "private")
check("read_room_level: an unreadable POLICY resolves private, it never authorizes",
      make(policy_error=http(500)).read_room_level(A) == "private")
check("read_room_level: a policy transport failure resolves private",
      make(policy_error=urllib.error.URLError("down")).read_room_level(A) == "private")
check("read_room_level: an unreadable OVERRIDE resolves private even with a "
      "direct default stored",
      make(policy={"default_level": "direct"},
           override_error=http(503)).read_room_level(A) == "private")
check("read_room_level: a malformed room id is private without any read",
      make(policy={"default_level": "direct"}).read_room_level("not-a-room") == "private")
check("read_room_level: None is private", make().read_room_level(None) == "private")
# D2-5 is the authorization for an unreviewed send, so it must be FRESH: no
# pass cache may hold it open (the per-batch optimisation is deliberately
# deferred).
u = make(policy={"default_level": "direct"})
u.read_room_level(A)
u.read_room_level(A)
check("read_room_level: re-reads the policy every time (a fresh point-read)",
      u.policy_reads == 2)

# ---------------------------------------------------------------------------
# 6. desired_shared under a default: attribution is still required
# ---------------------------------------------------------------------------
def source_sync(room_ids, overrides=None, extra_rooms=()):
    events = [{"type": "m.room.name", "content": {"name": "iMessage"}},
              {"type": "m.room.create", "content": {"type": "m.space"}}]
    for rid in room_ids:
        events.append({"type": "m.space.child", "state_key": rid,
                       "content": {"via": ["localhost"]}})
    join = {SPACE: {"state": {"events": events}}}
    for rid in list(room_ids) + list(extra_rooms):
        room = {"state": {"events": []}}
        if overrides and rid in overrides:
            room["account_data"] = {"events": [{"type": consent.SHARE_OVERRIDE_TYPE,
                                                "content": {"state": overrides[rid]}}]}
        join[rid] = room
    return {"rooms": {"join": join}}


MGMT = "!mgmt:localhost"      # a bridge management room: joined, NOT space-attributed
u = make(policy={"default_level": "direct"},
         sync=source_sync([A, B], extra_rooms=[MGMT]))
u.read_profiles = lambda: {}
desired, source_of, join, _ = u.desired_shared(u.sync_data)
check("desired_shared: an attributed room with no override takes the default",
      desired == {A: "direct", B: "direct"})
check("desired_shared: an UNATTRIBUTED room (e.g. a bridge management DM) is "
      "not a mirror candidate even under a direct default",
      MGMT not in desired and MGMT in join)
u = make(policy={"default_level": "direct"},
         sync=source_sync([A, B], overrides={A: "private"}))
u.read_profiles = lambda: {}
desired, _, _, _ = u.desired_shared(u.sync_data)
check("desired_shared: an explicit private beats the direct default",
      desired == {A: "private", B: "direct"})
u = make(sync=source_sync([A, B], overrides={A: "share"}))
u.read_profiles = lambda: {}
desired, _, _, _ = u.desired_shared(u.sync_data)
check("desired_shared: with no default, only the explicit share mirrors",
      desired == {A: "share", B: "private"})

print("\n%d passed, %d failed" % (passed, failed))
if failed:
    sys.exit(1)
