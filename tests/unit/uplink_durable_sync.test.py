#!/usr/bin/env python3
"""Offline lifecycle regressions: no running Matrix, contacts or real messages."""
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import urllib.error
import urllib.parse

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'agents/uplink'))
import uplink

ROOM = '!source:local'
MIRROR = '!mirror:master'


def message(eid, ts=1):
    return dict(event_id=eid, type='m.room.message', sender='@alice:local',
                origin_server_ts=ts, content={'msgtype': 'm.text', 'body': 'fixture'})


class DurableSyncTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.u = uplink.Uplink(uplink.Config(dict(
            LOCAL_HS_URL='http://local.invalid', LOCAL_USER='@alice:local',
            LOCAL_TOKEN='fixture', MASTER_HS_URL='https://master.invalid',
            MASTER_USER='@alice:master', MASTER_TOKEN='fixture',
            MASTER_SPACE='!space:master', MANAGER_MXID='@manager:master',
            UPLINK_DB=str(Path(self.tmp.name) / 'state.db'))))
        self.addCleanup(lambda: self.u.db.close())
        self.level = 'share'
        self.calls = []
        self.events = {}
        self.link = {'master_hs_url': 'https://master.invalid', 'master_token': 'fixture',
                     'master_user': '@alice:master', 'master_space': '!space:master',
                     'manager_mxid': '@manager:master'}
        self.u.local = self.local
        self.u.master = self.master

    def local(self, method, path, body=None, query=None, **kwargs):
        if path.endswith('/account_data/com.jkali.master_link'):
            if method == 'PUT':
                self.link = body
            return self.link
        if path.endswith('/account_data/com.jkali.share_override'):
            return {'state': self.level}
        if '/event/' in path:
            return self.events[urllib.parse.unquote(path.rsplit('/', 1)[1])]
        if '/state/m.room.member/' in path:
            return {'displayname': 'Fixture'}
        raise AssertionError((method, path, query))

    def master(self, method, path, body=None, query=None, **kwargs):
        self.calls.append((method, path, body))
        if path.endswith('/createRoom'):
            return {'room_id': MIRROR}
        if path.endswith('/joined_rooms'):
            return {'joined_rooms': []}
        return {'event_id': '$remote-' + str(len(self.calls))}

    def seed(self):
        self.u.db.execute('INSERT INTO mirror_rooms(local_room_id,master_room_id,source) VALUES (?,?,?)',
                          (ROOM, MIRROR, 'imessage'))
        self.u.db.commit()

    def test_empty_link_is_explicit_disconnect_even_with_env(self):
        self.link = {}
        self.assertFalse(self.u.refresh_master_config())
        self.assertEqual(self.u.meta_get('link_disabled'), '1')
        self.u.local = lambda *a, **k: (_ for _ in ()).throw(urllib.error.HTTPError('', 404, '', {}, None))
        self.assertFalse(self.u.refresh_master_config())

    def test_failed_revocation_retains_retryable_row_and_blocks_dispatch(self):
        self.seed()
        self.u.master = lambda *a, **k: (_ for _ in ()).throw(urllib.error.HTTPError('', 503, '', {}, None))
        with self.assertRaises(urllib.error.HTTPError):
            self.u.delete_mirror(ROOM)
        self.assertIsNotNone(self.u.mirror_for(ROOM))
        self.assertEqual(self.u.mirror_status(ROOM), 'revoking')
        self.assertEqual(self.u.forward_events(ROOM, MIRROR, [message('$blocked')]), 0)
        self.u.master = self.master
        self.u.delete_mirror(ROOM)
        self.assertIsNone(self.u.mirror_for(ROOM))

    def test_reshare_dedup_is_per_destination_room(self):
        self.seed()
        self.assertEqual(self.u.forward_events(ROOM, MIRROR, [message('$same')]), 1)
        self.u.delete_mirror(ROOM)
        self.u.db.execute('INSERT INTO mirror_rooms(local_room_id,master_room_id,source) VALUES (?,?,?)',
                          (ROOM, '!replacement:master', 'imessage'))
        self.u.db.commit()
        self.assertEqual(self.u.forward_events(ROOM, '!replacement:master', [message('$same')]), 1)

    def test_create_persists_room_before_link_failure(self):
        def fail_link(method, path, *args, **kwargs):
            if method == 'PUT':
                raise uplink.MasterUnreachable('fixture interruption')
            return self.master(method, path, *args, **kwargs)
        self.u.master = fail_link
        with self.assertRaises(uplink.MasterUnreachable):
            self.u.create_mirror(ROOM, 'imessage', 'Fixture')
        self.assertEqual(self.u.mirror_for(ROOM)[0], MIRROR)
        self.assertEqual(self.u.mirror_status(ROOM), 'linking')

    def test_reference_queue_survives_master_offline_and_private_suppresses_it(self):
        self.seed()
        event = message('$queued')
        self.u.enqueue_events(ROOM, MIRROR, [event])
        self.assertEqual(self.u.db.execute('SELECT count(*) FROM pending_events').fetchone()[0], 1)
        columns = [r[1] for r in self.u.db.execute('PRAGMA table_info(pending_events)')]
        self.assertNotIn('content', columns)
        self.level = 'private'
        self.u.deliver_pending()
        self.assertFalse(any('/send/' in p for _, p, _ in self.calls))

    def test_epoch_change_invalidates_archive_not_direct_ledger(self):
        self.link['master_data_epoch'] = 'original-1'
        self.assertTrue(self.u.refresh_master_config())
        self.seed()
        self.u.db.execute("INSERT INTO proposal_map VALUES ('$proposal','$record','sent')")
        self.u.db.execute("INSERT INTO direct_send_log VALUES (1,'hash')")
        self.u.db.commit()
        self.link['master_data_epoch'] = 'restored-2'
        self.assertTrue(self.u.refresh_master_config())
        self.assertEqual(self.u.existing_mirror_ids(), [])
        self.assertEqual(self.u.db.execute('SELECT outcome FROM proposal_map').fetchone()[0], 'sent')
        self.assertEqual(self.u.db.execute('SELECT count(*) FROM direct_send_log').fetchone()[0], 1)
        self.assertEqual(self.u.db.execute('SELECT master_room_id FROM retired_mirrors').fetchone()[0], MIRROR)

    def test_nested_community_uses_active_edges_and_resists_cycles(self):
        def room(events):
            return {'state': {'events': events}}
        def edge(target):
            return {'type': 'm.space.child', 'state_key': target, 'content': {'via': ['local']}}
        data = {'rooms': {'join': {
            '!root:local': room([{'type': 'm.room.name', 'content': {'name': 'WhatsApp'}}, edge('!community:local')]),
            '!community:local': room([edge(ROOM), edge('!root:local')]),
            ROOM: room([]),
        }}}
        self.assertEqual(self.u.sources_from_sync(data)[ROOM], 'whatsapp')
        data['rooms']['join']['!community:local']['timeline'] = {'events': [
            {'type': 'm.space.child', 'state_key': ROOM, 'content': {}}]}
        self.assertNotIn(ROOM, self.u.sources_from_sync(data))

    def test_history_default_is_unlimited_with_bounded_pages(self):
        self.assertEqual(self.u.cfg.backfill, 0)
        self.assertLessEqual(self.u.cfg.history_page_size, 200)

    def test_first_reconcile_runs_even_when_macos_monotonic_starts_at_zero(self):
        self.assertGreaterEqual(0 - self.u._last_reconcile, self.u.cfg.reconcile_ms / 1000)

    def test_transient_consent_read_does_not_convert_to_durable_revocation(self):
        self.seed()
        base = self.local
        def local(method, path, *a, **kw):
            if path.endswith('/account_data/com.jkali.share_override'):
                raise urllib.error.HTTPError('fixture', 503, 'retry', {}, None)
            return base(method, path, *a, **kw)
        self.u.local = local
        with self.assertRaises(urllib.error.HTTPError):
            self.u.forward_events(ROOM, MIRROR, [message('$retry')])
        self.assertNotEqual(self.u.mirror_status(ROOM), 'revoking')

    def test_paginated_history_survives_restart_and_delivers_source_order(self):
        self.seed()
        self.events = {eid: message(eid, ts) for ts, eid in enumerate(['$old', '$middle', '$new'], 1)}
        base_local = self.local
        pages = []
        def local(method, path, body=None, query=None, **kw):
            if path.endswith('/messages'):
                pages.append((query or {}).get('from'))
                if not (query or {}).get('from'):
                    return {'chunk': [self.events['$new'], self.events['$middle']], 'end': 'opaque-page2'}
                return {'chunk': [self.events['$old']]}
            return base_local(method, path, body, query, **kw)
        self.u.local = local
        self.u.backfill(ROOM, MIRROR)
        self.u.history_slice()
        self.assertEqual(self.u.db.execute('SELECT count(*) FROM pending_events').fetchone()[0], 0)
        path = self.u.cfg.db_path
        self.u.db.close()
        self.u.db = self.u._open_db(path)
        self.u.history_slice()
        self.u.deliver_pending()
        self.assertEqual(pages, [None, 'opaque-page2'])
        sent = [urllib.parse.unquote(p.rsplit('/', 1)[1]) for _, p, _ in self.calls if '/send/' in p]
        self.assertEqual(sent, ['uplink_$old', 'uplink_$middle', 'uplink_$new'])

    def test_limited_batch_records_gap_before_advancing_ingestion_offline(self):
        self.seed()
        self.u.meta_set('sync_since', 'old-sync')
        base = self.local
        def local(method, path, body=None, query=None, **kw):
            if path.endswith('/sync'):
                return {'next_batch': 'new-sync', 'rooms': {'join': {ROOM: {
                    'timeline': {'limited': True, 'prev_batch': 'opaque-gap', 'events': [message('$live')]}}}}}
            return base(method, path, body, query, **kw)
        self.u.local = local
        self.u.master = lambda *a, **k: (_ for _ in ()).throw(uplink.MasterUnreachable('offline'))
        self.u.tail_once()
        self.assertEqual(self.u.meta_get('sync_since'), 'new-sync')
        self.assertEqual(self.u.db.execute('SELECT cursor FROM history_jobs').fetchone()[0], 'opaque-gap')
        self.assertEqual(self.u.db.execute('SELECT local_event_id FROM pending_events').fetchone()[0], '$live')

    def test_current_batch_private_precedes_new_messages(self):
        self.seed()
        base = self.local
        def local(method, path, body=None, query=None, **kw):
            if path.endswith('/sync'):
                return {'next_batch': 'new', 'rooms': {'join': {ROOM: {
                    'account_data': {'events': [{'type': 'com.jkali.share_override', 'content': {'state': 'private'}}]},
                    'timeline': {'events': [message('$private')]}}}}}
            return base(method, path, body, query, **kw)
        self.u.local = local
        self.u.tail_once()
        self.assertEqual(self.u.mirror_status(ROOM), 'revoking')
        self.assertEqual(self.u.db.execute('SELECT count(*) FROM pending_events').fetchone()[0], 0)

    def test_empty_page_new_cursor_continues_repeated_cursor_is_incomplete(self):
        self.seed()
        self.u.schedule_history(ROOM, MIRROR, 'a')
        base = self.local
        self.u.local = lambda method, path, body=None, query=None, **kw: (
            {'chunk': [], 'end': 'b'} if path.endswith('/messages') else base(method, path, body, query, **kw))
        self.u.history_slice()
        self.assertEqual(self.u.db.execute('SELECT cursor,status FROM history_jobs').fetchone(), ('b', 'discover'))
        self.u.history_slice()
        self.assertEqual(self.u.db.execute('SELECT status,error FROM history_jobs').fetchone(), ('incomplete', 'repeated_cursor'))

    def test_consent_rechecked_between_event_dispatches(self):
        self.seed()
        def master(*args, **kwargs):
            result = self.master(*args, **kwargs)
            if '/send/' in args[1]:
                self.level = 'private'
            return result
        self.u.master = master
        self.assertEqual(self.u.forward_events(ROOM, MIRROR, [message('$first'), message('$second')]), 1)
        self.assertEqual(len([p for _, p, _ in self.calls if '/send/' in p]), 1)

    def test_transient_contact_failure_does_not_block_other_stages(self):
        import sqlite3
        def contacts():
            raise sqlite3.OperationalError('fixture lock')
        self.assertFalse(self.u.run_stage('contacts', contacts))
        ran = []
        self.assertTrue(self.u.run_stage('ingestion', lambda: ran.append(True)))
        self.assertEqual(ran, [True])

    def test_429_body_delay_is_honored(self):
        error = urllib.error.HTTPError('fixture', 429, 'slow', {}, io.BytesIO(b'{"retry_after_ms": 85000}'))
        self.assertEqual(self.u.retry_delay(error), 85)

    def test_imessage_from_me_requires_attested_bot_and_true_boolean(self):
        self.seed()
        for sender, declared, expected in [('@imessagebot:local', True, True),
                                            ('@mallory:local', True, False),
                                            ('@imessagebot:local', 'true', False)]:
            event = message('$attribution-' + str(len(self.calls)))
            event['sender'] = sender
            event['content']['com.jkali.from_me'] = declared
            self.u.forward_events(ROOM, MIRROR, [event])
            self.assertIs(self.calls[-1][2]['com.jkali.from_me'], expected)

    def test_lost_create_response_adopts_generation_instead_of_second_room(self):
        created = {}
        def master(method, path, body=None, **kwargs):
            if path.endswith('/createRoom'):
                self.assertFalse(created, 'must not allocate twice')
                created.update(body)
                raise uplink.MasterUnreachable('response lost after create')
            if '/directory/room/' in path:
                return {'room_id': MIRROR}
            if path.endswith('/state/com.beepa.mirror_generation'):
                return next(e['content'] for e in created['initial_state'] if e['type'] == 'com.beepa.mirror_generation')
            return self.master(method, path, body, **kwargs)
        self.u.master = master
        with self.assertRaises(uplink.MasterUnreachable):
            self.u.create_mirror(ROOM, 'imessage', 'Fixture')
        self.assertIsNone(self.u.mirror_for(ROOM))
        self.assertEqual(self.u.create_mirror(ROOM, 'imessage', 'Fixture'), MIRROR)

    def test_rotation_after_lost_response_is_not_blindly_reposted(self):
        self.seed()
        event = message('$uncertain')
        self.u.enqueue_events(ROOM, MIRROR, [event])
        self.u.master = lambda *a, **k: (_ for _ in ()).throw(uplink.MasterUnreachable('response lost'))
        with self.assertRaises(uplink.MasterUnreachable):
            self.u.forward_events(ROOM, MIRROR, [event])
        self.u.cfg.master_token = 'rotated-fixture-token'
        self.link['master_token'] = 'rotated-fixture-token'
        self.u.master = self.master
        with self.assertRaises(ValueError):
            self.u.forward_events(ROOM, MIRROR, [event])
        self.assertFalse(self.calls)
        self.assertEqual(self.u.db.execute('SELECT error FROM pending_events').fetchone()[0], 'ambiguous_credentials')

    def test_retired_organization_cleanup_survives_new_pairing(self):
        self.u.refresh_master_config()
        self.seed()
        self.link = dict(self.link, master_hs_url='https://second.invalid', master_token='new-fixture')
        self.u.refresh_master_config()
        connection = json.loads(self.u.db.execute('SELECT connection FROM retired_mirrors').fetchone()[0])
        self.assertEqual(connection['master_hs'], 'https://master.invalid')
        self.assertEqual(connection['master_token'], 'fixture')
        calls = []
        self.u.retired_request = lambda connection, *args: calls.append(connection['master_hs'])
        self.u.retry_retired_mirrors()
        self.assertEqual(calls, ['https://master.invalid'] * 3)

    def test_failed_cleanup_does_not_starve_other_rooms(self):
        self._cleanup_fairness(retired=False)

    def test_failed_retired_cleanup_does_not_starve_other_rooms(self):
        self._cleanup_fairness(retired=True)

    def _cleanup_fairness(self, retired):
        self.u.refresh_master_config()
        self.seed()
        second = '!second:local'
        self.u.db.execute('INSERT INTO mirror_rooms(local_room_id,master_room_id,source) VALUES (?,?,?)',
                          (second, '!second:master', 'imessage'))
        self.u.db.commit()
        if retired:
            self.link = dict(self.link, master_hs_url='https://second.invalid', master_token='new-fixture')
            self.u.refresh_master_config()
            retry, table, stage = self.u.retry_retired_mirrors, 'retired_mirrors', 'retired_revocation'
        else:
            self.u.mark_revoking(ROOM)
            self.u.mark_revoking(second)
            retry, table, stage = self.u.retry_revocations, 'mirror_lifecycle', 'revocation'
        requests = []
        def request(method, path, body=None, **kwargs):
            requests.append(path)
            if urllib.parse.quote(MIRROR, safe='') in path:
                raise urllib.error.HTTPError('', 403, '', {}, None)
            return {}
        self.u.master = request
        self.u.retired_request = lambda connection, *a, **k: request(*a, **k)
        with patch('time.time', return_value=1000):
            self.assertTrue(self.u.run_stage(stage, retry))
            self.assertTrue(self.u.run_stage(stage, retry))
            for _ in range(10):
                self.u.run_stage(stage, retry)
        self.assertEqual(len(requests), 4, 'one denied request plus three successful cleanup steps')
        self.assertEqual(self.u.db.execute('SELECT count(*) FROM ' + table).fetchone()[0], 1)
        self.assertEqual(self.u.sync_health()['errors'][stage], 'HTTPError')
        # Durable deadlines survive restart and honor explicit server limits.
        replacement = uplink.Uplink(self.u.cfg)
        self.addCleanup(replacement.db.close)
        self.u.db.close()
        self.u = replacement
        self.u.master = request
        self.u.retired_request = lambda connection, *a, **k: request(*a, **k)
        retry = self.u.retry_retired_mirrors if retired else self.u.retry_revocations
        with patch('time.time', return_value=1000):
            retry()
        self.assertEqual(len(requests), 4)
        def limited(*args, **kwargs):
            raise urllib.error.HTTPError('', 429, '', {'Retry-After': '120'}, None)
        self.u.master = self.u.retired_request = limited
        with patch('time.time', return_value=1100):
            retry()
        self.assertEqual(self.u.db.execute('SELECT next_attempt FROM ' + table).fetchone()[0], 1220)
        self.u.master = request
        self.u.retired_request = lambda connection, *a, **k: request(*a, **k)
        with patch('time.time', return_value=1219):
            retry()
        self.assertEqual(len(requests), 4)
        self.u.master = lambda *a, **k: {}
        self.u.retired_request = lambda *a, **k: {}
        with patch('time.time', return_value=1220):
            retry()
        self.assertEqual(self.u.db.execute('SELECT count(*) FROM ' + table).fetchone()[0], 0)
        self.assertNotIn(stage, self.u.sync_health()['errors'])

    def test_first_epoch_adoption_preserves_existing_mirrors(self):
        self.u.refresh_master_config()
        self.seed()
        self.link.update(master_authority_id='authority-1', master_data_epoch='epoch-1')
        self.u.refresh_master_config()
        self.assertEqual(self.u.mirror_for(ROOM)[0], MIRROR)

    def test_scoped_recovery_preserves_pairing_and_never_runs_disabled(self):
        self.link['master_enroll_url'] = 'https://master.invalid'
        self.u.refresh_master_config()
        requests = []
        def recovery(path, payload=None, bearer=None):
            requests.append((path, payload, bearer))
            if path == '/enroll/recovery':
                return dict(self.link, master_token='recovered-fixture', master_data_epoch='epoch-2', master_authority_id='authority-1')
            return {'master_authority_id': 'authority-1', 'master_data_epoch': 'epoch-1'}
        self.u.recovery_request = recovery
        self.u.refresh_recovery()
        self.assertEqual(requests[1][2], 'fixture')
        self.assertGreaterEqual(len(requests[1][1]['recovery_token']), 32)
        self.assertTrue(self.u.recover_master())
        self.assertEqual(self.link['master_token'], 'fixture')
        self.assertTrue(self.u.refresh_master_config())
        self.assertEqual(self.u.cfg.master_token, 'recovered-fixture')
        before = len(requests)
        self.u.meta_set('link_disabled', '1')
        self.assertFalse(self.u.recover_master())
        self.assertEqual(len(requests), before)

    def test_recovery_refuses_other_account_and_changed_authority(self):
        self.u.meta_set('recovery_issued', '1')
        self.u.meta_set('recovery_authority', 'expected')
        self.u.recovery_request = lambda *a, **k: dict(self.link, master_authority_id='expected',
            master_data_epoch='new', master_user='@someone-else:master')
        with self.assertRaises(ValueError):
            self.u.recover_master()
        self.assertEqual(self.u.cfg.master_token, 'fixture')

    def test_transient_media_failure_queues_retry_and_edits_placeholder(self):
        self.seed()
        event = message('$media')
        event['content'].update(msgtype='m.image', url='mxc://local/media')
        self.events['$media'] = event
        self.u._http_bytes = lambda *a, **k: (_ for _ in ()).throw(TimeoutError('fixture'))
        self.u.forward_events(ROOM, MIRROR, [event])
        self.assertEqual(self.u.db.execute('SELECT count(*) FROM media_retry').fetchone()[0], 1)
        self.u._reupload_media = lambda *a: 'mxc://master/recovered'
        self.u.retry_media_slice()
        replacement = self.calls[-1][2]
        self.assertEqual(replacement['m.relates_to']['rel_type'], 'm.replace')
        self.assertTrue(replacement['m.new_content']['com.jkali.from_me'])
        self.assertEqual(self.u.db.execute('SELECT count(*) FROM media_retry').fetchone()[0], 0)

    def test_edit_payload_uses_destination_target_and_trusted_inner_metadata(self):
        self.seed()
        self.u.forward_events(ROOM, MIRROR, [message('$original')])
        target = self.u.delivery_for(MIRROR, '$original')
        edit = message('$edit')
        edit['sender'] = '@other:local'
        edit['content'].update({'m.relates_to': {'rel_type': 'm.replace', 'event_id': '$original'},
                               'm.new_content': {'msgtype': 'm.text', 'body': 'edited', 'com.jkali.from_me': True}})
        self.u.forward_events(ROOM, MIRROR, [edit])
        content = self.calls[-1][2]
        self.assertEqual(content['m.relates_to']['event_id'], target)
        self.assertIs(content['m.new_content']['com.jkali.from_me'], False)
        self.assertEqual(content['m.new_content']['com.jkali.source'], 'imessage')

    def test_worker_runtime_write_cannot_overwrite_concurrent_disconnect(self):
        self.u.refresh_master_config()
        self.u.cfg.master_token = 'recovered'
        original_meta_set = self.u.meta_set
        def interleave(key, value):
            if key == 'master_runtime':
                self.link = {'enabled': False}  # user writes AFTER worker GET
            return original_meta_set(key, value)
        self.u.meta_set = interleave
        self.assertTrue(self.u.persist_master_runtime())
        self.assertEqual(self.link, {'enabled': False})
        self.assertFalse(self.u.refresh_master_config())
        self.assertFalse(self.u.active_link_for_dispatch())

    def test_old_runtime_overlay_never_overrides_new_pairing(self):
        self.u.refresh_master_config()
        self.u.cfg.master_token = 'recovered-old'
        self.u.persist_master_runtime()
        self.link = dict(self.link, master_hs_url='https://new.invalid', master_token='new-user-token')
        self.assertTrue(self.u.refresh_master_config())
        self.assertEqual(self.u.cfg.master_hs, 'https://new.invalid')
        self.assertEqual(self.u.cfg.master_token, 'new-user-token')

    def test_wire_version_legacy_accepted_unknown_refused_before_mutation(self):
        self.u.validate_wire_version({})
        self.u.validate_wire_version({'wire_version': 1})
        with self.assertRaises(ValueError):
            self.u.validate_wire_version({'wire_version': 2})


if __name__ == '__main__':
    unittest.main()
