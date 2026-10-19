"""Independent approval boundary tests using real journal and send dispatcher."""
import copy
import importlib.util
import json
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location('approval_fixture', Path(__file__).with_name('crm_approval.test.py'))
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)


class AdversarialApproval(fixture.ApprovalTests):
    def after_checkpoint(self, mutate):
        self.approval.ingest(self.decision(), self.room)
        original = self.batch.before_approved_send
        def checkpoint(*args):
            original(*args)
            mutate()
        self.batch.before_approved_send = checkpoint
        self.approval.drain()
        self.assertEqual(self.native, [], 'authority must be fresh after the durable attempt checkpoint')
        self.assertEqual(self.batch.item_for_event('$staged')['state'], 'outcome_unknown')
        self.batch.before_approved_send = original
        fixture.CRMApproval(self.batch).drain()
        self.batch.drain()
        self.assertEqual(self.native, [], 'reopening an attempted item must never retry its native send')

    def test_adversarial_disconnect_after_checkpoint_prevents_send_and_restart_retry(self):
        self.after_checkpoint(lambda: setattr(self.uplink, 'active_link_for_dispatch', lambda:False))

    def test_adversarial_expiry_after_checkpoint_prevents_send_and_restart_retry(self):
        self.after_checkpoint(lambda: setattr(self.control, 'clock', lambda:self.now + 600001))

    def test_adversarial_epoch_change_after_checkpoint_prevents_send_and_restart_retry(self):
        self.after_checkpoint(lambda: setattr(self.control, 'epoch', 'replaced-epoch'))

    def test_adversarial_shared_contact_version_change_after_checkpoint_prevents_send_and_restart_retry(self):
        self.after_checkpoint(lambda: setattr(self.control, 'resolve_contact',
                              lambda *args, **kwargs: {'status': 'stale', 'rooms': []}))

    def test_adversarial_contact_route_change_after_checkpoint_prevents_send_and_restart_retry(self):
        self.after_checkpoint(lambda: setattr(self.control, '_resolve',
                              lambda binding: [{'room_id': '!replacement:local'}]))

    def test_adversarial_same_contact_other_room_cannot_replace_reviewed_target(self):
        event = self.decision()
        self.approval.ingest(event, self.room)
        local_proposal = self.events[self.row['local_proposal_event_id']]
        local_proposal['content']['target_room'] = '!other-person-room:local'
        self.local.make_room('!other-person-room:local', {})
        self.uplink.room_is_attributed = lambda room: True
        self.control._resolve = lambda binding: [{'room_id': '!person:local'}, {'room_id': '!other-person-room:local'}]
        self.uplink.room_quiet_since = lambda *a, **kw: True
        self.approval.drain()
        self.assertEqual(self.native, [], 'approval authorized the reviewed route, not any room of the same contact')
        self.assertEqual(self.batch.item_for_event('$staged')['state'], 'refused')

    def test_adversarial_disconnect_during_intent_read_prevents_dispatch(self):
        event = self.decision()
        self.approval.ingest(event, self.room)
        live = [True]
        self.uplink.active_link_for_dispatch = lambda: live[0]
        original = self.uplink.local
        def disconnect(method, path, body=None, **kwargs):
            result = original(method, path, body, **kwargs)
            if path.endswith('/event/%24approval'):
                live[0] = False
            return result
        self.uplink.local = disconnect
        self.approval.drain()
        self.assertEqual(self.native, [], 'late account revocation must suppress dispatch')

    def test_adversarial_clock_expiry_during_contact_resolution_prevents_dispatch(self):
        event = self.decision()
        self.approval.ingest(event, self.room)
        original = self.control._resolve
        def expire(binding):
            result = original(binding)
            self.control.clock = lambda: self.now + 600001
            return result
        self.control._resolve = expire
        self.approval.drain()
        self.assertEqual(self.native, [])

    def test_adversarial_failed_intent_point_read_retries_read_only_until_recovered(self):
        event = self.decision()
        self.approval.ingest(event, self.room)
        original = self.uplink.local
        def outage(method, path, body=None, **kwargs):
            if path.endswith('/event/%24approval'):
                raise OSError('offline')
            return original(method, path, body, **kwargs)
        self.uplink.local = outage
        self.approval.drain()
        self.assertEqual(self.native, [])
        self.assertEqual(self.batch.item_for_event('$staged')['state'], 'approval_pending')
        self.uplink.local = original
        self.approval.drain()
        self.approval.drain()
        self.assertEqual(len(self.native), 1)

    def test_adversarial_future_or_expired_intent_never_claims(self):
        for stamp in [self.now - 600001, self.now + 60001, True, 'timestamp']:
            with self.subTest(stamp=stamp):
                event = self.decision()
                event['origin_server_ts'] = stamp
                with self.assertRaisesRegex(ValueError, 'expired'):
                    self.approval.ingest(event, self.room)
        self.assertIsNone(self.batch.item_for_event('$staged')['approval_json'])
        self.assertEqual(self.native, [])

    def test_adversarial_annotation_or_forwarded_body_change_prevents_send(self):
        event = self.decision()
        self.approval.ingest(event, self.room)
        self.events[self.row['local_proposal_event_id']]['content']['body'] = 'Rewritten source proposal'
        self.approval.drain()
        self.assertEqual(self.native, [])
        self.assertEqual(self.batch.item_for_event('$staged')['state'], 'refused')

    def test_adversarial_changed_authority_epoch_after_claim_prevents_send(self):
        self.approval.ingest(self.decision(), self.room)
        self.control.epoch = 'replacement-epoch'
        self.approval.drain()
        self.assertEqual(self.native, [])
        self.assertEqual(self.batch.item_for_event('$staged')['state'], 'refused')

    def test_adversarial_historical_approval_replay_never_changes_first_decision(self):
        first = self.decision('decline')
        self.approval.ingest(first, self.room)
        self.approval.ingest(copy.deepcopy(first), self.room)
        claimed = json.loads(self.batch.item_for_event('$staged')['approval_json'])
        self.assertEqual(claimed['event_id'], '$approval')
        with self.assertRaisesRegex(ValueError, 'decision-conflict'):
            self.approval.ingest(self.decision(event_id='$new-token', body='New text'), self.room)
        self.approval.drain()
        self.assertEqual(self.native, [])

    def test_adversarial_both_claim_and_result_use_exact_local_proposal(self):
        for mutation in [lambda e: e.update(event_id='$wrong-event'),
                         lambda e: e.update(sender='@other:local'),
                         lambda e: e.update(state_key=''),
                         lambda e: e.update(room_id='!other:local')]:
            with self.subTest(mutation=mutation):
                original = copy.deepcopy(self.events[self.row['local_proposal_event_id']])
                mutation(self.events[self.row['local_proposal_event_id']])
                with self.assertRaisesRegex(ValueError, 'proposal-authentication'):
                    self.approval.ingest(self.decision(), self.room)
                self.events[self.row['local_proposal_event_id']] = original
        self.assertIsNone(self.batch.item_for_event('$staged')['approval_json'])


def load_tests(loader, standard, pattern):
    return unittest.TestSuite(AdversarialApproval(name) for name in AdversarialApproval.__dict__ if name.startswith('test_adversarial_'))


if __name__ == '__main__':
    unittest.main()
