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

# Exercise persisted coalescing, retry, generation, and consent with real SQLite.
from unittest.mock import Mock
path = os.path.join(tempfile.mkdtemp(prefix="uplink-rs-"), "state.db")
u = object.__new__(uplink.Uplink)
u.db = uplink.Uplink._open_db(path)
u.db.execute("INSERT INTO mirror_rooms(local_room_id,master_room_id,source) VALUES (?,?,?)",
             ("!local:localhost", "!m:master", "imessage"))
u.db.commit()
u.active_link_for_dispatch = lambda: True
u.archive_level = lambda room: "share"
u.master = Mock(return_value={})
def queue(state, target="!m:master"):
    u._queue_read_state("!local:localhost", target, state)
def saved():
    return u.db.execute("SELECT teammate_read_ts,remote_read_ts,dirty FROM receipt_outbox").fetchone()
queue(rs)
check("queued durably without network", saved() == (950,800,1) and not u.master.called)
u.flush_read_state()
check("successful publish marks clean", saved() == (950,800,0))
check("state type and key", u.master.call_args.args[1].endswith("/state/com.jkali.read_state/"))
queue(rs); u.flush_read_state()
check("unchanged state not rewritten", u.master.call_count == 1)
queue({"remote_read_ts":1100})
check("remote update preserves teammate", saved() == (950,1100,1))
queue({"teammate_read_ts":1200})
check("teammate update preserves remote", saved() == (1200,1100,1))
u.flush_read_state(); queue({"teammate_read_ts":900})
check("old receipts do not regress or dirty state", saved() == (1200,1100,0))
queue({"remote_read_ts":1500})
u.master.side_effect = uplink.MasterUnreachable("fixture offline")
try:
    u.flush_read_state()
except uplink.MasterUnreachable:
    pass
check("outage retains dirty payload", saved() == (1200,1500,1))
u.db.close(); u.db = uplink.Uplink._open_db(path)
u.master.side_effect = None
u.flush_read_state()
check("restart retries without a new receipt", saved() == (1200,1500,0))
queue({"remote_read_ts":1600})
u.active_link_for_dispatch = lambda: False
u.master.reset_mock(); u.flush_read_state()
check("disconnected link never publishes", not u.master.called)
u.active_link_for_dispatch = lambda: True
u.archive_level = lambda room: "private"
u.flush_read_state()
check("privacy revocation drops queued receipts", saved() is None and not u.master.called)
u.archive_level = lambda room: "share"
queue(rs); u.mark_revoking("!local:localhost")
check("revoking lifecycle clears queued receipts", saved() is None)
u.db.execute("DELETE FROM mirror_lifecycle"); u.db.commit()
queue(rs)
u.db.execute("UPDATE mirror_rooms SET master_room_id='!new:master'"); u.db.commit()
u.flush_read_state()
check("stale generation never publishes", saved() is None and not u.master.called)
queue(rs,"!new:master"); u.flush_read_state()
check("same receipt publishes to replacement generation", u.master.call_count == 1 and saved() == (950,800,0))
# Replacing a queued generation cannot inherit the old destination's timestamp.
queue({"remote_read_ts":2000},"!new:master")
queue({"teammate_read_ts":100},"!third:master")
check("replacement generation starts fresh", saved() == (100,0,1))
queue({'unread_count': 3}, '!third:master')
check('server unread count persisted', u.db.execute('SELECT unread_count FROM room_unread').fetchone()[0] == 3)
queue({'unread_count': 0}, '!third:master')
check('zero unread is an update, not missing', u.db.execute('SELECT u.unread_count,r.dirty FROM receipt_outbox r JOIN room_unread u USING(local_room_id)').fetchone() == (0,1))
u.db.close()
print("uplink_read_state: %d passed, %d failed" % (passed, failed))
sys.exit(1 if failed else 0)
