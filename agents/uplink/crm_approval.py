"""Teammate approval of a CRM item, serialized in the existing item journal.

A browser writes only an intent into its private policy room. This adapter
re-reads that exact self-authored event before dispatch and delegates native
room sends to the existing scheduled-send dispatcher. No second send queue.
"""
import json
import urllib.parse

from crm_batch import ITEM_KEY, canonical, digest, require, valid, _EVENT, _ROOM, _ID

APPROVAL_REQUEST = "com.jkali.crm.approval_request"
FIELDS = {"schema", "child_id", "item_id", "local_proposal_event_id", "journal_epoch", "decision", "body"}
MAX_AGE_MS = 600000


class CRMApproval:
    def __init__(self, batch):
        self.batch, self.control, self.uplink, self.db = batch, batch.control, batch.uplink, batch.db

    def _row(self, child, item):
        return self.batch.one("SELECT * FROM crm_batch_items WHERE child_id=? AND item_id=?", (child, item))

    def _read(self, room, event):
        return self.uplink.local("GET", "/_matrix/client/v3/rooms/" + urllib.parse.quote(room, safe="")
                                 + "/event/" + urllib.parse.quote(event, safe=""), timeout=15)

    def _request(self, event, room):
        require(room == self.control._meta("local_room") and isinstance(event, dict)
                and event.get("sender") == self.uplink.cfg.local_user
                and event.get("type") == APPROVAL_REQUEST and "state_key" not in event
                and event.get("room_id", room) == room and valid(event.get("event_id"), _EVENT), "approval-authentication")
        c = event.get("content")
        require(isinstance(c, dict) and set(c) == FIELDS and c.get("schema") == 1
                and all(valid(c.get(k), _ID) for k in ("child_id", "item_id"))
                and valid(c.get("local_proposal_event_id"), _EVENT)
                and c.get("journal_epoch") == self.control.epoch
                and isinstance(c.get("decision"), str) and c["decision"] in {"approve", "decline"}, "invalid-approval")
        from uplink import sanitize_send_body
        require((c["decision"] == "decline" and c["body"] == "")
                or (c["decision"] == "approve" and isinstance(c["body"], str) and bool(c["body"]) and sanitize_send_body(c["body"]) == c["body"]), "invalid-approved-body")
        return c

    def _context(self, row):
        parent = self.batch.batch(row["child_id"])
        require(parent is not None and parent["state"] != "canceled", "batch-canceled")
        c, item = json.loads(parent["request_json"]), json.loads(row["item_json"])
        require(c["owner_mxid"] == self.uplink.cfg.master_user and c["generation"] == self.control.generation
                and c["journal_epoch"] == self.control.epoch, "stale-approval-authority")
        grant = self.control._command(item["membership_operation_id"])
        require(grant is not None and grant["kind"] == "crm" and grant["state"] != "canceling"
                and json.loads(grant["result_json"]).get("phase") != "cancel", "membership-canceled")
        request = json.loads(grant["request_json"])
        require(request.get("binding_id") == item["binding_id"] and request.get("membership_operation_id") == item["membership_operation_id"] and request.get("generation") == c["generation"]
                and request.get("contact") == {"source": item["route"]["source"], "network_id": item["route"]["network_id"]}, "membership-review-mismatch")
        # Cancellation retains the original immutable permission envelope;
        # current send authority comes from this newly reviewed catalog version.
        return c, item, {**request, "contact_version": item["contact_version"]}

    def _proposal(self, row, parent, item):
        room = self.uplink.meta_get("local_proposals_room")
        require(valid(room, _ROOM) and valid(row["local_proposal_event_id"], _EVENT), "approval-proposal-unavailable")
        event = self._read(room, row["local_proposal_event_id"])
        require(isinstance(event, dict) and event.get("sender") == self.uplink.cfg.local_user
                and event.get("type") == "com.jkali.proposal" and "state_key" not in event
                and event.get("event_id") == row["local_proposal_event_id"] and event.get("room_id", room) == room,
                "approval-proposal-authentication")
        c = event.get("content")
        require(isinstance(c, dict) and c.get(ITEM_KEY) == self.batch.annotation(parent, item)
                and c.get("body") == item["body"] and c.get("com.jkali.auto_sent") is not True
                and c.get("com.jkali.send_ambiguous") is not True, "approval-proposal-changed")
        route = item["route"]
        if route["mode"] == "direct_existing":
            require(c.get("target_room") == route["room_id"] and "target_source" not in c
                    and "target_identifier" not in c, "approval-proposal-route-changed")
        else:
            require(c.get("target_source") == route["source"] and c.get("target_identifier") == route["network_id"]
                    and "target_room" not in c, "approval-proposal-route-changed")
        return c

    def final_gate(self, row, claim):
        """Cheap fresh checks after remote reads, especially the restore fence.

        Caller holds the policy flock; a checkpoint is still only preparation,
        and does not freeze account authority or the approval's fire window.
        """
        require(getattr(self.control, "batch_enabled", False) and self.control.local_ready and self.control.connected and self.uplink.active_link_for_dispatch(),
                "approval-account-unavailable")
        current = self._row(row["child_id"], row["item_id"])
        require(current is not None and current["state"] in {"approval_pending", "attempted"}
                and current["approval_json"] == canonical(claim), "approval-no-longer-pending")
        _, item, binding = self._context(current)
        try:
            proofs = self.control._resolve(binding)
        except ValueError as error:
            if item["route"]["mode"] != "request_new_conversation_approval" or str(error) != "contact-no_existing_conversation":
                raise
        else:
            require(item["route"]["mode"] == "direct_existing"
                    and any(proof["room_id"] == item["route"]["room_id"] for proof in proofs), "approval-target-changed")
        # The proof read itself can yield to account/cancellation changes.
        require(getattr(self.control, "batch_enabled", False) and self.control.local_ready and self.control.connected
                and self.uplink.active_link_for_dispatch(), "approval-account-unavailable")
        current = self._row(row["child_id"], row["item_id"])
        require(current is not None and current["state"] in {"approval_pending", "attempted"}
                and current["approval_json"] == canonical(claim), "approval-no-longer-pending")
        self._context(current)
        require(self.control.clock() - MAX_AGE_MS <= claim["origin_server_ts"] <= self.control.clock() + 60000,
                "approval-expired")

    def ingest(self, event, room):
        c = self._request(event, room)
        row = self._row(c["child_id"], c["item_id"])
        require(row is not None and row["local_proposal_event_id"] == c["local_proposal_event_id"], "approval-item-mismatch")
        if row.get("approval_json"):
            require(json.loads(row["approval_json"])["request"] == c, "approval-decision-conflict")
            return
        parent, item, _ = self._context(row)
        self._proposal(row, parent, item)
        stamp = event.get("origin_server_ts")
        require(type(stamp) is int and self.control.clock() - MAX_AGE_MS <= stamp <= self.control.clock() + 60000,
                "approval-expired")
        claim = {"request": c, "request_hash": digest(c), "event_id": event["event_id"],
                 "room_id": room, "origin_server_ts": stamp}
        with self.control.journal._locked():
            row = self._row(c["child_id"], c["item_id"])
            if row.get("approval_json"):
                require(json.loads(row["approval_json"])["request"] == c, "approval-decision-conflict")
                return
            self._context(row)
            require(row["state"] == "waiting_for_approval", "approval-item-not-pending")
            with self.db:
                self.db.execute("UPDATE crm_batch_items SET approval_json=?,state=?,reason='' WHERE child_id=? AND item_id=?",
                    (canonical(claim), "declined" if c["decision"] == "decline" else "approval_pending", c["child_id"], c["item_id"]))

    def drain(self):
        if not getattr(self.control, "batch_enabled", False) or not self.control.local_ready or not self.control.connected:
            return
        rows = self.db.execute("SELECT child_id,item_id FROM crm_batch_items WHERE state='approval_pending' LIMIT 50").fetchall()
        for child, identity in rows:
            with self.control.journal._locked():
                row = self._row(child, identity)
                if row["state"] != "approval_pending":
                    continue
                try:
                    require(self.uplink.active_link_for_dispatch(), "approval-account-unavailable")
                    claim = json.loads(row["approval_json"])
                    event = self._read(claim["room_id"], claim["event_id"])
                    c = self._request(event, claim["room_id"])
                    require(event.get("event_id") == claim["event_id"] and c == claim["request"]
                            and digest(c) == claim["request_hash"] and c["decision"] == "approve"
                            and event.get("origin_server_ts") == claim["origin_server_ts"], "approval-intent-changed")
                    require(self.control.clock() - MAX_AGE_MS <= claim["origin_server_ts"] <= self.control.clock() + 60000, "approval-expired")
                    parent, item, binding = self._context(row)
                    proposal = self._proposal(row, parent, item)
                    body = c["body"]
                    if item["route"]["mode"] == "request_new_conversation_approval":
                        require(item["route"]["source"] == "imessage" and proposal.get("target_source") == "imessage"
                                and proposal.get("target_identifier") == item["route"]["network_id"], "approval-new-chat-target-changed")
                        dispatcher = getattr(self.uplink, "dispatch_crm_new_chat", None)
                        if dispatcher is None:
                            continue  # The UI remains pending; never falls back to a browser send.
                        outcome = dispatcher(row, claim, proposal, self.batch.annotation(parent, item))
                        if outcome:
                            self.batch.set_item(row, outcome["state"], outcome.get("reason", ""), **outcome.get("fields", {}))
                        continue
                    room = proposal.get("target_room")
                    require(valid(room, _ROOM) and room != self.uplink.meta_get("local_proposals_room")
                            and self.uplink.room_is_attributed(room), "approval-target-unavailable")
                    require(not self.db.execute("SELECT 1 FROM mirror_rooms WHERE master_room_id=?", (room,)).fetchone(), "approval-target-unavailable")
                    proofs = self.control._resolve(binding)
                    require(any(p["room_id"] == room for p in proofs), "approval-target-changed")
                    require(self.uplink.direct_send_under_cap(self.uplink._room_hash(room)), "approval-rate-cap")
                    require(self.uplink.room_quiet_since(room, claim["origin_server_ts"], exempt_own_scheduled=True), "approval-superseded")
                    self.final_gate(row, claim)
                    self.batch.before_approved_send(row, room, body)
                    self.final_gate(row, claim)
                    state, reason, sent = self.uplink._scheduled_dispatch(claim["event_id"], room, body,
                        crm_provenance={"local_proposal_event_id": row["local_proposal_event_id"], "annotation": self.batch.annotation(parent, item)})
                    if state == "sent" and valid(sent, _EVENT):
                        self.batch.set_item(row, "sent", "", sent_event_id=sent, sent_room_id=room)
                    elif state == "refused":
                        self.batch.set_item(row, "refused", reason or "approval-send-refused")
                    else:
                        self.batch.set_item(row, "outcome_unknown", reason or "approval-send-awaiting-reconciliation")
                except ValueError as error:
                    current = self._row(child, identity)
                    self.batch.set_item(current, "outcome_unknown" if current["state"] == "attempted" else "refused", str(error))
                except Exception:
                    current = self._row(child, identity)
                    if current["state"] == "attempted":
                        self.batch.set_item(current, "outcome_unknown", "approval-send-awaiting-reconciliation")
            if not self.control.local_ready:
                break
