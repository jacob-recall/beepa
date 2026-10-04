#!/usr/bin/env python3
"""Unit tests for the uplink's bridge-invite auto-join stage.

The predicate itself (agents/uplink/invites.py) is proven against the browser's
apps/user/invites.js by tests/conformance/invites_conformance.py — this file is
about everything AROUND it, i.e. the daemon-side bounds on joining rooms with
nobody watching:

  - the FAIL-CLOSED ack gate: no join at all unless the teammate's
    com.jkali.autojoin_ack says exactly {ok: true}. 404, 500, a transport
    error, a non-dict, {ok: "true"}, {ok: 1}, {} all mean no;
  - the per-pass cap (30) and the PERSISTED rolling hourly cap (200), which
    survives a restart because it lives in state.db;
  - hard 4xx failures memoized with an EXPONENTIAL, never permanent, deadline;
    429 and 5xx never memoized (they stay retryable and back the stage off);
  - the snapshot is consumed once: a pass never replays the previous
    rooms.invite;
  - the logging rule: nothing this path logs may contain a room id or an '@'
    mxid (a DM portal's name is the contact's name, a ghost mxid embeds a phone
    number).

Run: python3 tests/unit/uplink_invites.test.py   (exit 0 = all pass)
"""
import logging
import os
import sys
import tempfile
import time
import types
import urllib.error
import urllib.parse

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "agents", "uplink"))
import invites   # noqa: E402
import uplink    # noqa: E402

passed = 0
failed = 0

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
        print("FAIL: " + name)


LOCAL_USER = "@jkali:localhost"
GMSG_BOT = "@gmessagesbot:localhost"
GHOST = "@gmessages_abc:localhost"
ACK_OK = {"ok": True, "ts": 1}

HTTP = object()     # sentinel: raise instead of returning account-data


def invite(room_id, sender=GMSG_BOT, space=None):
    """A bridge DM portal invite in the real captured shape; `space` makes it an
    m.space invite with that name."""
    create = {"content": {"room_version": "11"}, "sender": sender,
              "state_key": "", "type": "m.room.create"}
    events = [create,
              {"content": {"membership": "invite", "displayname": "jkali"},
               "sender": sender, "state_key": LOCAL_USER, "type": "m.room.member"}]
    if space is not None:
        create["content"]["type"] = "m.space"
        events.insert(1, {"content": {"name": space}, "sender": sender,
                          "state_key": "", "type": "m.room.name"})
    return {"invite_state": {"events": events}}


def make_uplink(path=None, ack=ACK_OK, join_error=None):
    """A real state.db (the daemon's own _open_db) and a stubbed local transport."""
    u = object.__new__(uplink.Uplink)
    u.db_path = path or os.path.join(tempfile.mkdtemp(prefix="uplink-invites-"), "state.db")
    u.db = uplink.Uplink._open_db(u.db_path)
    u.cfg = types.SimpleNamespace(local_user=LOCAL_USER, local_hs="http://127.0.0.1:8008")
    u._last_invites = None
    u._invite_ack_logged = False
    u.ack = ack
    u.join_error = join_error          # callable(room_id) -> exception or None
    u.joins = []

    def local(method, path, body=None, query=None, timeout=60):
        if method == "GET" and path.endswith("/account_data/" + uplink.AUTOJOIN_ACK_TYPE):
            if u.ack is HTTP:
                raise urllib.error.HTTPError(path, 500, "boom", None, None)
            if u.ack is None:
                raise urllib.error.HTTPError(path, 404, "Not Found", None, None)
            if isinstance(u.ack, Exception):
                raise u.ack
            return u.ack
        if method == "POST" and path.endswith("/join"):
            rid = urllib.parse.unquote(path.split("/rooms/")[1].split("/join")[0])
            err = u.join_error(rid) if u.join_error else None
            if err:
                raise err
            u.joins.append(rid)
            return {"room_id": rid}
        raise AssertionError("unexpected local %s %s" % (method, path))

    u.local = local
    return u


def run(u, section):
    u._last_invites = section
    u.join_invites()
    return u.joins


# ---- the predicate is wired up with the REAL catalog identities -------------
check("bot allowlist is the code-owned catalog (7 sources, no 'all')",
      uplink.INVITE_BOT_MXIDS.count(GMSG_BOT) == 1
      and all(m.startswith("@") for m in uplink.INVITE_BOT_MXIDS)
      and len(uplink.INVITE_BOT_MXIDS) == len(uplink.INVITE_SOURCE_SPACES) >= 7)
check("source spaces carry childSpaceNames (Discord's 'Direct Messages')",
      any(s["botMxid"] == "@discordbot:localhost"
          and "Direct Messages" in s["childSpaceNames"]
          for s in uplink.INVITE_SOURCE_SPACES))

u = make_uplink()
check("a real bot DM invite is joined", run(u, {"!a:localhost": invite("!a:localhost")}) == ["!a:localhost"])
u = make_uplink()
check("a ghost-created invite is never joined",
      run(u, {"!a:localhost": invite("!a:localhost", sender=GHOST)}) == [])
u = make_uplink()
check("the Discord 'Direct Messages' child space is joined",
      run(u, {"!d:localhost": invite("!d:localhost", sender="@discordbot:localhost",
                                     space="Direct Messages")}) == ["!d:localhost"])
u = make_uplink()
check("a space named for another bridge is not joined",
      run(u, {"!d:localhost": invite("!d:localhost", sender="@discordbot:localhost",
                                     space="WhatsApp (+15551234567)")}) == [])

# ---- the ack gate, fail-closed ---------------------------------------------
for label, ack in (("404", None), ("500", HTTP),
                   ("transport error", urllib.error.URLError("boom")),
                   ("non-dict", ["ok"]), ("string", "ok"),
                   ("{ok:false}", {"ok": False}), ("{ok:'true'}", {"ok": "true"}),
                   ("{ok:1}", {"ok": 1}), ("{}", {})):
    u = make_uplink(ack=ack)
    check("ack gate fails closed on " + label,
          run(u, {"!a:localhost": invite("!a:localhost")}) == [])
u = make_uplink(ack=None)
run(u, {"!a:localhost": invite("!a:localhost")})
before = len(LOG_LINES)
run(u, {"!a:localhost": invite("!a:localhost")})
check("the 'waiting for the confirm' line is logged once, not per pass",
      len(LOG_LINES) == before)
check("that line names no room and no mxid",
      any("waiting for the teammate's confirm" in l for l in LOG_LINES))

# ---- caps -------------------------------------------------------------------
u = make_uplink()
many = {"!r%03d:localhost" % i: invite("!r%03d:localhost" % i) for i in range(40)}
check("per-pass cap: 30 joined, the rest deferred", len(run(u, many)) == 30)
check("per-pass cap is deterministic (sorted)", u.joins == sorted(many)[:30])

u = make_uplink()
total = 0
for pas in range(10):
    chunk = {"!p%d_%03d:localhost" % (pas, i): invite("!p%d_%03d:localhost" % (pas, i))
             for i in range(30)}
    u.joins = []
    total += len(run(u, chunk))
check("persisted rolling cap stops at 200 joins/hour", total == uplink.INVITE_JOIN_HOURLY)
# ...and it survives a restart: a NEW Uplink on the SAME state.db stays capped.
u2 = make_uplink(path=u.db_path)
check("the rolling cap survives a restart (it lives in state.db)",
      run(u2, {"!after:localhost": invite("!after:localhost")}) == [])
# The window is rolling, not absolute: age the ticks out and joins resume.
u2.db.execute("UPDATE invite_join_log SET ts = ts - ?", (uplink.INVITE_JOIN_WINDOW_S + 1,))
u2.db.commit()
u2.joins = []
check("the cap is a ROLLING window, not a permanent budget",
      run(u2, {"!later:localhost": invite("!later:localhost")}) == ["!later:localhost"])
check("expired cap ticks are pruned on read",
      u2.db.execute("SELECT COUNT(*) FROM invite_join_log").fetchone()[0] == 1)

# ---- hard-failure memo ------------------------------------------------------
def http(code):
    return lambda rid: urllib.error.HTTPError("/join", code, "x", None, None)


u = make_uplink(join_error=http(403))
run(u, {"!a:localhost": invite("!a:localhost")})
rh = uplink.Uplink._room_hash("!a:localhost")
row = u.db.execute("SELECT code, attempts, next_attempt FROM invite_join_failed "
                   "WHERE room_hash=?", (rh,)).fetchone()
check("a hard 4xx is memoized with a deadline", row and row[0] == 403 and row[1] == 1
      and row[2] > int(time.time()))
check("the memo stores the room HASH, never the room id",
      u.db.execute("SELECT COUNT(*) FROM invite_join_failed WHERE room_hash LIKE '%!%'"
                   ).fetchone()[0] == 0)
u.joins = []
check("a memoized room is not retried while its deadline is in the future",
      run(u, {"!a:localhost": invite("!a:localhost")}) == [])
# ...and the memo expires: the deadline passes, the room is retried, and a
# second failure backs off further. Never permanent.
u.db.execute("UPDATE invite_join_failed SET next_attempt=?", (int(time.time()) - 1,))
u.db.commit()
run(u, {"!a:localhost": invite("!a:localhost")})
row2 = u.db.execute("SELECT attempts, next_attempt FROM invite_join_failed WHERE room_hash=?",
                    (rh,)).fetchone()
check("an expired memo is retried", row2[0] == 2)
check("the retry deadline grows exponentially",
      row2[1] - int(time.time()) > uplink.INVITE_RETRY_BACKOFF_S[0])
# A later success clears the memo entirely.
u.join_error = None
u.db.execute("UPDATE invite_join_failed SET next_attempt=?", (int(time.time()) - 1,))
u.db.commit()
u.joins = []
check("a successful join clears the memo",
      run(u, {"!a:localhost": invite("!a:localhost")}) == ["!a:localhost"]
      and u.db.execute("SELECT COUNT(*) FROM invite_join_failed").fetchone()[0] == 0)
check("a daemon join is recorded (hash-only) in daemon_joined",
      u.db.execute("SELECT COUNT(*) FROM daemon_joined WHERE room_hash=?", (rh,)).fetchone()[0] == 1)

for code in (429, 500, 502, 503):
    u = make_uplink(join_error=http(code))
    try:
        run(u, {"!a:localhost": invite("!a:localhost")})
        raised = False
    except urllib.error.HTTPError:
        raised = True
    check("a %d is never memoized (stays retryable)" % code,
          raised and u.db.execute("SELECT COUNT(*) FROM invite_join_failed").fetchone()[0] == 0)

u = make_uplink(join_error=lambda rid: urllib.error.URLError("down"))
try:
    run(u, {"!a:localhost": invite("!a:localhost")})
    raised = False
except urllib.error.URLError:
    raised = True
check("a transport failure is never memoized",
      raised and u.db.execute("SELECT COUNT(*) FROM invite_join_failed").fetchone()[0] == 0)

# ---- the snapshot is consumed once -----------------------------------------
u = make_uplink()
run(u, {"!a:localhost": invite("!a:localhost")})
u.joins = []
u.join_invites()          # no new snapshot handed over
check("a pass never replays the previous rooms.invite", u.joins == [])
check("nothing is joined without a snapshot",
      u.db.execute("SELECT COUNT(*) FROM daemon_joined").fetchone()[0] == 1)

# ---- joining is membership only: no override is written, nothing is shared --
u = make_uplink()
run(u, {"!a:localhost": invite("!a:localhost")})
check("the join stage writes no account-data at all (no implicit share)",
      u.db.execute("SELECT COUNT(*) FROM mirror_rooms").fetchone()[0] == 0)

# ---- the logging rule -------------------------------------------------------
leaky = [l for l in LOG_LINES if "!" in l or "@" in l or "Direct Messages" in l
         or "WhatsApp" in l]
check("no log line from this path contains a room id, an mxid or a room name",
      not leaky)
if leaky:
    print("   leaked: %r" % (leaky[:3],))
check("the counts line is the only per-pass output",
      any(l.startswith("invites: joined=") for l in LOG_LINES))

print("\n%d passed, %d failed" % (passed, failed))
sys.exit(1 if failed else 0)
