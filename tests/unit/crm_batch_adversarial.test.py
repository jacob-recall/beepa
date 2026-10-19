"""Independent CRM queue adversarial cases using real D2 and disposable SQLite."""
import copy
import importlib.util
import json
from pathlib import Path
import sqlite3
import unittest

spec = importlib.util.spec_from_file_location('batch_adversarial_fixture', Path(__file__).with_name('crm_batch.test.py'))
fixture = importlib.util.module_from_spec(spec); spec.loader.exec_module(fixture)
import crm_batch


class BatchAdversarial(fixture.BatchTests):
    def accepted(self):
        event = self.envelope(); self.batch.ingest(event, '!proposals:master')
        return event

    def refreshed_review(self, version='8', network_id='person@example.com'):
        event = self.envelope()
        item = event['content']['items'][0]
        item['contact_version'] = version
        item['route']['network_id'] = network_id
        event['content']['child_hash'] = crm_batch.digest(crm_batch.child_payload(event['content']))
        self.events['$staged']['content'][crm_batch.ITEM_KEY] = self.batch.annotation(event['content'], item)
        self.events[event['event_id']] = copy.deepcopy(event)
        return event

    def test_current_contact_version_uses_original_grant_without_policy_rewrite(self):
        event = self.refreshed_review()
        original = self.control._command('member-add-1')['request_json']
        policy_count = self.uplink.db.execute('SELECT count(*) FROM crm_policy_operations').fetchone()[0]
        resolver = self.control.resolve_contact
        versions = []
        def current(binding, owner_mxid, generation):
            versions.append(binding['contact_version'])
            return resolver(binding, owner_mxid, generation) if binding['contact_version'] == '8' else {'status':'stale'}
        self.control.resolve_contact = current
        self.batch.ingest(event, '!proposals:master'); self.batch.drain()
        self.assertEqual(len(self.native), 1)
        self.assertEqual(set(versions), {'8'})
        self.assertEqual(self.control._command('member-add-1')['request_json'], original)
        self.assertEqual(self.uplink.db.execute('SELECT count(*) FROM crm_policy_operations').fetchone()[0],policy_count)

    def test_changed_identity_cannot_reuse_original_grant(self):
        event = self.refreshed_review(network_id='different@example.com')
        self.batch.ingest(event, '!proposals:master'); self.batch.drain()
        self.assertEqual(self.native, [])
        self.assertEqual(self.batch.item_for_event('$staged')['reason'], 'membership-review-mismatch')

    def test_stale_reviewed_contact_version_refuses(self):
        event = self.refreshed_review()
        self.control.resolve_contact = lambda *args: {'status':'stale'}
        self.batch.ingest(event, '!proposals:master'); self.batch.drain()
        self.assertEqual(self.native, [])

    def test_contact_unshared_after_remote_send_fence_refuses(self):
        event = self.refreshed_review()
        self.batch.ingest(event, '!proposals:master')
        original = self.uplink.local
        def changed(method,path,*args,**kwargs):
            result = original(method,path,*args,**kwargs)
            if method == 'GET' and crm_batch.SEND_CHECKPOINT in path:
                self.resolve_status = 'unshared'
            return result
        self.uplink.local = changed; self.batch.drain()
        self.assertEqual(self.native, [])
        self.assertEqual(self.batch.item_for_event('$staged')['state'], 'review_required')
        self.resolve_status = 'ready'; self.batch.drain()
        self.assertEqual(self.native, [])

    def test_revision_changes_between_review_check_and_dispatch_refuse(self):
        self.accepted()
        original = self.batch.proposal
        def changed(*args):
            result = original(*args)
            # Another writer changed policy after eligible() checked revision1.
            # Still Direct is insufficient: this review named the old revision.
            self.local.overrides[('!person:local', 'com.jkali.share_override')] = {'state': 'direct', 'changed': True}
            return result
        self.batch.proposal = changed
        self.batch.drain()
        self.assertEqual(self.native, [])
        self.assertEqual(self.batch.item_for_event('$staged')['state'], 'review_required')

    def test_post_checkpoint_revision_change_prevents_native_put(self):
        self.accepted(); original = self.uplink.local
        def changed(method, path, *args, **kwargs):
            result = original(method, path, *args, **kwargs)
            if method == 'GET' and crm_batch.SEND_CHECKPOINT in path:
                self.local.overrides[('!person:local', 'com.jkali.share_override')] = {'state': 'direct', 'changed': True}
            return result
        self.uplink.local = changed; self.batch.drain(); self.assertEqual(self.native, [])

    def test_post_checkpoint_disconnect_prevents_native_put(self):
        self.accepted(); original = self.uplink.local
        def changed(method, path, *args, **kwargs):
            result = original(method, path, *args, **kwargs)
            if method == 'GET' and crm_batch.SEND_CHECKPOINT in path:
                self.uplink.active_link_for_dispatch = lambda: False
            return result
        self.uplink.local = changed; self.batch.drain(); self.assertEqual(self.native, [])

    def test_cancel_while_final_quiet_read_is_running_prevents_send(self):
        event = self.accepted()
        def quiet(*args):
            self.batch.cancel(event['content']['child_id']); return True
        self.uplink.room_quiet_since = quiet
        self.batch.drain()
        self.assertEqual(self.native, [])
        self.assertNotEqual(self.batch.item_for_event('$staged')['state'], 'accepted')

    def test_post_dispatch_lost_reply_duplicate_token_does_not_send_twice(self):
        self.accepted(); original = self.uplink.local
        def lost(method, path, *args, **kwargs):
            result = original(method, path, *args, **kwargs)
            if '/send/m.room.message/' in path: raise OSError('accepted but lost reply')
            return result
        self.uplink.local = lost; self.batch.drain()
        self.assertEqual(len(self.native), 1)
        self.assertEqual(self.batch.item_for_event('$staged')['state'], 'outcome_unknown')
        self.uplink.local = original
        self.batch.ingest(self.envelope(proposal='$second-token'), '!proposals:master')
        self.batch.drain(); self.assertEqual(len(self.native), 1)

    def test_fence_write_lost_reply_prevents_native_and_later_replay(self):
        self.accepted(); original = self.uplink.local
        def lost(method, path, *args, **kwargs):
            result = original(method, path, *args, **kwargs)
            if method == 'PUT' and crm_batch.SEND_CHECKPOINT in path: raise OSError('fence reply lost')
            return result
        self.uplink.local = lost; self.batch.drain()
        self.assertFalse(self.control.local_ready); self.assertEqual(self.native, [])
        self.uplink.local = original
        self.batch.verify_checkpoints(self.local.rooms[self.room]['state'])
        self.control.local_ready = True; self.batch.drain()
        self.assertEqual(self.native, [])
        self.assertEqual(self.batch.item_for_event('$staged')['state'], 'outcome_unknown')

    def test_snapshot_restore_before_attempt_detected_by_remote_fence(self):
        self.accepted()
        backup = sqlite3.connect(':memory:'); self.uplink.db.backup(backup)
        self.addCleanup(backup.close)
        self.batch.drain(); self.assertEqual(len(self.native), 1)
        # Same journal epoch and accepted item survive an old snapshot: remote
        # immutable attempt still proves that local history is incomplete.
        backup.backup(self.uplink.db)
        with self.assertRaisesRegex(ValueError, 'recovery-required'):
            self.batch.verify_checkpoints(self.local.rooms[self.room]['state'])
        self.assertEqual(len(self.native), 1)

    def test_fence_missing_remote_is_not_treated_as_unattempted(self):
        self.accepted(); self.batch.drain()
        state = {key: value for key, value in self.local.rooms[self.room]['state'].items() if key[0] != crm_batch.SEND_CHECKPOINT}
        with self.assertRaisesRegex(ValueError, 'reconciliation-required'):
            self.batch.verify_checkpoints(state)

    def test_refreshed_staging_body_or_route_cannot_substitute_review(self):
        self.accepted(); self.events['$staged']['content']['body'] = 'substituted after acceptance'
        self.batch.drain(); self.assertEqual(self.native, [])
        self.assertEqual(self.batch.item_for_event('$staged')['reason'], 'proposal-review-mismatch')

    def test_malformed_success_cannot_become_exact_sent_evidence(self):
        self.accepted(); original = self.uplink.local
        def malformed(method, path, *args, **kwargs):
            result = original(method, path, *args, **kwargs)
            return {'event_id': 'not-an-event'} if '/send/m.room.message/' in path else result
        self.uplink.local = malformed; self.batch.drain()
        self.assertEqual(len(self.native), 1)
        row = self.batch.item_for_event('$staged')
        self.assertEqual(row['state'], 'outcome_unknown'); self.assertIsNone(row['sent_event_id'])

    def test_artifact_failure_after_send_recovers_record_without_resending(self):
        self.accepted(); original = self.uplink.local
        def lost(method, path, *args, **kwargs):
            if '/send/com.jkali.proposal/' in path: raise OSError('inbox unavailable')
            return original(method, path, *args, **kwargs)
        self.uplink.local = lost; self.batch.drain()
        self.assertEqual(len(self.native), 1)
        self.assertIsNone(self.batch.item_for_event('$staged')['local_proposal_event_id'])
        self.uplink.local = original; self.batch.drain()
        self.assertEqual(len(self.native), 1)
        self.assertTrue(self.batch.item_for_event('$staged')['local_proposal_event_id'])

    def test_rebinding_prevents_old_accepted_child_send(self):
        self.accepted(); self.control.generation = 'new-generation'
        self.batch.drain(); self.assertEqual(self.native, [])
        self.assertEqual(self.batch.item_for_event('$staged')['reason'], 'stale-batch-authority')

    def native_status(self, status, sender=None):
        self.local.state('!mgmt:local', 'com.jkali.imessage_start_chat',
            {'schema': 1, 'command_event_id': '$command', 'owner_mxid': self.uplink.cfg.local_user,
             'status': status, 'native_ids': ['untrusted-id'], 'message_event_ids': ['$untrusted'], 'read': True},
            '$command', sender or '@imessagebot:local')

    def pending_new_chat(self):
        self.accepted(); row = self.batch.item_for_event('$staged')
        self.local.make_room('!mgmt:local', {})
        self.batch.set_item(row, 'outcome_unknown', native_command_event_id='$command',
            native_management_room='!mgmt:local', native_bot_mxid='@imessagebot:local')

    def test_retryable_native_status_can_later_be_accepted(self):
        self.pending_new_chat(); self.native_status('retryable'); self.batch.reconcile_new_chats()
        self.assertEqual(self.batch.item_for_event('$staged')['state'], 'outcome_unknown')
        self.native_status('accepted'); self.batch.reconcile_new_chats()
        row = self.batch.item_for_event('$staged')
        self.assertEqual(row['native_status'], 'accepted'); self.assertEqual(row['state'], 'outcome_unknown')
        self.assertIsNone(row['sent_event_id']); self.assertIsNone(row['sent_room_id'])

    def test_foreign_native_status_cannot_even_claim_acceptance(self):
        self.pending_new_chat(); self.native_status('accepted', '@foreign:local'); self.batch.reconcile_new_chats()
        self.assertIsNone(self.batch.item_for_event('$staged')['native_status'])

    def test_accepted_native_status_never_imports_fabricated_message_ids(self):
        self.pending_new_chat(); self.native_status('accepted'); self.batch.reconcile_new_chats()
        row = self.batch.item_for_event('$staged')
        self.assertEqual(row['state'], 'outcome_unknown'); self.assertIsNone(row['sent_event_id'])
        self.assertIsNone(row['sent_room_id']); self.assertEqual(self.native, [])


for name in tuple(vars(fixture.BatchTests)):
    if name.startswith('test_'): setattr(BatchAdversarial, name, None)
if __name__ == '__main__': unittest.main()
