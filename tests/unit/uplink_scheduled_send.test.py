#!/usr/bin/env python3
"""Unit tests for F7 — the uplink's SECOND send path (scheduled send).

Direct auto-send is bounded by a live manager identity and a live consent read.
A scheduled send is bounded by something else entirely: a DURABLE RECORD OF THE
TEAMMATE'S OWN INTENT that every later check can only weaken. That is why it has
its own gate and its own dispatcher, and why this file exists separately from
tests/unit/uplink_direct_send.test.py. These tests are those bounds:

  S-1  authorship, from a FRESH re-read: the server-stamped sender must be the
       teammate, the type must be com.jkali.scheduled_send, and the event must
       live in the recorded local proposals room. content.created_by is cosmetic.
  S-2  not cancelled: a STATE-event point-read keyed by the scheduled event id.
       404 is the ONLY "not cancelled"; any other error HOLDS (fail closed).
  S-3  the same send-grade sanitization as Direct, including the leading-'!'
       bridge-command refusal.
  S-4  the fire window: send_at <= now < send_at+10min. Earlier is "not yet",
       later is HELD — never a late silent fire.
  S-5  a 30-day horizon, checked at arm AND at fire, against the server stamp.
  S-6  the target: an attributed, non-space conversation room that is neither
       the proposals room nor a master mirror room. NO consent gate — this is
       the teammate's own message.
  S-7  the SHARED persisted per-room rate cap.
  S-8  superseded, anchored on the scheduling event's SERVER stamp, exempting
       only this daemon's own scheduled sends.
  S-9  intent (+ the cap tick) committed BEFORE the PUT, deterministic txn id.
  S-10 exactly one outcome record either way; a 4xx is a refusal, a transport
       failure is ambiguous and is NEVER re-sent, and a crash after intent
       recovers as ambiguous.
  S-11 a hash-only audit row.

Plus the manager-TIMED leg (all twelve D2 gates re-run at send_at on a fresh
re-read), the static assertion that NO daemon code path WRITES a
com.jkali.scheduled_send, and the hash-only logging/storage rule.

Run: python3 tests/unit/uplink_scheduled_send.test.py
"""
import logging
import os
import re
import sqlite3
import sys
import tempfile
import time
import types
import urllib.error
import urllib.parse

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "agents", "uplink"))
import consent   # noqa: E402
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
MANAGER = "@manager:master"
MASTER_USER = "@alice:master"
MASTER_HS = "http://127.0.0.1:8018"
CONV = "!conv:localhost"
OTHER = "!other:localhost"
SPACE = "!space-child:localhost"
MGMT = "!mgmt:localhost"
LPR = "!props:localhost"
MPR = "!mprops:master"
MIRROR = "!m1:master"
BODY = "scheduled hello"
MIN = 60 * 1000


class HardCrash(BaseException):
    """Escapes `except Exception` — models the process dying after the PUT."""


def nf(path):
    return urllib.error.HTTPError(path, 404, "Not Found", None, None)


def sched_event(eid="$s1", target=CONV, body=BODY, send_at=None, ots=None,
                sender=LOCAL_USER, etype=uplink.SCHEDULED_SEND_TYPE, room=LPR, **extra):
    now = int(time.time() * 1000)
    c = {"target_room": target, "body": body,
         "send_at": (now - 1000) if send_at is None else send_at,
         "created_by": sender}
    c.update(extra)
    if target is None:
        c.pop("target_room")
    return {"type": etype, "event_id": eid, "sender": sender, "room_id": room,
            "origin_server_ts": (now - 5000) if ots is None else ots, "content": c}


def make(path=None, cap=20, attributed=(CONV,), mirrors=((CONV, MIRROR),),
         levels=None, send_error=None, crash_after_send=False, outcome_error=None):
    u = object.__new__(uplink.Uplink)
    u.db_path = path or os.path.join(tempfile.mkdtemp(prefix="uplink-sched-"), "state.db")
    u.db = uplink.Uplink._open_db(u.db_path)
    for lid, mid in mirrors:
        u.db.execute("INSERT OR REPLACE INTO mirror_rooms (local_room_id, master_room_id, source, "
                     "last_synced_pos) VALUES (?,?,?,?)", (lid, mid, "imessage", None))
    for rid in attributed:
        u.db.execute("INSERT OR REPLACE INTO attributed_rooms (room_hash, ts) VALUES (?,?)",
                     (uplink.Uplink._room_hash(rid), int(time.time())))
    for k, v in (("local_proposals_room", LPR), ("master_proposals_room", MPR),
                 ("proposal_sync_since", "s1"),
                 ("proposal_identity", "\n".join((MASTER_HS, MASTER_USER, MANAGER)))):
        u.db.execute("INSERT OR REPLACE INTO meta (k,v) VALUES (?,?)", (k, v))
    u.db.execute("INSERT OR REPLACE INTO proposal_map (master_event_id, local_event_id, outcome) "
                 "VALUES ('$seed','$l0','fallback')")
    u.db.commit()
    u.cfg = types.SimpleNamespace(
        local_user=LOCAL_USER, local_hs="http://127.0.0.1:8008",
        master_hs=MASTER_HS, master_user=MASTER_USER, manager_mxid=MANAGER,
        master_space="!space:master", master_token="tok", direct_send_cap=cap,
        sync_timeout=1000)
    u._direct_suspended = False
    u.events = {}            # event_id -> what the fresh re-read returns
    u.master_events = {}     # event_id -> what the master re-read returns
    u.cancelled = set()
    u.cancel_error = None
    u.reread_error = None
    u.messages = {"chunk": []}
    u.levels = dict(levels if levels is not None else {CONV: "direct"})
    u.send_error = send_error
    u.crash_after_send = crash_after_send
    u.outcome_error = outcome_error
    u.sends = []
    u.outcomes = []
    u.records = []
    u.link_ok = True

    def tail_id(path, marker):
        return urllib.parse.unquote(path.split(marker, 1)[1])

    def local(method, path, body=None, query=None, timeout=60):
        if method == "GET":
            if "/state/" + uplink.SCHEDULED_CANCEL_TYPE + "/" in path:
                if u.cancel_error is not None:
                    raise u.cancel_error
                eid = tail_id(path, "/state/" + uplink.SCHEDULED_CANCEL_TYPE + "/")
                if eid in u.cancelled:
                    return {"ts": 1}
                raise nf(path)
            if "/event/" in path:
                if u.reread_error is not None:
                    raise u.reread_error
                eid = tail_id(path, "/event/")
                if eid in u.events:
                    return u.events[eid]
                raise nf(path)
            if "/messages" in path:
                if isinstance(u.messages, Exception):
                    raise u.messages
                return u.messages
            if "/account_data/" + consent.SHARE_OVERRIDE_TYPE in path:
                for rid, lv in u.levels.items():
                    if urllib.parse.quote(rid, safe="") in path:
                        return {"state": lv}
                raise nf(path)
            raise nf(path)
        if method == "PUT":
            if "/send/m.room.message/" in path:
                u.sends.append((path, body))
                if u.crash_after_send:
                    raise HardCrash("killed between PUT and commit")
                if u.send_error is not None:
                    raise u.send_error
                return {"event_id": "$sent%d" % len(u.sends)}
            if "/send/" + uplink.SCHEDULED_OUTCOME_TYPE + "/" in path:
                if u.outcome_error is not None:
                    raise u.outcome_error
                u.outcomes.append((path, body))
                return {"event_id": "$out%d" % len(u.outcomes)}
            if "/send/" + uplink.PROPOSAL_TYPE + "/" in path:
                u.records.append((path, body))
                return {"event_id": "$rec%d" % len(u.records)}
        raise AssertionError("unexpected local %s %s" % (method, path))

    def master(method, path, body=None, query=None, timeout=60):
        if method == "GET" and "/event/" in path:
            eid = tail_id(path, "/event/")
            if eid in u.master_events:
                return u.master_events[eid]
            raise nf(path)
        raise AssertionError("unexpected master %s %s" % (method, path))

    u.local = local
    u.master = master
    u.active_link_for_dispatch = lambda: u.link_ok
    return u


def arm(u, ev):
    u.events[ev["event_id"]] = ev
    u._arm_scheduled(LPR, [ev])
    return ev["event_id"]


def row(u, eid="$s1"):
    return u.db.execute("SELECT state, reason, room_hash, authorship FROM scheduled_sends "
                        "WHERE event_id=?", (eid,)).fetchone()


def audits(u):
    return [r[0] for r in u.db.execute("SELECT outcome FROM direct_send_audit").fetchall()]


def last_outcome(u):
    return u.outcomes[-1][1] if u.outcomes else None


def fire(u):
    u.scheduled_once()


def refuses(label, u, state, reason, ev=None):
    arm(u, ev if ev is not None else sched_event())
    fire(u)
    content = last_outcome(u)
    check(label + ": nothing sent", u.sends == [])
    check(label + ": exactly one outcome record", len(u.outcomes) == 1)
    check(label + ": outcome state is %r" % state, content is not None and content["state"] == state)
    check(label + ": outcome reason is %r" % reason, content is not None and content["reason"] == reason)
    check(label + ": the row is terminal", (row(u) or ("",))[0] == state)


# ---------------------------------------------------------------------------
# Happy path — the shape everything below is a refusal of.
# ---------------------------------------------------------------------------
u = make()
arm(u, sched_event())
check("arm: one armed teammate row", row(u) == ("armed", None, uplink.Uplink._room_hash(CONV), "teammate"))
fire(u)
check("fire: exactly one message sent", len(u.sends) == 1)
send_path, send_body = u.sends[0]
check("fire: sent into the TARGET conversation as m.room.message",
      urllib.parse.quote(CONV, safe="") in send_path and "/send/m.room.message/" in send_path)
check("fire: deterministic txn id sched_<event_id> (S-9)", send_path.endswith("/sched_%24s1"))
check("fire: body and msgtype", send_body["body"] == BODY and send_body["msgtype"] == "m.text")
check("fire: cosmetic provenance names the scheduling event",
      send_body[uplink.FROM_SCHEDULE_KEY] == "$s1")
out = last_outcome(u)
check("fire: exactly one outcome record, state 'sent'",
      len(u.outcomes) == 1 and out["state"] == "sent" and out["sent_event_id"] == "$sent1")
check("fire: the outcome names the schedule and the target, never the body",
      out["scheduled_event_id"] == "$s1" and out["target_room"] == CONV and "body" not in out)
check("fire: outcome txn is deterministic", u.outcomes[0][0].endswith("/schedout_%24s1_sent"))
check("fire: row is 'sent'", row(u)[0] == "sent")
check("fire: one rate-cap tick on the SHARED counter (S-7)",
      u.db.execute("SELECT COUNT(*) FROM direct_send_log").fetchone()[0] == 1)
check("fire: hash-only audit row (S-11)", audits(u) == ["sched:sent"])
check("fire: the audit row is discriminated as a schedule",
      u.db.execute("SELECT source FROM direct_send_audit").fetchone()[0] == "schedule")
fire(u)
check("fire: a terminal row never fires again", len(u.sends) == 1 and len(u.outcomes) == 1)

# ---------------------------------------------------------------------------
# S-1 — authorship, from a FRESH re-read
# ---------------------------------------------------------------------------
for label, kw in (("sender is not the teammate", {"sender": "@intruder:localhost"}),
                  ("type is not scheduled_send", {"etype": "com.jkali.proposal"}),
                  ("event lives in another room", {"room": OTHER})):
    u = make()
    ev = sched_event(**kw)
    # Arm a legitimate row, then make the re-read return the hostile shape:
    # state.db alone must never be enough to fire.
    u._arm_scheduled(LPR, [sched_event()])
    u.events["$s1"] = dict(ev, event_id="$s1")
    fire(u)
    check("S-1 %s: nothing sent" % label, u.sends == [])
    check("S-1 %s: refused:authorship" % label,
          last_outcome(u) and last_outcome(u)["state"] == "refused"
          and last_outcome(u)["reason"] == "authorship")

u = make()
u._arm_scheduled(LPR, [sched_event()])     # armed, but the event is not re-readable
fire(u)
check("S-1 a 404 on the re-read refuses (never fires)",
      u.sends == [] and last_outcome(u)["reason"] == "authorship")

u = make()
arm(u, sched_event())
u.reread_error = urllib.error.HTTPError(LPR, 500, "boom", None, None)
raised = False
try:
    fire(u)
except urllib.error.HTTPError:
    raised = True
check("S-1 a 5xx on the re-read RAISES (back off; never fire, never drop)",
      raised and u.sends == [] and u.outcomes == [] and row(u)[0] == "armed")

u = make()
arm(u, sched_event(created_by=MANAGER))
fire(u)
check("S-1 a spoofed created_by changes nothing (it is cosmetic)", len(u.sends) == 1)

# A com.jkali.proposal must NEVER enter the teammate queue.
u = make()
u._arm_scheduled(LPR, [{"type": uplink.PROPOSAL_TYPE, "event_id": "$p1", "sender": MANAGER,
                        "room_id": LPR, "origin_server_ts": int(time.time() * 1000),
                        "content": {"target_room": CONV, "body": "hi",
                                    "send_at": int(time.time() * 1000)}}])
check("a com.jkali.proposal never enters the teammate queue",
      u.db.execute("SELECT COUNT(*) FROM scheduled_sends").fetchone()[0] == 0)

# ---------------------------------------------------------------------------
# S-2 — cancellation is a STATE event, point-read, fail closed
# ---------------------------------------------------------------------------
u = make()
arm(u, sched_event())
u.cancelled.add("$s1")
fire(u)
check("S-2 a cancel state event is terminal and nothing is sent",
      u.sends == [] and last_outcome(u)["state"] == "cancelled")

for label, err in (("500", urllib.error.HTTPError(LPR, 500, "boom", None, None)),
                   ("transport", urllib.error.URLError("down")),
                   ("403", urllib.error.HTTPError(LPR, 403, "no", None, None))):
    u = make()
    arm(u, sched_event())
    u.cancel_error = err
    fire(u)
    check("S-2 a %s on the cancel read HOLDS (fail closed)" % label,
          u.sends == [] and last_outcome(u)["state"] == "held"
          and last_outcome(u)["reason"] == "cancel_unreadable")

u = make()
arm(u, sched_event())
u._arm_scheduled(LPR, [{"type": uplink.SCHEDULED_CANCEL_TYPE, "state_key": "$s1",
                        "event_id": "$c1", "sender": LOCAL_USER, "content": {"ts": 1}}])
check("a cancel seen by tail_once marks the row", row(u)[0] == "cancel_pending")
fire(u)
check("a tail-seen cancel files the one cancelled record and never sends",
      u.sends == [] and last_outcome(u)["state"] == "cancelled" and row(u)[0] == "cancelled")

# ---------------------------------------------------------------------------
# S-3 — send-grade sanitization, verbatim
# ---------------------------------------------------------------------------
refuses("S-3 leading '!'", make(), "held", "sanitize", sched_event(body="!wa help"))
refuses("S-3 '!' behind whitespace", make(), "held", "sanitize", sched_event(body="  \n !wa ping"))
refuses("S-3 blank body", make(), "held", "sanitize", sched_event(body="   "))
u = make()
arm(u, sched_event(body="clean‮​\x07 text"))
fire(u)
check("S-3 control/bidi chars are stripped from what is actually sent",
      len(u.sends) == 1 and u.sends[0][1]["body"] == "clean text")

# ---------------------------------------------------------------------------
# S-4 — the fire window
# ---------------------------------------------------------------------------
now_ms = int(time.time() * 1000)
u = make()
arm(u, sched_event(send_at=now_ms + 10 * MIN))
fire(u)
check("S-4 a future schedule is not fired, not recorded and not logged",
      u.sends == [] and u.outcomes == [] and row(u)[0] == "armed")

u = make()
arm(u, sched_event(send_at=now_ms - 11 * MIN, ots=now_ms - 20 * MIN))
fire(u)
check("S-4 past-due beyond the window is HELD, never fired",
      u.sends == [] and last_outcome(u)["state"] == "held"
      and last_outcome(u)["reason"] == "late")

u = make()
arm(u, sched_event(send_at=now_ms - 9 * MIN, ots=now_ms - 20 * MIN))
fire(u)
check("S-4 inside the window it fires", len(u.sends) == 1)

# send_at type/shape at ARM time
for label, value in (("bool", True), ("float", 1.5), ("string", str(now_ms)),
                     ("negative", -5), ("missing", None)):
    u = make()
    ev = sched_event(send_at=value)
    if value is None:
        ev["content"].pop("send_at")
    arm(u, ev)
    check("S-4 a %s send_at is refused at arm" % label, row(u)[0] == "refuse_pending")
    fire(u)
    check("S-4 a %s send_at files one refusal and never fires" % label,
          u.sends == [] and last_outcome(u)["state"] == "refused"
          and last_outcome(u)["reason"] == "send_at")

# ---------------------------------------------------------------------------
# S-5 — the 30-day horizon, at arm AND at fire
# ---------------------------------------------------------------------------
u = make()
arm(u, sched_event(send_at=now_ms + uplink.SCHEDULED_HORIZON_MS + MIN, ots=now_ms))
check("S-5 an over-horizon schedule is refused at arm", row(u)[0] == "refuse_pending")
fire(u)
check("S-5 it files one refusal and never fires",
      u.sends == [] and last_outcome(u)["reason"] == "horizon")

u = make()
# Armed as legitimate, but the fresh re-read shows an over-horizon pair.
u._arm_scheduled(LPR, [sched_event()])
u.events["$s1"] = sched_event(send_at=now_ms - 1000,
                              ots=now_ms - uplink.SCHEDULED_HORIZON_MS - 10 * MIN)
fire(u)
check("S-5 the horizon is re-checked at FIRE time against the server stamp",
      u.sends == [] and last_outcome(u)["reason"] == "horizon")

# ---------------------------------------------------------------------------
# S-6 — the target
# ---------------------------------------------------------------------------
refuses("S-6 unattributed room", make(attributed=()), "refused", "target")
refuses("S-6 the proposals room itself", make(attributed=(LPR,)), "refused", "target",
        sched_event(target=LPR))
refuses("S-6 a bridge management room", make(attributed=(CONV,)), "refused", "target",
        sched_event(target=MGMT))
refuses("S-6 a master mirror room", make(attributed=(CONV, MIRROR)), "refused", "target",
        sched_event(target=MIRROR))
u = make()
ev = sched_event(target=None)
arm(u, ev)
check("S-6 a target-less schedule is refused at arm", row(u)[0] == "refuse_pending")
u = make()
arm(u, sched_event(target="not-a-room"))
check("S-6 a malformed room id is refused at arm", (row(u) or ("",))[1] == "target")

# attributed_rooms is the non-space output of sources_from_sync (fix 6).
u = make(attributed=())
sync = {"rooms": {"join": {
    "!src:localhost": {"state": {"events": [
        {"type": "m.room.create", "content": {"type": "m.space"}},
        {"type": "m.room.name", "content": {"name": "Discord"}},
        {"type": "m.space.child", "state_key": CONV, "content": {"via": ["localhost"]}},
        {"type": "m.space.child", "state_key": SPACE, "content": {"via": ["localhost"]}}]}},
    CONV: {"state": {"events": []}},
    SPACE: {"state": {"events": [{"type": "m.room.create", "content": {"type": "m.space"}}]}},
    MGMT: {"state": {"events": []}},
}}}
source_of = uplink.Uplink.sources_from_sync(sync)
u.persist_attributed_rooms(source_of, uplink.Uplink.space_ids_from_sync(sync))
check("fix 6: an attributed conversation room is persisted", u.room_is_attributed(CONV))
check("fix 6: an attributed CHILD SPACE is not", not u.room_is_attributed(SPACE))
check("fix 6: an unattributed management room is not", not u.room_is_attributed(MGMT))
check("fix 6: only hashes are stored",
      all(CONV not in str(r) and SPACE not in str(r) for r in
          u.db.execute("SELECT * FROM attributed_rooms").fetchall()))
refuses("S-6 a child SPACE target", u, "refused", "target", sched_event(target=SPACE))
u2 = make(attributed=())
u2.persist_attributed_rooms(source_of, uplink.Uplink.space_ids_from_sync(sync))
u2.db.execute("DELETE FROM mirror_rooms")
u2.db.commit()
arm(u2, sched_event())
fire(u2)
check("S-6 has NO consent gate: an unshared but attributed room still fires",
      len(u2.sends) == 1)

# ---------------------------------------------------------------------------
# S-7 — the shared rolling per-room cap
# ---------------------------------------------------------------------------
u = make(cap=1)
arm(u, sched_event("$a"))
arm(u, sched_event("$b"))
fire(u)
check("S-7 one dispatch per pass", len(u.sends) == 1)
fire(u)
check("S-7 the second is HELD by the cap",
      len(u.sends) == 1 and last_outcome(u)["state"] == "held"
      and last_outcome(u)["reason"] == "cap")
u = make(cap=1)
u.db.execute("INSERT INTO direct_send_log (ts, room_hash) VALUES (?,?)",
             (int(time.time()), uplink.Uplink._room_hash(CONV)))
u.db.commit()
refuses("S-7 a Direct auto-send consumes the SAME budget", u, "held", "cap")

# ---------------------------------------------------------------------------
# S-8 — superseded, and the exempt_own_scheduled leg (fix 8)
# ---------------------------------------------------------------------------
u = make()
u.messages = {"chunk": [{"type": "m.room.message", "origin_server_ts": now_ms,
                         "sender": "@whatsapp_555:localhost", "content": {"body": "they replied"}}]}
refuses("S-8 a later message", u, "held", "superseded")

u = make()
u.messages = {"chunk": [{"type": "m.room.message", "origin_server_ts": now_ms,
                         "sender": LOCAL_USER,
                         "content": {"body": "earlier scheduled",
                                     uplink.FROM_SCHEDULE_KEY: "$earlier"}}]}
arm(u, sched_event())
fire(u)
check("S-8 this daemon's OWN earlier scheduled send does not supersede the next",
      len(u.sends) == 1)

u = make()
u.messages = {"chunk": [{"type": "m.room.message", "origin_server_ts": now_ms,
                         "sender": LOCAL_USER, "content": {"body": "typed by the teammate"}}]}
refuses("S-8 a message the teammate typed still supersedes", u, "held", "superseded")

u = make()
u.messages = {"chunk": [{"type": "m.room.message", "origin_server_ts": now_ms,
                         "sender": "@whatsapp_555:localhost",
                         "content": {"body": "forged", uplink.FROM_SCHEDULE_KEY: "$x"}}]}
refuses("S-8 a remote party forging the provenance key still supersedes",
        u, "held", "superseded")

u = make()
u.messages = OSError("boom")
refuses("S-8 an unreadable timeline holds (fail closed)", u, "held", "superseded")

# exempt_own_scheduled is OFF by default — the Direct evaluation never gets it.
probe = make()
probe.messages = {"chunk": [{"type": "m.room.message", "origin_server_ts": now_ms,
                             "sender": LOCAL_USER,
                             "content": {uplink.FROM_SCHEDULE_KEY: "$x"}}]}
check("fix 8: exempt_own_scheduled defaults OFF",
      probe.room_quiet_since(CONV, now_ms - 10000) is False)
check("fix 8: exempt_own_scheduled=True exempts it",
      probe.room_quiet_since(CONV, now_ms - 10000, exempt_own_scheduled=True) is True)

# ---------------------------------------------------------------------------
# S-9 / S-10 — intent before dispatch, and the failure split
# ---------------------------------------------------------------------------
u = make(crash_after_send=True)
crashed = False
try:
    arm(u, sched_event())
    fire(u)
except HardCrash:
    crashed = True
check("S-9 the interruption escapes (models a killed process)", crashed)
check("S-9 the PUT was dispatched", len(u.sends) == 1)
check("S-9 intent was committed BEFORE the PUT", row(u)[0] == "attempted")
check("S-9 the cap tick was committed before the PUT too",
      u.db.execute("SELECT COUNT(*) FROM direct_send_log").fetchone()[0] == 1)
check("S-9 no outcome record was filed yet", u.outcomes == [])

u2 = make(path=u.db_path)
u2.events["$s1"] = sched_event()
fire(u2)
check("S-10 crash recovery: NO duplicate send", u2.sends == [])
check("S-10 crash recovery: exactly one record, state 'ambiguous'",
      len(u2.outcomes) == 1 and last_outcome(u2)["state"] == "ambiguous")
fire(u2)
check("S-10 crash recovery: a later pass files nothing more",
      u2.sends == [] and len(u2.outcomes) == 1)

u = make(send_error=urllib.error.HTTPError(CONV, 403, "Forbidden", None, None))
arm(u, sched_event())
fire(u)
check("S-10 a local 4xx is a REFUSAL (nothing was sent)",
      last_outcome(u)["state"] == "refused" and "sched:refused" in audits(u))
u = make(send_error=urllib.error.URLError("connection reset"))
arm(u, sched_event())
fire(u)
check("S-10 a transport failure is AMBIGUOUS and never re-sent",
      last_outcome(u)["state"] == "ambiguous" and "sched:ambiguous" in audits(u))
u = make(send_error=urllib.error.HTTPError(CONV, 502, "Bad Gateway", None, None))
arm(u, sched_event())
fire(u)
check("S-10 a 5xx is ambiguous (it may have been applied)",
      last_outcome(u)["state"] == "ambiguous")

# The state.db row is terminal ONLY after the outcome record's 2xx.
u = make(outcome_error=urllib.error.HTTPError(LPR, 500, "boom", None, None))
arm(u, sched_event(body="!nope"))
raised = False
try:
    fire(u)
except urllib.error.HTTPError:
    raised = True
check("a failed outcome write leaves the row non-terminal and raises (retry next pass)",
      raised and row(u)[0] == "armed" and u.sends == [])

# ---------------------------------------------------------------------------
# Storage and logging are hash-only (fix 2)
# ---------------------------------------------------------------------------
u = make()
arm(u, sched_event())
fire(u)
rows = (u.db.execute("SELECT * FROM scheduled_sends").fetchall()
        + u.db.execute("SELECT * FROM direct_send_audit").fetchall()
        + u.db.execute("SELECT * FROM direct_send_log").fetchall()
        + u.db.execute("SELECT * FROM attributed_rooms").fetchall())
check("no state.db row contains a room id or a body",
      all(CONV not in str(r) and LPR not in str(r) and BODY not in str(r) for r in rows))
check("scheduled_sends stores the room as a hash",
      row(u)[2] == uplink.Uplink._room_hash(CONV))
check("no log line contains a room id", not any(CONV in m or LPR in m or MGMT in m
                                                for m in LOG_LINES))
check("no log line contains a message body",
      not any(BODY in m or "!wa help" in m for m in LOG_LINES))

# ---------------------------------------------------------------------------
# Static: NO daemon code path WRITES a com.jkali.scheduled_send (fix 2)
# ---------------------------------------------------------------------------
SRC_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "agents", "uplink")
writers = []
reads_only = True
for name in ("uplink.py", "durable_sync.py"):
    src = open(os.path.join(SRC_DIR, name), encoding="utf-8").read()
    for i, line in enumerate(src.split("\n"), 1):
        mentions = ("SCHEDULED_SEND_TYPE" in line
                    or re.search(r'["\']com\.jkali\.scheduled_send["\']', line))
        if not mentions:
            continue
        if "/send/" in line or "/state/" in line or '"PUT"' in line or "'PUT'" in line:
            writers.append("%s:%d" % (name, i))
        if re.search(r'^\s*(self\.local|self\.master)\(', line):
            reads_only = False
    for i, line in enumerate(src.split("\n"), 1):
        if "/send/" in line and "scheduled_send" in line:
            writers.append("%s:%d(send path)" % (name, i))
check("the daemon NEVER writes a com.jkali.scheduled_send (it only reads them)",
      writers == [] and reads_only)
check("the daemon DOES write com.jkali.scheduled_outcome",
      any("/send/\" + SCHEDULED_OUTCOME_TYPE" in line
          for line in open(os.path.join(SRC_DIR, "uplink.py"), encoding="utf-8")))

# ---------------------------------------------------------------------------
# Schema: v2 -> v3 migration adds the audit discriminator
# ---------------------------------------------------------------------------
old_path = os.path.join(tempfile.mkdtemp(prefix="uplink-v2-"), "state.db")
old = sqlite3.connect(old_path)
old.execute("CREATE TABLE mirror_rooms (local_room_id TEXT PRIMARY KEY, master_room_id TEXT "
            "UNIQUE, source TEXT, last_synced_pos TEXT, stamped_level TEXT)")
old.execute("CREATE TABLE event_map (local_event_id TEXT PRIMARY KEY, master_event_id TEXT)")
old.execute("CREATE TABLE proposal_map (master_event_id TEXT PRIMARY KEY, local_event_id TEXT, "
            "outcome TEXT)")
old.execute("CREATE TABLE contact_mirror (source TEXT, network_id TEXT, mirrored_version INTEGER, "
            "master_state_key TEXT, PRIMARY KEY(source, network_id))")
old.execute("CREATE TABLE direct_send_log (ts INTEGER NOT NULL, room_hash TEXT NOT NULL)")
old.execute("CREATE TABLE direct_send_audit (ts INTEGER NOT NULL, master_event_id TEXT, "
            "room_hash TEXT, outcome TEXT)")
old.execute("INSERT INTO direct_send_audit VALUES (1, '$m', 'h', 'sent')")
old.execute("CREATE TABLE meta (k TEXT PRIMARY KEY, v TEXT)")
old.execute("PRAGMA user_version=2")
old.commit()
old.close()
err = None
try:
    u = make(path=old_path)
    arm(u, sched_event())
    fire(u)
except sqlite3.Error as e:                        # noqa: BLE001 — the thing under test
    err = e
check("a v2 state.db runs the F7 code with no sqlite error", err is None and len(u.sends) == 1)
check("v2 -> v3: the audit discriminator was added, old rows kept",
      u.db.execute("PRAGMA user_version").fetchone()[0] == uplink.SCHEMA_VERSION
      and u.db.execute("SELECT source FROM direct_send_audit WHERE master_event_id='$m'"
                       ).fetchone()[0] is None)
check("migration is idempotent",
      uplink.Uplink._open_db(old_path).execute("PRAGMA user_version").fetchone()[0]
      == uplink.SCHEMA_VERSION)

# ---------------------------------------------------------------------------
# Manager-TIMED proposals: the twelve D2 gates, re-run at send_at
# ---------------------------------------------------------------------------
def mprop(eid="$m1", target=CONV, body="timed hello", send_at=None, ots=None, sender=MANAGER):
    now = int(time.time() * 1000)
    ots = (now - 30000) if ots is None else ots
    c = {"target_room": target, "body": body}
    if send_at is not None:
        c["send_at"] = send_at
    return {"type": uplink.PROPOSAL_TYPE, "event_id": eid, "sender": sender,
            "origin_server_ts": ots, "content": c}


def make_manager(send_at_offset=-1000, **kw):
    u = make(**kw)
    now = int(time.time() * 1000)
    ev = mprop(ots=now - 60000, send_at=now + send_at_offset)
    u.master_events["$m1"] = ev
    u.forward_proposals(MPR, LPR, [ev], cold_start=False, suspended=False)
    return u, ev


# freshness_anchor_ms defaults to the event's own origin_server_ts.
probe = make()
probe.messages = {"chunk": []}
ev = mprop(ots=int(time.time() * 1000) - 1000)
clean = probe._sanitize_proposal(ev)
a = probe._direct_send_gate(ev, clean, cold_start=False, suspended=False)
b = probe._direct_send_gate(ev, clean, cold_start=False, suspended=False,
                            freshness_anchor_ms=ev["origin_server_ts"])
check("the freshness anchor DEFAULTS to origin_server_ts (Direct stays byte-identical)",
      a == b and a[0] == "timed hello")

u, ev = make_manager()
check("manager-timed: the proposal is PARKED, no artifact and no send yet",
      u.sends == [] and u.records == [])
check("manager-timed: the row is armed with authorship 'manager'",
      row(u, "$m1") == ("armed", None, uplink.Uplink._room_hash(CONV), "manager"))
check("manager-timed: proposal_map is marked 'scheduled'",
      u.db.execute("SELECT outcome FROM proposal_map WHERE master_event_id='$m1'"
                   ).fetchone()[0] == "scheduled")
u.scheduled_manager_once()
check("manager-timed: at send_at it runs the D2 dispatcher and sends once",
      len(u.sends) == 1 and u.sends[0][0].endswith("/autosend_%24m1"))
check("manager-timed: exactly one inbox artifact, the auto_sent record",
      len(u.records) == 1 and u.records[0][1][uplink.AUTO_SENT_KEY] is True)
check("manager-timed: NO scheduled_outcome event (the proposal record is the one artifact)",
      u.outcomes == [])
u.scheduled_manager_once()
check("manager-timed: it never fires twice", len(u.sends) == 1 and len(u.records) == 1)

u, ev = make_manager(send_at_offset=5 * MIN)
u.scheduled_manager_once()
check("manager-timed: before send_at nothing fires", u.sends == [] and u.records == [])

u, ev = make_manager()
u.messages = {"chunk": [{"type": "m.room.message", "origin_server_ts": int(time.time() * 1000),
                         "sender": "@whatsapp_555:localhost", "content": {"body": "they replied"}}]}
u.scheduled_manager_once()
check("manager-timed: superseded files the ORDINARY draft and sends nothing",
      u.sends == [] and len(u.records) == 1
      and uplink.AUTO_SENT_KEY not in u.records[0][1]
      and uplink.SEND_AMBIGUOUS_KEY not in u.records[0][1])
check("manager-timed: the refusal is audited against the manager-schedule source",
      any(a.startswith("sched:refused:superseded") for a in audits(u)))

u, ev = make_manager()
u.link_ok = False
u.scheduled_manager_once()
check("manager-timed: a disabled link stops the fire entirely",
      u.sends == [] and u.records == [] and row(u, "$m1")[0] == "armed")

u, ev = make_manager()
u.cfg.manager_mxid = "@newmanager:master"      # D2-11 rebinding
u.scheduled_manager_once()
check("manager-timed: an identity rebinding suspends the fire",
      u.sends == [] and u.records == [] and row(u, "$m1")[0] == "armed")

u, ev = make_manager()
u.levels = {CONV: "private"}                   # D2-5 re-read at fire time
u.scheduled_manager_once()
check("manager-timed: a direct->private flip before send_at files the ordinary draft",
      u.sends == [] and len(u.records) == 1 and uplink.AUTO_SENT_KEY not in u.records[0][1])

u, ev = make_manager()
u.master_events["$m1"] = mprop(body="!wa help", ots=ev["origin_server_ts"],
                               send_at=ev["content"]["send_at"])
u.scheduled_manager_once()
check("manager-timed: the body is re-read and re-sanitized at fire time",
      u.sends == [] and len(u.records) == 1)

u, ev = make_manager()
u.master_events["$m1"] = mprop(sender="@intruder:master", ots=ev["origin_server_ts"],
                               send_at=ev["content"]["send_at"])
u.scheduled_manager_once()
check("manager-timed: D2-1 is re-run on the FRESH re-read", u.sends == [])

# Horizon + level at arm time.
now = int(time.time() * 1000)
u = make()
far = mprop("$m2", send_at=now + uplink.SCHEDULED_MANAGER_HORIZON_MS + MIN, ots=now)
u.forward_proposals(MPR, LPR, [far], cold_start=False, suspended=False)
check("manager-timed: beyond the 24h horizon it is an ordinary draft, armed nowhere",
      len(u.records) == 1 and u.db.execute("SELECT COUNT(*) FROM scheduled_sends"
                                           ).fetchone()[0] == 0)
u = make(levels={CONV: "share"})
share = mprop("$m3", send_at=now + 60 * MIN, ots=now)
u.forward_proposals(MPR, LPR, [share], cold_start=False, suspended=False)
check("manager-timed: a SHARE-level room gets the ordinary draft (the app times it)",
      len(u.records) == 1 and u.records[0][1]["send_at"] == now + 60 * MIN
      and u.db.execute("SELECT COUNT(*) FROM scheduled_sends").fetchone()[0] == 0)
u = make()
cold = mprop("$m4", send_at=now + 60 * MIN, ots=now)
u.forward_proposals(MPR, LPR, [cold], cold_start=True, suspended=False)
check("manager-timed: a cold start never arms a schedule",
      len(u.records) == 1 and u.db.execute("SELECT COUNT(*) FROM scheduled_sends"
                                           ).fetchone()[0] == 0)

# ---------------------------------------------------------------------------
# Health counters (fix 12)
# ---------------------------------------------------------------------------
u = make()
arm(u, sched_event("$h1", send_at=now + 60 * MIN))
u.db.execute("INSERT OR REPLACE INTO scheduled_sends (event_id, room_hash, send_at, origin_ts, "
             "state, authorship) VALUES ('$h2','h',1,1,'held','teammate')")
u.db.commit()
health = uplink.Uplink.sync_health(u)
check("health: scheduled_pending / scheduled_held / oldest_scheduled_ts are ints",
      health["scheduled_pending"] == 1 and health["scheduled_held"] == 1
      and isinstance(health["oldest_scheduled_ts"], int))
check("health: the three counters are published to the master",
      all(k in uplink.durable_sync.DurableSync.HEALTH_TO_MASTER_FIELDS
          for k in ("scheduled_pending", "scheduled_held", "oldest_scheduled_ts")))

print("%d passed, %d failed" % (passed, failed))
sys.exit(1 if failed else 0)
