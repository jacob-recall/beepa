"""Uplink adapter for typed local policy and master CRM permission commands.

Uses existing outbound local/master Matrix transports, a private local control
room, fixed event types, strict source contact resolution and ConsentJournal.
No direct message send path exists here. A prepare/commit ticket pins even the
Private conversations that the master cannot inspect before adding a contact.
"""
import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.parse
import uuid

import consent
from crm_policy import ConsentJournal, PolicyRefused, PolicyRetryable
from crm_batch import CRMBatch, BATCH_REQUEST
from crm_approval import CRMApproval, APPROVAL_REQUEST
from crm_tracking import CRMTracking

LOCAL_CAPABILITY = "com.jkali.crm_control"
MASTER_CAPABILITY = "com.jkali.crm.capability"
CONTROL_MARKER = "com.jkali.policy_control"
POLICY_REQUEST = "com.jkali.policy.request"
POLICY_RESULT = "com.jkali.policy.result"
POLICY_REVISION = "com.jkali.policy.revision"
POLICY_CHECKPOINT = "com.jkali.policy.checkpoint"
DIRECT_REQUEST = "com.jkali.crm.direct_request"
DIRECT_RESULT = "com.jkali.crm.direct_result"
TTL_MS = 10 * 60 * 1000
MAX_ROOMS = 50
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}")
_ROOM = re.compile(r"![^\s:]+:[^\s]+")


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _require(condition, reason):
    if not condition:
        raise ValueError(reason)


def _id(value):
    return isinstance(value, str) and len(value) <= 128 and _ID.fullmatch(value) is not None


def _room(value):
    return isinstance(value, str) and len(value) <= 256 and _ROOM.fullmatch(value) is not None


def _quote(value):
    return urllib.parse.quote(value, safe="")


class CRMControl:
    """Owned once by Uplink; refresh() each configuration pass before stages.

    resolve_contact(binding, owner_mxid=..., generation=...) must invoke the
    strict local resolver using fresh provider/account/sharing evidence.
    Its canonical rooms contain stable proof fields and no guessed identities.
    """
    def __init__(self, uplink, resolve_contact, clock=None, batch_enabled=False):
        self.uplink = uplink
        self.db = uplink.db
        self.resolve_contact = resolve_contact
        self.clock = clock or (lambda: int(time.time() * 1000))
        self.connected = False
        self.local_ready = False
        self.batch_enabled = batch_enabled is True
        self.journal = None
        with self.db:
            self.db.execute("CREATE TABLE IF NOT EXISTS crm_control_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            self.db.execute("INSERT OR IGNORE INTO crm_control_meta VALUES ('journal_epoch',?)", (str(uuid.uuid4()),))
            self.db.execute("""CREATE TABLE IF NOT EXISTS crm_control_commands (
                command_id TEXT PRIMARY KEY, kind TEXT NOT NULL, destination TEXT NOT NULL,
                request_json TEXT NOT NULL, ticket_json TEXT, state TEXT NOT NULL,
                result_json TEXT NOT NULL, published_json TEXT)""")
            self.db.execute("""CREATE TABLE IF NOT EXISTS crm_control_children (
                operation_id TEXT PRIMARY KEY, command_id TEXT NOT NULL, room_id TEXT NOT NULL)""")
            self.db.execute("""CREATE TABLE IF NOT EXISTS crm_control_attempts (
                attempt_id TEXT PRIMARY KEY, operation_id TEXT NOT NULL,
                checkpoint_json TEXT NOT NULL, state TEXT NOT NULL)""")
        self.epoch = self._meta("journal_epoch")
        self.batch = CRMBatch(self)
        self.approval = CRMApproval(self.batch)
        self.tracking = CRMTracking(self.batch)
        self.uplink._crm_batch = self.batch

    def _meta(self, key, value=None):
        if value is not None:
            with self.db:
                self.db.execute("INSERT INTO crm_control_meta VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
        row = self.db.execute("SELECT value FROM crm_control_meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def _command(self, command_id):
        cursor = self.db.execute("SELECT * FROM crm_control_commands WHERE command_id=?", (command_id,))
        row = cursor.fetchone()
        return dict(zip([column[0] for column in cursor.description], row)) if row else None

    def refresh(self, connected):
        cfg = self.uplink.cfg
        self.connected = connected is True
        # A transport outage disables current dispatch without inventing a new
        # enrollment. Explicit unlink and changed authority rotate the UUID.
        identity = _json([cfg.local_user, cfg.master_hs, cfg.master_user, cfg.manager_mxid, getattr(cfg, "master_authority_id", ""), self.uplink.meta_get("link_disabled") == "1"])
        if self._meta("pairing_identity") != identity:
            with self.db:
                self.db.execute("INSERT INTO crm_control_meta VALUES ('pairing_identity',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (identity,))
                self.db.execute("INSERT INTO crm_control_meta VALUES ('generation',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(uuid.uuid4()),))
        self.generation = self._meta("generation")
        owner = cfg.master_user if cfg.master_user else cfg.local_user
        manager = cfg.manager_mxid if cfg.manager_mxid else cfg.local_user
        self.journal = ConsentJournal(self.db, owner_mxid=owner, generation=self.generation,
            manager_mxid=manager, local_user_mxid=cfg.local_user,
            read_override=self._read_override, write_override=self._write_override,
            lock_path=cfg.db_path + ".policy.lock", authorize=self._authorize)

    def _override_path(self, room):
        _require(_room(room), "invalid-room")
        return "/_matrix/client/v3/user/" + _quote(self.uplink.cfg.local_user) + "/rooms/" + _quote(room) + "/account_data/" + consent.SHARE_OVERRIDE_TYPE

    def _read_override(self, room):
        try:
            return self.uplink.local("GET", self._override_path(room), timeout=15)
        except urllib.error.HTTPError as error:
            if error.code == 404:
                return {}
            raise

    def _write_override(self, room, content):
        # The journal has committed its prepared intent and holds the shared
        # writer lock. A unique, immutable remote fence must precede EACH native
        # attempt, including retries of an operation after a positive refusal.
        # Restoring an older DB cannot then forget an in-flight policy PUT.
        try:
            control_room = self._meta("local_room")
            self._verify_checkpoints(self._room_state(self.uplink.local, control_room))
            operation_id = content["com.jkali.policy_journal"]["operation_id"]
            row = self.db.execute("SELECT sequence FROM crm_policy_operations WHERE operation_id=? AND state='prepared'", (operation_id,)).fetchone()
            _require(row is not None, "checkpoint-without-prepared-operation")
            attempt = str(uuid.uuid4())
            checkpoint = {"schema": 1, "journal_epoch": self.epoch, "attempt_id": attempt,
                          "operation_id": operation_id, "sequence": row[0]}
            with self.db:
                self.db.execute("INSERT INTO crm_control_attempts VALUES (?,?,?,'pending')", (attempt, operation_id, _json(checkpoint)))
            path = "/_matrix/client/v3/rooms/" + _quote(control_room) + "/state/" + POLICY_CHECKPOINT + "/" + attempt
            self.uplink.local("PUT", path, checkpoint, timeout=15)
            _require(self.uplink.local("GET", path, timeout=15) == checkpoint, "checkpoint-readback-mismatch")
            with self.db:
                self.db.execute("UPDATE crm_control_attempts SET state='confirmed' WHERE attempt_id=?", (attempt,))
        except Exception as error:
            # No policy request has started. Nevertheless an uncertain fence
            # PUT blocks ALL policy rooms until its exact marker is observed.
            # Its UUID state key cannot overwrite another (newer) restore fence.
            self.local_ready = False
            raise PolicyRetryable("checkpoint-reconciliation-required") from error
        try:
            self.uplink.local("PUT", self._override_path(room), content, timeout=15)
        except urllib.error.HTTPError as error:
            if error.code == 429:
                raise PolicyRetryable() from error
            if 400 <= error.code < 500:
                raise PolicyRefused() from error
            raise

    def _verify_checkpoints(self, state):
        remote = {}
        for (kind, key), event in state.items():
            if kind != POLICY_CHECKPOINT:
                continue
            content = event.get("content")
            _require(event.get("sender") == self.uplink.cfg.local_user and isinstance(content, dict)
                     and content.get("journal_epoch") == self.epoch and content.get("attempt_id") == key,
                     "prior-journal-recovery-required")
            row = self.db.execute("SELECT checkpoint_json FROM crm_control_attempts WHERE attempt_id=?", (key,)).fetchone()
            _require(row is not None and row[0] == _json(content), "prior-journal-recovery-required")
            remote[key] = content
        attempts = self.db.execute("SELECT attempt_id,operation_id,checkpoint_json,state FROM crm_control_attempts").fetchall()
        for attempt, operation, encoded, status in attempts:
            _require(self.db.execute("SELECT 1 FROM crm_policy_operations WHERE operation_id=?", (operation,)).fetchone() is not None,
                     "prior-journal-recovery-required")
            _require(attempt in remote and _json(remote[attempt]) == encoded,
                     "checkpoint-reconciliation-required" if status == "pending" else "prior-journal-recovery-required")
        with self.db:
            self.db.execute("UPDATE crm_control_attempts SET state='confirmed' WHERE state='pending'")

    def _room_state(self, transport, room):
        _require(_room(room), "invalid-room")
        result = transport("GET", "/_matrix/client/v3/rooms/" + _quote(room) + "/state", timeout=15)
        _require(isinstance(result, list), "invalid-room-state")
        return {(event.get("type"), event.get("state_key")): event for event in result if isinstance(event, dict)}

    def _verify_local_control(self, room):
        state = self._room_state(self.uplink.local, room)
        user = self.uplink.cfg.local_user
        create = state.get(("m.room.create", ""), {})
        marker = state.get((CONTROL_MARKER, ""), {})
        _require(create.get("sender") == user and create.get("content", {}).get("type") != "m.space", "invalid-control-creator")
        _require(marker.get("sender") == user and marker.get("content") == {"schema": 1, "journal_epoch": self.epoch}, "invalid-control-marker")
        _require(not any(key[0] in {"uk.half-shot.bridge", "m.bridge"} for key in state), "control-is-portal")
        _require("com.jkali.mirror_of" not in create.get("content", {}) and not any(key[0] in {"com.jkali.mirror_of", "m.room.encryption", "m.room.tombstone"} for key in state), "invalid-control-kind")
        members = [event for (kind, _), event in state.items() if kind == "m.room.member"]
        _require(all(event.get("state_key") == user for event in members) and any(event.get("state_key") == user and event.get("content", {}).get("membership") == "join" for event in members), "control-membership")
        levels = state.get(("m.room.power_levels", ""), {}).get("content", {})
        _require(levels.get("users") == {user: 100} and all(levels.get(key) == 100 for key in ("events_default", "state_default", "invite", "kick", "ban", "redact")), "unsafe-control-power")
        _require(state.get(("m.room.join_rules", ""), {}).get("content", {}).get("join_rule") == "invite"
                 and state.get(("m.room.history_visibility", ""), {}).get("content", {}).get("history_visibility") == "joined"
                 and state.get(("m.room.guest_access", ""), {}).get("content", {}).get("guest_access") == "forbidden", "unsafe-control-visibility")
        self._verify_checkpoints(state)
        self.batch.verify_checkpoints(state)

    def ensure_local(self):
        room = self._meta("local_room")
        cap_path = "/_matrix/client/v3/user/" + _quote(self.uplink.cfg.local_user) + "/account_data/" + LOCAL_CAPABILITY
        try:
            advertised = self.uplink.local("GET", cap_path, timeout=15)
        except urllib.error.HTTPError as error:
            if error.code != 404:
                raise
            advertised = None
        if advertised is not None:
            # Losing the DB also loses evidence of an in-flight remote PUT.
            # A different UUID does NOT make that request stop executing. Never
            # bootstrap a replacement writer over a prior advertised journal.
            _require(isinstance(advertised, dict) and advertised.get("schema") == 1
                     and advertised.get("journal_epoch") == self.epoch
                     and advertised.get("policy_room_id") == room,
                     "prior-journal-recovery-required")
        if not room:
            result = self.uplink.local("POST", "/_matrix/client/v3/createRoom", {
                "name": "Beepa privacy controls", "preset": "private_chat", "room_version": "11",
                "initial_state": [{"type": CONTROL_MARKER, "state_key": "", "content": {"schema": 1, "journal_epoch": self.epoch}},
                                  {"type": "m.room.history_visibility", "state_key": "", "content": {"history_visibility": "joined"}},
                                  {"type": "m.room.guest_access", "state_key": "", "content": {"guest_access": "forbidden"}}],
                "power_level_content_override": {"users": {self.uplink.cfg.local_user: 100}, "events_default": 100, "state_default": 100, "invite": 100, "kick": 100, "ban": 100, "redact": 100}}, timeout=15)
            room = result.get("room_id")
            _require(_room(room), "invalid-control-room")
            self._meta("local_room", room)
        self._verify_local_control(room)
        capability = {"schema": 1, "journal_epoch": self.epoch, "generation": self.generation,
                      "policy_room_id": room, "manual": True, "crm": self.connected, "crm_approval": True}
        if advertised != capability:
            self.uplink.local("PUT", cap_path, capability, timeout=15)
            self._meta("local_capability", _json(capability))
        return room

    def capability(self):
        return {"schema": 1, "owner_mxid": self.uplink.cfg.master_user,
                "generation": self.generation, "journal_epoch": self.epoch,
                "ordered_policy": True, "direct_prepare_commit": True, "direct_cancel": True, "batch": self.batch_enabled, "batch_cancel": True,
                "conversation_tracking": True}

    def _verify_master_room(self, room, migrate_guest=False):
        _require(self.connected and room == self.uplink.meta_get("master_proposals_room"), "inactive-master-room")
        state = self._room_state(self.uplink.master, room)
        cfg = self.uplink.cfg
        create = state.get(("m.room.create", ""), {})
        marker = state.get(("com.jkali.proposals", ""), {})
        _require(create.get("sender") == cfg.master_user and create.get("content", {}).get("type") != "m.space" and marker.get("sender") == cfg.master_user, "invalid-proposal-owner")
        _require("com.jkali.mirror_of" not in create.get("content", {}) and not any(key[0] in {"uk.half-shot.bridge", "m.bridge", "com.jkali.mirror_of", "m.room.encryption", "m.room.tombstone"} for key in state), "proposal-is-portal")
        member = state.get(("m.room.member", cfg.manager_mxid), {}).get("content", {})
        _require(member.get("membership") in {"join", "invite"}, "manager-not-member")
        _require(all(key in {cfg.master_user, cfg.manager_mxid} for (kind, key) in state if kind == "m.room.member"), "proposal-extra-members")
        _require(state.get(("m.room.join_rules", ""), {}).get("content", {}).get("join_rule") == "invite"
                 and state.get(("m.room.history_visibility", ""), {}).get("content", {}).get("history_visibility") in {"shared", "joined"}, "unsafe-proposal-visibility")
        guest = state.get(("m.room.guest_access", ""), {}).get("content", {}).get("guest_access")
        if guest == "can_join" and migrate_guest:
            # Synapse private_chat presets historically allowed invited guests.
            # Only upgrade the already verified exact two-identity private room;
            # public history or extra/former members are never adopted this way.
            path = "/_matrix/client/v3/rooms/" + _quote(room) + "/state/m.room.guest_access/"
            self.uplink.master("PUT", path, {"guest_access": "forbidden"}, timeout=15)
            guest = self.uplink.master("GET", path, timeout=15).get("guest_access")
        _require(guest == "forbidden", "unsafe-proposal-visibility")

    def publish_revision(self, room):
        row = self.db.execute("SELECT revision FROM crm_policy_rooms WHERE room_id=?", (room,)).fetchone()
        _require(row is not None, "policy-not-observed")
        revision = row[0]
        self.uplink.local("PUT", "/_matrix/client/v3/rooms/" + _quote(self._meta("local_room")) + "/state/" + POLICY_REVISION + "/" + _quote(room),
                          {"schema": 1, "journal_epoch": self.epoch, "room_id": room, "revision": revision}, timeout=15)
        return revision

    def publish_master(self):
        room = self.uplink.meta_get("master_proposals_room")
        self._verify_master_room(room, migrate_guest=True)
        # Narrow migration of an existing proposals room: preserve all existing
        # restrictions and grant exactly the new typed request, never messages.
        path = "/_matrix/client/v3/rooms/" + _quote(room) + "/state/m.room.power_levels/"
        levels = self.uplink.master("GET", path, timeout=15)
        _require(isinstance(levels, dict) and levels.get("users", {}).get(self.uplink.cfg.master_user) == 100, "invalid-proposal-power")
        _require(levels.get("users", {}).get(self.uplink.cfg.manager_mxid) == 50
                 and set(levels["users"]) == {self.uplink.cfg.master_user, self.uplink.cfg.manager_mxid}
                 and type(levels.get("events_default")) is int and levels["events_default"] >= 100
                 and type(levels.get("state_default")) is int and levels["state_default"] >= 100,
                 "unsafe-proposal-power")
        events = dict(levels.get("events", {}))
        _require(all(type(level) is int and (kind in {"com.jkali.proposal", DIRECT_REQUEST, BATCH_REQUEST, "com.jkali.monitoring_scope"} or level >= 100)
                     for kind, level in events.items()), "unsafe-proposal-event-power")
        if events.get(DIRECT_REQUEST) != 50 or events.get(BATCH_REQUEST) != 50 or events.get("com.jkali.monitoring_scope") != 50:
            events[DIRECT_REQUEST] = 50
            events[BATCH_REQUEST] = 50
            events["com.jkali.monitoring_scope"] = 50
            self.uplink.master("PUT", path, {**levels, "events": events}, timeout=15)
        cap = self.capability()
        for target in (room, self.uplink.cfg.master_space):
            _require(_room(target), "invalid-capability-target")
            self.uplink.master("PUT", "/_matrix/client/v3/rooms/" + _quote(target) + "/state/" + MASTER_CAPABILITY + "/", cap, timeout=15)

    def _put_command(self, command_id, kind, destination, request, state, result, ticket=None):
        with self.db:
            self.db.execute("INSERT INTO crm_control_commands(command_id,kind,destination,request_json,ticket_json,state,result_json) VALUES (?,?,?,?,?,?,?)", (command_id, kind, destination, _json(request), _json(ticket) if ticket else None, state, _json(result)))

    def _result(self, command_id, result, state=None):
        with self.db:
            if state is None:
                self.db.execute("UPDATE crm_control_commands SET result_json=? WHERE command_id=?", (_json(result), command_id))
            else:
                self.db.execute("UPDATE crm_control_commands SET result_json=?,state=? WHERE command_id=?", (_json(result), state, command_id))

    def _envelope(self, event, room, actor, expected_type):
        _require(isinstance(event, dict) and event.get("type") == expected_type and "state_key" not in event and event.get("sender") == actor and event.get("room_id", room) == room, "event-authentication")
        _require(isinstance(event.get("event_id"), str) and event["event_id"].startswith("$"), "missing-event-id")
        content = event.get("content")
        _require(type(content) is dict and len(_json(content).encode()) <= 8192 and content.get("schema") == 1 and _id(content.get("command_id")), "invalid-command")
        return content

    def ingest_manual(self, event, room):
        _require(room == self._meta("local_room"), "wrong-control-room")
        content = self._envelope(event, room, self.uplink.cfg.local_user, POLICY_REQUEST)
        _require(set(content) == {"schema", "command_id", "journal_epoch", "room_id", "level", "expected_revision"}, "invalid-manual-fields")
        _require(content["journal_epoch"] == self.epoch and _room(content["room_id"]), "stale-control-epoch")
        _require(type(content["expected_revision"]) is int and 0 <= content["expected_revision"] < 9007199254740991
                 and isinstance(content["level"], str) and content["level"] in {"private", "share", "direct", "inherit"}, "invalid-manual-policy")
        command_id = content["command_id"]
        previous = self._command(command_id)
        if previous:
            _require(previous["kind"] == "manual" and previous["request_json"] == _json(content), "command-id-collision")
            return json.loads(previous["result_json"])
        # Manual local policy does not need an active organization or CRM contact.
        self.journal.inspect(content["room_id"])
        self.publish_revision(content["room_id"])
        self._put_command(command_id, "manual", room, content, "manual_pending", {"schema": 1, "command_id": command_id, "room_id": content["room_id"], "state": "queued"})
        self._queue_manual(self._command(command_id))
        return json.loads(self._command(command_id)["result_json"])

    def _queue_manual(self, command):
        content = json.loads(command["request_json"])
        command_id = command["command_id"]
        operation = "manual-" + hashlib.sha256((self.epoch + command_id).encode()).hexdigest()
        result = self.journal.submit_manual(operation_id=operation, actor=self.uplink.cfg.local_user, room_id=content["room_id"], expected_revision=content["expected_revision"], level=content["level"])
        output = {"schema": 1, "command_id": command_id, "room_id": content["room_id"], **result}
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO crm_control_children VALUES (?,?,?)", (operation, command_id, content["room_id"]))
        self._result(command_id, output, "queued")

    def _resolve(self, request):
        result = self.resolve_contact({"contact": request["contact"], "contact_version": request["contact_version"]}, owner_mxid=request["owner_mxid"], generation=request["generation"])
        if isinstance(result, dict) and result.get("status") == "unavailable":
            raise OSError("contact-evidence-unavailable")
        _require(isinstance(result, dict) and result.get("status") == "ready", "contact-" + str(result.get("status", "unavailable") if isinstance(result, dict) else "unavailable"))
        rooms = result.get("rooms")
        _require(isinstance(rooms, list) and 0 < len(rooms) <= MAX_ROOMS and all(isinstance(item, dict) and _room(item.get("room_id")) for item in rooms), "invalid-resolved-scope")
        _require(len({item["room_id"] for item in rooms}) == len(rooms), "ambiguous-resolved-scope")
        return sorted(rooms, key=lambda item: item["room_id"])

    def ingest_direct(self, event, room):
        self._verify_master_room(room)
        content = self._envelope(event, room, self.uplink.cfg.manager_mxid, DIRECT_REQUEST)
        expected = {"schema", "command_id", "phase", "owner_mxid", "generation", "journal_epoch", "binding_id", "contact", "contact_version", "membership_operation_id"}
        _require(content.get("phase") in {"prepare", "commit", "cancel"} and set(content) == expected | ({"ticket_id"} if content.get("phase") == "commit" else set()), "invalid-direct-fields")
        _require(content["owner_mxid"] == self.uplink.cfg.master_user and content["generation"] == self.generation and content["journal_epoch"] == self.epoch, "stale-direct-authority")
        _require(_id(content["binding_id"]) and _id(content["membership_operation_id"]) and isinstance(content["contact_version"], str) and 0 < len(content["contact_version"]) <= 256, "invalid-contact-binding")
        _require(type(content["contact"]) is dict and set(content["contact"]) == {"source", "network_id"}, "invalid-contact-identity")
        command_id = content["command_id"]
        request = {key: value for key, value in content.items() if key not in {"phase", "ticket_id"}}
        previous = self._command(command_id)
        if previous:
            _require(previous["kind"] == "crm" and previous["destination"] == room and previous["request_json"] == _json(request), "command-id-collision")
            if content["phase"] == "cancel":
                # Persist the parent barrier BEFORE touching any child. Recovery
                # never queues a missing child from a partially committed grant.
                self._result(command_id, {"schema": 1, "command_id": command_id, "phase": "cancel", "state": "pending", "safe_to_change_membership": False}, "canceling")
                return self._cancel_commit(self._command(command_id))
            if content["phase"] == "prepare" or previous["state"] != "prepared":
                return json.loads(previous["result_json"])
        if content["phase"] == "cancel":
            result = {"schema": 1, "command_id": command_id, "phase": "cancel", "state": "canceled", "safe_to_change_membership": True, "rooms": []}
            self._put_command(command_id, "crm", room, request, "complete", result)
            return result
        stamp = event.get("origin_server_ts")
        _require(type(stamp) is int and self.clock() - TTL_MS <= stamp <= self.clock() + 60000, "stale-direct-request")
        _require(self.uplink.active_link_for_dispatch() and not getattr(self.uplink, "_direct_suspended", True), "direct-control-suspended")
        try:
            proofs = self._resolve(request)
        except ValueError as error:
            if content["phase"] != "prepare" or str(error) != "contact-no_existing_conversation":
                raise
            # Freeze the consent ordering baseline when list intent is first
            # accepted, not later when the user approves a new conversation.
            with self.journal._locked():
                sequence = self.db.execute("SELECT COALESCE(max(sequence),0) FROM crm_policy_operations").fetchone()[0]
                result = {"schema": 1, "command_id": command_id, "phase": "prepare", "state": "approval_required", "reason": "contact-no_existing_conversation"}
                self._put_command(command_id, "crm", room, request, "approval_required", result,
                                  {"proofs": [], "policy_sequence": sequence, "activation": "awaiting-approved-chat"})
            return result
        if content["phase"] == "prepare":
            revisions = {proof["room_id"]: self.journal.inspect(proof["room_id"])["revision"] for proof in proofs}
            ticket = {"proofs": proofs, "revisions": revisions, "expires_at": min(stamp, self.clock()) + TTL_MS}
            ticket["ticket_id"] = hashlib.sha256(_json([request, ticket]).encode()).hexdigest()
            result = {"schema": 1, "command_id": command_id, "phase": "prepare", "state": "prepared", "ticket_id": ticket["ticket_id"], "expires_at": ticket["expires_at"], "route_count": len(proofs)}
            self._put_command(command_id, "crm", room, request, "prepared", result, ticket)
            return result
        _require(previous is not None, "missing-prepare")
        ticket = json.loads(previous["ticket_json"])
        _require(content["ticket_id"] == ticket["ticket_id"] and self.clock() <= ticket["expires_at"], "expired-or-wrong-ticket")
        _require(_json(proofs) == _json(ticket["proofs"]), "contact-scope-changed")
        self._result(command_id, {"schema": 1, "command_id": command_id, "phase": "commit", "state": "queued"}, "committing")
        self._queue_commit(self._command(command_id))
        return json.loads(self._command(command_id)["result_json"])

    def activate_approved_conversation(self, item_row):
        """Finish a still-listed contact's grant after approved chat discovery.

        Engine acceptance is required but never supplies message/receipt IDs.
        Exact current contact/portal evidence and the original manual-consent
        baseline authorize only the new permission operation through the same
        journal, CAS, restore fence and cancellation paths as an existing room.
        """
        if not self.local_ready or not self.connected or not self.uplink.active_link_for_dispatch():
            return
        row = self.batch.item_for_event(item_row["proposal_event_id"])
        if row is None or row["native_status"] != "accepted" or not row["native_command_event_id"] or not row["approval_json"]:
            return
        try:
            context, item, request = self.approval._context(row)
            _require(item["route"]["mode"] == "request_new_conversation_approval", "activation-not-new-chat")
            command = self._command(item["membership_operation_id"])
            if command["state"] != "approval_required":
                return
            baseline = json.loads(command["ticket_json"]) if command["ticket_json"] else {}
            _require(baseline.get("activation") == "awaiting-approved-chat" and type(baseline.get("policy_sequence")) is int,
                     "activation-baseline-unavailable")
            claim = json.loads(row["approval_json"])
            event = self.approval._read(claim["room_id"], claim["event_id"])
            approved = self.approval._request(event, claim["room_id"])
            _require(approved == claim["request"] and approved["decision"] == "approve"
                     and event.get("event_id") == claim["event_id"], "activation-approval-changed")
            proofs = self._resolve(request)
            _require(len(proofs) == 1 and proofs[0].get("source") == item["route"]["source"], "activation-room-ambiguous")
            room = proofs[0]["room_id"]
            with self.journal._locked():
                current = self._command(command["command_id"])
                if current["state"] != "approval_required" or current["request_json"] != command["request_json"]:
                    return
                _require(not self._manual_after(room, baseline["policy_sequence"]), "activation-newer-manual-policy")
                content, encoded = self.journal._read(room)
                observed = self.journal._observe(room, encoded)
                # No room existed in the original scope, so an explicit
                # unjournaled override cannot safely be treated as older intent.
                _require(not content, "activation-existing-explicit-policy")
                ticket = {"proofs": proofs, "revisions": {room: observed["revision"]},
                          "activation": "approved-chat", "policy_sequence": baseline["policy_sequence"], "review_contact_version": item["contact_version"],
                          "approval_event_id": claim["event_id"], "native_command_event_id": row["native_command_event_id"]}
                result = {"schema": 1, "command_id": command["command_id"], "phase": "commit", "state": "queued"}
                with self.db:
                    self.db.execute("UPDATE crm_control_commands SET ticket_json=?,result_json=?,state='committing' WHERE command_id=? AND state='approval_required'",
                                    (_json(ticket), _json(result), command["command_id"]))
            self._queue_commit(self._command(command["command_id"]))
        except ValueError as error:
            if str(error) == "contact-no_existing_conversation":
                return
            # Keep the original membership visible, with an actionable reason;
            # never start another native conversation or silently rebase consent.
            item = json.loads(row["item_json"])
            command = self._command(item["membership_operation_id"])
            if command and command["state"] == "approval_required":
                self._result(command["command_id"], {"schema": 1, "command_id": command["command_id"],
                    "state": "approval_required", "reason": str(error), "activation": "review_required"})

    def _manual_after(self, room, sequence):
        rows = self.db.execute("SELECT request_json FROM crm_policy_operations WHERE room_id=? AND sequence>?", (room, sequence)).fetchall()
        return any(json.loads(row[0]).get("kind") == "manual" for row in rows)

    def _queue_commit(self, command):
        request, ticket = json.loads(command["request_json"]), json.loads(command["ticket_json"])
        for proof in ticket["proofs"]:
            if self._command(command["command_id"])["state"] != "committing":
                return
            room = proof["room_id"]
            operation = "crm-" + hashlib.sha256((self.epoch + command["command_id"] + room).encode()).hexdigest()
            self.journal.submit_crm(operation_id=operation, actor=self.uplink.cfg.manager_mxid,
                room_id=room, expected_revision=ticket["revisions"][room], binding_id=request["binding_id"], membership_operation_id=request["membership_operation_id"])
            with self.db:
                self.db.execute("INSERT OR IGNORE INTO crm_control_children VALUES (?,?,?)", (operation, command["command_id"], room))
        with self.db:
            self.db.execute("UPDATE crm_control_commands SET state='queued' WHERE command_id=? AND state='committing'", (command["command_id"],))

    def _cancel_commit(self, command):
        request = json.loads(command["request_json"])
        self.batch.cancel_membership(request["membership_operation_id"])
        ticket = json.loads(command["ticket_json"]) if command["ticket_json"] else {"proofs": []}
        outcomes = []
        for proof in ticket["proofs"]:
            room = proof["room_id"]
            operation = "crm-" + hashlib.sha256((self.epoch + command["command_id"] + room).encode()).hexdigest()
            outcome = self.journal.cancel_crm(operation_id=operation, actor=self.uplink.cfg.manager_mxid,
                binding_id=request["binding_id"], membership_operation_id=request["membership_operation_id"])
            if outcome is not None:
                outcomes.append({"room_id": room, **outcome})
        safe = all(outcome["state"] not in {"queued", "prepared", "verifying"} for outcome in outcomes)
        state = "applied" if outcomes and all(item["state"] == "applied" for item in outcomes) else "canceled"
        result = {"schema": 1, "command_id": command["command_id"], "phase": "cancel", "state": state if safe else "pending",
                  "safe_to_change_membership": safe, "rooms": outcomes}
        self._result(command["command_id"], result, "complete" if safe else "canceling")
        return result

    def _authorize(self, intent):
        if intent["kind"] == "manual":
            return intent["actor"] == self.uplink.cfg.local_user
        if self.uplink.meta_get("link_disabled") == "1":
            return False
        if not self.local_ready:
            raise OSError("local-policy-observation-incomplete")
        if not self.connected or not self.uplink.active_link_for_dispatch():
            raise OSError("active-link-unavailable")
        if getattr(self.uplink, "_direct_suspended", True):
            raise OSError("direct-control-awaiting-current-authority")
        # An explicit local Private request observed after an unexecuted CRM
        # grant wins. Do not apply Direct first and make the user's current-base
        # privacy intent fail CAS as a side effect of that pending automation.
        pending = self.db.execute("SELECT request_json FROM crm_policy_operations WHERE room_id=? AND state='queued'", (intent["room_id"],)).fetchall()
        if any(json.loads(row[0]).get("kind") == "manual" and json.loads(row[0]).get("level") == "private" for row in pending):
            return False
        row = self.db.execute("SELECT command_id FROM crm_control_children WHERE operation_id=?", (intent["operation_id"],)).fetchone()
        if not row:
            return False
        command = self._command(row[0])
        if command["state"] in {"canceling", "complete", "rejected"}:
            return False
        request, ticket = json.loads(command["request_json"]), json.loads(command["ticket_json"])
        if request["generation"] != self.generation or request["journal_epoch"] != self.epoch:
            return False
        if ticket.get("activation") == "approved-chat" and self._manual_after(intent["room_id"], ticket["policy_sequence"]):
            return False
        try:
            current_request = {**request, "contact_version": ticket["review_contact_version"]} if ticket.get("activation") == "approved-chat" else request
            return _json(self._resolve(current_request)) == _json(ticket["proofs"])
        except ValueError:
            return False

    def _poll(self, transport, room, kind):
        # A nonempty page is never proof we have observed the latest privacy
        # intent. Reach a stable empty tail using only server-issued tokens.
        for _ in range(12):
            if self._poll_page(transport, room, kind):
                return
        raise OSError("control-history-observation-incomplete")

    def _poll_page(self, transport, room, kind):
        cursor_key = "cursor:" + kind + ":" + room
        query = {"dir": "f", "limit": 50, "filter": _json({"types": [POLICY_REQUEST, APPROVAL_REQUEST] if kind == "manual" else [DIRECT_REQUEST, BATCH_REQUEST]})}
        cursor = self._meta(cursor_key)
        if cursor:
            query["from"] = cursor
        # Matrix >=1.3 permits omitted `from` to start at the beginning. Follow
        # server-issued tokens only; persist command work before advancing them.
        page = transport("GET", "/_matrix/client/v3/rooms/" + _quote(room) + "/messages", query=query, timeout=15)
        _require(isinstance(page, dict) and isinstance(page.get("chunk"), list), "invalid-control-page")
        for event in page["chunk"]:
            try:
                if kind == "manual" and event.get("type") == APPROVAL_REQUEST:
                    self.approval.ingest(event, room)
                elif kind == "manual":
                    self.ingest_manual(event, room)
                elif event.get("type") == BATCH_REQUEST:
                    _require(self.batch_enabled or (isinstance(event.get("content"), dict) and event["content"].get("phase") == "cancel"), "crm-batch-not-enabled")
                    self.batch.ingest(event, room)
                else:
                    self.ingest_direct(event, room)
            except ValueError as error:
                # Malformed/unauthorized input is not a reason to freeze every
                # later legitimate command. Operational failures still propagate.
                content = event.get("content") if isinstance(event, dict) else None
                if kind == "crm" and isinstance(event, dict) and event.get("type") == BATCH_REQUEST:
                    self.batch.reject(event, room, str(error))
                actor = self.uplink.cfg.local_user if kind == "manual" else self.uplink.cfg.manager_mxid
                if (isinstance(content, dict) and _id(content.get("command_id")) and event.get("sender") == actor
                        and event.get("type") == (POLICY_REQUEST if kind == "manual" else DIRECT_REQUEST)
                        and "state_key" not in event and event.get("room_id", room) == room):
                    outcome = "approval_required" if str(error) == "contact-no_existing_conversation" else "rejected"
                    result = {"schema": 1, "command_id": content["command_id"], "state": outcome, "reason": str(error)}
                    if kind == "manual" and _room(content.get("room_id")):
                        result["room_id"] = content["room_id"]
                        observed = self.db.execute("SELECT revision FROM crm_policy_rooms WHERE room_id=?", (content["room_id"],)).fetchone()
                        if observed:
                            result["current_revision"] = observed[0]
                    previous = self._command(content["command_id"])
                    if previous is None:
                        request = {key: value for key, value in content.items() if key not in {"phase", "ticket_id"}} if kind == "crm" else content
                        self._put_command(content["command_id"], kind, room, request, outcome, result)
                    elif previous["state"] == "prepared" and kind == "crm" and str(error) != "command-id-collision":
                        result["phase"] = content.get("phase")
                        self._result(content["command_id"], result, "rejected")
                continue
        if isinstance(page.get("end"), str) and page["end"] != cursor:
            self._meta(cursor_key, page["end"])
        if page["chunk"]:
            _require(isinstance(page.get("end"), str) and page["end"] != cursor, "control-cursor-no-progress")
            return False
        return not page.get("end") or page["end"] == cursor

    def _drain(self):
        for command_id, in self.db.execute("SELECT command_id FROM crm_control_commands WHERE state='canceling' LIMIT 50").fetchall():
            command = self._command(command_id)
            request = json.loads(command["request_json"])
            if request["generation"] == self.generation:
                self._cancel_commit(command)
        for command_id, in self.db.execute("SELECT command_id FROM crm_control_commands WHERE state='manual_pending' LIMIT 50").fetchall():
            try:
                self._queue_manual(self._command(command_id))
            except ValueError:
                stored = json.loads(self._command(command_id)["request_json"])
                self._result(command_id, {"schema": 1, "command_id": command_id, "room_id": stored.get("room_id"), "state": "rejected", "reason": "invalid-stored-manual-intent"}, "rejected")
        for command_id, in self.db.execute("SELECT command_id FROM crm_control_commands WHERE state='committing' LIMIT 20").fetchall():
            command = self._command(command_id)
            request = json.loads(command["request_json"])
            if request["generation"] != self.generation:
                self._result(command_id, {"schema": 1, "command_id": command_id, "state": "rejected", "reason": "authority-changed"}, "rejected")
            else:
                self._queue_commit(command)
        # One unresolved conversation must not block unrelated manual privacy.
        rooms = self.db.execute("SELECT DISTINCT room_id FROM crm_policy_operations WHERE state IN ('queued','prepared','verifying') LIMIT 50").fetchall()
        for room, in rooms:
            self.journal.process_next(room_id=room)
        for command_id, in self.db.execute("SELECT command_id FROM crm_control_commands WHERE state='canceling' LIMIT 50").fetchall():
            command = self._command(command_id)
            if json.loads(command["request_json"])["generation"] == self.generation:
                self._cancel_commit(command)
        for command_id, in self.db.execute("SELECT command_id FROM crm_control_commands WHERE state='queued' LIMIT 100").fetchall():
            command = self._command(command_id)
            children = self.db.execute("SELECT operation_id,room_id FROM crm_control_children WHERE command_id=? ORDER BY room_id", (command_id,)).fetchall()
            outcomes = [{"room_id": room, **self.journal.get(operation)} for operation, room in children]
            if not outcomes:
                continue
            done = all(item["state"] in {"applied", "rejected", "canceled", "outcome_unknown"} for item in outcomes)
            if command["kind"] == "manual":
                result = {"schema": 1, "command_id": command_id, **outcomes[0], "current_revision": self.publish_revision(outcomes[0]["room_id"])}
            else:
                state = "applied" if all(item["state"] == "applied" for item in outcomes) else "partial" if done else "pending"
                result = {"schema": 1, "command_id": command_id, "phase": "commit", "state": state, "rooms": outcomes}
            self._result(command_id, result, "complete" if done else "queued")

    def flush_results(self, kind):
        rows = self.db.execute("SELECT command_id FROM crm_control_commands WHERE kind=? AND (published_json IS NULL OR published_json<>result_json) LIMIT 50", (kind,)).fetchall()
        for command_id, in rows:
            command = self._command(command_id)
            transport, event_type = (self.uplink.local, POLICY_RESULT) if kind == "manual" else (self.uplink.master, DIRECT_RESULT)
            if kind == "crm" and (not self.connected or command["destination"] != self.uplink.meta_get("master_proposals_room")):
                continue
            result = json.loads(command["result_json"])
            if kind == "crm":
                request = json.loads(command["request_json"])
                result.update(owner_mxid=self.uplink.cfg.master_user,
                              generation=request.get("generation", self.generation),
                              journal_epoch=request.get("journal_epoch", self.epoch))
            transport("PUT", "/_matrix/client/v3/rooms/" + _quote(command["destination"]) + "/state/" + event_type + "/" + _quote(command_id), result, timeout=15)
            with self.db:
                self.db.execute("UPDATE crm_control_commands SET published_json=result_json WHERE command_id=?", (command_id,))

    def local_once(self):
        self.local_ready = False
        try:
            room = self.ensure_local()
            self._poll(self.uplink.local, room, "manual")
            self.local_ready = True
            self._drain()
            self.flush_results("manual")
            self.approval.drain()
            self.batch.reconcile_new_chats()
            self.tracking.update()
            self.batch.publish_item_status()
        except BaseException:
            self.local_ready = False
            raise

    def master_once(self):
        if not self.connected:
            return
        if not self.local_ready:
            raise OSError("local-policy-observation-incomplete")
        self.publish_master()
        self._poll(self.uplink.master, self.uplink.meta_get("master_proposals_room"), "crm")
        self.batch.mark_live()
        self._drain()
        self.flush_results("crm")
        self.batch.drain()
        self.tracking.update()
        self.batch.publish_item_status()
        self.batch.flush()

    def blocks_dispatch(self, room):
        if not self.local_ready:
            return True
        if self.batch.holds_policy_lock:
            # D2 re-reads consent inside the batch's shared writer lock. Avoid
            # recursively acquiring flock just to inspect already-locked rows.
            return self.db.execute("SELECT 1 FROM crm_policy_operations WHERE room_id=? AND state IN ('queued','prepared','verifying')", (room,)).fetchone() is not None
        return bool(self.journal.pending(room))

    def blocks_sharing(self, room):
        if not self.local_ready:
            return True
        return any(item["state"] in {"prepared", "verifying"} or item["level"] == "private" for item in self.journal.pending(room))
