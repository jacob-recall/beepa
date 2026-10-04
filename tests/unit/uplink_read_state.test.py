# tests/unit/uplink_read_state.test.py
# Run: python3 tests/unit/uplink_read_state.test.py
import os, sys, tempfile
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "agents", "uplink"))
import uplink

passed = failed = 0
def check(name, cond):
    global passed, failed
    if cond: passed += 1
    else: failed += 1; print("FAIL: " + name)

room = {
    "account_data": {"events": [{"type": "m.fully_read", "content": {"event_id": "$e2"}}]},
    "ephemeral": {"events": [{"type": "m.receipt", "content": {
        "$e1": {"m.read": {"@me:localhost": {"ts": 900}, "@whatsapp_555:localhost": {"ts": 700}}},
        "$e2": {"m.read": {"@whatsapp_123:localhost": {"ts": 950}, "@whatsapp_555:localhost": {"ts": 800}, "@whatsappbot:localhost": {"ts": 999}}},
    }}]},
}
selfs = {"@me:localhost", "@whatsapp_123:localhost"}
bots = {"@whatsappbot:localhost"}
rs = uplink.read_state_from_room(room, selfs, bots)
check("teammate read = newest own/ghost receipt", rs["teammate_read_ts"] == 950)
check("remote read = newest other-party receipt", rs["remote_read_ts"] == 800)
check("empty room -> zeros", uplink.read_state_from_room({}, selfs, bots) == {"teammate_read_ts": 0, "remote_read_ts": 0})

# the bot allowlist the daemon actually uses comes from the shared catalog
check("catalog bot mxids are real", "@whatsappbot:localhost" in uplink.SOURCE_BOT_MXIDS)

# _mirror_read_state writes once per change, never on no-change
puts = []
u = object.__new__(uplink.Uplink)
u.db = uplink.Uplink._open_db(os.path.join(tempfile.mkdtemp(prefix="uplink-rs-"), "state.db"))
u.master = lambda method, path, body=None, query=None, timeout=60: puts.append((method, path, body)) or {}
u._mirror_read_state("!local:localhost", "!m:master", rs)
u._mirror_read_state("!local:localhost", "!m:master", rs)
check("one PUT for an unchanged state", len(puts) == 1)
check("state type + key", puts[0][1].endswith("/state/com.jkali.read_state/") and puts[0][0] == "PUT")
check("content carries both ts", puts[0][2]["teammate_read_ts"] == 950 and puts[0][2]["remote_read_ts"] == 800)
u._mirror_read_state("!local:localhost", "!m:master", dict(rs, remote_read_ts=1000))
check("changed state writes again", len(puts) == 2)

# F9: a master that is down (or any other failure) never raises out of the leg,
# and the meta cache is not poisoned — the next change retries.
def boom(method, path, body=None, query=None, timeout=60):
    raise uplink.MasterUnreachable("down")
u2 = object.__new__(uplink.Uplink)
u2.db = uplink.Uplink._open_db(os.path.join(tempfile.mkdtemp(prefix="uplink-rs2-"), "state.db"))
u2.master = boom
u2._mirror_read_state("!local:localhost", "!m:master", rs)
check("MasterUnreachable swallowed", u2.meta_get("read_state:!local:localhost") is None)
u2.master = lambda method, path, body=None, query=None, timeout=60: {}
u2._mirror_read_state("!local:localhost", "!m:master", rs)
check("retried after a failure", u2.meta_get("read_state:!local:localhost") == "950:800")
print("uplink_read_state: %d passed, %d failed" % (passed, failed))
sys.exit(1 if failed else 0)
