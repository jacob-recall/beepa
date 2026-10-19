"""Ordered, durable room-consent writer shared by CRM and manual intents.

This is not an authorization/discovery adapter. Before submit_crm the caller
must authenticate the current manager/pairing, verify the contact is currently
shared, and resolve an exact EXISTING one-to-one local conversation. Never pass
a manager-supplied arbitrary room directly to this class. Revalidate those gates
before process_next (the optional authorize callback supports this).

Only the fixed com.jkali.share_override room account-data is writable. Injected
read_override(room) and write_override(room, content) callbacks must implement
that exact key. Reads return {} only for a verified 404; other failures raise.
No network implementation, message sending, or consent resolver copy lives here.

All compatible writers must use this journal and its same lock path. An
unsupported account-data writer cannot participate in a race-free protocol;
detected divergence invalidates expected revisions, and ambiguous PUTs are never
automatically repeated. Deployment must gate older user UIs before enabling CRM.
"""
from contextlib import contextmanager
import fcntl
import json
import os
import re
import sqlite3

import consent

POLICY_MARKER = "com.jkali.policy_journal"
POLICY_SCHEMA = 1
MAX_REVISION = 9007199254740991
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}")
_ROOM = re.compile(r"![^\s:]+:[^\s]+")
_MXID = re.compile(r"@[^\s:]+:[^\s]+")


class PolicyRefused(Exception):
    """Transport positively refused a write (e.g. definitive 4xx, except 429)."""


class PolicyRetryable(Exception):
    """Transport positively did NOT apply a write (e.g. verified HTTP 429).

    Do not translate policy PUT timeouts or generic 5xx into this exception.
    Those may have applied and require reconciliation without retry. A failed
    prerequisite before any policy PUT may use this exception, provided its own
    durable uncertainty barrier prevents unsafe retry (as CRM checkpoints do).
    """


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _check(condition, code):
    if not condition:
        raise ValueError(code)


def _string(value, cap=256):
    return isinstance(value, str) and 0 < len(value) <= cap and not any(ord(c) < 32 for c in value)


def _valid(value, regex):
    return _string(value) and regex.fullmatch(value) is not None


def _revision(value):
    return type(value) is int and 0 <= value < MAX_REVISION


class ConsentJournal:
    """Synchronous serialized writer backed by the uplink's private SQLite DB.

    A stable lock_path derived from the database path must be shared by every
    instance/process using this DB. A separate path is injectable for tests.
    No caller transaction may be active: intent commits must precede remote PUT.
    The existing uplink owns state.db mode 600 and its schema migration version;
    these independently versioned, additive tables do not touch user_version.
    """

    def __init__(self, db, *, owner_mxid, generation, manager_mxid,
                 local_user_mxid, read_override, write_override, lock_path,
                 authorize=None):
        _check(isinstance(db, sqlite3.Connection), "invalid-database")
        _check(_valid(owner_mxid, _MXID) and _valid(manager_mxid, _MXID)
               and _valid(local_user_mxid, _MXID), "invalid-authority")
        _check(_string(generation, 128), "invalid-generation")
        _check(isinstance(lock_path, (str, os.PathLike)), "invalid-lock")
        self.db = db
        self.owner_mxid = owner_mxid
        self.generation = generation
        self.manager_mxid = manager_mxid
        self.local_user_mxid = local_user_mxid
        self.read_override = read_override
        self.write_override = write_override
        self.lock_path = os.fspath(lock_path)
        self.authorize = authorize
        with self._locked():
            with self.db:
                self.db.execute("CREATE TABLE IF NOT EXISTS crm_policy_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
                schema = self._one("SELECT value FROM crm_policy_meta WHERE key='schema'")
                _check(schema is None or schema["value"] == str(POLICY_SCHEMA), "unsupported-policy-schema")
                self.db.execute("INSERT OR IGNORE INTO crm_policy_meta VALUES ('schema', ?)", (str(POLICY_SCHEMA),))
                self.db.execute("""CREATE TABLE IF NOT EXISTS crm_policy_rooms (
                    room_id TEXT PRIMARY KEY, revision INTEGER NOT NULL,
                    observed_json TEXT NOT NULL)""")
                self.db.execute("""CREATE TABLE IF NOT EXISTS crm_policy_operations (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    operation_id TEXT UNIQUE NOT NULL, room_id TEXT NOT NULL,
                    request_json TEXT NOT NULL, state TEXT NOT NULL,
                    reason TEXT NOT NULL DEFAULT '', intended_json TEXT,
                    resulting_revision INTEGER, previous_json TEXT)""")
                self.db.execute("CREATE INDEX IF NOT EXISTS crm_policy_pending ON crm_policy_operations(state, sequence)")

    @contextmanager
    def _locked(self):
        _check(not self.db.in_transaction, "caller-transaction-active")
        flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(self.lock_path, flags, 0o600)
        try:
            os.fchmod(fd, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX)
            try:
                yield
            finally:
                # A failure must never leak our transaction into a later call.
                if self.db.in_transaction:
                    self.db.rollback()
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def _one(self, sql, args=()):
        cursor = self.db.execute(sql, args)
        row = cursor.fetchone()
        return dict(zip([item[0] for item in cursor.description], row)) if row is not None else None

    def _result(self, operation):
        return {"operation_id": operation["operation_id"], "state": operation["state"],
                "reason": operation["reason"], "revision": operation["resulting_revision"]}

    def _get(self, operation_id):
        return self._one("SELECT * FROM crm_policy_operations WHERE operation_id=?", (operation_id,))

    def get(self, operation_id):
        _check(_valid(operation_id, _ID), "invalid-operation-id")
        with self._locked():
            operation = self._get(operation_id)
            return self._result(operation) if operation else None

    def pending(self, room_id):
        """Expose local dispatch/mirroring guards without another remote read.

        A queued manual Private must suppress sharing while it waits; prepared
        or verifying writes mean permission ordering is unresolved. This method
        returns metadata only, never a permission grant. Do not call journal
        methods recursively from a transport or authorize callback.
        """
        _check(_valid(room_id, _ROOM), "invalid-room")
        with self._locked():
            rows = self.db.execute("SELECT operation_id,state,reason,request_json FROM crm_policy_operations WHERE room_id=? AND state IN ('queued','prepared','verifying') ORDER BY sequence", (room_id,)).fetchall()
            return [{"operation_id": row[0], "state": row[1], "reason": row[2],
                     "kind": json.loads(row[3])["kind"], "level": json.loads(row[3])["level"]}
                    for row in rows]

    def _read(self, room_id):
        content = self.read_override(room_id)
        _check(type(content) is dict, "invalid-policy-read")
        encoded = _json(content)
        _check(len(encoded.encode("utf-8")) <= 8192, "policy-too-large")
        return content, encoded

    def _observe(self, room_id, encoded):
        """Update revision for external changes, or confirm a recorded PUT."""
        room = self._one("SELECT * FROM crm_policy_rooms WHERE room_id=?", (room_id,))
        if room is None:
            with self.db:
                self.db.execute("INSERT INTO crm_policy_rooms VALUES (?, 0, ?)", (room_id, encoded))
            return {"room_id": room_id, "revision": 0, "observed_json": encoded}
        if encoded == room["observed_json"]:
            return room
        prepared = self._one("SELECT * FROM crm_policy_operations WHERE room_id=? AND state IN ('prepared','verifying') ORDER BY sequence LIMIT 1", (room_id,))
        if prepared and encoded == prepared["intended_json"]:
            uninterrupted = room["revision"] == prepared["resulting_revision"] - 1 and room["observed_json"] == prepared["previous_json"]
            revision = prepared["resulting_revision"] if uninterrupted else room["revision"] + 1
            _check(_revision(revision), "revision-exhausted")
            with self.db:
                self.db.execute("UPDATE crm_policy_operations SET state=?, reason=? WHERE operation_id=?", ("applied" if uninterrupted else "outcome_unknown", "" if uninterrupted else "superseded-write-confirmed", prepared["operation_id"]))
                self.db.execute("UPDATE crm_policy_rooms SET revision=?, observed_json=? WHERE room_id=?", (revision, encoded, room_id))
        else:
            revision = room["revision"] + 1
            _check(_revision(revision), "revision-exhausted")
            with self.db:
                self.db.execute("UPDATE crm_policy_rooms SET revision=?, observed_json=? WHERE room_id=?", (revision, encoded, room_id))
        return {"room_id": room_id, "revision": revision, "observed_json": encoded}

    def inspect(self, room_id, policy=None):
        """Read confirmed current revision; read failures propagate, never empty.

        `policy` must be the caller's fresh account-default read when it needs
        effective level. Omitting it reports the resolver's safe default only.
        """
        _check(_valid(room_id, _ROOM), "invalid-room")
        with self._locked():
            content, encoded = self._read(room_id)
            room = self._observe(room_id, encoded)
            return {"room_id": room_id, "revision": room["revision"],
                    "explicit_level": consent.normalize_override(content),
                    "effective_level": consent.resolved_level(content, policy or {}),
                    "content": content}

    def _submit(self, *, operation_id, actor, room_id, expected_revision, kind,
                level, binding_id=None, membership_operation_id=None):
        _check(_valid(operation_id, _ID) and _valid(room_id, _ROOM) and _revision(expected_revision), "invalid-intent")
        required_actor = self.manager_mxid if kind == "crm" else self.local_user_mxid
        _check(actor == required_actor, "actor-mismatch")
        request = {"schema": POLICY_SCHEMA, "operation_id": operation_id,
                   "owner_mxid": self.owner_mxid, "generation": self.generation,
                   "manager_mxid": self.manager_mxid, "local_user_mxid": self.local_user_mxid,
                   "actor": actor, "room_id": room_id, "expected_revision": expected_revision,
                   "kind": kind, "level": level}
        if kind == "crm":
            _check(_valid(binding_id, _ID) and _valid(membership_operation_id, _ID), "invalid-membership")
            request.update(binding_id=binding_id, membership_operation_id=membership_operation_id)
        serialized = _json(request)
        with self._locked():
            previous = self._get(operation_id)
            if previous:
                matching = previous["request_json"] == serialized
                if kind == "manual":
                    old = json.loads(previous["request_json"])
                    fields = ("kind", "schema", "operation_id", "actor", "local_user_mxid", "room_id", "expected_revision", "level")
                    matching = all(old.get(key) == request.get(key) for key in fields)
                _check(matching, "operation-id-collision")
                return self._result(previous)
            observed = self._one("SELECT revision FROM crm_policy_rooms WHERE room_id=?", (room_id,))
            _check(observed is not None, "policy-not-observed")
            with self.db:
                self.db.execute("INSERT INTO crm_policy_operations(operation_id,room_id,request_json,state) VALUES (?,?,?,'queued')", (operation_id, room_id, serialized))
            return self._result(self._get(operation_id))

    def submit_crm(self, *, operation_id, actor, room_id, expected_revision,
                   binding_id, membership_operation_id):
        return self._submit(operation_id=operation_id, actor=actor, room_id=room_id,
                            expected_revision=expected_revision, kind="crm", level="direct",
                            binding_id=binding_id, membership_operation_id=membership_operation_id)

    def submit_manual(self, *, operation_id, actor, room_id, expected_revision, level):
        _check(isinstance(level, str) and level in {"private", "share", "direct", "inherit"}, "invalid-level")
        return self._submit(operation_id=operation_id, actor=actor, room_id=room_id,
                            expected_revision=expected_revision, kind="manual", level=level)

    def cancel_crm(self, *, operation_id, actor, binding_id, membership_operation_id):
        """Cancel only a never-attempted grant; preserve all remote-write barriers."""
        _check(_valid(operation_id, _ID) and actor == self.manager_mxid, "invalid-cancel-authority")
        with self._locked():
            operation = self._get(operation_id)
            if operation is None:
                return None
            request = json.loads(operation["request_json"])
            _check(request["kind"] == "crm" and request["actor"] == actor
                   and (request["owner_mxid"], request["generation"], request["manager_mxid"], request["local_user_mxid"])
                       == (self.owner_mxid, self.generation, self.manager_mxid, self.local_user_mxid)
                   and request["binding_id"] == binding_id and request["membership_operation_id"] == membership_operation_id,
                   "invalid-cancel-authority")
            if operation["state"] == "queued":
                return self._finish(operation, "canceled", "membership-intent-canceled")
            return self._result(operation)

    def _finish(self, operation, state, reason=""):
        with self.db:
            self.db.execute("UPDATE crm_policy_operations SET state=?, reason=? WHERE operation_id=?", (state, reason, operation["operation_id"]))
        return self._result(self._get(operation["operation_id"]))

    def _reconcile(self, operation):
        # No write from this path, including after crash or a lost PUT response.
        try:
            _, encoded = self._read(operation["room_id"])
        except Exception:
            return {**self._result(operation), "reason": "reconcile-read-failed"}
        room = self._observe(operation["room_id"], encoded)
        operation = self._get(operation["operation_id"])
        if operation["state"] in {"applied", "outcome_unknown"}:
            return self._result(operation)
        if operation["state"] == "prepared":
            # A timed-out server request may still commit AFTER this GET. A
            # baseline or different marker is not evidence it has finished.
            # Keep a durable per-room barrier, including across pairing changes,
            # so a later manual Private cannot be overwritten by that late PUT.
            with self.db:
                self.db.execute("UPDATE crm_policy_operations SET reason='write-unresolved-ordering-blocked' WHERE operation_id=?", (operation["operation_id"],))
            return self._result(self._get(operation["operation_id"]))
        # A 2xx response positively completed the PUT. Different readback is an
        # external ordering conflict, not an in-flight request; require review.
        return self._finish(operation, "outcome_unknown", "write-not-confirmed-review-required")

    def process_next(self, room_id=None):
        """Process one durable queued intent, or reconcile one attempted PUT.

        Returns None when idle. Read errors return queued/prepared with a reason
        for caller backoff. Unknown PUTs block later writes to their room until
        exact-marker reconciliation proves completion; even baseline GET cannot
        prove a timed-out request will not commit later. room_id lets the caller
        continue other rooms without jumping a blocked predecessor in this room.
        """
        _check(room_id is None or _valid(room_id, _ROOM), "invalid-room")
        with self._locked():
            query = "SELECT * FROM crm_policy_operations WHERE state IN ('queued','prepared','verifying')"
            if room_id is not None:
                query += " AND room_id=?"
            operation = self._one(query + " ORDER BY sequence LIMIT 1", (room_id,) if room_id else ())
            if operation is None:
                return None
            request = json.loads(operation["request_json"])
            if operation["state"] in {"prepared", "verifying"}:
                return self._reconcile(operation)
            authority = (request["owner_mxid"], request["generation"], request["manager_mxid"], request["local_user_mxid"])
            authority_matches = (request["local_user_mxid"] == self.local_user_mxid if request["kind"] == "manual"
                                 else authority == (self.owner_mxid, self.generation, self.manager_mxid, self.local_user_mxid))
            if not authority_matches:
                return self._finish(operation, "canceled", "authority-changed")
            if self.authorize is not None:
                try:
                    authorized = self.authorize(dict(request))
                except Exception:
                    return {**self._result(operation), "reason": "authorization-unavailable"}
                if authorized is not True:
                    return self._finish(operation, "rejected", "authorization-refused")
            try:
                _, encoded = self._read(operation["room_id"])
            except Exception:
                return {**self._result(operation), "reason": "policy-read-failed"}
            room = self._observe(operation["room_id"], encoded)
            if room["revision"] != request["expected_revision"]:
                return self._finish(operation, "rejected", "revision-conflict")
            resulting_revision = room["revision"] + 1
            _check(_revision(resulting_revision), "revision-exhausted")
            intended = {POLICY_MARKER: {"schema": POLICY_SCHEMA,
                                       "operation_id": operation["operation_id"],
                                       "revision": resulting_revision,
                                       "owner_mxid": self.owner_mxid,
                                       "generation": self.generation}}
            # Missing state implements inherit without changing resolver semantics.
            # Marker remains present so inherit is just as reconcilable as Private.
            if request["level"] != "inherit":
                intended["state"] = request["level"]
            with self.db:
                self.db.execute("UPDATE crm_policy_operations SET state='prepared',intended_json=?,resulting_revision=?,previous_json=? WHERE operation_id=?", (_json(intended), resulting_revision, encoded, operation["operation_id"]))
            operation = self._get(operation["operation_id"])
            try:
                self.write_override(operation["room_id"], intended)
            except PolicyRefused:
                return self._finish(operation, "rejected", "write-refused")
            except PolicyRetryable:
                # A positive non-application response permits retry after fresh
                # authorization/read/revision checks. Timeout is never this case.
                with self.db:
                    self.db.execute("UPDATE crm_policy_operations SET state='queued',reason='write-retryable',intended_json=NULL,resulting_revision=NULL,previous_json=NULL WHERE operation_id=?", (operation["operation_id"],))
                return self._result(self._get(operation["operation_id"]))
            except Exception:
                return {**self._result(operation), "reason": "write-outcome-pending-reconciliation"}
            with self.db:
                self.db.execute("UPDATE crm_policy_operations SET state='verifying' WHERE operation_id=?", (operation["operation_id"],))
            operation = self._get(operation["operation_id"])
            return self._reconcile(operation)
