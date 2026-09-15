#!/usr/bin/env python3
"""Real bridge/uplink/repair paths with synthetic source data; no native calls."""
import importlib.util
from pathlib import Path
import sys
import unittest
import urllib.error

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'shared'))
from message_timestamps import ORIGIN_TS, TS_SOURCE, native_metadata, valid_ts


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


bridge = module('timestamp_bridge', ROOT / 'tests/unit/imessage_durability.test.py')
uplink = module('timestamp_uplink', ROOT / 'tests/unit/uplink_durable_sync.test.py')
repair = module('timestamp_repair', ROOT / 'imessage/repair_timestamps.py')
NATIVE = 1704110400000
IMPORTED = 1788908400000


class BridgeTimestamps(bridge.DaemonTest):
    def test_native_time_survives_text_attachment_and_edit(self):
        calls = []
        self.d.mx = lambda method, path, body=None, **kw: calls.append(body) or {'event_id': '$fixture'}
        self.d.upload_media = lambda *a: 'mxc://test/file'
        m = self.message(timestamp=NATIVE, attachments=[self.attachment()], isSender=True)
        self.d._relay_message('chat', '!portal:test', 'Fixture', False, m)
        self.assertEqual([c[ORIGIN_TS] for c in calls], [NATIVE, NATIVE])
        self.assertTrue(all(c[TS_SOURCE] == 'imessage' for c in calls))
        m.update(text='edited', editedTimestamp=IMPORTED)
        self.d.reconcile_edit('chat', m)
        self.assertEqual(calls[-1]['m.new_content'][ORIGIN_TS], NATIVE)


class UplinkTimestamps(uplink.DurableSyncTests):
    def test_native_time_survives_delayed_delivery(self):
        self.seed()
        event = uplink.message('$native', ts=IMPORTED)
        event['content'].update(native_metadata({'timestamp': NATIVE}))
        self.u.forward_events(uplink.ROOM, uplink.MIRROR, [event])
        sent = [b for _, p, b in self.calls if '/send/m.room.message/' in p][-1]
        self.assertEqual(sent[ORIGIN_TS], NATIVE)

    def test_existing_local_correction_used_on_later_replay(self):
        self.seed()
        old = self.u.local
        self.u.local = lambda method, path, *a, **k: ({'version': 1, 'source': 'imessage', 'origin_ts': NATIVE}
            if '/state/com.beepa.timestamp_correction/' in path else old(method, path, *a, **k))
        self.u.forward_events(uplink.ROOM, uplink.MIRROR, [uplink.message('$old', ts=IMPORTED)])
        sent = [b for _, p, b in self.calls if '/send/m.room.message/' in p][-1]
        self.assertEqual(sent[ORIGIN_TS], NATIVE)

    def test_media_retry_keeps_native_time_in_replacement(self):
        self.seed()
        event = uplink.message('$media', ts=IMPORTED)
        event['content'].update(native_metadata({'timestamp': NATIVE}), msgtype='m.image', url='mxc://local/media')
        self.events['$media'] = event
        self.u._http_bytes = lambda *a, **k: (_ for _ in ()).throw(TimeoutError('fixture'))
        self.u.forward_events(uplink.ROOM, uplink.MIRROR, [event])
        self.u._reupload_media = lambda *a: 'mxc://master/recovered'
        self.u.retry_media_slice()
        replacement = self.calls[-1][2]
        self.assertEqual(replacement[ORIGIN_TS], NATIVE)
        self.assertEqual(replacement['m.new_content'][ORIGIN_TS], NATIVE)


class RepairTimestamps(unittest.TestCase):
    def test_history_pagination_recovers_older_mapped_ids(self):
        calls = []
        class Source:
            def cli_json(self, *args, **kwargs):
                calls.append(args)
                if '--before' in args:
                    return {'hasMore': False, 'items': [{'id': 'old', 'timestamp': NATIVE, 'cursor': '100'}]}
                return {'hasMore': True, 'items': [{'id': 'new', 'timestamp': IMPORTED, 'cursor': '200'}]}
        found, complete = repair.read_source_timestamps(Source(), 'chat', {'old', 'new'})
        self.assertEqual(found, {'old': NATIVE, 'new': IMPORTED})
        self.assertTrue(complete)
        self.assertEqual(calls[-1], ('messages', 'chat', '--before', '200'))

    def test_repeated_cursor_reports_incomplete_history(self):
        class Source:
            def cli_json(self, *args, **kwargs):
                return {'hasMore': True, 'items': [{'id': 'new', 'timestamp': IMPORTED, 'cursor': '200'}]}
        found, complete = repair.read_source_timestamps(Source(), 'chat', {'missing'})
        self.assertFalse(complete)
        self.assertEqual(found, {})

    def test_repair_is_state_only_idempotent_and_keeps_original(self):
        original = {'event_id': '$old', 'sender': '@owner:test', 'type': 'm.room.message',
                    'content': {'body': 'unchanged', 'msgtype': 'm.text'}}
        state, writes, journal = {}, [], []
        def request(method, path, body=None):
            if '/event/' in path:
                return original
            self.assertIn('/state/com.beepa.timestamp_correction/', path)
            if method == 'PUT':
                self.assertTrue(journal, 'journal must precede a write')
                writes.append(body)
                state[path] = body
            if path not in state:
                raise urllib.error.HTTPError('', 404, '', {}, None)
            return state[path]
        args = (request, '!room:test', '$old', NATIVE, lambda s: s == '@owner:test')
        self.assertEqual(repair.correction(*args), 'would_correct')
        self.assertFalse(writes)
        self.assertEqual(repair.correction(*args, apply=True, record=lambda *v: journal.append(v)), 'corrected')
        self.assertEqual(repair.correction(*args, apply=True, record=lambda *v: journal.append(v)), 'already_correct')
        self.assertEqual(len(writes), 1)
        self.assertEqual(original['content'], {'body': 'unchanged', 'msgtype': 'm.text'})
        self.assertEqual(repair.correction(request, '!room:test', '$old', NATIVE, lambda s: False), 'refused_target')

    def test_invalid_native_time_is_not_presented_as_original(self):
        for value in [True, '1704110400000', None, float('nan'), float('inf'), -1, 1.5]:
            self.assertFalse(valid_ts(value))
            self.assertEqual(native_metadata({'timestamp': value}), {TS_SOURCE: 'unknown'})


if __name__ == '__main__':
    unittest.main()
