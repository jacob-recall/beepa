#!/usr/bin/env python3
"""Real daemon handlers, isolated SQLite/config; no native executable or live hub."""
import importlib.util
import io
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]


class DaemonTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        # Copying permits a safe regression run against the old import-time I/O.
        shutil.copyfile(ROOT / 'imessage/daemon.py', root / 'daemon.py')
        self.config = dict(port=29350, hs_url='http://test.invalid', domain='test',
                           user_id='@owner:test', bot_id='@imessagebot:test',
                           as_token='fake-as', hs_token='fake-hs', cli_path='/not-a-native-cli',
                           attachment_allow_prefixes=[str(root)], backfill_count=0)
        (root / 'daemon.json').write_text(json.dumps(self.config))
        (root / 'tmp').mkdir()
        spec = importlib.util.spec_from_file_location('isolated_imessage', root / 'daemon.py')
        self.d = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.d)
        if hasattr(self.d, 'initialize'):
            self.d.initialize(self.config, state_dir=str(root))
        self.addCleanup(lambda: self.d.DB.close())
        self.real_ensure_portal = self.d.ensure_portal
        self.d.ensure_portal = lambda *a: '!portal:test'
        self.d.ensure_ghost = lambda *a: '@contact:test'
        self.d.ghost_join = lambda *a: None
        self.d.map_add('chat', '!portal:test')
        self.d.cli_json = lambda *a, **k: {'items': [self.message()]}
        self.d._runner = lambda *a, **k: self.fail('unexpected native CLI call')
        self.d.mx = lambda *a, **k: self.fail('unexpected Matrix call')

    def message(self, **kwargs):
        return dict(id='message1', text='hello', senderID='contact', **kwargs)

    def event(self, eid='$event'):
        return dict(event_id=eid, sender='@owner:test', room_id='!portal:test',
                    type='m.room.message', content={'msgtype': 'm.text', 'body': 'hello'})

    def transaction(self, events, txn='transaction1'):
        payload = json.dumps({'events': events}).encode()
        handler = object.__new__(self.d.Handler)
        handler.path = '/_matrix/app/v1/transactions/' + txn
        handler.headers = {'Host': '127.0.0.1:29350', 'Authorization': 'Bearer fake-hs',
                           'Content-Length': str(len(payload))}
        handler.rfile = io.BytesIO(payload)
        replies = []
        handler._reply = lambda status, body: replies.append(status)
        handler.do_PUT()
        return replies[-1]

    def test_inbound_retry_after_matrix_failure(self):
        calls = []
        def matrix(*args, **kwargs):
            calls.append(args)
            if len(calls) == 1:
                raise OSError('offline')
            return {'event_id': '$accepted'}
        self.d.mx = matrix
        for _ in range(2):
            try:
                self.d.handle_chat_delta({}, 'chat')
            except OSError:
                pass
        self.assertEqual(len(calls), 2)
        self.assertEqual(self.d.event_map_get('chat', 'message1')[0], '$accepted')

    def test_outbound_known_failure_is_not_acknowledged_as_done(self):
        self.d.engine_send = lambda *a: False
        self.assertEqual(self.transaction([self.event()]), 503)
        self.assertFalse(self.d.txn_seen('transaction1'))

    def test_event_replay_in_different_transaction_does_not_send_twice(self):
        calls = []
        self.d.rate_ok = lambda *a: True  # Do not mistake rate refusal for dedup.
        self.d.engine_send = lambda *a: calls.append(a) or True
        self.assertEqual(self.transaction([self.event()]), 200)
        self.assertEqual(self.transaction([self.event()], 'other'), 200)
        self.assertEqual(len(calls), 1)

    def test_initial_backfill_cap_does_not_record_completion(self):
        self.d.BACKFILL_COUNT = 25
        self.d._portal_backfill_ok = lambda: False
        self.d.maybe_backfill('chat', '!portal:test', 'Contact', False)
        self.assertNotEqual(self.d.meta_get('backfill:chat:!portal:test'), '1')

    def test_import_does_not_touch_configuration_database_or_native_cli(self):
        spec = importlib.util.spec_from_file_location('inert_imessage', ROOT / 'imessage/daemon.py')
        module = importlib.util.module_from_spec(spec)
        with patch('builtins.open', side_effect=AssertionError('configuration read')), \
                patch('sqlite3.connect', side_effect=AssertionError('database open')), \
                patch('subprocess.run', side_effect=AssertionError('native invocation')):
            spec.loader.exec_module(module)
        self.assertIsNone(module.DB)

    def attachment(self):
        path = Path(self.tmp.name) / 'picture.png'
        path.write_bytes(b'fixture')
        return {'id': 'attachment1', 'srcURL': 'asset://test/' + str(path).encode().hex()}

    def test_partial_success_retries_only_failed_component(self):
        self.d.upload_media = lambda *a: 'mxc://test/upload'
        m = self.message(attachments=[self.attachment()])
        self.d.cli_json = lambda *a, **k: {'items': [m]}
        calls = []
        def matrix(method, path, content, **kwargs):
            calls.append(content['msgtype'])
            if content['msgtype'] == 'm.text' and calls.count('m.text') == 1:
                raise OSError('offline')
            return {'event_id': '$' + content['msgtype']}
        self.d.mx = matrix
        self.assertFalse(self.d.handle_chat_delta({}, 'chat'))
        self.assertTrue(self.d.handle_chat_delta({}, 'chat'))
        self.assertEqual(calls, ['m.image', 'm.text', 'm.text'])
        self.assertEqual(self.d.event_map_get('chat', 'message1')[0], '$m.text')

    def test_text_success_attachment_failure_retains_text_receipt(self):
        calls = []
        att = self.attachment()
        self.d.cli_json = lambda *a, **k: {'items': [self.message(attachments=[att])]}
        self.d.upload_media = lambda *a: (_ for _ in ()).throw(OSError('upload unavailable'))
        self.d.mx = lambda method, path, content, **k: calls.append(content['msgtype']) or {'event_id': '$text'}
        self.assertFalse(self.d.handle_chat_delta({}, 'chat'))
        self.d.upload_media = lambda *a: 'mxc://test/upload'
        self.assertTrue(self.d.handle_chat_delta({}, 'chat'))
        self.assertEqual(calls, ['m.text', 'm.image'])
        self.assertEqual(self.d.event_map_get('chat', 'message1')[0], '$text')

    def test_response_loss_reuses_same_matrix_transaction(self):
        requests, visible = [], {}
        def matrix(method, path, content, **kwargs):
            requests.append(path)
            visible.setdefault(path, '$delivered')
            if len(requests) == 1:
                raise OSError('response lost after acceptance')
            return {'event_id': visible[path]}
        self.d.mx = matrix
        self.assertFalse(self.d.handle_chat_delta({}, 'chat'))
        self.assertTrue(self.d.handle_chat_delta({}, 'chat'))
        self.assertEqual(len(visible), 1)
        self.assertEqual(requests[0], requests[1])

    def test_pending_message_retried_when_chat_timestamp_unchanged(self):
        self.d.sync_portal_name = lambda *a: None
        self.d.cli_json = lambda command, *a: {'items': [{'id': 'chat', 'timestamp': 'same'}] if command == 'chats' else [self.message()]}
        self.d.mx = lambda *a, **k: (_ for _ in ()).throw(OSError('offline'))
        self.d.poll_once()
        self.assertIsNone(self.d.meta_get('cursor:chat'))
        self.d.mx = lambda *a, **k: {'event_id': '$delivered'}
        self.d.poll_once()
        self.assertEqual(self.d.meta_get('cursor:chat'), 'same')
        self.assertFalse(self.d.chat_pending('chat'))

    def test_unchanged_marker_late_older_inbound_self_echo_is_received_once(self):
        self.d.sync_portal_name = lambda *a: None
        outgoing = dict(id='outgoing', text='nonce', senderID='self', isSender=True, timestamp=200)
        incoming = dict(id='incoming', text='nonce', senderID='self', isSender=False, timestamp=199)
        history, reads, sent = [outgoing], [], []
        def source(command, *args):
            if command == 'chats':
                return {'items':[{'id':'chat','timestamp':200}]}
            reads.append(args[0])
            return {'items':list(history)}
        self.d.cli_json = source
        self.d.ledger_add('chat\0nonce')
        self.d.mx = lambda method, path, body, **kwargs: sent.append(body) or {'event_id':'$received'}
        with patch.object(self.d.time, 'monotonic', return_value=100) as clock:
            self.d.poll_once()
            self.assertEqual(self.d.meta_get('cursor:chat'), '200')
            self.assertEqual(sent, [])
            history.append(incoming)
            clock.return_value = 110
            self.d.poll_once()
            self.assertEqual(len(reads), 1)
            clock.return_value = 131
            self.d.poll_once()
            self.assertEqual(len(sent), 1)
            self.assertFalse(sent[0].get('com.jkali.from_me', False))
            clock.return_value = 162
            self.d.poll_once()
        self.assertEqual(len(sent), 1)
        self.assertEqual(self.d.event_map_get('chat','incoming')[0], '$received')

    def test_periodic_tail_budget_and_failed_source_do_not_starve_other_chats(self):
        self.d.sync_portal_name = lambda *a: None
        self.d.CFG['tail_rescan_chats_per_poll'] = 1
        self.d.CFG['tail_rescan_seconds'] = 5
        chats = [{'id':name,'timestamp':200} for name in ('first','second','third')]
        for chat in chats:
            self.d.meta_set('cursor:'+chat['id'], '200')
        attempts = []
        def source(command, *args):
            if command == 'chats':
                return {'items':chats}
            attempts.append(args[0])
            if args[0] == 'first':
                raise OSError('source unavailable')
            return {'items':[]}
        self.d.cli_json = source
        with patch.object(self.d.time, 'monotonic', return_value=100) as clock:
            for tick in (100,101,102):
                clock.return_value = tick
                before = len(attempts)
                self.d.poll_once()
                self.assertEqual(len(attempts), before+1)
            self.assertEqual(attempts, ['first','second','third'])
            self.assertNotIn('first', self.d._tail_scan_success)
            clock.return_value = 103
            self.d.poll_once()
            self.assertEqual(attempts[-1], 'first')
            self.assertEqual(self.d.meta_get('cursor:first'), '200')
            self.assertEqual(self.d._tail_scan_success['second'], 101)

    def test_more_than_25_messages_resume_in_bounded_slices(self):
        messages = [dict(id='m%d' % i, text='hello', senderID='contact') for i in range(61)]
        self.d.CFG['poll_message_budget'] = 20
        self.d.cli_json = lambda *a, **k: {'items': messages}
        sent = []
        self.d.mx = lambda method, path, body, **k: sent.append(path) or {'event_id': '$' + str(len(sent))}
        for expected in (False, False, False, True):
            self.assertIs(self.d.handle_chat_delta({}, 'chat'), expected)
        self.assertEqual(len(sent), 61)
        self.assertEqual(len(set(sent)), 61)

    def test_missing_source_stays_visible_incomplete(self):
        self.d.inbound_pending('chat', 'no-longer-returned')
        self.d.cli_json = lambda *a, **k: {'items': []}
        self.assertFalse(self.d.handle_chat_delta({}, 'chat'))
        self.assertEqual(self.d.delivery_status()['inbound']['source_unavailable'], 1)

    def test_reaction_failure_retries_with_stable_transaction(self):
        self.d.event_map_put('chat', 'message1', '$original', '@contact:test', self.d.sha('hello'))
        m = self.message(reactions=[{'id': 'reaction1', 'reactionKey': 'heart', 'participantID': 'contact'}])
        paths = []
        def matrix(method, path, body, **kwargs):
            paths.append(path)
            if len(paths) == 1:
                raise OSError('offline')
            return {'event_id': '$reaction'}
        self.d.mx = matrix
        with self.assertRaises(OSError):
            self.d.reconcile_reactions('chat', m)
        self.d.reconcile_reactions('chat', m)
        self.assertEqual(paths[0], paths[1])
        self.assertTrue(self.d.rxn_in_seen('chat', 'message1', 'reaction1'))

    def test_edit_failure_does_not_advance_hash(self):
        self.d.event_map_put('chat', 'message1', '$original', '@contact:test', self.d.sha('old'))
        m = self.message(editedTimestamp='now')
        self.d.mx = lambda *a, **k: (_ for _ in ()).throw(OSError('offline'))
        with self.assertRaises(OSError):
            self.d.reconcile_edit('chat', m)
        self.assertEqual(self.d.event_map_get('chat', 'message1')[2], self.d.sha('old'))
        self.d.mx = lambda *a, **k: {'event_id': '$edit'}
        self.d.reconcile_edit('chat', m)
        self.assertEqual(self.d.event_map_get('chat', 'message1')[2], self.d.sha('hello'))

    def test_mixed_transaction_does_not_resend_successful_item(self):
        self.d.rate_ok = lambda *a: True
        calls = []
        def engine(*args):
            calls.append(args)
            return len(calls) != 2
        self.d.engine_send = engine
        events = [self.event('$one'), self.event('$two')]
        self.assertEqual(self.transaction(events), 503)
        self.assertEqual(self.transaction(events), 200)
        self.assertEqual(len(calls), 3)

    def test_timeout_is_persisted_ambiguous_and_not_resent(self):
        calls = []
        def runner(*args, **kwargs):
            calls.append(args)
            raise subprocess.TimeoutExpired('fake', 60)
        self.d._runner = runner
        self.assertEqual(self.transaction([self.event()]), 200)
        self.assertEqual(self.d.outbound_get('$event')[0], 'ambiguous')
        self.assertEqual(self.transaction([self.event()], 'other'), 200)
        self.assertEqual(len(calls), 1)

    def test_crash_after_dispatch_recovers_ambiguous(self):
        def runner(*args, **kwargs):
            raise KeyboardInterrupt('crash after possible send')
        self.d._runner = runner
        with self.assertRaises(KeyboardInterrupt):
            self.transaction([self.event()])
        self.assertEqual(self.d.outbound_get('$event')[0], 'dispatching')
        self.d.initialize(self.config, state_dir=self.tmp.name, runner=lambda *a, **k: self.fail('resent after crash'))
        self.assertEqual(self.d.outbound_get('$event')[0], 'ambiguous')
        self.assertEqual(self.transaction([self.event()]), 200)

    def test_pre_dispatch_failure_is_retryable_after_restart(self):
        self.d.rate_ok = lambda *a: True
        self.d._runner = lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError('missing fake CLI'))
        self.assertEqual(self.transaction([self.event()]), 503)
        self.d.initialize(self.config, state_dir=self.tmp.name,
                          runner=lambda *a, **k: subprocess.CompletedProcess(a, 0, b'', b''))
        self.assertEqual(self.transaction([self.event()]), 200)
        self.assertEqual(self.d.outbound_get('$event'), ('confirmed', 'engine_accepted'))

    def test_accessibility_refusal_does_not_probe_python_trust(self):
        self.d._runner = lambda *a, **k: subprocess.CompletedProcess(a, 1, b'', b'Accessibility permission required')
        self.d._ax_trusted = lambda *a, **k: self.fail('wrong executable TCC probe')
        opened = []
        self.d.cmd_open = opened.append
        self.assertEqual(self.transaction([self.event()]), 200)
        self.assertEqual(self.d.outbound_get('$event'), ('refused', 'accessibility_required'))
        self.assertEqual(opened, ['accessibility'])

    def test_concurrent_transaction_defers_instead_of_duplicate_dispatch(self):
        entered, release = threading.Event(), threading.Event()
        def runner(*a, **k):
            entered.set()
            self.assertTrue(release.wait(2))
            return subprocess.CompletedProcess(a, 0, b'', b'')
        self.d._runner = runner
        worker = threading.Thread(target=lambda: self.transaction([self.event()]))
        worker.start()
        try:
            self.assertTrue(entered.wait(2))
            self.assertEqual(self.transaction([self.event()], 'other'), 503)
        finally:
            release.set()
            worker.join(2)
        self.assertEqual(self.transaction([self.event()], 'other'), 200)

    def test_outbound_journal_contains_ids_and_outcomes_not_message_body(self):
        self.d.engine_send = lambda *a: True
        self.transaction([self.event()])
        row = self.d.DB.execute('SELECT * FROM outbound_event').fetchone()
        self.assertNotIn('hello', row)

    def test_sender_room_and_cross_room_reaction_guards_remain(self):
        self.d.engine_send = lambda *a: self.fail('untrusted dispatch')
        ev = self.event('$wrong_sender')
        ev['sender'] = '@stranger:test'
        self.assertEqual(self.transaction([ev]), 200)
        ev = self.event('$wrong_room')
        ev['room_id'] = '!unknown:test'
        self.assertEqual(self.transaction([ev], 'second'), 200)
        self.d.event_map_put('otherchat', 'native_message', '$target', '@contact:test', '')
        ev = self.event('$reaction')
        ev['type'] = 'm.reaction'
        ev['content'] = {'m.relates_to': {'rel_type': 'm.annotation', 'event_id': '$target', 'key': '❤️'}}
        self.assertEqual(self.transaction([ev], 'third'), 200)

    def test_rate_cap_survives_restart(self):
        self.assertTrue(self.d.rate_ok('chat'))
        self.d.initialize(self.config, state_dir=self.tmp.name)
        self.assertFalse(self.d.rate_ok('chat'))

    def test_portal_link_failure_preserves_allocated_room(self):
        self.d.ensure_space = lambda: '!space:test'
        created, links = [], []
        self.d.create_or_recover_room = lambda *a: created.append(a) or '!new:test'
        def matrix(*args, **kwargs):
            links.append(args)
            if len(links) == 1:
                raise OSError('space link unavailable')
            return {}
        self.d.mx = matrix
        with self.assertRaises(OSError):
            self.real_ensure_portal('newchat', 'Contact', False)
        self.assertEqual(self.d.room_for_chat('newchat'), '!new:test')
        self.assertEqual(self.real_ensure_portal('newchat', 'Contact', False), '!new:test')
        self.assertEqual(len(created), 1)
        self.assertEqual(len(links), 2)

    def test_lost_room_creation_response_recovers_verified_alias(self):
        def matrix(method, path, body=None, **kwargs):
            if method == 'POST':
                raise OSError('accepted but response lost')
            if '/directory/' in path:
                return {'room_id': '!created:test'}
            return [{'type': 'm.room.create', 'sender': '@imessagebot:test', 'content': {}}]
        self.d.mx = matrix
        self.assertEqual(self.d.create_or_recover_room({'name': 'Contact'}, 'portal:test'), '!created:test')
        self.d.mx = lambda method, path, body=None, **k: (
            (_ for _ in ()).throw(OSError('collision')) if method == 'POST' else
            {'room_id': '!spoof:test'} if '/directory/' in path else
            [{'type': 'm.room.create', 'sender': '@stranger:test', 'content': {}}])
        with self.assertRaises(ValueError):
            self.d.create_or_recover_room({'name': 'Contact'}, 'portal:test')

    def test_attachment_path_refusal_and_missing_file_are_distinct(self):
        self.d.mx = lambda *a, **k: {'event_id': '$text'}
        outside = {'srcURL': 'asset://test/' + b'/untrusted/location'.hex()}
        missing = {'srcURL': 'asset://test/' + str(Path(self.tmp.name) / 'missing').encode().hex()}
        self.d._relay_message('chat', '!portal:test', '', False, self.message(attachments=[outside]))
        self.assertEqual(self.d.component_get('!portal:test', 'message1', 'attachment:0')[1], 'refused_path')
        other = dict(self.message(attachments=[missing]), id='missingfile')
        with self.assertRaises(OSError):
            self.d._relay_message('chat', '!portal:test', '', False, other)
        self.assertIsNone(self.d.component_get('!portal:test', 'missingfile', 'attachment:0'))

    def test_zero_exit_does_not_claim_recipient_delivery(self):
        self.d._runner = lambda *a, **k: subprocess.CompletedProcess(a, 0, b'', b'')
        outcome = self.d.engine_send('chat', 'fixture')
        self.assertEqual(outcome.state, 'confirmed')
        self.assertEqual(outcome.reason, 'engine_accepted')
        self.assertFalse(outcome.delivered)

    def test_structured_error_even_with_zero_exit_is_ambiguous(self):
        self.d._runner = lambda *a, **k: subprocess.CompletedProcess(a, 0, b'{"success":false}', b'')
        outcome = self.d.engine_send('chat', 'fixture')
        self.assertEqual(outcome.state, 'ambiguous')

    def test_long_delayed_transaction_receipts_are_not_expired(self):
        self.d.txn_mark('old')
        self.d.DB.execute('UPDATE txns SET ts=1 WHERE txn_id=?', ('old',))
        self.d.DB.commit()
        self.d.txn_mark('new')
        self.assertTrue(self.d.txn_seen('old'))

    def test_negative_content_length_is_refused_before_read(self):
        handler = object.__new__(self.d.Handler)
        handler.path = '/_matrix/app/v1/transactions/bad'
        handler.headers = {'Host': '127.0.0.1:29350', 'Authorization': 'Bearer fake-hs', 'Content-Length': '-1'}
        handler.rfile = None
        replies = []
        handler._reply = lambda code, body: replies.append(code)
        handler.do_PUT()
        self.assertEqual(replies, [400])


if __name__ == '__main__':
    unittest.main()
