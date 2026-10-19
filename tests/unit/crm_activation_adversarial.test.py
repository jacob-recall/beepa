"""Independent activation probes: delayed accepted sends cannot revive authority."""
import copy
import importlib.util
import json
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location('activation_fixture', Path(__file__).with_name('crm_activation.test.py'))
fixture = importlib.util.module_from_spec(spec); spec.loader.exec_module(fixture)


class ActivationAdversarialTests(fixture.ActivationTests):
    def assert_no_direct(self):
        self.assertNotEqual(self.local.overrides.get(('!created:local', 'com.jkali.share_override'), {}).get('state'), 'direct')
        self.assertEqual(self.native, [])

    def test_changed_approval_body_does_not_activate(self):
        self.portal_appears()
        self.events['$new-approval']['content']['body'] = 'replacement approval'
        self.reconcile()
        self.assert_no_direct()

    def test_decline_substitution_does_not_activate(self):
        self.portal_appears()
        self.events['$new-approval']['content']['decision'] = 'decline'
        self.reconcile()
        self.assert_no_direct()

    def test_revoked_contact_does_not_activate(self):
        self.portal_appears(); self.resolve_status = 'unshared'
        self.reconcile()
        self.assert_no_direct()

    def test_multiple_matching_portals_does_not_activate(self):
        self.portal_appears()
        self.proofs.append({**self.proofs[0], 'room_id': '!other:local'})
        self.reconcile()
        self.assert_no_direct()

    def test_rebinding_before_native_reconciliation_does_not_activate(self):
        self.portal_appears()
        self.uplink.cfg.master_authority_id = 'replacement-authority'
        self.control.refresh(True)
        self.reconcile()
        self.assert_no_direct()

    def test_private_arriving_after_activation_prepare_prevents_grant(self):
        self.portal_appears()
        self.batch.reconcile_new_chats()
        manual = self.manual('late-private')
        manual['content']['room_id'] = '!created:local'
        self.control.ingest_manual(manual, self.room)
        self.control._drain(); self.control._drain()
        self.assert_no_direct()
        self.assertEqual(self.local.overrides[('!created:local','com.jkali.share_override')]['state'], 'private')

    def test_cancel_between_activation_prepare_and_journal_execution_prevents_grant(self):
        self.portal_appears(); self.batch.reconcile_new_chats()
        canceled = copy.deepcopy(self.request); canceled['content']['phase'] = 'cancel'
        self.control.ingest_direct(canceled, '!proposals:master')
        self.control._drain()
        self.assert_no_direct()

    def test_approval_event_redaction_after_native_acceptance_does_not_activate(self):
        self.portal_appears()
        self.events['$new-approval']['content'] = {}
        self.reconcile()
        self.assert_no_direct()

    def test_lost_direct_response_cancellation_preserves_unknown_barrier(self):
        self.portal_appears()
        original = self.control.journal.write_override
        def lost(room, value):
            original(room, value)
            raise OSError('lost response after Direct write')
        self.control.journal.write_override = lost
        self.reconcile()
        canceled = copy.deepcopy(self.request); canceled['content']['phase'] = 'cancel'
        result = self.control.ingest_direct(canceled, '!proposals:master')
        self.assertFalse(result.get('safe_to_change_membership', False))
        self.control.journal.write_override = original
        self.control._drain()
        row = self.batch.item_for_event('$new-staged')
        self.assertIsNone(row['sent_event_id'])
        self.assertEqual(self.native, [])


def load_tests(loader, standard, pattern):
    return unittest.TestSuite(ActivationAdversarialTests(name) for name in ActivationAdversarialTests.__dict__ if name.startswith('test_'))

if __name__ == '__main__': unittest.main()
