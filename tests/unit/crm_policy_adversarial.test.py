"""Independent crash/ordering probes for the real CRM consent journal."""
import copy
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'agents' / 'uplink'))
from crm_policy import ConsentJournal

ROOM = '!person:local'
MANAGER = '@manager:master'
OWNER = '@person:master'
LOCAL = '@person:local'


class PolicyAdversarialTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.db = sqlite3.connect(str(Path(self.directory.name) / 'state.db'))
        self.addCleanup(self.db.close)
        self.remote = {}
        self.puts = []
        self.late_put = None
        self.timeout = False
        self.journal = ConsentJournal(self.db, owner_mxid=OWNER, generation='generation-1',
            manager_mxid=MANAGER, local_user_mxid=LOCAL, read_override=self.read,
            write_override=self.write, lock_path=str(Path(self.directory.name) / 'policy.lock'),
            authorize=lambda request: True)
        self.journal.inspect(ROOM)

    def read(self, room):
        self.assertEqual(room, ROOM)
        return copy.deepcopy(self.remote)

    def write(self, room, content):
        self.assertEqual(room, ROOM)
        self.assertFalse(self.db.in_transaction, 'intent must be committed before remote write')
        self.puts.append(copy.deepcopy(content))
        if self.timeout:
            self.late_put = copy.deepcopy(content)
            raise TimeoutError('client timeout; server request may still be running')
        self.remote = copy.deepcopy(content)

    def grant(self, operation_id='grant', revision=0):
        return self.journal.submit_crm(operation_id=operation_id, actor=MANAGER,
            room_id=ROOM, expected_revision=revision, binding_id='binding', membership_operation_id='membership')

    def test_late_prepared_confirmation_never_rolls_revision_back(self):
        self.journal.inspect(ROOM)
        self.grant()
        self.timeout = True
        self.assertEqual(self.journal.process_next()['state'], 'prepared')
        self.remote = {'state': 'private'}
        first = self.journal.inspect(ROOM)['revision']
        self.remote = {'state': 'share'}
        newer = self.journal.inspect(ROOM)['revision']
        self.assertGreater(newer, first)
        self.remote = copy.deepcopy(self.late_put)
        latest = self.journal.inspect(ROOM)['revision']
        self.assertGreaterEqual(latest, newer, 'late prepared marker cannot lower a revision already observed')

    def test_unresolved_server_put_blocks_later_conflicting_policy_write(self):
        self.journal.inspect(ROOM)
        self.grant()
        self.timeout = True
        self.assertEqual(self.journal.process_next()['state'], 'prepared')
        # A GET seeing the old policy does not prove the timed-out PUT has ended.
        self.journal.process_next()
        revision = self.journal.inspect(ROOM)['revision']
        self.journal.submit_manual(operation_id='make-private', actor=LOCAL,
            room_id=ROOM, expected_revision=revision, level='private')
        self.timeout = False
        self.journal.process_next()
        self.assertEqual(len(self.puts), 1,
            'later manual write must not be exposed to overwrite by the still-running older Direct PUT')

    def test_unresolved_write_barrier_survives_pairing_generation_change(self):
        self.grant()
        self.timeout = True
        self.journal.process_next()
        replacement = ConsentJournal(self.db, owner_mxid=OWNER, generation='generation-2',
            manager_mxid=MANAGER, local_user_mxid=LOCAL, read_override=self.read,
            write_override=self.write, lock_path=str(Path(self.directory.name) / 'policy.lock'),
            authorize=lambda request: True)
        replacement.process_next()
        replacement.submit_manual(operation_id='private-new-generation', actor=LOCAL,
            room_id=ROOM, expected_revision=replacement.inspect(ROOM)['revision'], level='private')
        self.timeout = False
        replacement.process_next()
        self.assertEqual(len(self.puts), 1)
        self.assertEqual(replacement.get('private-new-generation')['state'], 'queued')

    def test_late_marker_resolution_allows_fresh_manual_private_without_replaying_grant(self):
        self.grant()
        self.timeout = True
        self.journal.process_next()
        self.journal.process_next()
        self.remote = copy.deepcopy(self.late_put)
        self.assertEqual(self.journal.process_next()['state'], 'applied')
        self.timeout = False
        self.journal.submit_manual(operation_id='private-after-recovery', actor=LOCAL,
            room_id=ROOM, expected_revision=self.journal.inspect(ROOM)['revision'], level='private')
        self.assertEqual(self.journal.process_next()['state'], 'applied')
        self.assertEqual(self.remote['state'], 'private')
        self.assertEqual(len(self.puts), 2)

    def test_unrelated_room_can_progress_while_original_room_is_blocked(self):
        self.grant()
        self.timeout = True
        self.journal.process_next()
        other_room = '!other:local'
        self.journal.read_override = lambda room: {} if room == other_room else self.read(room)
        other_writes = []
        self.journal.write_override = lambda room, content: other_writes.append((room, content))
        self.journal.inspect(other_room)
        self.journal.submit_manual(operation_id='other-private', actor=LOCAL,
            room_id=other_room, expected_revision=0, level='private')
        self.journal.process_next(room_id=other_room)
        self.assertEqual(len(other_writes), 1)
        self.assertEqual(other_writes[0][0], other_room)
        self.assertEqual(self.journal.get('grant')['state'], 'prepared')

    def test_old_grant_replay_never_undoes_later_manual_private(self):
        self.journal.inspect(ROOM)
        self.grant()
        self.assertEqual(self.journal.process_next()['state'], 'applied')
        self.journal.submit_manual(operation_id='make-private', actor=LOCAL, room_id=ROOM,
            expected_revision=self.journal.inspect(ROOM)['revision'], level='private')
        self.assertEqual(self.journal.process_next()['state'], 'applied')
        count = len(self.puts)
        self.assertEqual(self.grant()['state'], 'applied')
        self.assertIsNone(self.journal.process_next())
        self.assertEqual(self.remote['state'], 'private')
        self.assertEqual(len(self.puts), count)

    def test_crash_after_remote_commit_recovers_without_second_put(self):
        self.journal.inspect(ROOM)
        self.grant()
        real_write = self.journal.write_override
        def crash(room, content):
            real_write(room, content)
            raise SystemExit('simulated crash after remote commit')
        self.journal.write_override = crash
        with self.assertRaises(SystemExit):
            self.journal.process_next()
        self.assertEqual(self.journal.get('grant')['state'], 'prepared')
        restarted = ConsentJournal(self.db, owner_mxid=OWNER, generation='generation-1',
            manager_mxid=MANAGER, local_user_mxid=LOCAL, read_override=self.read,
            write_override=self.write, lock_path=str(Path(self.directory.name) / 'policy.lock'),
            authorize=lambda request: True)
        self.assertEqual(restarted.process_next()['state'], 'applied')
        self.assertEqual(len(self.puts), 1)

    def test_read_failure_cannot_become_default_policy_authorization(self):
        self.grant()
        def unavailable(room):
            raise OSError('unavailable')
        self.journal.read_override = unavailable
        self.assertEqual(self.journal.process_next()['state'], 'queued')
        self.assertEqual(self.puts, [])

    def test_rebinding_cancels_queued_old_manager_work(self):
        self.grant()
        replacement = ConsentJournal(self.db, owner_mxid=OWNER, generation='generation-2',
            manager_mxid=MANAGER, local_user_mxid=LOCAL, read_override=self.read,
            write_override=self.write, lock_path=str(Path(self.directory.name) / 'policy.lock'),
            authorize=lambda request: True)
        self.assertEqual(replacement.process_next()['state'], 'canceled')
        self.assertEqual(self.puts, [])

    def test_changed_intent_under_old_id_is_rejected(self):
        self.grant()
        with self.assertRaisesRegex(ValueError, 'operation-id-collision'):
            self.grant(revision=1)
        self.assertEqual(self.puts, [])

    def test_authorization_is_rechecked_before_put(self):
        self.grant()
        self.journal.authorize = lambda request: False
        outcome = self.journal.process_next()
        self.assertEqual(outcome['state'], 'rejected')
        self.assertEqual(self.puts, [])

    def test_same_level_without_exact_marker_does_not_confirm_unknown_write(self):
        self.journal.inspect(ROOM)
        self.grant()
        self.timeout = True
        self.journal.process_next()
        self.remote = {'state': 'direct'}
        result = self.journal.process_next()
        self.assertNotEqual(result['state'], 'applied')
        self.assertEqual(len(self.puts), 1)


if __name__ == '__main__':
    unittest.main()
