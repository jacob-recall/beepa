# tests/unit/uplink_superseded.test.py
# Run: python3 tests/unit/uplink_superseded.test.py
import os, sys, tempfile, time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "agents", "uplink"))
import uplink

passed = failed = 0
def check(name, cond):
    global passed, failed
    if cond: passed += 1
    else: failed += 1; print("FAIL: " + name)

class Cfg:  # the fields _direct_send_gate reads
    direct_send_cap = 5; manager_mxid = "@manager:master"; master_user = "@me:master"; local_user = "@me:localhost"

def make(messages_response):
    u = object.__new__(uplink.Uplink)
    u.cfg = Cfg()
    u.db = uplink.Uplink._open_db(os.path.join(tempfile.mkdtemp(prefix="uplink-sup-"), "state.db"))
    u.db.execute("INSERT INTO mirror_rooms (local_room_id, master_room_id) VALUES (?,?)", ("!t:localhost", "!m:master"))
    u.read_room_level = lambda room: "direct"
    def local(method, path, body=None, query=None, timeout=60):
        if "/messages" in path:
            if isinstance(messages_response, Exception): raise messages_response
            return messages_response
        raise AssertionError("unexpected local call " + path)
    u.local = local
    return u

now = int(time.time() * 1000)
ev = {"sender": "@manager:master", "origin_server_ts": now - 1000, "event_id": "$p"}
clean = {"target_room": "!t:localhost", "body": "hello there"}

# quiet room: newest message is OLDER than the proposal -> passes to the cap gate and sends
u = make({"chunk": [{"type": "m.room.message", "origin_server_ts": now - 5000}]})
body, gate = u._direct_send_gate(ev, clean, cold_start=False, suspended=False)
check("quiet room passes", body == "hello there" and gate is None)

# activity after the proposal -> superseded
u = make({"chunk": [{"type": "m.room.message", "origin_server_ts": now - 500}]})
body, gate = u._direct_send_gate(ev, clean, cold_start=False, suspended=False)
check("later message refuses", body is None and gate == "superseded")

# unreadable timeline -> fail closed
u = make(OSError("boom"))
body, gate = u._direct_send_gate(ev, clean, cold_start=False, suspended=False)
check("read failure refuses", body is None and gate == "superseded")

# non-message events after the proposal do not count
u = make({"chunk": [{"type": "m.reaction", "origin_server_ts": now - 100}]})
body, gate = u._direct_send_gate(ev, clean, cold_start=False, suspended=False)
check("reaction is not activity", gate is None)

check("superseded is a loud gate", "superseded" not in uplink.Uplink.QUIET_GATES)

# F5: a future-dated proposal (within D2-3's +60s tolerance) must not hide a
# reply that falls inside its future window. The reply is stamped INSIDE that
# window (now+5s) rather than exactly at `now`: the gate recomputes now_ms a few
# ms after this line, so a reply at `now` is <= the clamp either way and would
# prove nothing. Unclamped, since_ts would be now+30s and this reply would be
# admitted; clamped to now_ms it supersedes.
u = make({"chunk": [{"type": "m.room.message", "origin_server_ts": now + 5000}]})
body, gate = u._direct_send_gate(dict(ev, origin_server_ts=now + 30000), clean, cold_start=False, suspended=False)
check("future-dated proposal still superseded by a reply inside its window", gate == "superseded")

# F12: inbound provenance stamps are stripped unless the event is from_me.
# _forward_message returns before the PUT at active_link_for_dispatch(), so the
# forwarded content is captured through a wrapped module-level stamp_timestamp
# (called after the stripping block and before any master write).
fw = object.__new__(uplink.Uplink)
fw.cfg = Cfg(); fw.self_mxids = set()
fw.timestamp_event = lambda room, source, e: e
fw._member_profile = lambda room, sender: ("Someone", None)
fw.active_link_for_dispatch = lambda: False      # stop before the PUT; we only inspect content
captured = {}
real_stamp = uplink.stamp_timestamp
def spy(content, ev): captured.update(content); return real_stamp(content, ev)
uplink.stamp_timestamp = spy
try:
    inbound = {"type": "m.room.message", "event_id": "$in", "sender": "@whatsapp_555:localhost",
               "origin_server_ts": now, "content": {"msgtype": "m.text", "body": "hi",
               "com.jkali.origin_avatar": "mxc://attacker.example/x", "com.jkali.from_proposal": "$p",
               "com.jkali.auto_sent_from_proposal": "$p"}}
    try: fw._forward_message("!t:localhost", "!m:master", "whatsapp", inbound)
    except Exception: pass
    check("inbound origin_avatar stripped", "com.jkali.origin_avatar" not in captured)
    check("inbound from_proposal stripped on non-from_me", "com.jkali.from_proposal" not in captured)
    check("inbound auto_sent_from_proposal stripped on non-from_me", "com.jkali.auto_sent_from_proposal" not in captured)

    # ... and a from_me message keeps the teammate's own stamps (the avatar key
    # is ours alone and is stripped on every path).
    captured.clear()
    own = {"type": "m.room.message", "event_id": "$own", "sender": "@me:localhost",
           "origin_server_ts": now, "content": {"msgtype": "m.text", "body": "hi",
           "com.jkali.origin_avatar": "mxc://attacker.example/x", "com.jkali.from_proposal": "$p"}}
    try: fw._forward_message("!t:localhost", "!m:master", "whatsapp", own)
    except Exception: pass
    check("from_me keeps from_proposal", captured.get("com.jkali.from_proposal") == "$p")
    check("from_me still has no inbound avatar", "com.jkali.origin_avatar" not in captured)
finally:
    uplink.stamp_timestamp = real_stamp
print("uplink_superseded: %d passed, %d failed" % (passed, failed))
sys.exit(1 if failed else 0)
