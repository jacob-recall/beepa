#!/usr/bin/env python3
"""Offline evidence for CODEBASE-REVIEW-2026-09-04.md.

Runs production functions against temporary SQLite databases and fake transports.
No credentials, running services, network calls, or real message sends are used.
These probes assert the observed defects, not the intended corrected behavior.
They are review evidence, not additions to the product's regression suite.
"""
import ast
import io
import json
import logging
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
from types import SimpleNamespace
import urllib.error
import urllib.parse

REPO = Path(__file__).resolve().parents[2]
sys.dont_write_bytecode = True
sys.path.insert(0, str(REPO / "agents/uplink"))
import uplink

uplink.log.disabled = True
RESULTS = []


def record(name, **observed):
    RESULTS.append(dict(probe=name, observed=observed))


def instance(directory, name, enrolled=False):
    env = dict(LOCAL_HS_URL="http://local.invalid", LOCAL_USER="@alice:localhost",
               LOCAL_TOKEN="synthetic", UPLINK_DB=str(Path(directory) / (name + ".db")))
    if enrolled:
        env.update(MASTER_HS_URL="https://old.invalid", MASTER_USER="@alice:master",
                   MASTER_TOKEN="synthetic", MANAGER_MXID="@manager:master",
                   MASTER_SPACE="!space:master")
    obj = uplink.Uplink(uplink.Config(env))
    # Any accidentally unmocked transport must fail before network access.
    def forbidden(*args, **kwargs):
        raise AssertionError("Unexpected transport call")
    obj.local = forbidden
    obj.master = forbidden
    return obj


def seed(obj):
    obj.db.execute("INSERT INTO mirror_rooms (local_room_id,master_room_id,source) "
                   "VALUES ('!local:localhost','!old:master','imessage')")
    obj.db.commit()


def message(eid):
    return dict(event_id=eid, type="m.room.message", sender="@alice:localhost",
                origin_server_ts=1, content=dict(msgtype="m.text", body="synthetic"))


def extract_function(name, namespace, class_name=None):
    # imessage/daemon.py has import-time live-config/DB reads. Extract the exact
    # function AST instead of importing that module or touching those files.
    tree = ast.parse((REPO / "imessage/daemon.py").read_text())
    body = tree.body
    if class_name:
        body = next(n for n in body if isinstance(n, ast.ClassDef)
                    and n.name == class_name).body
    node = next(n for n in body if isinstance(n, ast.FunctionDef) and n.name == name)
    exec(compile(ast.Module(body=[node], type_ignores=[]), "imessage/daemon.py", "exec"), namespace)
    return namespace[name]


with tempfile.TemporaryDirectory(prefix="beepa-review-") as directory:
    u = instance(directory, "revoke", True)
    seed(u)
    calls = []
    def refuse(method, path, *args, **kwargs):
        calls.append(path)
        raise urllib.error.HTTPError("https://synthetic.invalid", 503, "synthetic", {}, None)
    u.master = refuse
    u.delete_mirror("!local:localhost")
    assert u.mirror_for("!local:localhost") is None and len(calls) == 3
    record("failed_revocation_forgotten", failed_requests=len(calls), mapping_deleted=True)
    u.db.close()

    u = instance(directory, "reshare", True)
    seed(u)
    u.db.execute("INSERT INTO event_map VALUES ('$prior', '$old-master-event')")
    u.db.commit()
    u.master = lambda *a, **k: {}
    u.delete_mirror("!local:localhost")
    u.db.execute("INSERT INTO mirror_rooms (local_room_id,master_room_id,source) "
                 "VALUES ('!local:localhost','!new:master','imessage')")
    u.db.commit()
    posted = u.forward_events("!local:localhost", "!new:master", [message("$prior")])
    assert posted == 0
    record("reshare_skips_previous_history", history_events_offered=1, history_events_posted=posted)
    u.db.close()

    u = instance(directory, "limited", True)
    seed(u)
    u.meta_set("sync_since", "before-gap")
    calls, forwarded = [], []
    def limited(method, path, *args, **kwargs):
        calls.append(path)
        return {"next_batch": "after-gap", "rooms": {"join": {"!local:localhost": {
            "timeline": {"limited": True, "prev_batch": "gap-end", "events": [message("$newest")]}}}}}
    u.local = limited
    u.forward_events = lambda local, master, events: forwarded.extend(e["event_id"] for e in events)
    u.tail_once()
    assert u.meta_get("sync_since") == "after-gap" and len(calls) == 1
    record("limited_sync_gap_skipped", requests=calls, forwarded=forwarded, cursor=u.meta_get("sync_since"))
    u.db.close()

    u = instance(directory, "backfill", True)
    calls = []
    u.master = lambda method, path, *a, **k: {"room_id": "!new:master"} if path.endswith("createRoom") else {}
    def fail_backfill(*args):
        calls.append("backfill")
        raise uplink.MasterUnreachable("synthetic interruption")
    u.backfill = fail_backfill
    try:
        u.create_mirror("!local:localhost", "imessage", "synthetic")
    except uplink.MasterUnreachable:
        pass
    assert u.mirror_for("!local:localhost") is not None
    u.read_self_mxids = lambda: set()
    u.migrate_explicit_levels = lambda: {}
    u.full_sync = lambda: {}
    u.desired_shared = lambda *args: ({"!local:localhost": "share"}, {}, {}, {})
    u.reconcile()
    assert calls == ["backfill"]
    record("interrupted_backfill_never_retried", backfill_attempts_after_next_reconcile=len(calls), mirror_retained=True)
    u.db.close()

    u = instance(directory, "disconnect", True)
    u.local = lambda *args, **kwargs: {}  # exactly what the UI writes on Disconnect
    connected = u.refresh_master_config()
    assert connected and u.cfg.master_hs == "https://old.invalid"
    record("disconnect_reactivates_env_link", connected=connected, master=u.cfg.master_hs)
    u.db.close()

    u = instance(directory, "rebind", True)
    seed(u)
    u.meta_set("master_contacts_room", "!old-contacts:master")
    u.meta_set(u.IDENTITY_META, "\n".join(u.direct_send_identity()))
    u.cfg.master_hs = "https://new.invalid"
    u._write_suspension = lambda *args: None
    u._direct_send_ack_matches = lambda *args: False
    suspended = u.refresh_direct_send_binding()
    assert suspended and u.mirror_for("!local:localhost")[0] == "!old:master"
    assert u.meta_get("master_contacts_room") == "!old-contacts:master"
    record("master_change_retains_old_mirror_state", direct_suspended=suspended,
           mirror=u.mirror_for("!local:localhost")[0], contacts_room=u.meta_get("master_contacts_room"))
    u.db.close()

    u = instance(directory, "consent-window", True)
    seed(u)
    seen = []
    u.local = lambda *a, **k: {"next_batch": "new", "rooms": {"join": {"!local:localhost": {
        "account_data": {"events": [{"type": "com.jkali.share_override", "content": {"state": "private"}}]},
        "timeline": {"events": [message("$after-private")]}}}}}
    u.forward_events = lambda local, master, events: seen.extend(e["event_id"] for e in events)
    u.tail_once()
    assert seen == ["$after-private"]
    record("tail_ignores_same_batch_private_override", forwarded=seen)
    u.db.close()

    u = instance(directory, "direction", True)
    u._display_name = lambda *args: "iMessage bot"
    payloads = []
    def capture(method, path, body, **kwargs):
        payloads.append(body)
        return {"event_id": "$mirrored"}
    u.master = capture
    ev = message("$phone-outgoing")
    ev["sender"] = "@imessagebot:localhost"
    ev["content"]["com.jkali.from_me"] = True
    u._forward_message("!local:localhost", "!old:master", "imessage", ev)
    assert payloads[0]["com.jkali.from_me"] is False
    record("imessage_outgoing_mislabeled_on_master", local_from_me=True, master_from_me=False)
    u.db.close()

    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE seen_msg (chat_id TEXT, msg_id TEXT, PRIMARY KEY(chat_id,msg_id))")
    attempts = []
    def failed_delivery(*args):
        attempts.append(1)
        raise OSError("synthetic local-homeserver outage")
    ns = dict(DB=db, DBLOCK=threading.Lock(), cli_json=lambda *a: {"items": [{"id": "m1"}]},
              chat_display_name=lambda c: "synthetic", deliver_inbound=failed_delivery,
              reconcile_edit=lambda *a: None, reconcile_reactions=lambda *a: None)
    fn = extract_function("handle_chat_delta", ns)
    try:
        fn({}, "chat1")
    except OSError:
        pass
    fn({}, "chat1")
    assert len(attempts) == 1 and db.execute("SELECT COUNT(*) FROM seen_msg").fetchone()[0] == 1
    record("imessage_marks_seen_before_delivery", polls=2, delivery_attempts=len(attempts), seen_rows=1)
    db.close()

    marks, replies = [], []
    def failed_event(ev):
        raise OSError("synthetic engine failure")
    ns = dict(urllib=urllib, json=json, MAX_BODY=1024, txn_seen=lambda t: False,
              handle_event=failed_event, txn_mark=marks.append, log=logging.getLogger("probe"))
    fn = extract_function("do_PUT", ns, "Handler")
    body = json.dumps({"events": [{"event_id": "$synthetic"}]}).encode()
    handler = SimpleNamespace(path="/_matrix/app/v1/transactions/synthetic",
                              headers={"Content-Length": str(len(body))},
                              _host_ok=lambda: True, _authed=lambda: True,
                              rfile=io.BytesIO(body), _deny=lambda c: replies.append(c),
                              _reply=lambda c, payload: replies.append(c))
    fn(handler)
    assert marks == ["synthetic"] and replies == [200]
    record("imessage_acks_failed_outbound_transaction", transaction_marked_done=True, response=200)

print(json.dumps(RESULTS, indent=2))
