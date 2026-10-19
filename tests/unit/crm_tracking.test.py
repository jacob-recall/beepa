#!/usr/bin/env python3
import copy
import importlib.util
import json
from pathlib import Path
import types
import unittest

spec = importlib.util.spec_from_file_location('crm_batch_fixture', Path(__file__).with_name('crm_batch.test.py'))
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)
from crm_tracking import project_window


def message(identity, order, incoming=False, event=None):
    return {'native_id': identity, 'order': str(order), 'timestamp': order * 1000,
            'incoming': incoming, 'event_id': event}


def snapshot(events, parts, more=False):
    return {'window_complete': True, 'has_more': more, 'events': events, 'outgoing_parts': parts,
            'room_id': '!person:local', 'source': 'imessage', 'bridge_epoch': 'native-epoch'}


def observed(events, parts, more=False):
    return {**snapshot(events, parts, more), 'source':'linkedin', 'evidence_kind':'observed_native',
            'complete':False, 'window_complete':False, 'observed_complete':True,
            'observed_window_complete':True}


class ProjectTests(unittest.TestCase):
    def test_observed_history_has_explicit_coverage_and_late_backfill_correction(self):
        proof = observed([message('a',1),message('r',3,True,'$later')], {'$a':['a']})
        first = project_window(None, proof, ['$a'])
        self.assertFalse(first['complete'])
        self.assertTrue(first['observed_complete'])
        self.assertEqual(first['evidence_kind'], 'observed_native')
        proof['events'].insert(1,message('b',2,True,'$earlier'))
        second = project_window(first,proof,['$a'])
        self.assertEqual(second['answers']['$a']['incoming_event_id'],'$earlier')
        proof['events'].pop(1)
        with self.assertRaisesRegex(ValueError,'provider-observed-history-changed'):
            project_window(second,proof,['$a'])

    def test_observed_gap_tie_or_false_native_claim_cannot_attribute(self):
        proof = observed([message('a',1),message('r',2,True,'$reply')], {'$a':['a']})
        for patch in ({'observed_complete':False}, {'observed_window_complete':False},
                      {'window_complete':True}, {'complete':True},
                      {'events':[message('a',1),message('r',1,True,'$reply')]}):
            with self.subTest(patch=patch), self.assertRaises(ValueError):
                project_window(None,{**proof,**patch},['$a'])

    def test_latest_unanswered_and_older_second_reply(self):
        proof = snapshot([message('a', 1), message('b', 2), message('r', 3, True, '$reply'), message('s', 4, True, '$reply2')], {'$a':['a'], '$b':['b']})
        result = project_window(None, proof, ['$a', '$b'])
        self.assertEqual(result['answers']['$b']['incoming_event_id'], '$reply')
        self.assertEqual(result['answers']['$a']['incoming_event_id'], '$reply2')

    def test_old_backfill_does_not_answer_later_outgoing(self):
        proof = snapshot([message('old', 1, True, '$old'), message('a', 2)], {'$a':['a']})
        self.assertEqual(project_window(None, proof, ['$a'])['answers'], {})

    def test_complete_recent_suffix_without_entire_chat(self):
        proof = snapshot([message('boundary', 1, True, '$boundary'), message('a', 2), message('r', 3, True, '$reply')], {'$a':['a']}, True)
        self.assertIn('$a', project_window(None, proof, ['$a'])['answers'])
        proof['events'].pop(0)
        with self.assertRaisesRegex(ValueError, 'native-baseline-incomplete'):
            project_window(None, proof, ['$a'])

    def test_overlap_extends_durable_window_without_old_parts_in_page(self):
        first = project_window(None, snapshot([message('a', 1), message('b', 2)], {'$a':['a']}), ['$a'])
        second = project_window(first, snapshot([message('b', 2), message('r', 3, True, '$reply')], {'$a':['a']}, True), ['$a'])
        self.assertEqual(second['answers']['$a']['incoming_event_id'], '$reply')
        with self.assertRaisesRegex(ValueError, 'native-window-gap'):
            project_window(first, snapshot([message('r', 4, True, '$reply')], {'$a':['a']}, True), ['$a'])

    def test_ties_missing_parts_inversions_and_unmapped_responses_are_pending(self):
        cases = [([message('a', 1), message('r', 1, True, '$reply')], {'$a':['a']}),
                 ([message('a', 1)], {'$a':['missing']}),
                 ([message('a', 2), {**message('r', 3, True, '$reply'), 'timestamp':1000}], {'$a':['a']}),
                 ([message('a', 1), message('r', 2, True)], {'$a':['a']})]
        for events, parts in cases:
            with self.subTest(events=events), self.assertRaises(ValueError):
                project_window(None, snapshot(events, parts), ['$a'])

    def test_delayed_original_incoming_rebuilds_exact_link(self):
        parts = {'$a':['a']}
        first = project_window(None, snapshot([message('a', 1),message('late', 3, True, '$late')], parts), ['$a'])
        corrected = project_window(first, snapshot([message('a', 1),message('earlier', 2, True, '$earlier'),message('late', 3, True, '$late')], parts), ['$a'])
        self.assertEqual(corrected['answers']['$a']['incoming_event_id'], '$earlier')
        self.assertEqual(len(corrected['answers']), 1)


class TrackingTests(fixture.BatchTests):
    def setUp(self):
        super().setUp()
        event = self.envelope()
        self.child = event['content']['child_id']
        self.batch.ingest(event, '!proposals:master')
        self.batch.drain()
        self.sent = self.native[0]['event_id']
        self.proof = snapshot([message('sent', 1), message('reply', 2, True, '$reply')], {self.sent:['sent']})
        self.uplink._crm_native_history = types.SimpleNamespace(snapshot=lambda *a, **kw: copy.deepcopy(self.proof))
        self.uplink.archive_level = lambda room: 'direct'
        self.uplink.delivery_for = lambda mirror, event: '$mirror-' + event[1:]

    def result(self):
        return self.batch.result(self.child)['items'][0]['tracking']

    def test_provider_reader_projection_and_privacy_keep_observed_qualifier(self):
        row = self.batch.item_for_event(self.uplink.db.execute('SELECT proposal_event_id FROM crm_batch_items WHERE child_id=?',(self.child,)).fetchone()[0])
        item = json.loads(row['item_json'])
        item['route']['source'] = 'linkedin'
        with self.uplink.db:
            self.uplink.db.execute('UPDATE crm_batch_items SET item_json=? WHERE child_id=?', (json.dumps(item),self.child))
        proof = observed([message('sent',1),message('reply',2,True,'$reply')], {self.sent:['sent']})
        calls = []
        def read(source,room,targets,**kw):
            calls.append((source,room,targets))
            return copy.deepcopy(proof)
        self.uplink._crm_provider_history = types.SimpleNamespace(snapshot=read)
        self.control.tracking.update()
        self.assertEqual(calls[0],('linkedin','!person:local',[self.sent]))
        self.assertEqual(self.result()['response']['state'],'replied')
        self.assertEqual(self.result()['response']['coverage'],'observed_history')
        self.assertEqual(self.uplink.db.execute('SELECT reason FROM crm_tracking_corrections').fetchone()[0],'observed-native-history')
        self.uplink.archive_level = lambda room:'private'
        self.control.tracking.update()
        self.assertEqual(len(calls),1)
        self.assertEqual(self.result()['response']['state'],'reconciling')
        self.assertEqual(self.result()['response']['evidence_kind'],'observed_native')
        self.assertIsNone(self.result()['response']['incoming_event_id'])

    def test_exact_receipts_and_reply_projection_are_independent(self):
        self.control.tracking.update()
        first = self.result()
        self.assertEqual(first['response']['state'], 'replied')
        self.assertEqual(first['response']['incoming_event_id'], '$reply')
        self.assertEqual(first['response']['mirror_room_id'], '!mirror:master')
        self.assertEqual(first['delivery']['sent_event_id'], self.sent)
        self.assertEqual(first['delivery']['state'], 'sent')
        with self.uplink.db:
            self.uplink.db.execute('INSERT INTO message_receipts(local_room_id,master_room_id,target,read,delivered,teammate_read,sent,failed,dirty) VALUES (?,?,?,1,1,0,1,0,1)', ('!person:local','!mirror:master',self.sent))
        self.control.tracking.update()
        self.assertEqual(self.result()['delivery']['state'], 'read')
        self.assertEqual(self.result()['response']['incoming_event_id'], '$reply')
        self.assertGreater(self.result()['revision'], first['revision'])
        self.assertNotIn('body', json.dumps(self.result()))

    def test_incomplete_scope_retains_only_reconciling_link_and_recovers(self):
        self.control.tracking.update()
        self.proof['window_complete'] = False
        self.control.tracking.update()
        self.assertEqual(self.result()['response']['state'], 'reconciling')
        self.assertEqual(self.result()['response']['incoming_event_id'], '$reply')
        self.proof['window_complete'] = True
        self.control.tracking.update()
        self.assertEqual(self.result()['response']['state'], 'replied')

    def test_privacy_hides_response_links(self):
        self.control.tracking.update()
        self.uplink.archive_level = lambda room: 'private'
        self.control.tracking.update()
        self.assertIsNone(self.result()['response']['incoming_event_id'])
        self.assertEqual(self.result()['response']['state'], 'reconciling')

    def test_backfill_correction_is_atomic_and_audited(self):
        self.control.tracking.update()
        # Use a fresh baseline with wider integer source cursors.
        with self.uplink.db:
            self.uplink.db.execute('DELETE FROM crm_tracking_scopes')
            self.uplink.db.execute('DELETE FROM crm_tracking_corrections')
        self.proof['events'] = [{**message('sent', 1),'order':'10'}, {**message('reply', 2, True,'$reply'),'order':'20'}]
        self.control.tracking.update()
        self.proof['events'].insert(1, {**message('earlier', 2, True,'$earlier'),'order':'15','timestamp':1500})
        self.control.tracking.update()
        self.assertEqual(self.result()['response']['incoming_event_id'], '$earlier')
        rows = self.uplink.db.execute('SELECT old_links,new_links FROM crm_tracking_corrections ORDER BY revision').fetchall()
        self.assertEqual(json.loads(rows[-1][0])[self.sent]['incoming_event_id'], '$reply')
        self.assertEqual(json.loads(rows[-1][1])[self.sent]['incoming_event_id'], '$earlier')


for name in vars(fixture.BatchTests):
    if name.startswith('test_'):
        setattr(TrackingTests, name, None)

if __name__ == '__main__':
    unittest.main()
