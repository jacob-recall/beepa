"""No providers: exact ordered obligations and scoped publication fixtures."""
import copy
import json
from pathlib import Path
import sqlite3
import sys
import types
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'agents/uplink'))
from conversation_tracking import ConversationTracking, project_conversation


def msg(n, incoming=False, **changes):
    return {'native_id': str(n), 'order': str(n), 'timestamp': n * 1000,
            'incoming': incoming, 'event_id': '$' + str(n), **changes}


def snap(events, **changes):
    return {'monitoring_ready': True, 'evidence_kind': 'native_complete', 'window_complete': True,
            'complete': True, 'has_more': False, 'outgoing_parts': {}, 'events': events,
            'room_id': '!local:test', 'source': 'imessage', 'bridge_epoch': 'bridge', **changes}


class ProjectionTests(unittest.TestCase):
    def test_each_direction_answers_latest_unanswered_and_keeps_older(self):
        p = project_conversation(None, snap([msg(1), msg(2), msg(3, True), msg(4, True), msg(5)]))
        messages = {m['event_id']: m for m in p['messages']}
        self.assertEqual(messages['$2']['answered_by'], '$3')
        self.assertEqual(messages['$1']['answered_by'], '$4')
        self.assertEqual(messages['$4']['answered_by'], '$5')
        self.assertIsNone(messages['$3']['answered_by'])
        self.assertIsNone(messages['$5']['answered_by'])

    def test_incoming_only_and_empty_native_eof(self):
        self.assertTrue(project_conversation(None, snap([]))['complete'])
        self.assertIsNone(project_conversation(None, snap([msg(1, True)]))['messages'][0]['answered_by'])

    def test_observed_empty_does_not_prove_uncontacted_native_conversation(self):
        p = project_conversation(None, snap([], evidence_kind='observed_native', window_complete=False,
                                          complete=False, observed_window_complete=True))
        self.assertFalse(p['complete']); self.assertEqual(p['coverage'], 'observed_history')

    def test_retained_overlap_restart_and_partial_baseline(self):
        first = project_conversation(None, snap([msg(1), msg(2)], has_more=True))
        self.assertFalse(first['complete']); self.assertEqual([m['event_id'] for m in first['messages']], ['$2'])
        second = project_conversation(json.loads(json.dumps(first)), snap([msg(2), msg(3, True)], has_more=True))
        self.assertEqual(second['messages'][0]['answered_by'], '$3')
        with self.assertRaisesRegex(ValueError, 'native-window-gap'):
            project_conversation(second, snap([msg(4)]))

    def test_native_backfill_rebuilds_links_without_arrival_order(self):
        first = project_conversation(None, snap([msg(1), msg(3, True)]))
        corrected = project_conversation(first, snap([msg(3, True), msg(2, True), msg(1)]))
        self.assertEqual(corrected['messages'][0]['answered_by'], '$2')

    def test_missing_conflicting_tied_unmapped_events_fail_closed(self):
        first = project_conversation(None, snap([msg(1), msg(2)]))
        cases = [snap([msg(2)]), snap([msg(1, True), msg(2)]), snap([msg(1, event_id='$new'), msg(2)])]
        for s in cases:
            with self.subTest(s=s), self.assertRaises(ValueError): project_conversation(first, s)
        for s in [snap([msg(1), msg(2, order='1')]), snap([msg(1), msg(2, timestamp=1)]),
                  snap([msg(1, event_id=None)]), snap([msg(1), msg(1)]), snap([], monitoring_ready=False)]:
            with self.subTest(s=s), self.assertRaises(ValueError): project_conversation(None, s)

    def test_exact_multipart_send_is_one_obligation(self):
        p = project_conversation(None, snap([msg(1, event_id=None), msg(2, event_id=None), msg(3, True)], outgoing_parts={'$send': ['1', '2']}))
        self.assertEqual(len(p['messages']), 2)
        self.assertEqual(p['messages'][0]['event_id'], '$send')
        self.assertEqual(p['messages'][0]['answered_by'], '$3')
        with self.assertRaises(ValueError): project_conversation(None, snap([msg(1)], outgoing_parts={'$send': ['1', 'missing']}))
        with self.assertRaises(ValueError): project_conversation(None, snap([msg(1), msg(2, True), msg(3)], outgoing_parts={'$send': ['1', '3']}))


class PublishTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:'); self.addCleanup(self.db.close)
        self.posts = []; self.proof = snap([msg(1), msg(2, True)])
        self.level = 'direct'; self.mapping = lambda mirror, event: '$mirror' + event[1:]
        self.control = types.SimpleNamespace(local_ready=True, connected=True, generation='gen', epoch='journal')
        self.reader = types.SimpleNamespace(conversation_snapshot=lambda *a, **kw: copy.deepcopy(self.proof))
        self.u = types.SimpleNamespace(db=self.db, cfg=types.SimpleNamespace(master_user='@owner:test'), _crm_control=self.control,
            _crm_native_history=self.reader, existing_mirror_ids=lambda: ['!local:test'],
            mirror_for=lambda room: ('!mirror:test', 'imessage', 0), mirror_status=lambda room: 'live',
            active_link_for_dispatch=lambda: True, archive_level=lambda room: self.level,
            delivery_for=lambda *a: self.mapping(*a), master=lambda *a, **kw: self.posts.append(a))
        self.tracking = ConversationTracking(self.u, clock=lambda: 100)

    def test_scoped_ids_only_and_durable_overlap(self):
        self.tracking.update_room('!local:test'); p = self.posts[0][2]
        self.assertEqual(p['state'], 'ready'); self.assertEqual(p['messages'][0]['answered_by'], '$mirror2')
        self.assertNotIn('native_id', json.dumps(p)); self.assertNotIn('body', json.dumps(p))
        self.proof = snap([msg(2, True), msg(3)], has_more=True)
        ConversationTracking(self.u, clock=lambda: 101).update_room('!local:test')
        self.assertEqual(len(self.posts[-1][2]['messages']), 3)

    def test_private_disconnected_unready_and_revoking_do_not_read_or_write(self):
        self.reader.conversation_snapshot = lambda *a, **kw: self.fail('Native read outside permission')
        self.level = 'private'; self.tracking.update_room('!local:test')
        self.level = 'share'; self.control.connected = False; self.tracking.update_room('!local:test')
        self.control.connected = True; self.control.local_ready = False; self.tracking.update_room('!local:test')
        self.control.local_ready = True; self.u.mirror_status = lambda room: 'revoking'; self.tracking.update_room('!local:test')
        self.assertEqual(self.posts, [])

    def test_revoke_or_generation_change_during_native_read_blocks_write(self):
        def read(*a, **kw): self.control.generation = 'new'; return self.proof
        self.reader.conversation_snapshot = read; self.tracking.update_room('!local:test'); self.assertEqual(self.posts, [])

    def test_unmapped_message_unknown_order_or_changed_epoch_is_reconciling(self):
        self.tracking.update_room('!local:test')
        self.proof['bridge_epoch'] = 'new'; self.tracking.update_room('!local:test')
        self.assertEqual(self.posts[-1][2]['state'], 'reconciling')
        self.proof['bridge_epoch'] = 'bridge'; self.mapping = lambda *a: None; self.tracking.update_room('!local:test')
        self.assertEqual(self.posts[-1][2]['messages'], [])

    def test_failed_remote_write_retains_native_overlap_locally(self):
        def fail(*a, **kw): raise OSError('offline')
        self.u.master = fail
        with self.assertRaises(OSError): self.tracking.update_room('!local:test')
        self.assertEqual(self.db.execute('SELECT count(*) FROM conversation_tracking').fetchone()[0], 1)

    def test_only_fresh_scoped_manager_list_hints_schedule_reads(self):
        self.u.cfg.manager_mxid = '@manager:test'
        self.u.meta_get = lambda key: '!proposals:test'
        verified = []
        self.control._verify_master_room = lambda room: verified.append(room)
        event = {'sender': '@manager:test', 'type': 'com.jkali.monitoring_scope', 'origin_server_ts': 100000,
                 'content': {'schema': 1, 'owner_mxid': '@owner:test', 'generation': 'gen', 'journal_epoch': 'journal', 'rooms': ['!local:test']}}
        self.u.master = lambda *a, **kw: {'chunk': [copy.deepcopy(event)]}
        reads = []
        self.tracking.update_room = lambda room: reads.append(room)
        self.tracking.update(); self.assertEqual(reads, ['!local:test']); self.assertEqual(verified, ['!proposals:test'])
        for patch in ({'rooms': []}, {'rooms': ['!foreign:test']}, {'generation': 'old'}, {'journal_epoch': 'old'}, {'owner_mxid': '@other:test'}):
            original = copy.deepcopy(event['content']); event['content'].update(patch)
            self.tracking.next_scan = 0; self.tracking.update(); event['content'] = original
        event['origin_server_ts'] = -300000; self.tracking.next_scan = 0; self.tracking.update()
        event['origin_server_ts'] = 100000; event['sender'] = '@imposter:test'; self.tracking.next_scan = 0; self.tracking.update()
        self.assertEqual(reads, ['!local:test'])

if __name__ == '__main__': unittest.main()
