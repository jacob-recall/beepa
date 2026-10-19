"""Approval claims on real batch rows and the existing scheduled dispatcher."""
import copy
import importlib.util
import json
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location('crm_batch_fixture', Path(__file__).with_name('crm_batch.test.py'))
fixture = importlib.util.module_from_spec(spec); spec.loader.exec_module(fixture)
from crm_approval import APPROVAL_REQUEST, CRMApproval


class ApprovalTests(fixture.BatchTests):
    def setUp(self):
        super().setUp()
        self.submitted = self.envelope(); self.batch.ingest(self.submitted, '!proposals:master')
        self.uplink._direct_suspended = True
        self.batch.drain(); self.uplink._direct_suspended = False
        # Compatibility fixture: an already-filed approval from the previous
        # release. New Direct gate failures are refused, never approval drafts.
        self.assertEqual(self.batch.item_for_event('$staged')['state'], 'refused')
        self.uplink.db.execute("UPDATE crm_batch_items SET state='waiting_for_approval' WHERE proposal_event_id='$staged'")
        self.uplink.db.commit()
        self.row = self.batch.item_for_event('$staged')
        self.assertEqual(self.row['state'], 'waiting_for_approval')
        self.uplink.room_is_attributed = lambda room: room == '!person:local'
        self.approval = CRMApproval(self.batch)

    def decision(self, decision='approve', body='Edited approved text', event_id='$approval'):
        e = {'event_id': event_id, 'sender': self.uplink.cfg.local_user, 'type': APPROVAL_REQUEST,
             'room_id': self.room, 'origin_server_ts': self.now,
             'content': {'schema': 1, 'child_id': self.row['child_id'], 'item_id': self.row['item_id'],
                 'local_proposal_event_id': self.row['local_proposal_event_id'], 'journal_epoch': self.control.epoch,
                 'decision': decision, 'body': '' if decision == 'decline' else body}}
        self.events[event_id] = copy.deepcopy(e)
        return e

    def test_approval_contact_revoked_after_checkpoint_blocks_native_send(self):
        self.approval.ingest(self.decision(), self.room)
        original = self.batch.before_approved_send
        def revoke(*args):
            original(*args)
            self.resolve_status = 'unshared'
        self.batch.before_approved_send = revoke
        self.approval.drain()
        self.assertEqual(self.native, [])
        self.assertEqual(self.batch.item_for_event('$staged')['state'], 'outcome_unknown')
        self.resolve_status = 'ready'; CRMApproval(self.batch).drain()
        self.assertEqual(self.native, [])

    def test_approval_exact_user_intent_reuses_scheduled_send_and_records_exact_ids(self):
        event = self.decision(); self.approval.ingest(event, self.room); self.approval.drain()
        self.assertEqual(len(self.native), 1)
        self.assertEqual(self.native[0]['content']['body'], 'Edited approved text')
        self.assertEqual(self.native[0]['content']['com.jkali.from_proposal'], self.row['local_proposal_event_id'])
        self.assertEqual(self.batch.item_for_event('$staged')['sent_event_id'], self.native[0]['event_id'])
        self.approval.drain(); self.assertEqual(len(self.native), 1)

    def test_approval_two_tokens_same_intent_claims_first_event_and_sends_once(self):
        self.approval.ingest(self.decision(event_id='$token-one'), self.room)
        self.approval.ingest(self.decision(event_id='$token-two'), self.room)
        claim = json.loads(self.batch.item_for_event('$staged')['approval_json'])
        self.assertEqual(claim['event_id'], '$token-one')
        self.approval.drain(); self.assertEqual(len(self.native), 1)
        self.approval.ingest(self.decision(event_id='$browser-restart'), self.room)
        self.approval.drain(); self.assertEqual(len(self.native), 1)

    def test_approval_different_body_or_decision_cannot_replace_first_claim(self):
        self.approval.ingest(self.decision(), self.room)
        with self.assertRaisesRegex(ValueError, 'decision-conflict'):
            self.approval.ingest(self.decision(body='Different', event_id='$different'), self.room)
        with self.assertRaisesRegex(ValueError, 'decision-conflict'):
            self.approval.ingest(self.decision('decline', event_id='$decline'), self.room)
        self.approval.drain(); self.assertEqual(self.native[0]['content']['body'], 'Edited approved text')

    def test_approval_decline_is_durable_and_never_sends(self):
        self.approval.ingest(self.decision('decline'), self.room)
        CRMApproval(self.batch).drain(); self.assertEqual(self.native, [])
        self.assertEqual(self.batch.item_for_event('$staged')['state'], 'declined')
        with self.assertRaisesRegex(ValueError, 'decision-conflict'):
            self.approval.ingest(self.decision(event_id='$later-approve'), self.room)

    def test_approval_forged_envelope_or_null_body_never_claims(self):
        for mutate in [lambda e: e.update(sender='@foreign:local'), lambda e: e.update(state_key=''),
                       lambda e: e.update(room_id='!wrong:local'), lambda e: e['content'].update(body=None),
                       lambda e: e['content'].update(local_proposal_event_id='$other'),
                       lambda e: e['content'].update(journal_epoch='old')]:
            event = self.decision(); mutate(event)
            with self.assertRaises(ValueError): self.approval.ingest(event, self.room)
        self.assertIsNone(self.batch.item_for_event('$staged')['approval_json']); self.assertEqual(self.native, [])

    def test_approval_fresh_re_read_rejects_changed_or_redacted_intent(self):
        event = self.decision(); self.approval.ingest(event, self.room)
        self.events[event['event_id']]['content'] = {}
        self.approval.drain(); self.assertEqual(self.native, [])
        self.assertEqual(self.batch.item_for_event('$staged')['state'], 'refused')

    def test_approval_cancel_after_claim_prevents_dispatch(self):
        self.approval.ingest(self.decision(), self.room)
        self.batch.cancel(self.row['child_id']); self.approval.drain()
        self.assertEqual(self.native, []); self.assertEqual(self.batch.item_for_event('$staged')['state'], 'canceled')

    def test_approval_rate_cap_and_current_room_proof_are_enforced(self):
        self.approval.ingest(self.decision(), self.room)
        self.uplink.direct_send_under_cap = lambda room: False
        self.approval.drain(); self.assertEqual(self.native, [])
        self.assertEqual(self.batch.item_for_event('$staged')['state'], 'refused')

    def test_approval_native_timeout_is_terminal_unknown_and_never_retried(self):
        self.approval.ingest(self.decision(), self.room)
        original = self.uplink.local; attempted = []
        def timeout(method, path, body=None, **kwargs):
            if method == 'PUT' and '/send/m.room.message/' in path:
                attempted.append(body); raise TimeoutError('server may still send')
            return original(method, path, body, **kwargs)
        self.uplink.local = timeout
        self.approval.drain(); CRMApproval(self.batch).drain(); self.batch.drain()
        self.assertEqual(len(attempted), 1); self.assertEqual(self.batch.item_for_event('$staged')['state'], 'outcome_unknown')


    def test_approval_rollout_disabled_keeps_claim_pending_without_dispatch(self):
        self.approval.ingest(self.decision(), self.room)
        self.control.batch_enabled = False; self.approval.drain()
        self.assertEqual(self.native, []); self.assertEqual(self.batch.item_for_event('$staged')['state'], 'approval_pending')
        self.control.batch_enabled = True; self.approval.drain()
        self.assertEqual(len(self.native), 1)

    def test_approval_rollout_disabled_during_checkpoint_prevents_dispatch(self):
        self.approval.ingest(self.decision(), self.room)
        original = self.batch.before_approved_send
        def disable(*args):
            original(*args); self.control.batch_enabled = False
        self.batch.before_approved_send = disable
        self.approval.drain(); self.assertEqual(self.native, [])
        self.assertEqual(self.batch.item_for_event('$staged')['state'], 'outcome_unknown')


def load_tests(loader, standard, pattern):
    return unittest.TestSuite(ApprovalTests(name) for name in ApprovalTests.__dict__ if name.startswith('test_approval_'))

if __name__ == '__main__': unittest.main()
