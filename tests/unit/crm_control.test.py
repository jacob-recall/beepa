#!/usr/bin/env python3
"""Real uplink seams with disposable state DB and strict injected Matrix IO."""
import copy
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import types
import unittest
import urllib.error
import urllib.parse

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "agents" / "uplink"))
import crm_control as control
import uplink


class Matrix:
    def __init__(self, user):
        self.user, self.rooms, self.account, self.overrides, self.writes = user, {}, {}, {}, []

    def state(self, room, kind, content, key="", sender=None):
        self.rooms[room]["state"][(kind, key)] = {"type": kind, "state_key": key, "sender": sender or self.user, "content": content}

    def make_room(self, room, body):
        self.rooms[room] = {"state": {}, "events": []}
        self.state(room, "m.room.create", {"creator": self.user, "room_version": body.get("room_version", "11")})
        self.state(room, "m.room.member", {"membership": "join"}, self.user)
        self.state(room, "m.room.join_rules", {"join_rule": "invite"})
        self.state(room, "m.room.guest_access", {"guest_access": "forbidden"})
        self.state(room, "m.room.history_visibility", {"history_visibility": "shared"})
        self.state(room, "m.room.power_levels", body.get("power_level_content_override", {}))
        for event in body.get("initial_state", []):
            self.state(room, event["type"], event["content"], event.get("state_key", ""))
        for invited in body.get("invite", []):
            self.state(room, "m.room.member", {"membership": "invite"}, invited)

    def __call__(self, method, path, body=None, query=None, timeout=60):
        if method != "GET":
            self.writes.append((method, path, copy.deepcopy(body)))
        prefix = "/_matrix/client/v3/"
        self_path = path[len(prefix):]
        parts = [urllib.parse.unquote(p) for p in self_path.split("/")]
        if parts[0] == "createRoom":
            room = "!control:local"
            self.make_room(room, body)
            return {"room_id": room}
        if parts[0] == "user":
            if parts[2] == "rooms":
                key = (parts[3], parts[5])
                store = self.overrides
            else:
                key = parts[3]
                store = self.account
            if method == "PUT":
                store[key] = copy.deepcopy(body)
                return {}
            if key not in store:
                raise urllib.error.HTTPError(path, 404, "missing", {}, None)
            return copy.deepcopy(store[key])
        if parts[0] == "rooms":
            room = parts[1]
            if parts[2] == "state":
                if len(parts) == 3:
                    return copy.deepcopy(list(self.rooms[room]["state"].values()))
                kind, key = parts[3], parts[4] if len(parts) > 4 else ""
                if method == "PUT":
                    self.state(room, kind, copy.deepcopy(body), key)
                    return {}
                return copy.deepcopy(self.rooms[room]["state"][(kind, key)]["content"])
            if parts[2] == "messages":
                events = self.rooms[room]["events"]
                start = int(query.get("from", "0"))
                types_wanted = json.loads(query["filter"])["types"]
                page = [event for event in events[start:start + query["limit"]] if event["type"] in types_wanted]
                return {"chunk": copy.deepcopy(page), "end": str(min(len(events), start + query["limit"]))}
        raise AssertionError((method, path, body, query))


class ControlTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.local, self.master = Matrix("@elliot:local"), Matrix("@elliot:master")
        self.uplink = uplink.Uplink.__new__(uplink.Uplink)
        self.uplink.cfg = types.SimpleNamespace(local_user="@elliot:local", master_user="@elliot:master", manager_mxid="@manager:master", master_hs="https://master.invalid", master_space="!space:master", master_authority_id="authority-1", db_path=os.path.join(self.tmp.name, "state.db"), contacts_db=os.path.join(self.tmp.name, "contacts.db"))
        self.uplink.db = uplink.Uplink._open_db(self.uplink.cfg.db_path)
        self.uplink.local, self.uplink.master = self.local, self.master
        self.uplink._direct_suspended = False
        self.uplink.active_link_for_dispatch = lambda: True
        self.uplink._conn_state = True
        self.uplink.meta_set("master_proposals_room", "!proposals:master")
        self.master.make_room("!proposals:master", {"invite": ["@manager:master"], "initial_state": [{"type": "com.jkali.proposals", "state_key": "", "content": {}}], "power_level_content_override": {"users": {"@elliot:master": 100, "@manager:master": 50}, "events_default": 100, "state_default": 100, "events": {"com.jkali.proposal": 50}}})
        self.master.make_room("!space:master", {})
        self.local.overrides[("!person:local", "com.jkali.share_override")] = {"state": "private"}
        self.proofs = [{"room_id": "!person:local", "source": "imessage", "network_id": "person@example.com", "bridge_epoch": "bridge-1"}]
        self.resolve_status = "ready"
        self.now = 1000000
        self.control = control.CRMControl(self.uplink, self.resolve, clock=lambda: self.now)
        self.control.refresh(True)
        self.uplink._crm_control = self.control
        self.room = self.control.ensure_local()
        self.control.local_once()

    def tearDown(self):
        self.uplink.db.close()
        self.tmp.cleanup()

    def resolve(self, binding, owner_mxid, generation):
        self.assertEqual(owner_mxid, "@elliot:master")
        self.assertEqual(generation, self.control.generation)
        return {"status": self.resolve_status, "rooms": copy.deepcopy(self.proofs)}

    def manual(self, command="manual", level="private", revision=0):
        return {"event_id": "$" + command, "type": control.POLICY_REQUEST, "sender": "@elliot:local", "room_id": self.room,
                "content": {"schema": 1, "command_id": command, "journal_epoch": self.control.epoch, "room_id": "!person:local", "level": level, "expected_revision": revision}}

    def direct(self, command="grant", phase="prepare", ticket=None):
        content = {"schema": 1, "command_id": command, "phase": phase, "owner_mxid": "@elliot:master", "generation": self.control.generation, "journal_epoch": self.control.epoch, "binding_id": "binding-1", "contact": {"source": "imessage", "network_id": "person@example.com"}, "contact_version": "7", "membership_operation_id": "member-add-1"}
        if ticket:
            content["ticket_id"] = ticket
        return {"event_id": "$" + command + phase, "type": control.DIRECT_REQUEST, "sender": "@manager:master", "room_id": "!proposals:master", "origin_server_ts": self.now, "content": content}

    def override(self):
        return self.local.overrides[("!person:local", "com.jkali.share_override")]

    def test_local_manual_works_disconnected_through_real_uplink_stage(self):
        self.uplink._conn_state = False
        self.local.rooms[self.room]["events"].append(self.manual(level="share"))
        self.uplink.crm_local_once()
        self.assertEqual(self.override()["state"], "share")
        result = self.local.rooms[self.room]["state"][(control.POLICY_RESULT, "manual")]["content"]
        self.assertEqual(result["state"], "applied")
        self.assertEqual(result["current_revision"], 1)
        self.assertFalse(self.local.account[control.LOCAL_CAPABILITY]["crm"])
        self.assertEqual(self.master.writes, [])
        self.assertEqual(self.local.rooms[self.room]["state"][(control.POLICY_REVISION, "!person:local")]["content"]["revision"], 1)

    def test_prepare_does_not_grant_commit_grants_exact_existing_private_room(self):
        prepared = self.control.ingest_direct(self.direct(), "!proposals:master")
        self.assertEqual(self.override()["state"], "private")
        committed = self.control.ingest_direct(self.direct(phase="commit", ticket=prepared["ticket_id"]), "!proposals:master")
        self.assertEqual(committed["state"], "queued")
        self.control._drain()
        self.assertEqual(self.override()["state"], "direct")
        self.assertEqual(json.loads(self.control._command("grant")["result_json"])["state"], "applied")
        self.assertTrue(all("/send/m.room.message/" not in path for _, path, _ in self.local.writes + self.master.writes))

    def test_manual_private_after_prepare_invalidates_cas_and_old_grant_replay(self):
        ticket = self.control.ingest_direct(self.direct(), "!proposals:master")["ticket_id"]
        self.control.ingest_manual(self.manual(), self.room)
        self.control._drain()
        self.control.ingest_direct(self.direct(phase="commit", ticket=ticket), "!proposals:master")
        self.control._drain()
        self.assertEqual(self.override()["state"], "private")
        result = json.loads(self.control._command("grant")["result_json"])
        self.assertEqual(result["rooms"][0]["reason"], "revision-conflict")
        prior = len(self.local.writes)
        self.control.ingest_direct(self.direct(phase="commit", ticket=ticket), "!proposals:master")
        self.control._drain()
        self.assertEqual(len(self.local.writes), prior)

    def test_revocation_or_scope_change_before_execution_refuses_grant(self):
        ticket = self.control.ingest_direct(self.direct(), "!proposals:master")["ticket_id"]
        self.control.ingest_direct(self.direct(phase="commit", ticket=ticket), "!proposals:master")
        self.resolve_status = "unshared"
        self.control._drain()
        self.assertEqual(self.override()["state"], "private")
        # Fail-closed resolver rejection currently stays queued for adapter retry;
        # it never becomes permission when a read is unavailable.
        self.assertNotEqual(json.loads(self.control._command("grant")["result_json"])["state"], "applied")

    def test_epoch_durable_outage_stable_unlink_rotates_and_state_loss_rejects(self):
        epoch, generation = self.control.epoch, self.control.generation
        self.control.refresh(False)
        self.assertEqual(self.control.generation, generation)
        replacement = control.CRMControl(self.uplink, self.resolve)
        replacement.refresh(True)
        self.assertEqual((replacement.epoch, replacement.generation), (epoch, generation))
        self.uplink.meta_set("link_disabled", "1")
        replacement.refresh(False)
        self.assertNotEqual(replacement.generation, generation)
        self.uplink.meta_set("link_disabled", "0")
        self.control.refresh(True)
        old = self.manual()
        old["content"]["journal_epoch"] = "old-database"
        with self.assertRaisesRegex(ValueError, "stale-control-epoch"):
            self.control.ingest_manual(old, self.room)

    def test_forged_sender_state_event_raw_room_and_stale_generation_are_denied(self):
        for mutation in (lambda e: e.update(sender="@evil:master"), lambda e: e.update(state_key=""), lambda e: e["content"].update(generation="old"), lambda e: e["content"].update(room_id="!person:local")):
            event = self.direct()
            mutation(event)
            with self.assertRaises(ValueError):
                self.control.ingest_direct(event, "!proposals:master")
        self.assertEqual(self.override()["state"], "private")

    def test_poisoned_control_room_is_never_advertised(self):
        self.local.state(self.room, "m.room.history_visibility", {"history_visibility": "world_readable"})
        with self.assertRaisesRegex(ValueError, "unsafe-control-visibility"):
            self.control.ensure_local()
        self.local.state(self.room, "m.room.history_visibility", {"history_visibility": "joined"})
        self.local.state(self.room, "m.room.member", {"membership": "leave"}, "@former:local")
        with self.assertRaisesRegex(ValueError, "control-membership"):
            self.control.ensure_local()

    def test_missing_remote_capability_is_republished_despite_local_cache(self):
        del self.local.account[control.LOCAL_CAPABILITY]
        self.control.ensure_local()
        self.assertEqual(self.local.account[control.LOCAL_CAPABILITY]["journal_epoch"], self.control.epoch)

    def test_narrow_permission_migration_and_authenticated_capability(self):
        self.control.publish_master()
        levels = self.master.rooms["!proposals:master"]["state"][("m.room.power_levels", "")]["content"]
        self.assertEqual(levels["events_default"], 100)
        self.assertEqual(levels["events"][control.DIRECT_REQUEST], 50)
        self.assertEqual(levels["events"]["com.jkali.monitoring_scope"], 50)
        self.assertEqual(levels["events"]["com.jkali.read_request"], 50)
        self.assertTrue(self.control.capability()["conversation_tracking"])
        self.assertNotIn("m.room.message", levels["events"])
        cap = self.master.rooms["!space:master"]["state"][(control.MASTER_CAPABILITY, "")]
        self.assertEqual(cap["sender"], "@elliot:master")
        self.assertFalse(cap["content"]["batch"])

    def test_expired_commit_has_explicit_ack_and_retryable_evidence_keeps_cursor(self):
        ticket = self.control.ingest_direct(self.direct(), "!proposals:master")["ticket_id"]
        self.now += control.TTL_MS + 1
        self.master.rooms["!proposals:master"]["events"].append(self.direct(phase="commit", ticket=ticket))
        self.control._poll(self.master, "!proposals:master", "crm")
        self.control.flush_results("crm")
        result = self.master.rooms["!proposals:master"]["state"][(control.DIRECT_RESULT, "grant")]["content"]
        self.assertEqual(result["state"], "rejected")
        self.assertEqual(result["reason"], "expired-or-wrong-ticket")
        self.master.rooms["!proposals:master"]["events"].append(self.direct(command="unavailable"))
        self.resolve_status = "unavailable"
        previous = self.control._meta("cursor:crm:!proposals:master")
        with self.assertRaises(OSError):
            self.control._poll(self.master, "!proposals:master", "crm")
        self.assertEqual(self.control._meta("cursor:crm:!proposals:master"), previous)

    def test_pending_manual_privacy_suppresses_real_dispatch_and_archive(self):
        self.control.ingest_manual(self.manual(), self.room)
        self.assertEqual(self.uplink.read_room_level("!person:local"), "private")
        self.assertEqual(self.uplink.archive_level("!person:local"), "private")
        self.assertEqual(self.control.journal.pending("!person:local")[0]["level"], "private")

    def test_cancel_prepared_and_queued_grants_never_replays_direct(self):
        for name, queued in (("prepared-cancel", False), ("queued-cancel", True)):
            ticket = self.control.ingest_direct(self.direct(command=name), "!proposals:master")["ticket_id"]
            if queued:
                self.control.ingest_direct(self.direct(command=name, phase="commit", ticket=ticket), "!proposals:master")
            canceled = self.control.ingest_direct(self.direct(command=name, phase="cancel"), "!proposals:master")
            self.assertEqual(canceled["state"], "canceled")
            self.assertTrue(canceled["safe_to_change_membership"])
            self.control._drain()
            replay = self.control.ingest_direct(self.direct(command=name, phase="commit", ticket=ticket), "!proposals:master")
            self.assertEqual(replay["state"], "canceled")
            self.assertEqual(self.override()["state"], "private")

    def test_cancel_attempted_grant_waits_exact_reconciliation_and_retains_direct(self):
        ticket = self.control.ingest_direct(self.direct(), "!proposals:master")["ticket_id"]
        self.control.ingest_direct(self.direct(phase="commit", ticket=ticket), "!proposals:master")
        original = self.uplink.local
        delayed = []
        def timeout(method, path, body=None, **kwargs):
            if method == "PUT" and path.endswith("/account_data/com.jkali.share_override"):
                delayed.append((method, path, body, kwargs))
                raise TimeoutError()
            return original(method, path, body, **kwargs)
        self.uplink.local = timeout
        self.control._drain()
        canceled = self.control.ingest_direct(self.direct(phase="cancel"), "!proposals:master")
        self.assertEqual(canceled["state"], "pending")
        self.assertFalse(canceled["safe_to_change_membership"])
        self.control._drain()
        self.assertEqual(len(delayed), 1)
        self.uplink.local = original
        method, path, body, kwargs = delayed[0]
        original(method, path, body, **kwargs)
        self.control._drain()
        result = json.loads(self.control._command("grant")["result_json"])
        self.assertEqual(result["state"], "applied")
        self.assertTrue(result["safe_to_change_membership"])
        self.assertEqual(self.override()["state"], "direct")

    def test_cancel_authentication_and_exact_payload(self):
        self.control.ingest_direct(self.direct(), "!proposals:master")
        for mutate in (lambda e: e.update(sender="@evil:master"), lambda e: e["content"].update(generation="old"), lambda e: e["content"].update(contact_version="8")):
            event = self.direct(phase="cancel")
            mutate(event)
            with self.assertRaises(ValueError):
                self.control.ingest_direct(event, "!proposals:master")
        self.assertEqual(self.control._command("grant")["state"], "prepared")

    def test_cancel_before_prepare_tombstone_blocks_late_matching_envelopes(self):
        result = self.control.ingest_direct(self.direct(phase="cancel"), "!proposals:master")
        self.assertTrue(result["safe_to_change_membership"])
        self.assertEqual(self.control.ingest_direct(self.direct(), "!proposals:master")["state"], "canceled")
        self.assertEqual(self.control.ingest_direct(self.direct(phase="commit", ticket="late"), "!proposals:master")["state"], "canceled")
        changed = self.direct()
        changed["content"]["contact_version"] = "changed"
        with self.assertRaisesRegex(ValueError, "command-id-collision"):
            self.control.ingest_direct(changed, "!proposals:master")
        self.control._drain()
        self.assertEqual(self.override()["state"], "private")


if __name__ == "__main__":
    unittest.main()
