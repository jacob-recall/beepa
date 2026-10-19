"""Independent native reply projection and privacy boundary tests."""
import copy
import importlib.util
import json
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location('tracking_adversarial_fixture', Path(__file__).with_name('crm_tracking.test.py'))
fixture = importlib.util.module_from_spec(spec); spec.loader.exec_module(fixture)
from crm_tracking import project_window
message, snapshot = fixture.message, fixture.snapshot


class ProjectAdversarial(unittest.TestCase):
    def test_same_matrix_incoming_cannot_answer_two_outreach_messages(self):
        value = snapshot([message('a', 1), message('b', 2), message('r1', 3, True, '$same'),
            message('r2', 4, True, '$same')], {'$a': ['a'], '$b': ['b']})
        with self.assertRaises(ValueError): project_window(None, value, ['$a', '$b'])

    def test_disjoint_full_snapshot_cannot_extend_old_retained_send(self):
        prior = project_window(None, snapshot([message('a', 1), message('b', 2)], {'$a': ['a']}), ['$a'])
        with self.assertRaisesRegex(ValueError, 'window-gap'):
            project_window(prior, snapshot([message('reply', 3, True, '$reply')], {'$a': ['a']}, False), ['$a'])

    def test_overlap_with_changed_native_direction_or_matrix_identity_refuses(self):
        prior = project_window(None, snapshot([message('a', 1), message('r', 2, True, '$reply')], {'$a': ['a']}), ['$a'])
        for change in [{'incoming': False}, {'event_id': '$different'}, {'order': '3'}, {'timestamp': 2500}]:
            value = snapshot([message('a', 1), {**message('r', 2, True, '$reply'), **change}], {'$a': ['a']})
            with self.subTest(change=change), self.assertRaises(ValueError): project_window(prior, value, ['$a'])

    def test_duplicate_snapshot_rows_are_idempotent_and_not_second_responses(self):
        a, r = message('a', 1), message('r', 2, True, '$reply')
        value = snapshot([a, copy.deepcopy(a), r, copy.deepcopy(r)], {'$a': ['a']})
        projected = project_window(None, value, ['$a'])
        self.assertEqual(len(projected['events']), 2); self.assertEqual(len(projected['answers']), 1)
        self.assertEqual(project_window(projected, value, ['$a']), projected)

    def test_unmapped_first_incoming_cannot_be_skipped_for_later_mapped_reply(self):
        value = snapshot([message('a', 1), message('missing', 2, True), message('later', 3, True, '$later')], {'$a': ['a']})
        with self.assertRaisesRegex(ValueError, 'incoming-not-mapped'): project_window(None, value, ['$a'])

    def test_incoming_inside_native_outgoing_parts_never_selects_older_candidate(self):
        value = snapshot([message('older', 1), message('a1', 2), message('r', 3, True, '$reply'), message('a2', 4)],
            {'$old': ['older'], '$new': ['a1', 'a2']})
        with self.assertRaisesRegex(ValueError, 'between-outgoing-parts'): project_window(None, value, ['$old', '$new'])

    def test_native_parts_cannot_belong_to_multiple_outreach_messages(self):
        with self.assertRaisesRegex(ValueError, 'multiple-outreach'):
            project_window(None, snapshot([message('a', 1)], {'$a': ['a'], '$b': ['a']}), ['$a', '$b'])


class ObservedProjectAdversarial(unittest.TestCase):
    def observed(self, events, parts, **extra):
        return {**snapshot(events,parts), 'evidence_kind':'observed_native',
            'complete':False,'window_complete':False,'observed_complete':True,
            'observed_window_complete':True, **extra}

    def test_disappeared_tail_reply_cannot_remain_proven_observed(self):
        a, r = message('a',1), message('r',2,True,'$reply')
        prior=project_window(None,self.observed([a,r],{'$a':['a']}),['$a'])
        with self.assertRaisesRegex(ValueError,'history-changed'):
            project_window(prior,self.observed([a],{'$a':['a']}),['$a'])

    def test_observed_late_incoming_corrects_earlier_assignment(self):
        a, r = message('a',1), message('r',3,True,'$late')
        prior=project_window(None,self.observed([a,r],{'$a':['a']}),['$a'])
        result=project_window(prior,self.observed([a,message('earlier',2,True,'$earlier'),r],{'$a':['a']}),['$a'])
        self.assertEqual(result['answers']['$a']['incoming_event_id'],'$earlier')
        self.assertFalse(result['complete']); self.assertTrue(result['observed_complete'])

    def test_observed_cannot_silently_upgrade_to_native_complete(self):
        a=message('a',1)
        prior=project_window(None,self.observed([a],{'$a':['a']}),['$a'])
        with self.assertRaisesRegex(ValueError,'evidence-changed'):
            project_window(prior,snapshot([a],{'$a':['a']}),['$a'])

    def test_observed_pending_reader_does_not_reuse_old_completion(self):
        a=message('a',1)
        prior=project_window(None,self.observed([a],{'$a':['a']}),['$a'])
        with self.assertRaises(ValueError):
            project_window(prior,self.observed([a],{'$a':['a']}, observed_window_complete=False,reason='provider-backfill-pending'),['$a'])


class TrackingAdversarial(fixture.TrackingTests):
    def test_tracking_foreign_room_source_or_bridge_epoch_cannot_reassign_response(self):
        self.control.tracking.update(); first = self.result()
        for change in [{'room_id': '!foreign:local'}, {'source': 'whatsapp'}, {'bridge_epoch': 'replacement'}]:
            previous = copy.deepcopy(self.proof); self.proof.update(change)
            self.proof['events'].append(message('foreign', 3, True, '$foreign'))
            self.control.tracking.update(); result = self.result()
            self.assertEqual(result['response']['state'], 'reconciling')
            self.assertEqual(result['response']['incoming_event_id'], first['response']['incoming_event_id'])
            self.proof = previous

    def test_tracking_replay_does_not_bump_projection_revision(self):
        self.control.tracking.update(); first = self.result()
        self.control.tracking.update(); self.assertEqual(self.result(), first)

    def test_private_room_cannot_publish_new_receipt_or_message_navigation(self):
        self.control.tracking.update()
        self.uplink.archive_level = lambda room: 'private'
        with self.uplink.db:
            self.uplink.db.execute('INSERT INTO message_receipts(local_room_id,master_room_id,target,read,delivered,teammate_read,sent,failed,dirty) VALUES (?,?,?,1,1,0,1,0,1)', ('!person:local', '!mirror:master', self.sent))
        self.control.tracking.update(); result = self.result()
        self.assertNotEqual(result['delivery']['state'], 'read', 'Private must not publish new read evidence')
        self.assertIsNone(result['delivery']['mirror_event_id']); self.assertIsNone(result['delivery']['mirror_room_id'])
        self.assertIsNone(result['response']['incoming_event_id'])

    def test_tracking_storage_drops_unnecessary_message_body_fields(self):
        self.proof['events'][0]['body'] = 'private sentinel message text'
        self.control.tracking.update()
        for table in ['crm_tracking_scopes', 'crm_tracking_items', 'crm_tracking_corrections']:
            self.assertNotIn('private sentinel', json.dumps(self.uplink.db.execute('SELECT * FROM ' + table).fetchall()))


for name in vars(fixture.TrackingTests):
    if name.startswith('test_'): setattr(TrackingAdversarial, name, None)
if __name__ == '__main__': unittest.main()
