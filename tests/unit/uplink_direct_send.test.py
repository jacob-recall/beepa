#!/usr/bin/env python3
"""Unit tests for D2 — the uplink's `direct`-level auto-send (S3).

This is the ONE path in the whole system where code, not the teammate, puts a
message into a real conversation on their real accounts. The plan's threat
model is explicit that a compromised manager session or master homeserver is
then a remote send capability, and that everything bounding it is the eleven
gates in agents/uplink/uplink.py. These tests are those bounds:

  D2.1  a sender that is not the freshly-resolved manager mxid is refused, the
        teammate's own scoped master account (PL 100 in that room) is refused
        by name, and a `created_by` claiming to be the manager changes nothing;
  D2.2  send-grade sanitization: control/bidi/zero-width stripped, 8000 clamp,
        and any body whose first non-whitespace char is '!' refused outright;
  D2.3  auto-send needs an INCREMENTAL sync and a fresh event — a cold start
        (no watermark / empty dedup map) routes the whole batch to the inbox,
        as does a stale origin_server_ts;
  D2.4  the target must be room-targeted AND in the mirrored set — a
        person-targeted proposal and an unmirrored room both fall back;
  D2.5  the level is re-read at SEND time, so a direct->private flip between
        pull and send lands in the inbox; any read error means "not direct";
  D2.6  the per-room hourly cap is persisted, so it survives a restart;
  D2.7  intent is committed BEFORE the PUT with a deterministic txn id, so an
        interrupted send is recoverable and never replays as a second send;
  D2.8/9 exactly ONE inbox artifact per proposal on every path, flagged
        auto_sent / send_ambiguous / plain-draft per the S2 wire contract;
  D2.10 a durable, hash-only audit row (never a body, never a room id);
  D2.11 a master-identity rebinding suspends auto-send until the teammate's
        ack matches that exact identity tuple.

Plus the S3 schema migration (a pre-S3 state.db drives both paths with no
sqlite error) and the one-time proposals-room topic re-PUT.

Run: python3 tests/unit/uplink_direct_send.test.py
"""
import logging
import os
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

# Capture the daemon's log instead of printing it, so the run stays quiet AND
# the hash-only logging rule can be asserted at the end of the file.
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
LPR = "!props:localhost"
MPR = "!mprops:master"
IDENTITY = (MASTER_HS, MASTER_USER, MANAGER)

READ_ERROR = object()   # sentinel: make the level point-read fail


class HardCrash(BaseException):
    """Escapes _auto_send's `except Exception` — models the process being killed
    between the send PUT and the outcome commit."""


def make_uplink(path=None, mirrors=((CONV, "!m1:master"),), levels=None, cap=20,
                identity=IDENTITY, bind=True, since="s1", seed_map=True,
                send_error=None, crash_after_send=False, ack=None, suspended_ts=None):
    """A real state.db (via the daemon's own _open_db + migration) and stubbed
    transports. `path` may point at an existing db to model a restart."""
    u = object.__new__(uplink.Uplink)
    u.db_path = path or os.path.join(tempfile.mkdtemp(prefix="uplink-direct-"), "state.db")
    u.db = uplink.Uplink._open_db(u.db_path)
    for lid, mid in mirrors:
        u.db.execute("INSERT OR REPLACE INTO mirror_rooms (local_room_id, master_room_id, "
                     "source, last_synced_pos) VALUES (?,?,?,?)", (lid, mid, "imessage", None))
    for k, v in (("local_proposals_room", LPR), ("master_proposals_room", MPR),
                 ("proposal_sync_since", since),
                 ("proposal_identity", "\n".join(identity) if bind else None),
                 (uplink.Uplink.SUSPENDED_META, str(suspended_ts) if suspended_ts else None)):
        if v is not None:
            u.db.execute("INSERT OR REPLACE INTO meta (k,v) VALUES (?,?)", (k, v))
    if seed_map:
        # A non-empty proposal_map is half of "not a cold start" (D2.3).
        u.db.execute("INSERT OR REPLACE INTO proposal_map (master_event_id, local_event_id, "
                     "outcome) VALUES ('$seed','$l0','fallback')")
    u.db.commit()
    u.cfg = types.SimpleNamespace(
        local_user=LOCAL_USER, local_hs="http://127.0.0.1:8008",
        master_hs=identity[0], master_user=identity[1], manager_mxid=identity[2],
        master_space="!space:master", direct_send_cap=cap, sync_timeout=1000)
    u._direct_suspended = True
    u.levels = dict(levels if levels is not None else {CONV: "direct"})
    u.ack = ack
    u.send_error = send_error
    u.crash_after_send = crash_after_send
    u.sends = []      # (path, content) for every m.room.message PUT
    u.records = []    # (path, content) for every com.jkali.proposal PUT
    u.acct = []       # (path, content) for every account-data PUT
    u.topics = []     # ('local'|'master', content) for every topic PUT
    u.master_sync = {"next_batch": "s2", "rooms": {"join": {}}}

    def nf(path):
        return urllib.error.HTTPError(path, 404, "Not Found", None, None)

    def local(method, path, body=None, query=None, timeout=60):
        if method == "GET":
            if "/account_data/" + consent.SHARE_OVERRIDE_TYPE in path:
                for rid, lv in u.levels.items():
                    if urllib.parse.quote(rid, safe="") in path:
                        if lv is READ_ERROR:
                            raise urllib.error.URLError("boom")
                        return {"state": lv}
                raise nf(path)
            if path.endswith("/account_data/" + uplink.DIRECT_SEND_ACK_TYPE):
                if u.ack is None:
                    raise nf(path)
                return u.ack
            raise nf(path)
        if method == "PUT":
            if "/send/m.room.message/" in path:
                u.sends.append((path, body))
                if u.crash_after_send:
                    raise HardCrash("killed between PUT and commit")
                if u.send_error is not None:
                    raise u.send_error
                return {"event_id": "$sent%d" % len(u.sends)}
            if "/send/" + uplink.PROPOSAL_TYPE + "/" in path:
                u.records.append((path, body))
                return {"event_id": "$rec%d" % len(u.records)}
            if "/state/m.room.topic" in path:
                u.topics.append(("local", body))
                return {}
            if "/account_data/" in path:
                u.acct.append((path, body))
                return {}
        raise AssertionError("unexpected local %s %s" % (method, path))

    def master(method, path, body=None, query=None, timeout=60):
        if method == "GET" and "/sync" in path:
            return u.master_sync
        if method == "PUT" and "/state/m.room.topic" in path:
            u.topics.append(("master", body))
            return {}
        raise AssertionError("unexpected master %s %s" % (method, path))

    u.local = local
    u.master = master
    return u


def prop(eid="$m1", target=CONV, body="hello there", sender=MANAGER, ts=None, **extra):
    c = {"target_room": target, "body": body}
    c.update(extra)
    if target is None:
        c.pop("target_room")
    return {"type": uplink.PROPOSAL_TYPE, "event_id": eid, "sender": sender,
            "origin_server_ts": int(time.time() * 1000) if ts is None else ts,
            "content": c}


def fwd(u, events, cold_start=False, suspended=False):
    return u.forward_proposals(MPR, LPR, events, cold_start=cold_start, suspended=suspended)


def outcome(u, meid="$m1"):
    r = u.db.execute("SELECT outcome FROM proposal_map WHERE master_event_id=?",
                     (meid,)).fetchone()
    return r[0] if r else None


def audits(u):
    return [r[0] for r in u.db.execute("SELECT outcome FROM direct_send_audit").fetchall()]


def only_record(u):
    """The single filed inbox artifact's content (asserts there is exactly one)."""
    return u.records[0][1] if len(u.records) == 1 else None


def refused(u, label, events=None):
    """Every refusal shares one shape: no send, exactly ONE plain actionable
    draft in the inbox, and no auto_sent/ambiguous flag on it."""
    n = fwd(u, events if events is not None else [prop()])
    rec = only_record(u)
    check(label + ": nothing sent", u.sends == [])
    check(label + ": exactly one inbox artifact", n == 1 and rec is not None)
    check(label + ": the artifact is a plain actionable draft",
          rec is not None and uplink.AUTO_SENT_KEY not in rec
          and uplink.SEND_AMBIGUOUS_KEY not in rec)


# ---------------------------------------------------------------------------
# Happy path — the shape everything else is a refusal of.
# ---------------------------------------------------------------------------
u = make_uplink()
n = fwd(u, [prop()])
check("direct: exactly one message sent", len(u.sends) == 1)
send_path, send_body = u.sends[0]
check("direct: sent into the TARGET conversation, as m.room.message",
      urllib.parse.quote(CONV, safe="") in send_path and "/send/m.room.message/" in send_path)
check("direct: deterministic txn id autosend_<master_event_id> (D2.7)",
      send_path.endswith("/autosend_%24m1"))
check("direct: body and msgtype", send_body["body"] == "hello there"
      and send_body["msgtype"] == "m.text")
check("direct: cosmetic provenance field names the master event (F14)",
      send_body[uplink.AUTO_SENT_FROM_PROPOSAL_KEY] == "$m1")
rec = only_record(u)
check("direct: exactly one inbox artifact", n == 1 and rec is not None)
check("direct: the artifact is the non-actionable auto_sent record (wire contract)",
      rec[uplink.AUTO_SENT_KEY] is True and rec[uplink.SENT_EVENT_ID_KEY] == "$sent1"
      and uplink.SEND_AMBIGUOUS_KEY not in rec)
check("direct: the record still carries the proposal fields",
      rec["target_room"] == CONV and rec["body"] == "hello there")
check("direct: proposal_map outcome is 'sent'", outcome(u) == "sent")
check("direct: durable audit row (D2.10)", audits(u) == ["sent"])
rows = u.db.execute("SELECT ts, room_hash FROM direct_send_log").fetchall()
check("direct: one rate-cap tick recorded (D2.6)", len(rows) == 1)
check("direct: rate-cap/audit tables are HASH-ONLY (no room id in the clear)",
      all(CONV not in str(r) for r in
          u.db.execute("SELECT * FROM direct_send_log").fetchall()
          + u.db.execute("SELECT * FROM direct_send_audit").fetchall())
      and rows[0][1] == uplink.Uplink._room_hash(CONV))

# A replay of the same master event neither re-sends nor files a second artifact.
n2 = fwd(u, [prop()])
check("direct: replayed master event is not re-sent or re-filed",
      n2 == 0 and len(u.sends) == 1 and len(u.records) == 1)

# ---------------------------------------------------------------------------
# D2.1 — sender verification (F1/F16)
# ---------------------------------------------------------------------------
refused(make_uplink(), "sender mismatch", [prop(sender="@intruder:master")])
refused(make_uplink(), "master_user as sender",
        [prop(sender=MASTER_USER)])
refused(make_uplink(), "missing sender", [prop(sender=None)])
u = make_uplink(identity=(MASTER_HS, MASTER_USER, ""))
u.db.execute("UPDATE meta SET v=? WHERE k='proposal_identity'",
             ("\n".join((MASTER_HS, MASTER_USER, "")),))
refused(u, "unconfigured manager mxid", [prop(sender=MANAGER)])

# created_by is cosmetic: a spoofed one neither authorizes a send nor survives.
u = make_uplink()
fwd(u, [prop(sender="@intruder:master", created_by=MANAGER)])
check("created_by spoof does not authorize a send", u.sends == [])
check("created_by is pinned to the server-stamped sender, not content (F16)",
      only_record(u)["created_by"] == "@intruder:master")
u = make_uplink()
fwd(u, [prop(created_by="@someone-else:master")])
check("created_by is overwritten even on the auto-send path",
      len(u.sends) == 1 and only_record(u)["created_by"] == MANAGER)

# ---------------------------------------------------------------------------
# D2.2 — send-grade sanitization (F2)
# ---------------------------------------------------------------------------
sb = uplink.sanitize_send_body
check("sanitize_send_body refuses a leading '!'", sb("!wa help") is None)
check("sanitize_send_body refuses a '!' after whitespace", sb("   \n !wa ping") is None)
check("sanitize_send_body refuses a '!' hidden behind a zero-width char",
      sb("​!wa help") is None)
check("sanitize_send_body allows '!' that is not first", sb("hi! there") == "hi! there")
check("sanitize_send_body strips control/bidi/zero-width",
      sb("a‮b​c\x07d\te") == "abcde")
check("sanitize_send_body keeps newlines", sb("a\nb") == "a\nb")
check("sanitize_send_body clamps to 8000", len(sb("x" * 9000)) == 8000)
check("sanitize_send_body refuses blank/whitespace-only", sb("   \n ") is None
      and sb("​​") is None)
check("sanitize_send_body refuses a non-string", sb(7) is None and sb(None) is None)

refused(make_uplink(), "'!'-prefixed body", [prop(body="!wa help")])
refused(make_uplink(), "bare '!' body", [prop(body=" ! ")])

u = make_uplink()
fwd(u, [prop(body="clean‮​\x07 text")])
check("control/bidi chars are stripped from what is actually sent",
      len(u.sends) == 1 and u.sends[0][1]["body"] == "clean text")
u = make_uplink()
fwd(u, [prop(body="y" * 9000)])
check("an oversized body is clamped to 8000 before sending",
      len(u.sends) == 1 and len(u.sends[0][1]["body"]) == 8000)

# ---------------------------------------------------------------------------
# D2.3 — freshness / replay bound (F3)
# ---------------------------------------------------------------------------
u = make_uplink()
n = fwd(u, [prop("$a"), prop("$b"), prop("$c")], cold_start=True)
check("cold start: the WHOLE batch routes to the inbox, nothing sent",
      u.sends == [] and n == 3 and len(u.records) == 3)
check("cold start: every artifact is a plain draft",
      all(uplink.AUTO_SENT_KEY not in b and uplink.SEND_AMBIGUOUS_KEY not in b
          for _, b in u.records))

now_ms = int(time.time() * 1000)
refused(make_uplink(), "stale origin_server_ts",
        [prop(ts=now_ms - 11 * 60 * 1000)])
refused(make_uplink(), "implausibly future origin_server_ts",
        [prop(ts=now_ms + 10 * 60 * 1000)])
no_ts = prop()
del no_ts["origin_server_ts"]
refused(make_uplink(), "missing origin_server_ts", [no_ts])
bool_ts = prop()
bool_ts["origin_server_ts"] = True          # bool is an int in Python; not a timestamp
refused(make_uplink(), "boolean origin_server_ts", [bool_ts])

# pull_proposals derives cold_start itself: no watermark, or an empty dedup map.
for label, kw in (("no watermark", {"since": None}), ("empty proposal_map", {"seed_map": False})):
    u = make_uplink(**kw)
    u._direct_suspended = False
    u.master_sync = {"next_batch": "s9",
                     "rooms": {"join": {MPR: {"timeline": {"events": [prop()]}}}}}
    u.pull_proposals()
    check("pull_proposals treats '%s' as a cold start" % label,
          u.sends == [] and len(u.records) == 1)

# An INCREMENTAL sync with a seeded map does auto-send — proving the two cold
# start tests above fail for the reason claimed, not because pull is inert.
u = make_uplink()
u._direct_suspended = False
u.master_sync = {"next_batch": "s9",
                 "rooms": {"join": {MPR: {"timeline": {"events": [prop()]}}}}}
u.pull_proposals()
check("pull_proposals auto-sends on an incremental sync with a seeded map",
      len(u.sends) == 1 and only_record(u)[uplink.AUTO_SENT_KEY] is True)

# ---------------------------------------------------------------------------
# D2.4 — positive target check (F9)
# ---------------------------------------------------------------------------
refused(make_uplink(), "target outside the mirrored set",
        [prop(target=OTHER)])
refused(make_uplink(mirrors=()), "no mirrors at all", [prop()])
u = make_uplink()
n = fwd(u, [prop(target=None, target_source="imessage",
                 target_identifier="+14155550123")])
check("person-targeted proposal is never auto-sent", u.sends == [] and n == 1)
check("person-targeted proposal is filed as a plain actionable draft",
      only_record(u).get("target_identifier") == "+14155550123"
      and uplink.AUTO_SENT_KEY not in only_record(u))

# ---------------------------------------------------------------------------
# D2.5 — fresh consent point-read at send time (F10)
# ---------------------------------------------------------------------------
refused(make_uplink(levels={CONV: "private"}), "direct->private flip before send")
refused(make_uplink(levels={CONV: "share"}), "share level")
refused(make_uplink(levels={CONV: "junk"}), "unrecognized level")
refused(make_uplink(levels={}), "no override stored (absent = private)")
refused(make_uplink(levels={CONV: READ_ERROR}), "consent read error")
u = make_uplink()
check("read_room_level refuses a malformed room id",
      u.read_room_level("not-a-room") == "private" and u.read_room_level(None) == "private")

# ---------------------------------------------------------------------------
# D2.6 — persisted rolling per-room cap, and its survival across a restart
# ---------------------------------------------------------------------------
u = make_uplink(cap=2)
fwd(u, [prop("$c1"), prop("$c2")])
check("cap: sends up to the cap", len(u.sends) == 2)
n = fwd(u, [prop("$c3")])
check("cap: the next proposal is refused and lands in the inbox",
      len(u.sends) == 2 and n == 1 and outcome(u, "$c3") == "fallback")
check("cap: the refusal is audited (D2.10)", "refused:cap" in audits(u))

db_path = u.db_path
u2 = make_uplink(path=db_path, cap=2)
n = fwd(u2, [prop("$c4")])
check("cap SURVIVES a restart (fresh Uplink over the same state.db)",
      u2.sends == [] and n == 1 and outcome(u2, "$c4") == "fallback")
# Ticks older than the window no longer count.
u2.db.execute("UPDATE direct_send_log SET ts = ts - ?", (uplink.DIRECT_SEND_WINDOW_S + 60,))
u2.db.commit()
fwd(u2, [prop("$c5")])
check("cap: the window rolls — expired ticks stop counting", len(u2.sends) == 1)

refused(make_uplink(cap=0), "cap of 0 disables auto-send entirely")

# ---------------------------------------------------------------------------
# D2.7/D2.9 — interrupted send: recovered as AMBIGUOUS, never re-sent
# ---------------------------------------------------------------------------
u = make_uplink(crash_after_send=True)
crashed = False
try:
    fwd(u, [prop()])
except HardCrash:
    crashed = True
check("crash: the interruption escapes (models a killed process)", crashed)
check("crash: the PUT was dispatched", len(u.sends) == 1)
check("crash: intent was committed BEFORE the PUT (D2.7)", outcome(u) == "attempted")
check("crash: no inbox artifact was filed yet", u.records == [])
first_txn = u.sends[0][0]

u2 = make_uplink(path=u.db_path)          # restart over the same state.db
n = fwd(u2, [prop()])
check("crash recovery: NO duplicate send", u2.sends == [])
check("crash recovery: exactly one inbox artifact", n == 1 and len(u2.records) == 1)
rec = only_record(u2)
check("crash recovery: labelled send_ambiguous, never a plain pending draft",
      rec[uplink.SEND_AMBIGUOUS_KEY] is True and uplink.AUTO_SENT_KEY not in rec)
check("crash recovery: outcome recorded", outcome(u2) == "ambiguous")
check("crash recovery: audited", "ambiguous_recovered" in audits(u2))
n = fwd(u2, [prop()])
check("crash recovery: a second replay files nothing more",
      n == 0 and len(u2.records) == 1 and u2.sends == [])
check("the send txn id is deterministic, so a retry the hs DID receive dedups",
      first_txn.endswith("/autosend_%24m1"))

# A transport failure on the PUT is the same ambiguous case, in one pass.
u = make_uplink(send_error=urllib.error.URLError("connection reset"))
n = fwd(u, [prop()])
check("transport failure mid-send: exactly one artifact, flagged ambiguous",
      n == 1 and only_record(u)[uplink.SEND_AMBIGUOUS_KEY] is True)
check("transport failure mid-send: audited as ambiguous", audits(u) == ["ambiguous"])
u = make_uplink(send_error=urllib.error.HTTPError(CONV, 502, "Bad Gateway", None, None))
fwd(u, [prop()])
check("a 5xx is treated as ambiguous (it may have been applied)",
      only_record(u)[uplink.SEND_AMBIGUOUS_KEY] is True)
# A 4xx is a KNOWN refusal: nothing was sent, so the teammate gets a real draft.
u = make_uplink(send_error=urllib.error.HTTPError(CONV, 403, "Forbidden", None, None))
n = fwd(u, [prop()])
rec = only_record(u)
check("a 4xx refusal files the ordinary actionable draft (nothing was sent)",
      n == 1 and uplink.SEND_AMBIGUOUS_KEY not in rec and uplink.AUTO_SENT_KEY not in rec)
check("a 4xx refusal is audited as failed", audits(u) == ["failed"])

# ---------------------------------------------------------------------------
# D2.11 — master-identity binding (F12)
# ---------------------------------------------------------------------------
u = make_uplink()
check("binding: an unchanged identity is not suspended",
      u.refresh_direct_send_binding() is False)

NEW_MANAGER = "@newmanager:master"
u = make_uplink(bind=True)
u.cfg.manager_mxid = NEW_MANAGER
suspended = u.refresh_direct_send_binding()
check("rebinding: auto-send is suspended", suspended is True)
sus = [b for p, b in u.acct if uplink.DIRECT_SEND_SUSPENDED_TYPE in p]
check("rebinding: the suspension is surfaced to apps/user with the new tuple",
      len(sus) == 1 and sus[0]["master_hs"] == MASTER_HS
      and sus[0]["master_user"] == MASTER_USER
      and sus[0]["manager_mxid"] == NEW_MANAGER
      and isinstance(sus[0]["ts"], int))
check("rebinding: discovery/cursor invalidated but prior proposal safety outcomes survive",
      u.meta_get("master_proposals_room") is None
      and u.meta_get("proposal_sync_since") is None
      and u.db.execute("SELECT COUNT(*) FROM proposal_map").fetchone()[0] > 0)
n = fwd(u, [prop(sender=NEW_MANAGER)], suspended=suspended)
check("rebinding: auto-send is refused while suspended",
      u.sends == [] and n == 1 and uplink.AUTO_SENT_KEY not in only_record(u))

ts = sus[0]["ts"]
u.ack = {"master_hs": MASTER_HS, "master_user": MASTER_USER,
         "manager_mxid": MANAGER, "ts": ts}          # ack for the OLD manager
check("rebinding: an ack for a DIFFERENT identity does not resume",
      u.refresh_direct_send_binding() is True)
u.ack = {"master_hs": MASTER_HS, "master_user": MASTER_USER,
         "manager_mxid": NEW_MANAGER, "ts": ts + 1}  # right tuple, wrong ts
check("rebinding: an ack with a stale ts does not resume",
      u.refresh_direct_send_binding() is True)
u.ack = {"master_hs": MASTER_HS, "master_user": MASTER_USER,
         "manager_mxid": NEW_MANAGER, "ts": ts}
check("rebinding: the matching ack resumes auto-send",
      u.refresh_direct_send_binding() is False)
check("rebinding: the suspension marker is cleared",
      u.meta_get(uplink.Uplink.SUSPENDED_META) is None)
u.db.execute("INSERT OR REPLACE INTO meta (k,v) VALUES ('local_proposals_room',?)", (LPR,))
u.db.commit()
fwd(u, [prop("$after-ack", sender=NEW_MANAGER)])
check("rebinding: auto-send works again once re-confirmed",
      len(u.sends) == 1 and outcome(u, "$after-ack") == "sent")

u = make_uplink(suspended_ts="not-a-number")
check("binding: an unreadable suspension marker stays suspended",
      u.refresh_direct_send_binding() is True)
u = make_uplink(bind=False)
check("binding: the FIRST bind (no stored identity) is adoption, not a rebinding",
      u.refresh_direct_send_binding() is False and u.acct == []
      and u.meta_get("master_proposals_room") == MPR)

# ---------------------------------------------------------------------------
# Exactly ONE inbox artifact per proposal, in every path, across one batch
# ---------------------------------------------------------------------------
u = make_uplink(levels={CONV: "direct", OTHER: "share"},
                mirrors=((CONV, "!m1:master"), (OTHER, "!m2:master")))
events = [prop("$p1"),                                    # auto-sent
          prop("$p2", target=OTHER),                      # share level -> draft
          prop("$p3", body="!wa help"),                   # refused -> draft
          prop("$p4", sender="@intruder:master"),         # refused -> draft
          prop("$p5", target=None, target_source="imessage",
               target_identifier="+14155550123"),         # person -> draft
          prop("$p6", body="   ")]                        # malformed -> no artifact
n = fwd(u, events)
check("mixed batch: exactly one message sent", len(u.sends) == 1)
check("mixed batch: one artifact per VALID proposal, none for the malformed one",
      n == 5 and len(u.records) == 5)
ids = [p.split("/proposal_")[-1] for p, _ in u.records]
check("mixed batch: each proposal filed exactly once",
      sorted(ids) == sorted(urllib.parse.quote("$p%d" % i, safe="") for i in range(1, 6)))
check("mixed batch: exactly one artifact carries auto_sent",
      sum(1 for _, b in u.records if b.get(uplink.AUTO_SENT_KEY)) == 1)
check("mixed batch: the malformed proposal is recorded handled, never posted",
      outcome(u, "$p6") == "fallback")

# ---------------------------------------------------------------------------
# Schema migration: a PRE-S3 state.db drives both paths with no sqlite error
# ---------------------------------------------------------------------------
old_path = os.path.join(tempfile.mkdtemp(prefix="uplink-pre-s3-"), "state.db")
old = sqlite3.connect(old_path)
old.execute("CREATE TABLE mirror_rooms (local_room_id TEXT PRIMARY KEY, "
            "master_room_id TEXT UNIQUE, source TEXT, last_synced_pos TEXT)")
old.execute("CREATE TABLE event_map (local_event_id TEXT PRIMARY KEY, master_event_id TEXT)")
old.execute("CREATE TABLE proposal_map (master_event_id TEXT PRIMARY KEY, local_event_id TEXT)")
old.execute("CREATE TABLE contact_mirror (source TEXT, network_id TEXT, "
            "mirrored_version INTEGER, master_state_key TEXT, PRIMARY KEY(source, network_id))")
old.execute("CREATE TABLE meta (k TEXT PRIMARY KEY, v TEXT)")
old.execute("INSERT INTO proposal_map (master_event_id, local_event_id) VALUES ('$old','$lold')")
old.commit()
old.close()
check("pre-S3 db starts at user_version 0",
      sqlite3.connect(old_path).execute("PRAGMA user_version").fetchone()[0] == 0)

err = None
try:
    u = make_uplink(path=old_path)            # _open_db runs the migration
    n1 = fwd(u, [prop("$s1", target=OTHER)])  # inbox forward (unmirrored target)
    n2 = fwd(u, [prop("$s2")])                # auto-send
except sqlite3.Error as e:                    # noqa: BLE001 — the thing under test
    err = e
check("pre-S3 db: no sqlite error running the S3 code", err is None)
check("pre-S3 db: one inbox forward and one auto-send both work",
      err is None and n1 == 1 and n2 == 1 and len(u.sends) == 1 and len(u.records) == 2)
check("pre-S3 db: schema is at the S3 version",
      u.db.execute("PRAGMA user_version").fetchone()[0] == uplink.SCHEMA_VERSION)
check("pre-S3 db: the pre-existing proposal_map row survived with a NULL outcome",
      u.db.execute("SELECT local_event_id, outcome FROM proposal_map "
                   "WHERE master_event_id='$old'").fetchone() == ("$lold", None))
check("pre-S3 db: an old NULL-outcome row is still treated as handled",
      fwd(u, [prop("$old")]) == 0)
check("migration is idempotent (re-opening changes nothing)",
      uplink.Uplink._open_db(old_path).execute("PRAGMA user_version").fetchone()[0]
      == uplink.SCHEMA_VERSION)

# ---------------------------------------------------------------------------
# Room-topic copy: the absolutes are gone, and the re-PUT happens exactly once
# ---------------------------------------------------------------------------
for topic in (uplink.MASTER_PROPOSALS_TOPIC, uplink.LOCAL_PROPOSALS_TOPIC):
    check("topic copy states the Direct exception", "Direct" in topic)
check("topic copy drops the pre-D2 absolutes",
      "nothing here is sent automatically" not in uplink.LOCAL_PROPOSALS_TOPIC
      and "nothing here is ever sent externally" not in uplink.MASTER_PROPOSALS_TOPIC)

u = make_uplink()
u.refresh_proposal_topics(MPR, LPR)
check("topic re-PUT: both rooms updated once",
      sorted(w for w, _ in u.topics) == ["local", "master"])
check("topic re-PUT: the new copy is what was written",
      dict(u.topics)["master"]["topic"] == uplink.MASTER_PROPOSALS_TOPIC
      and dict(u.topics)["local"]["topic"] == uplink.LOCAL_PROPOSALS_TOPIC)
u.refresh_proposal_topics(MPR, LPR)
check("topic re-PUT happens EXACTLY once (state.db flag)", len(u.topics) == 2)
u2 = make_uplink(path=u.db_path)
u2.refresh_proposal_topics(MPR, LPR)
check("topic re-PUT: the flag survives a restart", u2.topics == [])

# ---------------------------------------------------------------------------
# Hash-only logging (a body or a room id in the log would defeat the whole
# point of hashing them in state.db).
# ---------------------------------------------------------------------------
check("log lines name the failed gate", any("gate 'sanitize'" in m for m in LOG_LINES))
check("no log line names a CONVERSATION room id (only its hash)",
      not any(CONV in m or OTHER in m for m in LOG_LINES))
check("no log line contains a message body",
      not any("hello there" in m or "!wa help" in m for m in LOG_LINES))

print("%d passed, %d failed" % (passed, failed))
sys.exit(1 if failed else 0)
