"""Browser delivery evidence for the shared router; no real push or accounts."""
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import threading
import unittest
from urllib.parse import unquote

spec = importlib.util.spec_from_file_location('notification_fixture', Path(__file__).with_name('master_notifications.test.py'))
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)
m, ROOM, OWNER = fixture.m, fixture.ROOM, fixture.OWNER


class AdapterTests(fixture.NotificationsTest):
    def reminder_fixture(self):
        self.subscribe(); entry = self.enable_adapter(); self.service.dispatcher_handoff()
        due = int(self.clock*1000)-1
        entry.update(kind='reminder', refs={'due': due, 'slot': due, 'rules': [{'id': 'canonical', 'revision': 1, 'slot': due}]})
        self.obligation = {'event_id': entry['event'], 'due_at': due, 'snooze_until': None, 'waiting': 'waiting_for_us'}
        def canonical(data):
            if data['action'] == 'rules': return {'rules': self.rules}
            if data['action'] == 'updates': return {'updates': [{'id': ROOM, 'list_id': 'canonical', 'fresh': getattr(self, 'fresh', True), 'obligations': [self.obligation]}]}
            self.fail('Unexpected controller call')
        self.service.canonical = canonical
        return entry

    def test_stale_message_reminder_resumes_once_after_restart(self):
        entry = self.reminder_fixture(); nid = self.enqueue_adapter(entry)['id']; self.fresh = False
        self.service.deliver(); self.assertEqual(self.outcome(nid)['state'], 'pending'); self.assertEqual(self.sent, [])
        reopened = m.Notifications(self.tmp.name, self.matrix, lambda: {OWNER: fixture.SPACE}, push=self.push, clock=lambda: self.clock, canonical=self.service.canonical)
        self.addCleanup(reopened.db.close); self.clock += 7200; reopened.deliver(); self.assertEqual(self.sent, [])
        self.fresh = True; reopened.deliver(); reopened.deliver()
        self.assertEqual(self.outcome(nid)['state'], 'accepted'); self.assertEqual(len(self.sent), 1)

    def test_stale_message_reminder_rechecks_answer_on_recovery(self):
        entry = self.reminder_fixture(); nid = self.enqueue_adapter(entry)['id']; self.fresh = False
        self.service.deliver(); self.assertEqual(self.outcome(nid)['state'], 'pending')
        self.fresh = True; self.obligation['waiting'] = 'answered'; self.service.deliver()
        self.assertEqual(self.outcome(nid)['state'], 'canceled'); self.assertEqual(self.sent, [])

    def test_stale_message_reminder_does_not_mask_policy_removal(self):
        entry = self.reminder_fixture(); nid = self.enqueue_adapter(entry)['id']; self.fresh = False
        self.service.deliver(); self.rules = []; self.service.deliver()
        self.assertEqual(self.outcome(nid)['state'], 'canceled'); self.assertEqual(self.sent, [])

    def test_message_reminder_snooze_new_cycle_preserves_deadline_and_receipts(self):
        entry = self.reminder_fixture(); old = self.enqueue_adapter(entry)['id']
        self.obligation.update(snooze_until=int(self.clock*1000)+60000, episode_at=int(self.clock*1000)); self.rules[0]['revision'] = 2
        self.service.deliver(); self.assertEqual(self.outcome(old)['state'], 'canceled'); self.assertEqual(self.sent, [])
        self.clock += 61
        next_entry = copy.deepcopy(entry); next_entry['refs']['episode'] = self.obligation['episode_at']; next_entry['refs']['rules'][0]['revision'] = 2
        new = self.enqueue_adapter(next_entry)['id']; self.assertNotEqual(old, new)
        self.service.deliver(); self.assertEqual(self.outcome(new)['state'], 'accepted'); self.assertEqual(len(self.sent), 1)
        payload = self.sent[0][1]; self.service.receipt({'token': payload['receipt_token'], 'stage': 'shown'})
        self.assertIsNotNone(self.service.snapshot(new)['history'][0]['deliveries'][0]['shown_at'])
        self.assertEqual(json.loads(self.service.snapshot(new)['history'][0]['rule_refs'])['due'], entry['refs']['due'])
        reopened = m.Notifications(self.tmp.name, self.matrix, lambda: {OWNER: fixture.SPACE}, push=self.push, clock=lambda: self.clock, canonical=self.service.canonical)
        self.addCleanup(reopened.db.close); reopened.control_enqueue([next_entry]); reopened.deliver(); self.assertEqual(len(self.sent), 1)

    def test_message_reminder_old_unknown_outcome_is_not_replayed(self):
        entry = self.reminder_fixture(); old = self.enqueue_adapter(entry)['id']
        def uncertain(*args): raise TimeoutError('Acknowledgment lost')
        self.service.push = uncertain; self.service.deliver(); self.assertEqual(self.outcome(old)['state'], 'unknown')
        self.obligation.update(episode_at=int(self.clock*1000), snooze_until=int(self.clock*1000)+60000); self.rules[0]['revision'] = 2
        self.clock += 61; self.service.push = self.push
        entry['refs']['episode'] = self.obligation['episode_at']; entry['refs']['rules'][0]['revision'] = 2
        new = self.enqueue_adapter(entry)['id']; self.service.deliver(); self.service.deliver()
        self.assertEqual(self.outcome(old)['state'], 'unknown'); self.assertEqual(self.outcome(new)['state'], 'accepted'); self.assertEqual(len(self.sent), 1)

    def test_message_episode_identity_matches_js_and_rejects_invalid_values(self):
        import subprocess
        entry = self.reminder_fixture(); entry['refs']['episode'] = 100000
        source = {'kind': 'reminder', 'room': ROOM, 'event': entry['event'], 'due': entry['refs']['due'], 'slot': entry['refs']['slot'], 'episode': 100000}
        script = "import {notificationIdentity} from './master/notification-routing.mjs'; console.log(notificationIdentity(JSON.parse(process.argv[1])))"
        actual = subprocess.check_output(['node', '--input-type=module', '-e', script, json.dumps(source)], cwd=Path(__file__).resolve().parents[2], text=True).strip()
        self.assertEqual(self.enqueue_adapter(entry)['id'], actual)
        for invalid in [None, True, 0, -1, '100000', 1.5]:
            bad = copy.deepcopy(entry); bad['refs']['episode'] = invalid
            with self.subTest(invalid=invalid), self.assertRaises(ValueError): self.enqueue_adapter(bad)

    def test_dispatcher_handoff_freezes_discovery_and_is_restart_safe(self):
        self.subscribe(); entry = self.enable_adapter()
        follow = dict(self.service.db.execute('SELECT * FROM follows').fetchone())
        handoff = self.service.handle({'action': 'control_dispatcher'})
        self.assertEqual(handoff['owner'], 'control')
        self.assertEqual(handoff['follows'][0]['cursor'], follow['cursor'])
        self.assertEqual(self.service.handle({'action': 'control_dispatcher'}), handoff)
        self.service.poll_room(follow)
        self.assertEqual(self.service.db.execute('SELECT count(*) FROM history').fetchone()[0], 0)
        self.assertEqual(self.service.db.execute('SELECT cursor FROM follows').fetchone()[0], follow['cursor'])
        result = self.enqueue_adapter(entry); self.service.deliver()
        self.assertEqual(self.outcome(result['id'])['state'], 'accepted')
        self.assertEqual(len(self.sent), 1)
        reopened = m.Notifications(self.tmp.name, self.matrix, lambda: {OWNER: fixture.SPACE}, push=self.push, clock=lambda: self.clock, canonical=self.service.canonical)
        self.addCleanup(reopened.db.close)
        self.assertEqual(reopened.dispatcher_handoff(), handoff)
        reopened.poll_room(follow)
        self.assertEqual(reopened.db.execute('SELECT cursor FROM follows').fetchone()[0], follow['cursor'])

    def test_direct_delivery_rechecks_resolved_reminder_after_handoff(self):
        self.subscribe(); entry = self.enable_adapter(); self.service.dispatcher_handoff()
        due = int(self.clock*1000)-1
        entry.update(kind='reminder', refs={'due': due, 'slot': due, 'rules': [{'id': 'canonical', 'revision': 1, 'slot': due}]})
        result = self.enqueue_adapter(entry)
        def canonical(data):
            if data['action'] == 'rules': return {'rules': self.rules}
            if data['action'] == 'updates':
                return {'updates': [{'id': ROOM, 'list_id': 'canonical', 'fresh': True, 'obligations': [{'event_id': entry['event'], 'due_at': due, 'waiting': 'answered'}]}]}
            self.fail('Unexpected controller call')
        self.service.canonical = canonical
        self.service.deliver()
        self.assertEqual(self.outcome(result['id'])['state'], 'canceled')
        self.assertEqual(self.sent, [])

    def test_handoff_during_legacy_network_read_prevents_late_enqueue(self):
        self.subscribe(); self.enable_adapter()
        follow = dict(self.service.db.execute('SELECT * FROM follows').fetchone())
        original = self.service.matrix
        def matrix(path):
            result = original(path)
            if '/messages?' in path:
                self.service.dispatcher_handoff()
            return result
        self.service.matrix = matrix
        self.service.poll_room(follow)
        self.assertEqual(self.service.db.execute('SELECT count(*) FROM history').fetchone()[0], 0)

    def matrix(self, path):
        if '/event/' in path:
            return next((e for e in self.events if e['event_id'] == unquote(path.rsplit('/', 1)[-1])), {})
        return super().matrix(path)

    def enable_adapter(self):
        self.follow()
        self.events = [fixture.message()]
        self.calls = []
        self.rules = [{'room': ROOM, 'owner': OWNER, 'title': 'Test person', 'source': 'imessage',
                       'list_id': 'canonical', 'list_name': 'Canonical', 'revision': 1,
                       'channels': ['browser', 'telegram'], 'all_messages': False}]
        def canonical(data):
            self.calls.append(data)
            if data['action'] == 'rules':
                return {'rules': self.rules}
            raise AssertionError('Unexpected callback during adapter enqueue')
        self.service.canonical = canonical
        return {'room': ROOM, 'event': '$event1', 'kind': 'message',
                'refs': {'outgoing': False, 'rules': [{'id': 'canonical', 'revision': 1}]}}

    def enqueue_adapter(self, entry):
        return self.service.handle({'action': 'control_enqueue', 'entries': [entry]})['outcomes'][0]

    def outcome(self, nid):
        return self.service.handle({'action': 'control_outcomes', 'ids': [nid]})['outcomes'][0]

    def test_adapter_is_idempotent_and_never_calls_back_to_waiting_controller(self):
        self.subscribe(); entry = self.enable_adapter()
        result = self.enqueue_adapter(entry)
        nid = hashlib.sha256((ROOM+'\0$event1').encode()).hexdigest()
        self.assertEqual(result['id'], nid); self.assertEqual(result['state'], 'pending'); self.assertEqual(self.calls, [])
        self.assertEqual(self.enqueue_adapter(entry), result)
        self.service.deliver(); self.assertEqual(len(self.sent), 1); self.assertEqual(self.outcome(nid)['state'], 'accepted')
        self.enqueue_adapter(entry); self.service.deliver(); self.assertEqual(len(self.sent), 1)
        self.service.poll_room(dict(self.service.db.execute('SELECT * FROM follows').fetchone()))
        self.assertEqual(self.service.db.execute('SELECT count(*) FROM history').fetchone()[0], 1)
        self.service.matrix = lambda _: self.fail('outcomes are operational reads only')
        serialized = json.dumps(self.service.handle({'action': 'control_outcomes', 'ids': [nid, 'a'*64]}))
        for secret in ['endpoint', 'receipt_token', 'private body', 'Test person']:
            self.assertNotIn(secret, serialized)
        self.assertEqual(self.outcome('a'*64)['state'], 'missing')

    def test_missing_devices_does_not_replay_history_on_later_subscribe(self):
        entry = self.enable_adapter(); nid = self.enqueue_adapter(entry)['id']
        self.assertEqual(self.outcome(nid)['state'], 'unavailable'); self.subscribe(); self.enqueue_adapter(entry)
        self.service.deliver(); self.assertEqual(self.sent, [])

    def test_fresh_list_and_source_checks_still_gate_delivery(self):
        self.subscribe(); entry = self.enable_adapter(); nid = self.enqueue_adapter(entry)['id']
        self.rules = []; self.service.deliver(); self.assertEqual(self.outcome(nid)['state'], 'canceled'); self.assertEqual(self.sent, [])
        self.shared = False
        with self.assertRaises(ValueError): self.enqueue_adapter(entry)

    def test_source_direction_sender_and_entry_validation_are_atomic(self):
        self.subscribe(); entry = self.enable_adapter()
        malformed = [dict(entry, event='$missing'), dict(entry, refs={**entry['refs'], 'outgoing': True}),
                     dict(entry, refs={'outgoing': False, 'rules': [{'id': 'canonical', 'revision': True}]}),
                     dict(entry, title='Spoofed title'), dict(entry, refs={'outgoing': False, 'rules': entry['refs']['rules']*2})]
        for bad in malformed:
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.service.handle({'action': 'control_enqueue', 'entries': [entry, bad]})
            self.assertEqual(self.service.db.execute('SELECT count(*) FROM history').fetchone()[0], 0)
        with self.assertRaises(ValueError): self.service.handle({'action': 'control_enqueue', 'entries': [entry, entry]})
        self.events[0]['sender'] = '@forged:test'
        with self.assertRaises(ValueError): self.enqueue_adapter(entry)
        for ids in [[{}], ['bad'], ['a'*64]*2, None]:
            with self.assertRaises(ValueError): self.service.handle({'action': 'control_outcomes', 'ids': ids})

    def test_multi_device_aggregate_never_calls_uncertainty_failure(self):
        first = self.subscribe(); sub = fixture.subscription(); sub['endpoint'] += '/other'
        second = self.service.handle({'action': 'subscribe', 'subscription': sub})['device']
        nid = self.enqueue_adapter(self.enable_adapter())['id']
        def state(device, value):
            with self.service.db: self.service.db.execute('UPDATE deliveries SET state=? WHERE id=? AND device=?', (value, nid, device))
        state(first, 'sending'); self.assertEqual(self.outcome(nid)['state'], 'in_flight')
        state(first, 'unknown'); self.assertEqual(self.outcome(nid)['state'], 'unknown')
        state(second, 'accepted'); self.assertEqual(self.outcome(nid)['state'], 'accepted')
        state(first, 'failed'); state(second, 'expired'); self.assertEqual(self.outcome(nid)['state'], 'failed')
        state(first, 'canceled'); state(second, 'canceled'); self.assertEqual(self.outcome(nid)['state'], 'canceled')

    def test_ambiguous_single_push_is_not_replayed_and_late_receipt_reconciles(self):
        self.subscribe(); nid = self.service.handle({'action': 'test'})['notification_id']
        def uncertain(sub, payload):
            self.assertEqual(self.service.db.execute('SELECT state FROM deliveries').fetchone()[0], 'sending')
            self.sent.append((sub, payload)); raise TimeoutError('Lost provider response')
        self.service.push = uncertain; self.service.deliver(); self.assertEqual(self.outcome(nid)['state'], 'unknown')
        reopened = m.Notifications(self.tmp.name, self.matrix, lambda: {OWNER: fixture.SPACE}, push=self.push, clock=lambda: self.clock)
        self.addCleanup(reopened.db.close); self.clock += 60; reopened.deliver(); self.assertEqual(len(self.sent), 1)
        reopened.receipt({'token': self.sent[0][1]['receipt_token'], 'stage': 'shown'})
        self.assertEqual(reopened.control_outcomes([nid])['outcomes'][0]['state'], 'accepted')

    def test_definitive_rejection_retries_are_bounded(self):
        self.subscribe(); nid = self.service.handle({'action': 'test'})['notification_id']; self.service.push = lambda *_: 503
        for _ in range(7): self.service.deliver(); self.clock += 301
        outcome = self.outcome(nid); self.assertEqual(outcome['state'], 'failed'); self.assertEqual(outcome['deliveries'][0]['attempts'], 5)
        other = self.service.handle({'action': 'test', 'request_id': 'rejected'})['notification_id']
        self.service.push = lambda *_: 403; self.service.deliver(); self.assertEqual(self.outcome(other)['state'], 'failed')

    def test_two_delivery_consumers_cannot_claim_the_same_push(self):
        self.subscribe(); nid = self.service.handle({'action': 'test'})['notification_id']; started, release = threading.Event(), threading.Event()
        def held(sub, payload):
            self.sent.append((sub, payload)); started.set(); self.assertTrue(release.wait(2)); return 201
        self.service.push = held; worker = threading.Thread(target=self.service.deliver); worker.start()
        try:
            self.assertTrue(started.wait(2)); self.assertEqual(self.outcome(nid)['state'], 'in_flight')
            self.service.deliver(); self.assertEqual(len(self.sent), 1)
        finally: release.set(); worker.join(2)
        self.assertEqual(self.outcome(nid)['state'], 'accepted')

    def test_upgrade_does_not_guess_whether_a_legacy_retry_was_delivered(self):
        self.subscribe(); nid = self.service.handle({'action': 'test'})['notification_id']
        with self.service.db:
            self.service.db.execute('UPDATE deliveries SET attempts=1')
            self.service.db.execute("DELETE FROM control_metadata WHERE key='delivery_claim_version'")
        reopened = m.Notifications(self.tmp.name, self.matrix, lambda: {OWNER: fixture.SPACE}, push=self.push, clock=lambda: self.clock)
        self.addCleanup(reopened.db.close); reopened.deliver(); self.assertEqual(self.sent, [])
        self.assertEqual(reopened.control_outcomes([nid])['outcomes'][0]['state'], 'unknown')


if __name__ == '__main__': unittest.main()
