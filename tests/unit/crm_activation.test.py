"""Complete listed-contact Direct setup after a user-approved new chat appears."""
import copy
import importlib.util
import json
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location('batch_fixture', Path(__file__).with_name('crm_batch.test.py'))
fixture = importlib.util.module_from_spec(spec); spec.loader.exec_module(fixture)
from crm_batch import digest, child_payload, ITEM_KEY
from crm_approval import APPROVAL_REQUEST


class ActivationTests(fixture.BatchTests):
    def setUp(self):
        super().setUp()
        self.resolve_status = 'no_existing_conversation'
        self.request = self.direct('new-member')
        self.request['content']['membership_operation_id'] = 'new-member'
        result = self.control.ingest_direct(self.request, '!proposals:master')
        self.assertEqual(result['state'], 'approval_required')
        event = self.envelope('new-chat', '$new-staged')
        item = event['content']['items'][0]
        item['membership_operation_id'] = 'new-member'
        item['route'] = {'mode':'request_new_conversation_approval','source':'imessage','network_id':'person@example.com','consent_revision':0}
        event['content']['child_hash'] = digest(child_payload(event['content']))
        self.events['$new-staged']['content'] = {'body':item['body'],'target_source':'imessage','target_identifier':'person@example.com',ITEM_KEY:self.batch.annotation(event['content'],item)}
        self.events[event['event_id']] = copy.deepcopy(event)
        self.batch.ingest(event, '!proposals:master'); self.batch.drain()
        self.row = self.batch.item_for_event('$new-staged')
        self.assertEqual(self.row['state'], 'waiting_for_approval')
        approval = {'event_id':'$new-approval','sender':self.uplink.cfg.local_user,'type':APPROVAL_REQUEST,'room_id':self.room,'origin_server_ts':self.now,
                    'content':{'schema':1,'child_id':self.row['child_id'],'item_id':self.row['item_id'],'local_proposal_event_id':self.row['local_proposal_event_id'],
                               'journal_epoch':self.control.epoch,'decision':'approve','body':'Start this approved chat'}}
        self.events[approval['event_id']] = copy.deepcopy(approval)
        self.control.approval.ingest(approval, self.room)
        self.local.make_room('!native-management:local', {})
        self.local.make_room('!created:local', {})
        self.batch.set_item(self.row, 'outcome_unknown', 'waiting-native', native_command_event_id='$native-command', native_management_room='!native-management:local',native_bot_mxid='@imessagebot:local')
        self.native_status('accepted')

    def native_status(self, state):
        self.local.state('!native-management:local', 'com.jkali.imessage_start_chat',
            {'schema':1,'command_event_id':'$native-command','owner_mxid':self.uplink.cfg.local_user,'status':state,'correlation':'unavailable'}, '$native-command', '@imessagebot:local')

    def portal_appears(self):
        self.resolve_status = 'ready'
        self.proofs = [{'room_id':'!created:local','source':'imessage','native_id':'person@example.com','bridge_epoch':'current-native'}]

    def reconcile(self):
        self.batch.reconcile_new_chats(); self.control._drain()
        return json.loads(self.control._command('new-member')['result_json'])

    def test_native_acceptance_waits_for_exact_portal_then_finishes_original_grant(self):
        self.assertEqual(self.reconcile()['state'], 'approval_required')
        self.portal_appears()
        result = self.reconcile()
        self.assertEqual(result['state'], 'applied')
        self.assertEqual(result['rooms'][0]['room_id'], '!created:local')
        self.assertEqual(self.local.overrides[('!created:local','com.jkali.share_override')]['state'], 'direct')
        row = self.batch.item_for_event('$new-staged')
        self.assertEqual(row['state'], 'outcome_unknown')
        self.assertIsNone(row['sent_event_id'], 'granting the conversation never fabricates its first outgoing ID')
        count = self.uplink.db.execute('SELECT count(*) FROM crm_policy_operations').fetchone()[0]
        self.reconcile()
        self.assertEqual(self.uplink.db.execute('SELECT count(*) FROM crm_policy_operations').fetchone()[0], count)
        self.assertEqual(self.native, [])

    def test_newer_manual_private_is_never_overridden(self):
        self.portal_appears()
        manual = self.manual('new-room-private')
        manual['content']['room_id'] = '!created:local'
        self.control.ingest_manual(manual, self.room); self.control._drain()
        result = self.reconcile()
        self.assertEqual(result['reason'], 'activation-newer-manual-policy')
        self.assertEqual(self.local.overrides[('!created:local','com.jkali.share_override')]['state'], 'private')

    def test_canceled_membership_and_batch_do_not_activate(self):
        canceled = copy.deepcopy(self.request); canceled['content']['phase'] = 'cancel'
        self.control.ingest_direct(canceled, '!proposals:master')
        self.portal_appears(); self.reconcile()
        self.assertNotIn(('!created:local','com.jkali.share_override'), self.local.overrides)

    def test_disable_batch_only_stops_new_sends_not_accepted_command_reconciliation(self):
        self.control.batch_enabled = False
        self.portal_appears()
        self.assertEqual(self.reconcile()['state'], 'applied')
        self.assertEqual(self.native, [])

    def test_retryable_or_ambiguous_native_command_does_not_activate(self):
        self.portal_appears()
        for status in ('retryable','ambiguous'):
            self.native_status(status)
            self.assertEqual(self.reconcile()['state'], 'approval_required')
        self.native_status('accepted')
        self.assertEqual(self.reconcile()['state'], 'applied')

    def test_unjournaled_explicit_private_is_not_assumed_older_than_membership(self):
        self.portal_appears()
        self.local.overrides[('!created:local','com.jkali.share_override')] = {'state':'private'}
        self.assertEqual(self.reconcile()['reason'], 'activation-existing-explicit-policy')
        self.assertEqual(self.local.overrides[('!created:local','com.jkali.share_override')]['state'], 'private')


def load_tests(loader, standard, pattern):
    return unittest.TestSuite(ActivationTests(name) for name in ActivationTests.__dict__ if name.startswith('test_'))

if __name__ == '__main__': unittest.main()
