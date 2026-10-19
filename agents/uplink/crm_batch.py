"""Durable CRM child queue over real, manager-authored proposal events.

Only Uplink's existing D2 dispatcher sends messages. This adapter verifies and
queues immutable reviewed references, quarantines staged proposals, and records
exact outcomes. A source accepts only its own account's child, never the other
accounts' contact/message data from the parent review.
"""
import hashlib
import json
import re
import urllib.error
import urllib.parse
import uuid
from contextlib import contextmanager

BATCH_REQUEST = "com.jkali.crm.batch_request"
BATCH_RESULT = "com.jkali.crm.batch_result"
ITEM_KEY = "com.jkali.crm.item"
ITEM_STATUS = "com.jkali.crm.item_status"
SEND_CHECKPOINT = "com.jkali.crm.send_checkpoint"
APPROVAL_RESULT = "com.jkali.crm.approval_result"
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}")
_HASH = re.compile(r"[a-f0-9]{64}")
_ROOM = re.compile(r"![^\s:]+:[^\s]+")
_EVENT = re.compile(r"\$[^\s]{1,255}")
TTL_MS = 600000


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def require(value, reason):
    if not value:
        raise ValueError(reason)


def valid(value, regex):
    return isinstance(value, str) and regex.fullmatch(value) is not None


def quote(value):
    return urllib.parse.quote(value, safe="")


def child_payload(content):
    return {key: ([{k: v for k, v in item.items() if k != "proposal_event_id"} for item in value]
                  if key == "items" else value)
            for key, value in content.items() if key not in {"phase", "child_hash"}}


class CRMBatch:
    def __init__(self, control):
        self.control, self.uplink, self.db = control, control.uplink, control.db
        self.permit = None
        self.holds_policy_lock = False
        with self.db:
            self.db.execute("""CREATE TABLE IF NOT EXISTS crm_batches (
                child_id TEXT PRIMARY KEY, destination TEXT NOT NULL, request_json TEXT NOT NULL,
                source_event_id TEXT NOT NULL, source_timestamp INTEGER NOT NULL,
                state TEXT NOT NULL, published_json TEXT)""")
            if "cold_start" not in {row[1] for row in self.db.execute("PRAGMA table_info(crm_batches)")}:
                self.db.execute("ALTER TABLE crm_batches ADD COLUMN cold_start INTEGER NOT NULL DEFAULT 1")
            self.db.execute("""CREATE TABLE IF NOT EXISTS crm_batch_items (
                child_id TEXT NOT NULL, item_id TEXT NOT NULL, proposal_event_id TEXT UNIQUE NOT NULL,
                item_json TEXT NOT NULL, state TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '',
                local_proposal_event_id TEXT, sent_event_id TEXT, sent_room_id TEXT,
                PRIMARY KEY(child_id,item_id))""")
            if "approval_json" not in {row[1] for row in self.db.execute("PRAGMA table_info(crm_batch_items)")}:
                self.db.execute("ALTER TABLE crm_batch_items ADD COLUMN approval_json TEXT")
            for column in ("native_command_event_id", "native_management_room", "native_bot_mxid", "native_status", "artifact_json", "artifact_outcome"):
                if column not in {row[1] for row in self.db.execute("PRAGMA table_info(crm_batch_items)")}:
                    self.db.execute("ALTER TABLE crm_batch_items ADD COLUMN " + column + " TEXT")
            if "reason" not in {row[1] for row in self.db.execute("PRAGMA table_info(crm_batches)")}:
                self.db.execute("ALTER TABLE crm_batches ADD COLUMN reason TEXT NOT NULL DEFAULT ''")
            self.db.execute("""CREATE TABLE IF NOT EXISTS crm_batch_attempts (
                attempt_id TEXT PRIMARY KEY, proposal_event_id TEXT NOT NULL,
                checkpoint_json TEXT NOT NULL, state TEXT NOT NULL)""")

    def one(self, sql, args=()):
        cursor = self.db.execute(sql, args)
        row = cursor.fetchone()
        return dict(zip([column[0] for column in cursor.description], row)) if row else None

    def batch(self, child):
        return self.one("SELECT * FROM crm_batches WHERE child_id=?", (child,))

    def item_for_event(self, event):
        return self.one("SELECT * FROM crm_batch_items WHERE proposal_event_id=?", (event,))

    def set_item(self, row, state, reason="", **fields):
        allowed = {"local_proposal_event_id", "sent_event_id", "sent_room_id", "approval_json", "native_command_event_id", "native_management_room", "native_bot_mxid", "native_status"}
        require(set(fields) <= allowed, "invalid-item-fields")
        with self.db:
            self.db.execute("UPDATE crm_batch_items SET state=?,reason=?" + "".join("," + key + "=?" for key in fields)
                            + " WHERE child_id=? AND item_id=?", (state, reason, *fields.values(), row["child_id"], row["item_id"]))

    def annotation(self, content, item):
        return {"schema": 1, **{key: content[key] for key in ("batch_id", "child_id", "owner_mxid", "generation", "journal_epoch", "payload_hash", "child_hash")}, "item_id": item["item_id"]}

    def proposal(self, content, item):
        event = self.uplink.master("GET", "/_matrix/client/v3/rooms/" + quote(item["proposal_room_id"]) + "/event/" + quote(item["proposal_event_id"]), timeout=15)
        require(isinstance(event, dict) and event.get("event_id") == item["proposal_event_id"]
                and event.get("sender") == self.uplink.cfg.manager_mxid and event.get("type") == "com.jkali.proposal"
                and "state_key" not in event and event.get("room_id", item["proposal_room_id"]) == item["proposal_room_id"], "proposal-authentication")
        body = event.get("content")
        require(isinstance(body, dict) and body.get("body") == item["body"]
                and body.get(ITEM_KEY) == self.annotation(content, item), "proposal-review-mismatch")
        expected = {"body", ITEM_KEY}
        route = item["route"]
        if route["mode"] == "direct_existing":
            expected.add("target_room")
            require(body.get("target_room") == route["room_id"], "proposal-route-mismatch")
        else:
            expected.update(("target_source", "target_identifier"))
            require(body.get("target_source") == route["source"] and body.get("target_identifier") == route["network_id"], "proposal-route-mismatch")
        require(set(body) == expected, "proposal-extra-fields")
        require(type(event.get("origin_server_ts")) is int, "proposal-timestamp")
        return event

    def validate(self, event, room):
        cfg, control = self.uplink.cfg, self.control
        require(event.get("sender") == cfg.manager_mxid and event.get("type") == BATCH_REQUEST
                and "state_key" not in event and event.get("room_id", room) == room
                and valid(event.get("event_id"), _EVENT), "batch-authentication")
        c = event.get("content")
        require(isinstance(c, dict) and len(canonical(c).encode()) <= 48000 and c.get("schema") == 1, "invalid-batch")
        require(c.get("owner_mxid") == cfg.master_user and c.get("generation") == control.generation
                and c.get("journal_epoch") == control.epoch, "stale-batch-authority")
        require(all(valid(c.get(key), _ID) for key in ("batch_id", "child_id"))
                and valid(c.get("child_hash"), _HASH), "invalid-batch-identity")
        require(c["child_id"] == "child-" + digest([c["batch_id"], c["owner_mxid"], c["generation"]]), "invalid-child-identity")
        return c

    def ingest(self, event, room):
        self.control._verify_master_room(room)
        c = self.validate(event, room)
        previous = self.batch(c["child_id"])
        if previous and previous["state"] == "rejected":
            require(json.loads(previous["request_json"])["child_hash"] == c["child_hash"], "batch-id-collision")
            return self.result(c["child_id"])
        if c.get("phase") == "cancel":
            require(set(c) == {"schema", "phase", "owner_mxid", "generation", "journal_epoch", "batch_id", "child_id", "child_hash"}, "invalid-batch-cancel")
            if previous:
                old = json.loads(previous["request_json"])
                require(all(old.get(key) == value for key, value in c.items() if key != "phase"), "batch-id-collision")
            else:
                with self.db:
                    self.db.execute("INSERT INTO crm_batches(child_id,destination,request_json,source_event_id,source_timestamp,state) VALUES (?,?,?,?,?,'canceled')", (c["child_id"], room, canonical(c), event["event_id"], event.get("origin_server_ts", 0)))
            self.cancel(c["child_id"])
            return self.result(c["child_id"])
        require(c.get("phase") == "submit" and set(c) == {"schema", "phase", "owner_mxid", "generation", "journal_epoch", "batch_id", "child_id", "workspace_id", "payload_hash", "child_hash", "reviewed_at", "parent_item_count", "items"}, "invalid-batch-fields")
        require(valid(c["workspace_id"], _ROOM) and valid(c["payload_hash"], _HASH)
                and type(c["reviewed_at"]) is int and c["reviewed_at"] > 0
                and type(c["parent_item_count"]) is int and 1 <= c["parent_item_count"] <= 50, "invalid-batch-review")
        require(isinstance(c["items"], list) and 0 < len(c["items"]) <= c["parent_item_count"]
                and digest(child_payload(c)) == c["child_hash"], "invalid-child-hash")
        if previous:
            old = json.loads(previous["request_json"])
            if old.get("phase") == "cancel":
                require(all(c.get(key) == value for key, value in old.items() if key != "phase"), "batch-id-collision")
                return self.result(c["child_id"])
            # Matrix transaction deduplication is scoped to a token. Another
            # manager session can stage equivalent real proposal events; the
            # immutable reviewed child wins and its original references remain.
            require(canonical(child_payload(old)) == canonical(child_payload(c)), "batch-id-collision")
            return self.result(c["child_id"])
        require(type(event.get("origin_server_ts")) is int and c["reviewed_at"] <= event["origin_server_ts"] + 60000, "invalid-review-time")
        items, people, events = set(), set(), set()
        baseline = self.control._meta("batch_live_after:" + self.control.generation)
        cold = not baseline or event["origin_server_ts"] < int(baseline)
        for item in c["items"]:
            require(isinstance(item, dict) and set(item) == {"item_id", "person_id", "binding_id", "binding_revision", "membership_revision", "membership_operation_id", "contact_version", "owner_mxid", "generation", "child_id", "proposal_room_id", "route", "body", "proposal_event_id"}, "invalid-item-fields")
            require(all(valid(item.get(key), _ID) for key in ("item_id", "person_id", "binding_id", "membership_operation_id"))
                    and item["item_id"] not in items and item["person_id"] not in people and item["proposal_event_id"] not in events, "duplicate-or-invalid-item")
            items.add(item["item_id"]); people.add(item["person_id"]); events.add(item["proposal_event_id"])
            require(all(item[key] == c[key] for key in ("owner_mxid", "generation", "child_id"))
                    and item["proposal_room_id"] == room and valid(item["proposal_event_id"], _EVENT), "item-owner-mismatch")
            require(all(type(item[key]) is int and 0 <= item[key] < 9007199254740991 for key in ("binding_revision", "membership_revision"))
                    and isinstance(item["contact_version"], str) and 0 < len(item["contact_version"]) <= 256, "invalid-item-revision")
            route = item["route"]
            require(isinstance(route, dict) and route.get("mode") in {"direct_existing", "request_new_conversation_approval"}, "invalid-item-route")
            require(set(route) == {"mode", "source", "network_id", "consent_revision"} | ({"room_id"} if route["mode"] == "direct_existing" else set())
                    and isinstance(route["source"], str) and re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", route["source"])
                    and isinstance(route["network_id"], str) and 0 < len(route["network_id"]) <= 512
                    and type(route["consent_revision"]) is int and 0 <= route["consent_revision"] < 9007199254740991,
                    "invalid-item-route")
            require(route["mode"] != "direct_existing" or valid(route["room_id"], _ROOM), "invalid-item-route")
            # Use the identical D2 leaf; review text must not change in transit.
            from uplink import sanitize_send_body
            require(isinstance(item["body"], str) and bool(item["body"]) and sanitize_send_body(item["body"]) == item["body"], "invalid-reviewed-body")
            require(self.item_for_event(item["proposal_event_id"]) is None, "proposal-already-bound")
            proposal = self.proposal(c, item)
            cold = cold or proposal["origin_server_ts"] < int(baseline or 0)
        with self.db:
            self.db.execute("INSERT INTO crm_batches(child_id,destination,request_json,source_event_id,source_timestamp,state,cold_start) VALUES (?,?,?,?,?,'accepted',?)", (c["child_id"], room, canonical(c), event["event_id"], event["origin_server_ts"], int(cold)))
            for item in c["items"]:
                self.db.execute("INSERT INTO crm_batch_items(child_id,item_id,proposal_event_id,item_json,state) VALUES (?,?,?,?,'accepted')", (c["child_id"], item["item_id"], item["proposal_event_id"], canonical(item)))
        return self.result(c["child_id"])

    def reject(self, event, room, reason):
        # Acknowledge only an authenticated, correctly scoped child identity.
        # Never replace a previously accepted child with a bad retry payload.
        try:
            c = self.validate(event, room)
        except ValueError:
            return
        if self.batch(c["child_id"]):
            return
        identity = {key: c[key] for key in ("schema", "batch_id", "child_id", "owner_mxid", "generation", "journal_epoch", "child_hash")}
        with self.db:
            self.db.execute("INSERT INTO crm_batches(child_id,destination,request_json,source_event_id,source_timestamp,state,reason) VALUES (?,?,?,?,?,'rejected',?)",
                            (c["child_id"], room, canonical(identity), event["event_id"], event.get("origin_server_ts", 0), reason))

    def cancel(self, child):
        with self.db:
            self.db.execute("UPDATE crm_batches SET state='canceled' WHERE child_id=?", (child,))
            self.db.execute("UPDATE crm_batch_items SET state='canceled',reason='batch-canceled' WHERE child_id=? AND state IN ('accepted','waiting_for_approval','review_required','approval_pending')", (child,))

    def mark_live(self):
        key = "batch_live_after:" + self.control.generation
        if self.control._meta(key) is None:
            self.control._meta(key, str(self.control.clock()))

    def cancel_membership(self, operation):
        for row in self.db.execute("SELECT child_id,item_id,item_json FROM crm_batch_items WHERE state IN ('accepted','waiting_for_approval','review_required','approval_pending')").fetchall():
            if json.loads(row[2])["membership_operation_id"] == operation:
                self.set_item({"child_id": row[0], "item_id": row[1]}, "canceled", "membership-canceled")

    def result(self, child):
        batch = self.batch(child)
        c = json.loads(batch["request_json"])
        rows = self.db.execute("SELECT item_id,state,reason,local_proposal_event_id,sent_event_id,sent_room_id,approval_json,native_command_event_id,native_status FROM crm_batch_items WHERE child_id=? ORDER BY item_id", (child,)).fetchall()
        keys = ("item_id", "state", "reason", "local_proposal_event_id", "sent_event_id", "sent_room_id", "approval", "native_command_event_id", "native_status")
        items = [dict(zip(keys, row)) for row in rows]
        for item in items:
            tracking = getattr(self.control, "tracking", None)
            item["tracking"] = tracking.result(child, item["item_id"]) if tracking else None
            approval = json.loads(item["approval"]) if item["approval"] else None
            item["approval"] = ({key: value for key, value in approval.items() if key != "request"}
                                | {"decision": approval["request"]["decision"]}) if approval else None
        return {"schema": 1, **{key: c[key] for key in ("batch_id", "child_id", "owner_mxid", "generation", "journal_epoch", "child_hash")},
                "state": batch["state"], "reason": batch["reason"], "items": items}

    def verify_checkpoints(self, state):
        remote = {}
        for (kind, key), event in state.items():
            if kind != SEND_CHECKPOINT:
                continue
            c = event.get("content")
            row = self.one("SELECT * FROM crm_batch_attempts WHERE attempt_id=?", (key,))
            require(event.get("sender") == self.uplink.cfg.local_user and isinstance(c, dict)
                    and c.get("journal_epoch") == self.control.epoch and c.get("attempt_id") == key
                    and row is not None and row["checkpoint_json"] == canonical(c), "batch-journal-recovery-required")
            remote[key] = c
        for attempt, event, encoded in self.db.execute("SELECT attempt_id,proposal_event_id,checkpoint_json FROM crm_batch_attempts").fetchall():
            require(self.item_for_event(event) is not None and attempt in remote and canonical(remote[attempt]) == encoded,
                    "batch-checkpoint-reconciliation-required")
        with self.db:
            self.db.execute("UPDATE crm_batch_attempts SET state='confirmed' WHERE state='pending'")

    def before_send(self, event, room, body):
        row = self.item_for_event(event)
        if row is None:
            return
        require(self.permit == event and row["state"] == "accepted", "unclaimed-crm-dispatch")
        batch = self.batch(row["child_id"])
        c, item = json.loads(batch["request_json"]), json.loads(row["item_json"])
        require(item["route"].get("room_id") == room and item["body"] == body, "dispatch-review-mismatch")
        self.final_direct_gate(row, c, item)
        self._claim_attempt(row, item, room, body, approved=False)
        # Check again after the remote checkpoint. Compatible manual writers
        # are locked out; a detected unsupported raw writer still invalidates
        # the review before the actual message PUT.
        self.final_direct_gate(row, c, item)

    def final_direct_gate(self, row, c, item):
        control = self.control
        grant = control._command(item["membership_operation_id"])
        require(grant is not None, "missing-membership-intent")
        current_binding = {**json.loads(grant["request_json"]), "contact_version": item["contact_version"]}
        require(any(proof["room_id"] == item["route"].get("room_id") for proof in control._resolve(current_binding)), "route-changed")
        self._check_revision(item)
        require(control.batch_enabled and control.local_ready and control.connected and self.uplink.active_link_for_dispatch()
                and not getattr(self.uplink, "_direct_suspended", True), "account-unavailable")
        require(c["owner_mxid"] == self.uplink.cfg.master_user and c["generation"] == control.generation
                and c["journal_epoch"] == control.epoch, "stale-batch-authority")
        require(control.clock() - TTL_MS <= c["reviewed_at"] <= control.clock() + 60000, "review-expired")
        require(self.batch(row["child_id"])["state"] != "canceled", "batch-canceled")
        grant = control._command(item["membership_operation_id"])
        require(grant is not None and grant["state"] != "canceling"
                and json.loads(grant["result_json"]).get("phase") != "cancel", "membership-canceled")

    def before_approved_send(self, row, room, body):
        item = json.loads(row["item_json"])
        approval = json.loads(row["approval_json"])
        require(approval["request"]["body"] == body, "approval-body-mismatch")
        self._claim_attempt(row, item, room, body, approved=True)

    def _claim_attempt(self, row, item, room, body, approved):
        # Atomic local claim precedes the immutable remote restore fence. A
        # concurrent cancellation either wins before this transaction or sees
        # an attempted operation that cannot be recalled or duplicated.
        event = row["proposal_event_id"]
        attempt = str(uuid.uuid4())
        fence = {"schema": 1, "journal_epoch": self.control.epoch, "attempt_id": attempt,
                 "child_id": row["child_id"], "item_id": row["item_id"], "proposal_event_id": event}
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            current = self.item_for_event(event)
            require(current["state"] == ("approval_pending" if approved else "accepted"), "item-no-longer-queued")
            require(self.batch(row["child_id"])["state"] != "canceled", "batch-canceled")
            grant = self.control._command(item["membership_operation_id"])
            require(grant is not None and grant["state"] != "canceling" and json.loads(grant["result_json"]).get("phase") != "cancel", "membership-canceled")
            self.db.execute("UPDATE crm_batch_items SET state='attempted',sent_room_id=? WHERE child_id=? AND item_id=?", (room, row["child_id"], row["item_id"]))
            self.db.execute("INSERT INTO crm_batch_attempts VALUES (?,?,?,'pending')", (attempt, event, canonical(fence)))
        path = "/_matrix/client/v3/rooms/" + quote(self.control._meta("local_room")) + "/state/" + SEND_CHECKPOINT + "/" + attempt
        try:
            self.uplink.local("PUT", path, fence, timeout=15)
            require(self.uplink.local("GET", path, timeout=15) == fence, "batch-checkpoint-readback-mismatch")
            with self.db:
                self.db.execute("UPDATE crm_batch_attempts SET state='confirmed' WHERE attempt_id=?", (attempt,))
        except Exception:
            self.control.local_ready = False
            raise

    def record_send(self, event, outcome, sent_id, room):
        row = self.item_for_event(event)
        if row is None:
            return
        if outcome == "sent" and valid(sent_id, _EVENT):
            self.set_item(row, "sent", sent_event_id=sent_id, sent_room_id=room)
        elif outcome == "fallback":
            self.set_item(row, "refused", "local-send-refused")
        else:
            self.set_item(row, "outcome_unknown", "send-awaiting-exact-reconciliation")

    def record_artifact(self, event, content, outcome, local_id=None):
        row = self.item_for_event(event)
        if row is None:
            return content
        c, item = json.loads(self.batch(row["child_id"])["request_json"]), json.loads(row["item_json"])
        content = {**content, ITEM_KEY: self.annotation(c, item)}
        if local_id is None:
            with self.db:
                self.db.execute("UPDATE crm_batch_items SET artifact_json=COALESCE(artifact_json,?),artifact_outcome=COALESCE(artifact_outcome,?) WHERE child_id=? AND item_id=?",
                                (canonical(content), outcome, row["child_id"], row["item_id"]))
            # Preserve the exact first artifact across a lost PUT response.
            content = json.loads(self.item_for_event(event)["artifact_json"])
        if local_id is not None:
            require(valid(local_id, _EVENT), "invalid-local-proposal-id")
            state = "waiting_for_approval" if outcome == "fallback" and row["state"] != "sent" else row["state"]
            if outcome == "refused":
                state = "refused"
            if outcome in {"ambiguous", "attempted"} and row["state"] != "sent":
                state = "outcome_unknown"
            self.set_item(row, state, row["reason"], local_proposal_event_id=local_id)
        return content

    def reconcile_sends(self):
        # Matrix exposes transaction_id only to the sending client. Exact
        # server-attested transaction provenance can resolve a lost response;
        # cosmetic proposal stamps or body similarity alone never can.
        for event, in self.db.execute("SELECT proposal_event_id FROM crm_batch_items WHERE state='outcome_unknown' AND sent_room_id IS NOT NULL AND sent_event_id IS NULL LIMIT 50").fetchall():
            row = self.item_for_event(event)
            item = json.loads(row["item_json"])
            approval = json.loads(row["approval_json"]) if row["approval_json"] else None
            seed = approval["event_id"] if approval else event
            txn = ("sched_" if approval else "autosend_") + seed
            body = approval["request"]["body"] if approval else item["body"]
            new_chat = bool(approval and item["route"]["mode"] == "request_new_conversation_approval")
            if new_chat:
                body = "start-chat " + item["route"]["network_id"] + " | " + body
            try:
                page = self.uplink.local("GET", "/_matrix/client/v3/rooms/" + quote(row["sent_room_id"]) + "/messages",
                                         query={"dir": "b", "limit": 100}, timeout=15)
                require(isinstance(page, dict) and isinstance(page.get("chunk"), list), "invalid-send-reconciliation-page")
                matches = []
                for candidate in page["chunk"]:
                    if not isinstance(candidate, dict) or candidate.get("sender") != self.uplink.cfg.local_user or candidate.get("type") != "m.room.message" or "state_key" in candidate:
                        continue
                    if candidate.get("room_id", row["sent_room_id"]) != row["sent_room_id"] or not valid(candidate.get("event_id"), _EVENT):
                        continue
                    unsigned, content = candidate.get("unsigned"), candidate.get("content")
                    if not isinstance(unsigned, dict) or unsigned.get("transaction_id") != txn or not isinstance(content, dict) or content.get("msgtype") != "m.text" or content.get("body") != body:
                        continue
                    if approval:
                        context = json.loads(self.batch(row["child_id"])["request_json"])
                        if content.get("com.jkali.from_proposal") != row["local_proposal_event_id"] or content.get(ITEM_KEY) != self.annotation(context, item):
                            continue
                    elif content.get("com.jkali.auto_sent_from_proposal") != event:
                        continue
                    matches.append(candidate["event_id"])
                if len(set(matches)) == 1:
                    if new_chat:
                        self.set_item(row, "outcome_unknown", "new-chat-awaiting-native-correlation", native_command_event_id=matches[0], sent_room_id=None)
                    else:
                        self.set_item(row, "sent", "exact-transaction-reconciled", sent_event_id=matches[0])
            except (ValueError, OSError, urllib.error.HTTPError):
                continue

    def recover_artifacts(self):
        from uplink import AUTO_SENT_KEY, SENT_EVENT_ID_KEY, SEND_AMBIGUOUS_KEY
        inbox = self.uplink.meta_get("local_proposals_room")
        if not valid(inbox, _ROOM):
            return
        for event, in self.db.execute("SELECT proposal_event_id FROM crm_batch_items WHERE local_proposal_event_id IS NULL AND state IN ('sent','outcome_unknown','waiting_for_approval') LIMIT 50").fetchall():
            row = self.item_for_event(event)
            try:
                if row["artifact_json"]:
                    content, outcome = json.loads(row["artifact_json"]), row["artifact_outcome"]
                else:
                    c = json.loads(self.batch(row["child_id"])["request_json"])
                    item = json.loads(row["item_json"])
                    content = self.uplink._sanitize_proposal(self.proposal(c, item))
                    require(content is not None, "invalid-recovery-proposal")
                    outcome = "fallback"
                    if row["state"] == "sent":
                        outcome = "sent"
                        content.update({AUTO_SENT_KEY: True, SENT_EVENT_ID_KEY: row["sent_event_id"]})
                    elif row["state"] == "outcome_unknown":
                        outcome = "ambiguous"
                        content[SEND_AMBIGUOUS_KEY] = True
                # This writes only the proposal artifact using its original
                # transaction ID; it never enters D2 or writes a message.
                self.uplink._file_proposal_record(inbox, event, content, outcome)
            except (OSError, ValueError, urllib.error.HTTPError):
                continue

    def record_gate(self, event, gate):
        row = self.item_for_event(event)
        if row is not None:
            self.set_item(row, row["state"], "direct-" + gate)

    def authorized(self, event):
        return self.permit is not None and event.get("event_id") == self.permit

    def _check_revision(self, item):
        require(self.holds_policy_lock, "missing-dispatch-policy-lock")
        room = item["route"].get("room_id")
        if not room:
            return
        _, encoded = self.control.journal._read(room)
        observed = self.control.journal._observe(room, encoded)
        require(observed["revision"] == item["route"]["consent_revision"], "consent-revision-changed")

    @contextmanager
    def dispatch_lock(self):
        with self.control.journal._locked():
            self.holds_policy_lock = True
            try:
                yield
            finally:
                self.holds_policy_lock = False

    def eligible(self, c, item):
        control = self.control
        require(control.local_ready and control.connected and self.uplink.active_link_for_dispatch(), "account-unavailable")
        require(c["owner_mxid"] == self.uplink.cfg.master_user and c["generation"] == control.generation
                and c["journal_epoch"] == control.epoch, "stale-batch-authority")
        require(control.clock() - c["reviewed_at"] <= TTL_MS and c["reviewed_at"] <= control.clock() + 60000, "review-expired")
        grant = control._command(item["membership_operation_id"])
        require(grant is not None and grant["kind"] == "crm", "missing-membership-intent")
        request, result = json.loads(grant["request_json"]), json.loads(grant["result_json"])
        require(result.get("phase") != "cancel" and grant["state"] != "canceling", "membership-canceled")
        require(request.get("binding_id") == item["binding_id"] and request.get("membership_operation_id") == item["membership_operation_id"]
                and request.get("generation") == c["generation"]
                and request.get("contact") == {"source": item["route"]["source"], "network_id": item["route"]["network_id"]}, "membership-review-mismatch")
        try:
            proofs = control._resolve({**request, "contact_version": item["contact_version"]})
        except ValueError as error:
            if str(error) == "contact-no_existing_conversation" and item["route"]["mode"] == "request_new_conversation_approval":
                return
            raise
        require(item["route"]["mode"] == "direct_existing", "route-changed")
        room = item["route"]["room_id"]
        require(any(proof["room_id"] == room for proof in proofs), "route-changed")
        # inspect() takes the shared journal lock, so call eligibility before
        # entering the dispatch lock; inside it use the captured revision plus
        # another exact raw override comparison immediately before D2.
        observed = control.journal.inspect(room)
        require(observed["revision"] == item["route"]["consent_revision"], "consent-revision-changed")

    def drain(self):
        if not self.control.local_ready or not self.control.connected:
            return
        # A persisted attempt may have reached the server; never call D2 again.
        for row in self.db.execute("SELECT child_id,item_id FROM crm_batch_items WHERE state='attempted'").fetchall():
            self.set_item({"child_id": row[0], "item_id": row[1]}, "outcome_unknown", "interrupted-send-awaiting-reconciliation")
        self.reconcile_sends()
        self.recover_artifacts()
        if not self.control.batch_enabled:
            return
        for event, in self.db.execute("SELECT proposal_event_id FROM crm_batch_items WHERE state='accepted' LIMIT 50").fetchall():
            row = self.item_for_event(event)
            batch = self.batch(row["child_id"])
            c, item = json.loads(batch["request_json"]), json.loads(row["item_json"])
            if batch["state"] == "canceled":
                self.set_item(row, "canceled", "batch-canceled")
                continue
            try:
                self.eligible(c, item)
                current = self.uplink.master("GET", "/_matrix/client/v3/rooms/" + quote(batch["destination"]) + "/event/" + quote(batch["source_event_id"]), timeout=15)
                require(current.get("sender") == self.uplink.cfg.manager_mxid and current.get("type") == BATCH_REQUEST
                        and current.get("event_id") == batch["source_event_id"] and current.get("content") == c, "batch-source-changed")
                proposal = self.proposal(c, item)
                lpr = self.uplink.meta_get("local_proposals_room")
                require(valid(lpr, _ROOM), "local-inbox-unavailable")
                if item["route"]["mode"] == "direct_existing":
                    require(self.uplink.room_quiet_since(item["route"]["room_id"], min(c["reviewed_at"], proposal["origin_server_ts"], self.control.clock())), "review-superseded")
                with self.dispatch_lock():
                    self._check_revision(item)
                    self.permit = event
                    self.uplink.forward_proposals(batch["destination"], lpr, [proposal], cold_start=bool(batch["cold_start"]), suspended=getattr(self.uplink, "_direct_suspended", True))
            except ValueError as error:
                self.set_item(row, "review_required", str(error))
            except Exception:
                # Reads before any attempt are retryable. A before-send fence
                # or dispatched write retains its durable unknown outcome.
                changed = self.item_for_event(event)
                if changed["state"] == "attempted":
                    self.set_item(changed, "outcome_unknown", "send-awaiting-exact-reconciliation")
            finally:
                self.permit = None
            if not self.control.local_ready:
                break

    def publish_item_status(self):
        room = self.uplink.meta_get("local_proposals_room")
        if not valid(room, _ROOM):
            return
        for child, item in self.db.execute("SELECT child_id,item_id FROM crm_batch_items").fetchall():
            result = self.result(child)
            record = next(row for row in result["items"] if row["item_id"] == item)
            status = {**{key: value for key, value in result.items() if key not in {"items", "state"}}, **record}
            self.uplink.local("PUT", "/_matrix/client/v3/rooms/" + quote(room) + "/state/" + ITEM_STATUS + "/" + quote(child + "/" + item), status, timeout=15)

    def dispatch_new_chat(self, row, claim, proposal, annotation):
        from uplink import PROPOSAL_E164_RE, PROPOSAL_EMAIL_RE
        item = json.loads(row["item_json"])
        handle, body = item["route"]["network_id"], claim["request"]["body"]
        require(item["route"]["source"] == "imessage" and not handle.startswith("-")
                and (PROPOSAL_E164_RE.fullmatch(handle) or PROPOSAL_EMAIL_RE.fullmatch(handle))
                and body.strip() == body, "invalid-new-chat-target-or-body")
        reader = getattr(self.uplink, "_crm_contact_reader", None)
        require(reader is not None, "new-chat-reader-unavailable")
        context = json.loads(self.batch(row["child_id"])["request_json"])
        resolved = self.control.resolve_contact({"contact": {"source": "imessage", "network_id": handle}, "contact_version": item["contact_version"]},
                                                owner_mxid=context["owner_mxid"], generation=context["generation"])
        if resolved.get("status") == "unavailable":
            raise OSError("new-chat-source-unavailable")
        require(resolved.get("status") == "no_existing_conversation", "new-chat-contact-or-route-changed")
        connection = reader.read_connections("imessage")
        room, bot = connection["management_room"], connection["bot_mxid"]
        require(connection.get("local_owner_mxid") == self.uplink.cfg.local_user and valid(room, _ROOM), "new-chat-owner-changed")
        require(self.uplink.direct_send_under_cap(self.uplink._room_hash(room)), "new-chat-rate-cap")
        self.set_item(row, row["state"], row["reason"], native_management_room=room, native_bot_mxid=bot)
        self.control.approval.final_gate(row, claim)
        self.before_approved_send(row, room, body)
        self.control.approval.final_gate(row, claim)
        state, reason, command = self.uplink._scheduled_dispatch(claim["event_id"], room, "start-chat " + handle + " | " + body,
            crm_provenance={"local_proposal_event_id": row["local_proposal_event_id"], "annotation": annotation})
        if state == "refused":
            return {"state": "refused", "reason": reason or "new-chat-command-refused"}
        fields = {"native_management_room": room, "native_bot_mxid": bot, "sent_room_id": None}
        if valid(command, _EVENT):
            fields["native_command_event_id"] = command
        return {"state": "outcome_unknown", "reason": "new-chat-awaiting-native-correlation", "fields": fields}

    def reconcile_new_chats(self):
        for event, in self.db.execute("SELECT proposal_event_id FROM crm_batch_items WHERE state='outcome_unknown' AND native_command_event_id IS NOT NULL").fetchall():
            row = self.item_for_event(event)
            try:
                state = self.control._room_state(self.uplink.local, row["native_management_room"])
                marker = state.get(("com.jkali.imessage_start_chat", row["native_command_event_id"]), {})
                content = marker.get("content", {})
                require(marker.get("sender") == row["native_bot_mxid"] and content.get("schema") == 1
                        and content.get("command_event_id") == row["native_command_event_id"]
                        and content.get("owner_mxid") == self.uplink.cfg.local_user, "new-chat-status-authentication")
                status = content.get("status")
                require(status in {"accepted", "ambiguous", "refused", "retryable"}, "new-chat-status-invalid")
                if status == "refused":
                    self.set_item(row, "refused", "native-" + status, native_status=status)
                else:
                    # The pinned native CLI gives no exact sent ID. Engine
                    # acceptance is an observation, not a sent/delivered receipt.
                    self.set_item(row, "outcome_unknown", "new-chat-native-correlation-unavailable", native_status=status)
                    if status == "accepted":
                        self.control.activate_approved_conversation(row)
            except (ValueError, OSError, urllib.error.HTTPError):
                continue

    def flush(self):
        for child, in self.db.execute("SELECT child_id FROM crm_batches").fetchall():
            batch = self.batch(child)
            if not self.control.connected or batch["destination"] != self.uplink.meta_get("master_proposals_room"):
                continue
            result = self.result(child)
            encoded = canonical(result)
            if batch["published_json"] != encoded:
                self.uplink.master("PUT", "/_matrix/client/v3/rooms/" + quote(batch["destination"]) + "/state/" + BATCH_RESULT + "/" + quote(child), result, timeout=15)
                with self.db:
                    self.db.execute("UPDATE crm_batches SET published_json=? WHERE child_id=?", (encoded, child))
