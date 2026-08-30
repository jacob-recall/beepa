#!/usr/bin/env python3
"""Unit tests for the uplink's pure proposal-sanitize whitelist logic.

Covers pm_mng-q5u.3: agents/uplink/uplink.py's sanitize_proposal_content()
must accept BOTH proposal shapes coming DOWN from the master, and fail closed
on anything malformed:

  - ROOM proposal:  valid target_room (SHAPE only) -> carries target_room, no
    identifier keys. Existing behavior, unchanged.
  - PERSON-TARGETED proposal:  no target_room, but a valid E.164 OR strict-email
    target_identifier, a short lowercase target_source, and a non-empty body ->
    carries target_source/target_identifier/target_display?, NO target_room.
  - anything else (bad handle, bad/empty source, empty body, neither target) ->
    None (dropped, recorded handled, never retried).
  - target_room precedence: if a valid target_room is present the room shape
    wins and no identifier keys leak.

The identifier carried here is inert data; the teammate's guarded start-chat
path re-validates it authoritatively before anything is sent.

Run: python3 tests/unit/uplink_proposal_sanitize.test.py  (exit 0 = all pass).
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "..", "agents", "uplink"))
from uplink import sanitize_proposal_content  # noqa: E402

_pass = 0
_fail = 0
_failures = []


def check(cond, label):
    global _pass, _fail
    if cond:
        _pass += 1
    else:
        _fail += 1
        _failures.append(label)


def san(content):
    # Fixed envelope fields; the function under test is pure.
    return sanitize_proposal_content(
        content, sender="@mgr:master", event_id="$evt1", origin_ts=1000)


# ---- (a) valid room proposal -> carries target_room, no identifier keys ----
room = san({"target_room": "!abc:local", "body": "hi there",
            "created_by": "@mgr:master", "origin_ts": 42, "template": True})
check(room is not None, "a: room proposal not None")
check(room and room.get("target_room") == "!abc:local", "a: target_room carried")
check(room and room.get("body") == "hi there", "a: body carried")
check(room and room.get("created_by") == "@mgr:master", "a: created_by carried")
check(room and room.get("origin_ts") == 42, "a: origin_ts carried")
check(room and room.get("com.jkali.proposal_source_event") == "$evt1",
      "a: source_event carried")
check(room and room.get("template") is True, "a: template carried")
check(room and "target_identifier" not in room, "a: no target_identifier leak")
check(room and "target_source" not in room, "a: no target_source leak")
check(room and "target_display" not in room, "a: no target_display leak")

# origin_ts / created_by fallback to envelope when absent in content.
room2 = san({"target_room": "!abc:local", "body": "x"})
check(room2 and room2.get("origin_ts") == 1000, "a: origin_ts fallback to envelope")
check(room2 and room2.get("created_by") == "@mgr:master",
      "a: created_by fallback to envelope sender")

# ---- (b) valid identifier proposal (E.164) -> carries identifier, NO room ----
ph = san({"target_source": "imessage", "target_identifier": "+14155550123",
          "target_display": "Alice", "body": "ping", "created_by": "@mgr:master",
          "origin_ts": 7})
check(ph is not None, "b: E.164 identifier proposal not None")
check(ph and ph.get("target_source") == "imessage", "b: target_source carried")
check(ph and ph.get("target_identifier") == "+14155550123",
      "b: target_identifier carried")
check(ph and ph.get("target_display") == "Alice", "b: target_display carried")
check(ph and ph.get("body") == "ping", "b: body carried")
check(ph and ph.get("origin_ts") == 7, "b: origin_ts carried")
check(ph and ph.get("com.jkali.proposal_source_event") == "$evt1",
      "b: source_event carried")
check(ph and "target_room" not in ph, "b: NO target_room key")

# ---- (c) valid email identifier -> carried ----
em = san({"target_source": "li", "target_identifier": "a.b@example.com",
          "body": "hello"})
check(em is not None, "c: email identifier proposal not None")
check(em and em.get("target_identifier") == "a.b@example.com",
      "c: email identifier carried")
check(em and em.get("target_source") == "li", "c: source carried")
check(em and "target_room" not in em, "c: NO target_room key")
# target_display omitted when absent/non-string.
check(em and "target_display" not in em, "c: display omitted when absent")

# ---- (d) identifier proposal with a bad handle -> None ----
check(san({"target_source": "imessage", "target_identifier": "not a phone",
           "body": "x"}) is None, "d: bad handle -> None")
check(san({"target_source": "imessage", "target_identifier": "+0123",
           "body": "x"}) is None, "d: E.164 leading zero -> None")
check(san({"target_source": "imessage", "target_identifier": "noatsign.com",
           "body": "x"}) is None, "d: email without @ -> None")
check(san({"target_source": "imessage", "target_identifier": 12345,
           "body": "x"}) is None, "d: non-string handle -> None")

# ---- (e) identifier with a bad/empty target_source -> None ----
check(san({"target_source": "", "target_identifier": "+14155550123",
           "body": "x"}) is None, "e: empty source -> None")
check(san({"target_source": "IMessage", "target_identifier": "+14155550123",
           "body": "x"}) is None, "e: uppercase source -> None")
check(san({"target_source": "has space", "target_identifier": "+14155550123",
           "body": "x"}) is None, "e: source with space -> None")
check(san({"target_source": "a" * 40, "target_identifier": "+14155550123",
           "body": "x"}) is None, "e: over-long source -> None")
check(san({"target_source": None, "target_identifier": "+14155550123",
           "body": "x"}) is None, "e: missing source -> None")

# ---- (f) empty body (either shape) -> None ----
check(san({"target_room": "!abc:local", "body": ""}) is None,
      "f: room shape empty body -> None")
check(san({"target_room": "!abc:local", "body": "   "}) is None,
      "f: room shape whitespace body -> None")
check(san({"target_room": "!abc:local"}) is None,
      "f: room shape missing body -> None")
check(san({"target_source": "imessage", "target_identifier": "+14155550123",
           "body": ""}) is None, "f: identifier shape empty body -> None")
check(san({"target_source": "imessage",
           "target_identifier": "+14155550123"}) is None,
      "f: identifier shape missing body -> None")

# ---- (g) neither target -> None ----
check(san({"body": "just a body"}) is None, "g: no target -> None")
check(san({"target_room": "not-a-room-id", "body": "x"}) is None,
      "g: malformed room id, no identifier -> None")
check(san({}) is None, "g: empty content -> None")
check(san(None) is None, "g: None content -> None")

# ---- (h) target_room precedence: room wins, no identifier keys leak ----
both = san({"target_room": "!abc:local",
            "target_source": "imessage", "target_identifier": "+14155550123",
            "target_display": "Alice", "body": "hi"})
check(both is not None, "h: both-present not None")
check(both and both.get("target_room") == "!abc:local", "h: room wins")
check(both and "target_identifier" not in both, "h: no identifier leak")
check(both and "target_source" not in both, "h: no source leak")
check(both and "target_display" not in both, "h: no display leak")

# If target_room is present but MALFORMED, it must not win; fall through to the
# (here valid) identifier shape rather than carrying a bad room id.
badroom = san({"target_room": "bogus",
               "target_source": "imessage", "target_identifier": "+14155550123",
               "body": "hi"})
check(badroom is not None, "h: malformed room falls through to identifier")
check(badroom and "target_room" not in badroom,
      "h: malformed room id not carried")
check(badroom and badroom.get("target_identifier") == "+14155550123",
      "h: identifier carried on malformed-room fallthrough")

# ---- extra: non-string / over-long target_display is coerced/omitted ----
nod = san({"target_source": "imessage", "target_identifier": "+14155550123",
           "target_display": 999, "body": "x"})
check(nod is not None and "target_display" not in nod,
      "extra: non-string display omitted")
longd = san({"target_source": "imessage", "target_identifier": "+14155550123",
             "target_display": "D" * 500, "body": "x"})
check(longd is not None and longd.get("target_display") == "D" * 128,
      "extra: over-long display clamped to 128")


print("uplink_proposal_sanitize: %d passed, %d failed" % (_pass, _fail))
for f in _failures:
    print("  FAIL:", f)
sys.exit(1 if _fail else 0)
