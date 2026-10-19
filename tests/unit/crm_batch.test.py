#!/usr/bin/env python3
"""CRM acceptance and dispatch through the existing real D2 implementation."""
import copy
import importlib.util
import json
from pathlib import Path
import time
import subprocess
import unittest
import urllib.parse

spec = importlib.util.spec_from_file_location("crm_control_fixture", Path(__file__).with_name("crm_control.test.py"))
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)
import crm_batch


class BatchTests(fixture.ControlTests):
    # Reuse only the setup/transport fixture, not its inherited test methods.
    def setUp(self):
        super().setUp()
        self.now = int(time.time() * 1000)
        self.uplink.cfg.direct_send_cap = 20
        self.uplink.read_policy = lambda: {}
        self.uplink.meta_set("local_proposals_room", "!inbox:local")
        self.uplink.meta_set("proposal_sync_since", "live-watermark")
        self.local.make_room("!inbox:local", {})
        self.local.make_room("!person:local", {})
        self.uplink.db.execute("INSERT INTO proposal_map VALUES ('$old', '$localold', 'fallback')")
        self.uplink.db.execute("INSERT INTO mirror_rooms(local_room_id,master_room_id,source,last_synced_pos) VALUES ('!person:local','!mirror:master','imessage','')")
        self.uplink.db.commit()
        prepared = self.control.ingest_direct(self.direct(), "!proposals:master")
        self.control.ingest_direct(self.direct(phase="commit", ticket=prepared["ticket_id"]), "!proposals:master")
        self.control._drain()
        self.events = {}
        self.native = []
        original_local, original_master = self.uplink.local, self.uplink.master
        def matrix(base, method, path, body=None, **kwargs):
            parts = [urllib.parse.unquote(value) for value in path.split("/")]
            if "event" in parts:
                return copy.deepcopy(self.events[parts[-1]])
            if "send" in parts:
                offset = parts.index("send")
                event = {"event_id": "$local-" + str(len(self.events)), "sender": self.uplink.cfg.local_user,
                         "room_id": parts[offset - 1], "type": parts[offset + 1], "content": copy.deepcopy(body), "origin_server_ts": self.now}
                self.events[event["event_id"]] = event
                if event["type"] == "m.room.message":
                    self.native.append(event)
                return {"event_id": event["event_id"]}
            if "messages" in parts and parts[-2] == "!person:local":
                return {"chunk": []}
            return base(method, path, body, **kwargs)
        self.uplink.local = lambda method, path, body=None, **kwargs: matrix(original_local, method, path, body, **kwargs)
        self.uplink.master = lambda method, path, body=None, **kwargs: matrix(original_master, method, path, body, **kwargs)
        self.batch = self.control.batch
        self.control.batch_enabled = True
        self.batch.mark_live()

    def envelope(self, name="batch-one", proposal="$staged"):
        child = "child-" + crm_batch.digest([name, self.uplink.cfg.master_user, self.control.generation])
        item = {"item_id": "item-one", "person_id": "person", "binding_id": "binding-1", "binding_revision": 1,
                "membership_revision": 1, "membership_operation_id": "member-add-1", "contact_version": "7",
                "owner_mxid": self.uplink.cfg.master_user, "generation": self.control.generation, "child_id": child,
                "proposal_room_id": "!proposals:master", "body": "Hello ✓ 😀\nReviewed text",
                "route": {"mode": "direct_existing", "source": "imessage", "network_id": "person@example.com", "room_id": "!person:local", "consent_revision": 1}}
        # The direct fixture uses a separate command name; production uses the
        # membership operation ID for both, so clone the accepted fixture row.
        old = self.control._command("grant")
        with self.uplink.db:
            self.uplink.db.execute("INSERT OR IGNORE INTO crm_control_commands VALUES (?,?,?,?,?,?,?,?)", ("member-add-1", old["kind"], old["destination"], old["request_json"], old["ticket_json"], old["state"], old["result_json"], old["published_json"]))
        c = {"schema": 1, "phase": "submit", "owner_mxid": self.uplink.cfg.master_user, "generation": self.control.generation,
             "journal_epoch": self.control.epoch, "batch_id": name, "child_id": child, "workspace_id": "!crm:master",
             "payload_hash": "a" * 64, "reviewed_at": self.now, "parent_item_count": 1, "items": [item]}
        c["child_hash"] = crm_batch.digest(crm_batch.child_payload(c))
        item["proposal_event_id"] = proposal
        self.events[proposal] = {"event_id": proposal, "type": "com.jkali.proposal", "sender": self.uplink.cfg.manager_mxid,
            "room_id": "!proposals:master", "origin_server_ts": self.now,
            "content": {"body": item["body"], "target_room": "!person:local", crm_batch.ITEM_KEY: self.batch.annotation(c, item)}}
        event = {"event_id": "$" + name + proposal[1:], "type": crm_batch.BATCH_REQUEST, "sender": self.uplink.cfg.manager_mxid,
                 "room_id": "!proposals:master", "origin_server_ts": self.now, "content": c}
        self.events[event["event_id"]] = copy.deepcopy(event)
        return event

    def test_staged_proposal_is_inert_until_atomic_child_acceptance(self):
        event = self.envelope()
        self.uplink.forward_proposals("!proposals:master", "!inbox:local", [self.events["$staged"]], cold_start=False, suspended=False)
        self.assertEqual(self.native, [])
        result = self.batch.ingest(event, "!proposals:master")
        self.assertEqual(result["state"], "accepted")
        self.assertEqual(self.native, [])
        self.batch.drain()
        result = self.batch.result(event["content"]["child_id"])
        self.assertEqual(len(self.native), 1)
        self.assertEqual(result["items"][0]["state"], "sent")
        self.assertEqual(result["items"][0]["sent_event_id"], self.native[0]["event_id"])
        self.assertTrue(result["items"][0]["local_proposal_event_id"])
        self.batch.drain()
        self.assertEqual(len(self.native), 1)

    def test_second_token_equivalent_proposal_uses_first_accepted_item(self):
        first = self.envelope()
        self.batch.ingest(first, "!proposals:master")
        second = self.envelope(proposal="$different-token-staged")
        self.batch.ingest(second, "!proposals:master")
        self.batch.drain()
        self.assertEqual(len(self.native), 1)
        self.assertEqual(self.native[0]["content"]["com.jkali.auto_sent_from_proposal"], "$staged")

    def test_cancel_and_permission_revision_changes_prevent_dispatch(self):
        event = self.envelope()
        self.batch.ingest(event, "!proposals:master")
        self.batch.cancel(event["content"]["child_id"])
        self.batch.drain()
        self.assertEqual(self.native, [])
        other = self.envelope("second", "$second-staged")
        self.batch.ingest(other, "!proposals:master")
        self.local.overrides[("!person:local", "com.jkali.share_override")] = {"state": "private"}
        self.batch.drain()
        self.assertEqual(self.batch.result(other["content"]["child_id"])["items"][0]["reason"], "consent-revision-changed")
        self.assertEqual(self.native, [])

    def test_changed_text_duplicate_and_foreign_sender_rejected(self):
        event = self.envelope()
        self.events["$staged"]["content"]["body"] = "unreviewed"
        with self.assertRaisesRegex(ValueError, "proposal-review-mismatch"):
            self.batch.ingest(event, "!proposals:master")
        self.assertIsNone(self.batch.batch(event["content"]["child_id"]))
        event["sender"] = self.uplink.cfg.master_user
        with self.assertRaisesRegex(ValueError, "batch-authentication"):
            self.batch.ingest(event, "!proposals:master")

    def test_fresh_ready_account_needs_no_dummy_ordinary_proposal(self):
        with self.uplink.db:
            self.uplink.db.execute("DELETE FROM proposal_map")
        event = self.envelope()
        self.batch.ingest(event, "!proposals:master")
        self.batch.drain()
        self.assertEqual(len(self.native), 1)

    def test_historical_first_scan_stays_cold_after_baseline_established(self):
        with self.uplink.db:
            self.uplink.db.execute("DELETE FROM crm_control_meta WHERE key LIKE 'batch_live_after:%'")
        event = self.envelope()
        self.batch.ingest(event, "!proposals:master")
        self.batch.mark_live()
        self.batch.drain()
        self.assertEqual(self.native, [])
        item = self.batch.result(event["content"]["child_id"])["items"][0]
        self.assertEqual(item["state"], "refused")
        self.assertEqual(item["reason"], "direct-cold_start")

    def test_js_review_child_hash_matches_python_for_unicode(self):
        script = """
          import {createCRMState,applyCRMOperation,createReviewedBatch,createCRMChildEnvelope} from './shared/model/crm.js';
          let state=createCRMState({workspace_id:'!crm:master',manager_mxid:'@manager:master'}), n=0;
          for(const [type,entity_id,payload] of [
            ['person.create','person',{label:'Zoë 😀'}],
            ['binding.create','binding',{person_id:'person',owner_mxid:'@owner:master',generation:'generation',contact:{source:'imessage',network_id:'zoe@example.com'},contact_version:'7'}],
            ['list.create','list',{name:'Test'}],
            ['member.set','member',{list_id:'list',person_id:'person',binding_id:'binding'}]]) {
            const result=applyCRMOperation(state,{schema:1,workspace_id:'!crm:master',type,entity_id,payload,base_revision:0,op_id:'op-'+ ++n});
            if(result.outcome.code!=='accepted')throw Error(result.outcome.code); state=result.state;
          }
          const review=await createReviewedBatch(state,{batch_id:'batch',list_id:'list',reviewed_at:1700000000123,items:[{item_id:'item',person_id:'person',proposal_room_id:'!proposals:master',journal_epoch:'epoch',body:'Hello ✓ 😀\\nZoë',route:{mode:'direct_existing',source:'imessage',network_id:'zoe@example.com',room_id:'!person:local',consent_revision:3}}]});
          console.log(JSON.stringify(await createCRMChildEnvelope(review,review.children[0].child_id,{item:'$proposal'})));
        """
        child = json.loads(subprocess.check_output(['node', '--input-type=module', '-e', script], cwd=Path(__file__).resolve().parents[2], text=True))
        self.assertEqual(child['child_hash'], crm_batch.digest(crm_batch.child_payload(child)))
        self.assertEqual(child['child_id'], 'child-' + crm_batch.digest([child['batch_id'], child['owner_mxid'], child['generation']]))

    def test_rejected_child_ack_is_durable_without_poisoning_accepted_child(self):
        event = self.envelope()
        self.events['$staged']['content']['body'] = 'not reviewed'
        with self.assertRaisesRegex(ValueError, 'proposal-review-mismatch'):
            self.batch.ingest(event, '!proposals:master')
        self.batch.reject(event, '!proposals:master', 'proposal-review-mismatch')
        result = self.batch.result(event['content']['child_id'])
        self.assertEqual(result['state'], 'rejected')
        self.assertEqual(result['reason'], 'proposal-review-mismatch')
        self.events['$staged']['content']['body'] = event['content']['items'][0]['body']
        self.assertEqual(self.batch.ingest(event, '!proposals:master')['state'], 'rejected')
        accepted = self.envelope('valid', '$valid')
        self.batch.ingest(accepted, '!proposals:master')
        self.batch.reject(accepted, '!proposals:master', 'bad retry')
        self.assertEqual(self.batch.result(accepted['content']['child_id'])['state'], 'accepted')

    def test_lost_send_ack_requires_exact_server_transaction_to_reconcile(self):
        event = self.envelope()
        self.batch.ingest(event, '!proposals:master')
        transport = self.uplink.local
        lost = [False]
        def local(method, path, body=None, **kwargs):
            if method == 'GET' and '/messages' in path and '!person' in urllib.parse.unquote(path):
                return {'chunk': copy.deepcopy(self.native)}
            response = transport(method, path, body, **kwargs)
            if method == 'PUT' and '/send/m.room.message/' in path and not lost[0]:
                lost[0] = True
                raise OSError('lost acknowledgment')
            return response
        self.uplink.local = local
        self.batch.drain()
        self.assertEqual(self.batch.result(event['content']['child_id'])['items'][0]['state'], 'outcome_unknown')
        self.batch.drain()
        self.assertEqual(self.batch.result(event['content']['child_id'])['items'][0]['state'], 'outcome_unknown', 'cosmetic stamp and matching text are not sufficient')
        self.native[0]['unsigned'] = {'transaction_id': 'autosend_$staged'}
        self.batch.drain()
        self.assertEqual(self.batch.result(event['content']['child_id'])['items'][0]['sent_event_id'], self.native[0]['event_id'])
        self.assertEqual(len(self.native), 1)

    def test_disabled_batches_preserve_cancellation_without_dispatch(self):
        event = self.envelope()
        self.batch.ingest(event, '!proposals:master')
        self.control.batch_enabled = False
        self.batch.drain()
        self.assertEqual(self.native, [])
        canceled = copy.deepcopy(event)
        canceled['content'] = {key:value for key,value in event['content'].items() if key in {'schema','owner_mxid','generation','journal_epoch','batch_id','child_id','child_hash'}}
        canceled['content']['phase'] = 'cancel'
        self.assertEqual(self.batch.ingest(canceled, '!proposals:master')['state'], 'canceled')
        self.assertTrue(self.control.capability()['batch_cancel'])
        self.assertFalse(self.control.capability()['batch'])

    def test_catalog_refresh_uses_new_review_without_renewing_old_permission(self):
        event = self.envelope()
        old_request = self.control._command('member-add-1')['request_json']
        old_count = self.uplink.db.execute('SELECT count(*) FROM crm_policy_operations').fetchone()[0]
        item = event['content']['items'][0]
        item['contact_version'] = '8'
        event['content']['child_hash'] = crm_batch.digest(crm_batch.child_payload(event['content']))
        self.events['$staged']['content'][crm_batch.ITEM_KEY] = self.batch.annotation(event['content'], item)
        self.events[event['event_id']] = copy.deepcopy(event)
        observed = []
        def current(binding, owner_mxid, generation):
            observed.append(copy.deepcopy(binding))
            return {'status':'ready' if binding['contact_version'] == '8' else 'stale', 'rooms':copy.deepcopy(self.proofs)}
        self.control.resolve_contact = current
        self.batch.ingest(event, '!proposals:master'); self.batch.drain()
        self.assertEqual(len(self.native), 1)
        self.assertTrue(observed and all(binding['contact_version'] == '8' for binding in observed))
        self.assertEqual(self.control._command('member-add-1')['request_json'], old_request, 'cancel envelope remains immutable')
        self.assertEqual(self.uplink.db.execute('SELECT count(*) FROM crm_policy_operations').fetchone()[0], old_count, 'catalog maintenance never grants Direct again')

    def test_catalog_refresh_does_not_admit_stale_review_or_revoked_contact(self):
        for status in ('stale','unshared'):
            event = self.envelope('catalog-' + status, '$catalog-' + status)
            self.control.resolve_contact = lambda *a, **kw: {'status':status,'rooms':[]}
            self.batch.ingest(event, '!proposals:master'); self.batch.drain()
            self.assertEqual(self.batch.result(event['content']['child_id'])['items'][0]['reason'], 'contact-' + status)
        self.assertEqual(self.native, [])


for _name in tuple(vars(fixture.ControlTests)):
    if _name.startswith("test_"):
        setattr(BatchTests, _name, None)

if __name__ == "__main__":
    unittest.main()
