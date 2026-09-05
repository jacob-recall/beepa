#!/usr/bin/env python3
"""Unit tests for the uplink proposal pull path (V2 down-direction).

Context (found live 2026-08-29): the manager's "sent" proposals had never
reached the master proposals room at all (console-side, pre-HTTP), so the
pull path had effectively never run against real traffic. A live end-to-end
probe (manager PUT -> master room -> pull_proposals -> local proposals room)
passed; these tests pin the safety rules of forward_proposals /
_sanitize_proposal so the down-direction stays correct as it starts carrying
real proposals:

  - writes go ONLY to the RECORDED local proposals room (meta), never a
    caller-supplied room;
  - a proposals target colliding with a mirror room id is refused outright;
  - a master proposal event forwards exactly once (proposal_map dedup);
  - a malformed proposal is recorded as handled and never posted or retried;
  - only whitelisted fields are carried down, the event type is the literal
    com.jkali.proposal, and target_room is validated by shape only.

Run: python3 tests/unit/uplink_proposals.test.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "agents", "uplink"))
import uplink

passed = 0
failed = 0


def check(name, cond):
    global passed, failed
    if cond:
        passed += 1
    else:
        failed += 1
        print("FAIL: " + name)


def make_uplink(recorded="!props:localhost", mirrors=()):
    """A minimal Uplink over the REAL state.db schema + a capturing transport.

    The schema comes from Uplink._open_db (which also runs the versioned
    migration) rather than a hand-rolled copy, so a schema change cannot leave
    these tests passing against a shape the daemon no longer uses.
    """
    u = object.__new__(uplink.Uplink)
    # This fixture isolates proposal framing/dedup; live connection-control
    # reads are covered by uplink_durable_sync.test.py.
    u.active_link_for_dispatch = lambda: True
    u.db = uplink.Uplink._open_db(
        os.path.join(tempfile.mkdtemp(prefix="uplink-props-"), "state.db"))
    for local_id, master_id in mirrors:
        u.db.execute("INSERT INTO mirror_rooms (local_room_id, master_room_id) "
                     "VALUES (?,?)", (local_id, master_id))
    if recorded is not None:
        u.db.execute("INSERT INTO meta (k,v) VALUES ('local_proposals_room',?)",
                     (recorded,))
    u.db.commit()
    u.sent = []

    def local(method, path, body=None, query=None, timeout=60):
        u.sent.append((method, path, body))
        return {"event_id": "$local_%d" % len(u.sent)}

    u.local = local
    return u


def proposal_ev(eid="$m1", target="!conv:localhost", body="hello", **extra):
    c = {"target_room": target, "body": body}
    c.update(extra)
    return {"type": "com.jkali.proposal", "event_id": eid, "sender": "@manager:master",
            "origin_server_ts": 1234, "content": c}


san = lambda ev: uplink.Uplink._sanitize_proposal(None, ev)

# ---- _sanitize_proposal: whitelist + shape gates --------------------------

out = san(proposal_ev(created_by="@manager:master", origin_ts=99,
                      formatted_body="<b>x</b>", html="<b>x</b>"))
check("sanitize keeps only whitelisted fields",
      out is not None and set(out) == {"target_room", "body", "created_by",
                                       "origin_ts", "com.jkali.proposal_source_event"})
check("sanitize carries target/body/created_by/origin_ts",
      out["target_room"] == "!conv:localhost" and out["body"] == "hello"
      and out["created_by"] == "@manager:master" and out["origin_ts"] == 99)
check("sanitize records provenance event id",
      out["com.jkali.proposal_source_event"] == "$m1")

out = san(proposal_ev())
check("sanitize falls back to sender/origin_server_ts",
      out["created_by"] == "@manager:master" and out["origin_ts"] == 1234)

check("sanitize passes template:True through",
      san(proposal_ev(template=True)).get("template") is True)
check("sanitize drops non-True template",
      "template" not in san(proposal_ev(template="yes")))

check("sanitize refuses malformed target_room shape",
      san(proposal_ev(target="not-a-room")) is None)
check("sanitize refuses missing target_room",
      san({"type": "com.jkali.proposal", "event_id": "$x",
           "content": {"body": "hi"}}) is None)
check("sanitize refuses empty body", san(proposal_ev(body="   ")) is None)
check("sanitize refuses non-string body", san(proposal_ev(body=7)) is None)

# ---- forward_proposals: write-target assertions ---------------------------

u = make_uplink()
n = u.forward_proposals("!mprops:master", "!SOMEWHERE-ELSE:localhost",
                        [proposal_ev()])
check("refuses a target that is not the recorded proposals room",
      n == 0 and u.sent == [])

u = make_uplink(recorded=None)
n = u.forward_proposals("!mprops:master", "!props:localhost", [proposal_ev()])
check("refuses when no local proposals room is recorded", n == 0 and u.sent == [])

u = make_uplink(recorded="!props:localhost",
                mirrors=[("!conv:localhost", "!props:localhost")])
n = u.forward_proposals("!mprops:master", "!props:localhost", [proposal_ev()])
check("refuses a proposals target colliding with a mirror room id",
      n == 0 and u.sent == [])

# ---- forward_proposals: happy path, type pinning, dedup -------------------

u = make_uplink()
n = u.forward_proposals("!mprops:master", "!props:localhost",
                        [proposal_ev("$m1"), {"type": "m.room.message",
                                              "event_id": "$msg",
                                              "content": {"body": "not a proposal"}}])
check("posts exactly the one valid proposal", n == 1 and len(u.sent) == 1)
method, path, body = u.sent[0]
check("posts into the recorded room with the literal proposal type",
      method == "PUT" and "/rooms/%21props%3Alocalhost/send/com.jkali.proposal/" in path)
check("posted content is the sanitized whitelist", body["body"] == "hello"
      and body["com.jkali.proposal_source_event"] == "$m1")

n = u.forward_proposals("!mprops:master", "!props:localhost", [proposal_ev("$m1")])
check("a replayed master event is not forwarded twice (proposal_map dedup)",
      n == 0 and len(u.sent) == 1)
row = u.db.execute("SELECT local_event_id FROM proposal_map "
                   "WHERE master_event_id='$m1'").fetchone()
check("proposal_map records the local event id", row and row[0] == "$local_1")

# ---- forward_proposals: malformed events are handled once -----------------

u = make_uplink()
bad = proposal_ev("$bad", target="junk")
n = u.forward_proposals("!mprops:master", "!props:localhost", [bad])
check("malformed proposal is never posted", n == 0 and u.sent == [])
row = u.db.execute("SELECT master_event_id, local_event_id FROM proposal_map "
                   "WHERE master_event_id='$bad'").fetchone()
check("malformed proposal is recorded as handled (NULL local id)",
      row is not None and row[1] is None)
n = u.forward_proposals("!mprops:master", "!props:localhost", [bad])
check("malformed proposal is not retried", n == 0 and u.sent == [])

# ---- forward_proposals: missing event id is skipped ------------------------

u = make_uplink()
ev = proposal_ev()
del ev["event_id"]
n = u.forward_proposals("!mprops:master", "!props:localhost", [ev])
check("event without an id is skipped", n == 0 and u.sent == [])

print("%d passed, %d failed" % (passed, failed))
sys.exit(1 if failed else 0)
