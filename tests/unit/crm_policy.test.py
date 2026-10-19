#!/usr/bin/env python3
"""Ordered consent writer: isolated SQLite, injected transport, no real accounts."""
import copy
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "agents" / "uplink"))
from crm_policy import ConsentJournal, POLICY_MARKER, PolicyRefused, PolicyRetryable


class SimulatedCrash(BaseException):
    pass


class JournalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "state.db")
        self.lock = self.path + ".policy.lock"
        self.db = sqlite3.connect(self.path)
        self.remote = {"!a:local": {"state": "private"}, "!b:local": {}}
        self.writes = []
        self.read_hook = None
        self.write_hook = None
        self.journal = self.make(self.db)
        self.assertEqual(self.journal.inspect("!a:local")["revision"], 0)

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def read(self, room):
        if self.read_hook:
            self.read_hook(room)
        return copy.deepcopy(self.remote[room])

    def write(self, room, content):
        # Durable attempted intent must be visible to a separate DB connection
        # before the transport can possibly mutate real account-data.
        with sqlite3.connect(self.path) as verification:
            recorded = verification.execute("SELECT state,intended_json FROM crm_policy_operations WHERE operation_id=?", (content[POLICY_MARKER]["operation_id"],)).fetchone()
        self.assertEqual(recorded[0], "prepared")
        self.assertEqual(json.loads(recorded[1]), content)
        self.writes.append((room, copy.deepcopy(content)))
        if self.write_hook:
            return self.write_hook(room, content)
        self.remote[room] = copy.deepcopy(content)

    def make(self, db, **overrides):
        args = dict(owner_mxid="@elliot:master", generation="enrollment-1", manager_mxid="@manager:master", local_user_mxid="@elliot:local", read_override=self.read, write_override=self.write, lock_path=self.lock)
        args.update(overrides)
        return ConsentJournal(db, **args)

    def crm(self, op="grant", revision=0, **overrides):
        args = dict(operation_id=op, actor="@manager:master", room_id="!a:local", expected_revision=revision, binding_id="contact-1", membership_operation_id="membership-1")
        args.update(overrides)
        return self.journal.submit_crm(**args)

    def manual(self, op="manual", revision=1, level="private"):
        return self.journal.submit_manual(operation_id=op, actor="@elliot:local", room_id="!a:local", expected_revision=revision, level=level)

    def test_manual_private_wins_and_old_grant_replay_is_inert(self):
        self.assertEqual(self.crm()["state"], "queued")
        self.assertEqual(self.journal.process_next()["state"], "applied")
        self.assertEqual(self.remote["!a:local"]["state"], "direct")
        self.manual()
        self.assertEqual(self.journal.process_next()["state"], "applied")
        self.assertEqual(self.remote["!a:local"]["state"], "private")
        self.assertEqual(self.crm()["state"], "applied")
        self.assertIsNone(self.journal.process_next())
        self.assertEqual(len(self.writes), 2)
        self.assertEqual(self.journal.inspect("!a:local")["revision"], 2)
        with self.assertRaisesRegex(ValueError, "operation-id-collision"):
            self.crm(revision=2)

    def test_stale_queued_revision_and_actor_are_refused(self):
        self.crm()
        self.crm("stale")
        self.journal.process_next()
        self.assertEqual(self.journal.process_next()["reason"], "revision-conflict")
        self.assertEqual(len(self.writes), 1)
        with self.assertRaisesRegex(ValueError, "actor-mismatch"):
            self.crm("evil", actor="@evil:master")
        with self.assertRaisesRegex(ValueError, "invalid-intent"):
            self.crm("bad", expected_revision=True)
        with self.assertRaisesRegex(ValueError, "invalid-intent"):
            self.crm("bad", room_id="!a:local\n")
        with self.assertRaisesRegex(TypeError, "level"):
            self.crm("bad", level="private")
        with self.assertRaisesRegex(ValueError, "policy-not-observed"):
            self.crm("unobserved", room_id="!b:local")

    def test_inherit_keeps_provenance_and_existing_default_semantics(self):
        self.manual(revision=0, level="inherit")
        self.assertEqual(self.journal.process_next()["state"], "applied")
        self.assertNotIn("state", self.remote["!a:local"])
        self.assertIn(POLICY_MARKER, self.remote["!a:local"])
        view = self.journal.inspect("!a:local", {"default_level": "direct"})
        self.assertIsNone(view["explicit_level"])
        self.assertEqual(view["effective_level"], "direct")
        self.assertEqual(view["revision"], 1)

    def test_crash_after_applied_put_reconciles_exact_marker_without_repeat(self):
        def crash(room, content):
            self.remote[room] = copy.deepcopy(content)
            raise SimulatedCrash()
        self.write_hook = crash
        self.crm()
        with self.assertRaises(SimulatedCrash):
            self.journal.process_next()
        self.assertFalse(self.db.in_transaction)
        self.assertEqual(self.journal.get("grant")["state"], "prepared")
        self.write_hook = None
        self.journal = self.make(self.db)
        self.assertEqual(self.journal.process_next()["state"], "applied")
        self.assertEqual(len(self.writes), 1)
        self.manual()
        self.journal.process_next()
        self.assertEqual(self.crm()["state"], "applied")
        self.assertEqual(self.remote["!a:local"]["state"], "private")

    def test_crash_before_put_does_not_guess_or_auto_reapply(self):
        self.crm()
        def crash(room, content):
            raise SimulatedCrash()
        self.write_hook = crash
        with self.assertRaises(SimulatedCrash):
            self.journal.process_next()
        self.write_hook = None
        result = self.journal.process_next()
        self.assertEqual(result["state"], "prepared")
        self.assertEqual(result["reason"], "write-unresolved-ordering-blocked")
        self.assertEqual(self.journal.inspect("!a:local")["revision"], 0)
        self.manual()
        self.assertEqual(self.journal.pending("!a:local")[-1]["level"], "private")
        self.assertEqual(self.journal.process_next()["state"], "prepared")
        self.assertEqual(len(self.writes), 1, 'later manual must wait for potentially in-flight Direct')
        self.remote["!a:local"] = copy.deepcopy(self.writes[0][1])
        self.assertEqual(self.journal.process_next()["state"], "applied")
        self.assertEqual(self.journal.process_next()["state"], "applied")
        self.assertEqual(self.remote["!a:local"]["state"], "private")
        self.assertEqual(self.crm()["state"], "applied")
        self.assertEqual(len(self.writes), 2)

    def test_timeout_applied_and_readback_failure_recover_without_retry(self):
        def lost(room, content):
            self.remote[room] = copy.deepcopy(content)
            raise TimeoutError()
        self.write_hook = lost
        self.crm()
        self.assertEqual(self.journal.process_next()["state"], "prepared")
        def offline(room):
            raise OSError()
        self.read_hook = offline
        self.assertEqual(self.journal.process_next()["reason"], "reconcile-read-failed")
        self.assertFalse(self.db.in_transaction)
        self.read_hook = None
        self.assertEqual(self.journal.process_next()["state"], "applied")
        self.assertEqual(len(self.writes), 1)

    def test_external_writer_after_timeout_cannot_be_overwritten_by_replay(self):
        def lost(room, content):
            self.remote[room] = copy.deepcopy(content)
            raise TimeoutError()
        self.write_hook = lost
        self.crm()
        self.journal.process_next()
        self.remote["!a:local"] = {"state": "private", "external_revision": 42}
        self.assertEqual(self.journal.process_next()["state"], "prepared")
        self.assertEqual(self.remote["!a:local"]["state"], "private")
        self.assertEqual(len(self.writes), 1)

    def test_same_state_without_exact_marker_is_not_confirmation(self):
        self.write_hook = lambda room, content: self.remote.update({room: {"state": "direct"}})
        self.crm()
        self.assertEqual(self.journal.process_next()["state"], "outcome_unknown")
        self.assertEqual(len(self.writes), 1)

    def test_external_change_including_same_level_invalidates_review(self):
        self.crm()
        self.remote["!a:local"] = {"state": "private", "other_writer": "new"}
        self.assertEqual(self.journal.process_next()["reason"], "revision-conflict")
        self.assertEqual(self.writes, [])
        self.assertEqual(self.journal.inspect("!a:local")["revision"], 1)

    def test_read_errors_are_retryable_but_never_empty_policy(self):
        self.crm()
        def offline(room):
            raise OSError()
        self.read_hook = offline
        self.assertEqual(self.journal.process_next()["reason"], "policy-read-failed")
        self.assertEqual(self.journal.get("grant")["state"], "queued")
        with self.assertRaises(OSError):
            self.journal.inspect("!a:local")
        self.assertEqual(self.writes, [])
        self.read_hook = None
        self.assertEqual(self.journal.process_next()["state"], "applied")

    def test_definitive_refusal_and_positive_retryable_non_application(self):
        def refusal(room, content):
            raise PolicyRefused()
        self.crm()
        self.write_hook = refusal
        self.assertEqual(self.journal.process_next()["state"], "rejected")
        self.assertIsNone(self.journal.process_next())
        def throttle(room, content):
            raise PolicyRetryable()
        self.write_hook = throttle
        self.crm("second")
        self.assertEqual(self.journal.process_next()["state"], "queued")
        self.remote["!a:local"] = {"state": "private", "changed": True}
        self.write_hook = None
        self.assertEqual(self.journal.process_next()["reason"], "revision-conflict")
        self.assertEqual(len(self.writes), 2)

    def test_changed_pairing_and_live_authorization_cancel_queued_work(self):
        self.crm()
        self.journal = self.make(self.db, generation="enrollment-2")
        self.assertEqual(self.journal.process_next()["state"], "canceled")
        self.assertEqual(self.writes, [])
        self.journal = self.make(self.db, authorize=lambda request: False)
        self.crm("second")
        self.assertEqual(self.journal.process_next()["reason"], "authorization-refused")
        self.assertEqual(self.writes, [])

    def test_multiple_instances_serialize_and_schema_initialization_is_additive(self):
        self.db.execute("CREATE TABLE existing_state (value TEXT)")
        self.db.execute("INSERT INTO existing_state VALUES ('preserved')")
        self.db.commit()
        self.crm()
        self.crm("second")
        results, errors = [], []
        def worker():
            connection = sqlite3.connect(self.path)
            try:
                journal = self.make(connection)
                results.append(journal.process_next())
            except BaseException as error:
                errors.append(error)
            finally:
                connection.close()
        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5)
            self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertCountEqual([r["state"] for r in results], ["applied", "rejected"])
        self.assertEqual(len(self.writes), 1)
        self.assertEqual(self.db.execute("SELECT value FROM existing_state").fetchone()[0], "preserved")
        self.assertEqual(os.stat(self.lock).st_mode & 0o777, 0o600)

    def test_caller_transaction_is_never_committed_by_journal(self):
        self.db.execute("INSERT INTO crm_policy_meta VALUES ('test', 'pending')")
        with self.assertRaisesRegex(ValueError, "caller-transaction-active"):
            self.journal.inspect("!a:local")
        self.assertTrue(self.db.in_transaction)
        self.db.rollback()
        self.assertIsNone(self.db.execute("SELECT value FROM crm_policy_meta WHERE key='test'").fetchone())


if __name__ == "__main__":
    unittest.main()
