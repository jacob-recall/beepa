"""Typed no-room reminders through the real browser adapter, without real push."""
import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('notification_fixture', Path(__file__).with_name('master_notifications.test.py'))
f = importlib.util.module_from_spec(spec); spec.loader.exec_module(f)
m = f.m

class ContactAdapterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.clock = 100.; self.sent = []; self.calls = []
        self.source = dict(kind='contact_reminder', binding='contact-1', owner='@david:master', generation='gen-é', created=90000, episode=90000, due=99000, slot=99000)
        self.rules = [dict(list_id='list', revision=1, delivery_slot=99000, channels=['browser'], title='Ada', owner='@david:master', list_name='Outreach')]
        self.service = m.Notifications(self.tmp.name, lambda _: self.fail('No room read for a contact reminder'), lambda: {}, clock=lambda:self.clock, canonical=self.canonical, push=self.push)
        self.addCleanup(self.service.db.close)
        self.service.handle(dict(action='subscribe', subscription=f.subscription()))
    def canonical(self, data):
        self.calls.append(data)
        if data['action']=='rules': return {'rules':[]}
        if data['action']=='contact_notification':
            self.assertEqual(data['source'],self.source); return {'rules':self.rules}
        self.fail('Unexpected controller callback')
    def push(self, sub, payload): self.sent.append(payload); return 201
    def entry(self): return dict(kind='contact_reminder',contact=self.source,refs=dict(due=99000,slot=99000,rules=[dict(id='list',revision=1,slot=99000)]))
    def enqueue(self, entry=None): return self.service.control_enqueue([entry or self.entry()])['outcomes'][0]
    def outcome(self): return self.service.control_outcomes([m.contact_identity(self.source)])['outcomes'][0]
    def test_parity_dedup_receipt_and_restart(self):
        script="import {notificationIdentity} from './master/notification-routing.mjs'; console.log(notificationIdentity(JSON.parse(process.argv[1])))"
        js=subprocess.check_output(['node','--input-type=module','-e',script,json.dumps(self.source)],cwd=Path(__file__).resolve().parents[2],text=True).strip()
        result=self.enqueue(); self.assertEqual(result['id'],js); self.assertEqual(self.calls,[])
        self.enqueue(); self.service.deliver(); self.assertEqual(len(self.sent),1); self.assertEqual(self.outcome()['state'],'accepted')
        self.assertIn('Ada',self.sent[0]['title']); self.assertIn('contact-by',self.sent[0]['body']); self.assertNotIn('room',self.entry())
        row=self.service.snapshot()['history'][0]; self.assertEqual((row['room'],row['event']),('',''))
        self.service.receipt(dict(id=result['id'],token=self.sent[0]['receipt_token'],stage='shown'))
        self.assertIsNotNone(self.service.snapshot()['history'][0]['deliveries'][0]['shown_at'])
        reopened=m.Notifications(self.tmp.name,lambda _:self.fail('Unexpected Matrix read'),lambda:{},clock=lambda:self.clock,canonical=self.canonical,push=self.push)
        self.addCleanup(reopened.db.close); reopened.control_enqueue([self.entry()]); reopened.deliver(); self.assertEqual(len(self.sent),1)
    def test_stale_contact_reminder_defers_then_revalidates(self):
        self.enqueue(); original = self.service.canonical
        def stale(data):
            if data['action'] == 'contact_notification': return {'rules': [], 'deferred_rules': self.rules}
            return original(data)
        self.service.canonical = stale; self.service.deliver()
        self.assertEqual(self.outcome()['state'], 'pending'); self.assertEqual(self.sent, [])
        self.clock += 7200; self.service.canonical = original; self.service.deliver(); self.service.deliver()
        self.assertEqual(self.outcome()['state'], 'accepted'); self.assertEqual(len(self.sent), 1)

    def test_stale_contact_reminder_cancels_when_scope_is_removed(self):
        self.enqueue(); original = self.service.canonical
        self.service.canonical = lambda data: {'rules': [], 'deferred_rules': self.rules} if data['action'] == 'contact_notification' else original(data)
        self.service.deliver(); self.assertEqual(self.outcome()['state'], 'pending')
        self.rules = []; self.service.deliver()
        self.assertEqual(self.outcome()['state'], 'canceled'); self.assertEqual(self.sent, [])

    def test_revoked_completed_or_edited_task_cancels_before_push(self):
        self.enqueue(); self.rules=[]; self.service.deliver(); self.assertEqual(self.sent,[]); self.assertEqual(self.outcome()['state'],'canceled')
    def test_policy_revision_and_slot_changes_do_not_send(self):
        self.enqueue(); self.rules[0]['revision']=2; self.service.deliver(); self.assertEqual(self.outcome()['state'],'canceled')
    def test_recheck_immediately_before_external_call(self):
        self.enqueue(); original=self.service.canonical; reads=0
        def revoke(data):
            nonlocal reads
            if data['action']=='contact_notification':
                reads+=1
                if reads==2:self.rules=[]
            return original(data)
        self.service.canonical=revoke; self.service.deliver(); self.assertEqual(self.sent,[]); self.assertEqual(self.outcome()['state'],'canceled')
    def test_unknown_never_replays_and_late_receipt_recovers(self):
        self.enqueue()
        def uncertain(sub,payload):self.sent.append(payload);raise TimeoutError()
        self.service.push=uncertain; self.service.deliver(); self.assertEqual(self.outcome()['state'],'unknown')
        self.clock+=10; self.enqueue(); self.service.deliver(); self.assertEqual(len(self.sent),1)
        self.service.receipt(dict(id=self.sent[0]['id'],token=self.sent[0]['receipt_token'],stage='shown'))
        self.assertEqual(self.outcome()['state'],'accepted')
    def test_transient_canonical_failure_leaves_pending(self):
        self.enqueue();self.service.canonical=lambda _: (_ for _ in ()).throw(RuntimeError('Temporary outage'))
        with self.assertRaises(RuntimeError):self.service.deliver()
        self.assertEqual(self.outcome()['state'],'pending');self.assertEqual(self.sent,[])
    def test_strict_shapes_fail_atomically(self):
        invalid=[dict(self.entry(),room='!fake:master'),dict(self.entry(),title='Injected')]
        for key,value in [('created',True),('episode',0),('slot',1),('owner','bad'),('binding','constructor'),('room','!fake:master')]:
            entry=copy.deepcopy(self.entry());entry['contact'][key]=value;invalid.append(entry)
        entry=copy.deepcopy(self.entry());entry['refs']['due']=1;invalid.append(entry)
        entry=copy.deepcopy(self.entry());entry['refs']['rules'][0]['revision']=True;invalid.append(entry)
        for entry in invalid:
            with self.subTest(entry=entry),self.assertRaises(ValueError):self.service.control_enqueue([self.entry(),entry])
        self.assertEqual(self.service.snapshot()['history'],[]);self.assertEqual(self.calls,[])

if __name__=='__main__':unittest.main()
