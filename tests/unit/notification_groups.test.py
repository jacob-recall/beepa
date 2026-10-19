import base64
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('notification_groups', ROOT/'master/notifications.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class NotificationGroupsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.clock, self.sent, self.revoked, self.failure, self.fast_receipt = 1700000000.01, [], set(), None, False
        self.rules = [dict(room='!ada-david:test', owner='@david:test', title='Ada from David'), dict(room='!ada-elliot:test', owner='@elliot:test', title='Ada from Elliot')]
        for r in self.rules:
            r.update(list_id='shared', list_name='Shared', revision=1, channels=['browser'], all_messages=False, presentation=dict(mode='digest', interval_ms=60000))
        self.service = self.create()
        self.addCleanup(lambda: self.service.db.close())
        self.device = self.subscribe('one')

    def create(self):
        service = m.Notifications(self.tmp.name, lambda _: self.fail('Unexpected Matrix request'), lambda: {}, canonical=lambda _: {'rules': self.rules}, clock=lambda: self.clock, push=self.push)
        def verified(room):
            if room in self.revoked:
                raise ValueError('Revoked')
            r = next(r for r in self.rules if r['room'] == room)
            return r['owner'], r['title']
        service.verified = verified
        with service.db:
            for r in self.rules:
                service.db.execute('INSERT OR IGNORE INTO follows VALUES(?,?,?,?,?,?)', (r['room'], r['owner'], r['title'], '0', int(self.clock*1000), 0))
        return service

    def subscribe(self, name):
        encode = lambda b: base64.urlsafe_b64encode(b).decode().rstrip('=')
        return self.service.handle({'action': 'subscribe', 'subscription': {'endpoint': 'https://updates.push.services.mozilla.com/'+name, 'keys': {'auth': encode(b'a'*16), 'p256dh': encode(b'\x04'+b'a'*64)}}})['device']

    def queue(self, count=3):
        ids = []
        with self.service.db:
            for i in range(count):
                r = self.rules[i % 2]
                nid = hashlib.sha256(str(i).encode()).hexdigest()
                refs = {'outgoing': False, 'rules': [{'id': x['list_id'], 'revision': x['revision']} for x in self.rules if x['room'] == r['room']]}
                self.service.enqueue(nid, r['room'], '$event'+str(i), r['title'], rule_refs=refs)
                ids.append(nid)
        return ids

    def push(self, subscription, payload):
        self.sent.append(dict(payload))
        if self.fast_receipt:
            self.service.receipt({'token': payload['receipt_token'], 'stage': 'shown'})
        if isinstance(self.failure, Exception):
            raise self.failure
        return self.failure or 201

    def test_digest_one_push_exact_picker_and_scoped_receipt(self):
        self.queue(); self.service.deliver(); self.assertEqual(self.sent, [])
        self.clock += 60; self.service.deliver(); self.assertEqual(len(self.sent), 1)
        self.assertIn('3 messages in 2 conversations', self.sent[0]['title'])
        self.assertEqual(len(self.sent[0]['url'].split('=')[1].split(',')), 2)
        token = self.sent[0]['receipt_token']
        self.service.receipt({'token': token, 'stage': 'shown'})
        self.assertTrue(all(h['deliveries'][0]['shown_at'] for h in self.service.snapshot()['history']))
        with self.assertRaises(ValueError):
            self.service.receipt({'token': 'A'*43, 'stage': 'shown'})
        self.assertNotIn(token, json.dumps(self.service.snapshot()))
        self.service.deliver(); self.assertEqual(len(self.sent), 1)
        with self.service.db:
            self.service.enqueue('later-message', self.rules[0]['room'], '$later', 'Later update')
        self.service.receipt({'token': token, 'stage': 'clicked'})
        later = self.service.db.execute('SELECT state,clicked_at FROM deliveries WHERE id=?', ('later-message',)).fetchone()
        self.assertEqual(later['state'], 'pending'); self.assertIsNone(later['clicked_at'])

    def test_grouped_updates_are_per_conversation(self):
        for r in self.rules:
            r['presentation']['mode'] = 'grouped'
        self.queue(); self.clock += 60; self.service.deliver()
        self.assertEqual(len(self.sent), 2)
        self.assertTrue(all(len(p['url'].split('=')[1].split(',')) == 1 for p in self.sent))

    def test_overdue_collection_windows_collapse_on_resume(self):
        ids = self.queue()
        with self.service.db:
            for i, nid in enumerate(ids):
                self.service.db.execute('UPDATE history SET created=? WHERE id=?', (int(self.clock*1000)-(i+1)*120000, nid))
        self.service.deliver(); self.assertEqual(len(self.sent), 1)
        self.assertIn('3 messages in 2 conversations', self.sent[0]['title'])

    def test_immediate_overlap_wins_without_later_digest(self):
        self.rules.append(dict(self.rules[0], list_id='immediate', list_name='Immediate', presentation=None))
        self.queue(1); self.service.deliver(); self.assertEqual(len(self.sent), 1)
        self.assertIn('Lists: Immediate', self.sent[0]['body']); self.assertNotIn('Shared', self.sent[0]['body'])
        self.clock += 120; self.service.deliver(); self.assertEqual(len(self.sent), 1)

    def test_group_rate_limit_waits_and_rechecks_revocation(self):
        self.queue(); self.clock += 60; self.failure = 429; self.service.deliver(); self.assertEqual(len(self.sent), 1)
        self.service.deliver(); self.assertEqual(len(self.sent), 1)
        self.clock += 60; self.failure = None; self.revoked.add(self.rules[1]['room']); self.service.deliver()
        self.assertEqual(len(self.sent), 2); self.assertNotIn('Elliot', self.sent[-1]['body'])
        rejected = [h for h in self.service.snapshot()['history'] if h['room'] in self.revoked]
        self.assertTrue(all(h['deliveries'][0]['state'] == 'canceled' for h in rejected))

    def test_unknown_group_does_not_resend_on_restart_but_late_receipt_can_reconcile(self):
        self.queue(); self.clock += 60; self.failure = OSError('Lost response'); self.service.deliver()
        self.assertTrue(all(h['deliveries'][0]['state'] == 'unknown' for h in self.service.snapshot()['history']))
        self.service.db.close(); self.service = self.create(); self.failure = None; self.clock += 60; self.service.deliver(); self.assertEqual(len(self.sent), 1)
        self.service.receipt({'token': self.sent[0]['receipt_token'], 'stage': 'shown'})
        self.assertTrue(all(h['deliveries'][0]['state'] == 'accepted' for h in self.service.snapshot()['history']))

    def test_fast_receipt_keeps_accepted_state_and_attempt_count(self):
        self.queue(); self.clock += 60; self.failure = 503; self.fast_receipt = True; self.service.deliver()
        self.assertEqual(len(self.sent), 1)
        for h in self.service.snapshot()['history']:
            self.assertEqual(h['deliveries'][0]['attempts'], 1); self.assertEqual(h['deliveries'][0]['state'], 'accepted')

    def test_group_capability_cannot_acknowledge_another_device(self):
        second = self.subscribe('two'); self.queue(); self.clock += 60; self.service.deliver(); self.assertEqual(len(self.sent), 2)
        token = self.sent[0]['receipt_token']
        target = self.service.db.execute('SELECT device FROM receipt_groups WHERE token=?', (token,)).fetchone()[0]
        self.service.receipt({'token': token, 'stage': 'clicked'})
        for row in self.service.db.execute('SELECT device,clicked_at FROM deliveries'):
            self.assertEqual(bool(row['clicked_at']), row['device'] == target)
        self.assertNotEqual(self.device, second)

    def test_presentation_windows_match_js(self):
        cases = []
        for mode, interval in [('immediate', None), ('grouped', 60000), ('digest', 3600000)]:
            for created in [120000, 120001, 179999]:
                cases.append([[dict(list_id='list', presentation=dict(mode=mode, interval_ms=interval))], created, 180000, '!r:test'])
        cases.append([[dict(list_id=k, presentation=dict(mode='digest', interval_ms=60000)) for k in ['a', 'A']], 120000, 180000, '!r:test'])
        for invalid in [{}, False, [], 0, dict(mode='digest', interval_ms=5)]:
            cases.append([[dict(list_id='bad', presentation=invalid)], 120000, 180000, '!r:test'])
        script = "import fs from 'node:fs';import {monitoringDeliveryPlan} from './shared/model/crm.js';console.log(JSON.stringify(JSON.parse(fs.readFileSync(0,'utf8')).map(x=>monitoringDeliveryPlan(...x))));"
        result = subprocess.run(['node', '--input-type=module', '-e', script], input=json.dumps(cases), text=True, capture_output=True, cwd=ROOT, check=True)
        self.assertEqual(json.loads(result.stdout), [m.delivery_plan(*case) for case in cases])


if __name__ == '__main__':
    unittest.main()
