#!/usr/bin/env python3
"""Pinned create-chat semantics; temporary SQLite and injected native transport.

v0.24.4: https://github.com/beeper/platform-imessage/blob/v0.24.4/src/IMessage/Sources/IMessage/PlatformAPI.swift
createThread returns true for a new chat; its Thread alternative is read before
the send. Neither alternative contains the new message's authoritative IDs.
"""
import importlib.util
import json
from pathlib import Path
import subprocess
import time
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('durability_fixture', Path(__file__).with_name('imessage_durability.test.py'))
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)


class StartChatStatusTest(unittest.TestCase):
    setUp = fixture.DaemonTest.setUp
    message = fixture.DaemonTest.message
    transaction = fixture.DaemonTest.transaction

    def command(self, event_id='$command'):
        self.d.meta_set('mgmt_room', '!mgmt:test')
        return dict(event_id=event_id, sender='@owner:test', room_id='!mgmt:test',
                    origin_server_ts=int(time.time() * 1000), type='m.room.message',
                    content={'msgtype': 'm.text', 'body': 'start-chat person@example.invalid | fixture first message',
                             'com.jkali.from_proposal': '$proposal'})

    def native_result(self, result):
        self.native_calls = []
        def run(argv, **kwargs):
            self.native_calls.append(argv)
            self.assertEqual(argv[1:4], ['--json', '--no-events', 'create-chat'])
            self.assertFalse(kwargs['shell'])
            return subprocess.CompletedProcess(argv, 0, json.dumps(result).encode(), b'')
        self.d._runner = run

    def publish(self):
        self.puts = []
        self.d.mx = lambda method, path, content, **kw: self.puts.append((method, path, content, kw)) or {}
        self.d.publish_start_chat_statuses()
        return self.puts[-1][2] if self.puts else None

    def assert_no_mapping(self):
        self.assertEqual(self.d.DB.execute('SELECT count(*) FROM native_receipt').fetchone()[0], 0)
        self.assertEqual(self.d.DB.execute('SELECT engine_id FROM outbound_event').fetchone()[0], None)

    def test_new_chat_boolean_acceptance_has_no_native_receipts(self):
        self.native_result(True)
        self.assertEqual(self.transaction([self.command()]), 200)
        result = self.publish()
        self.assertEqual(result, dict(schema=1, command_event_id='$command', owner_mxid='@owner:test',
                                     status='accepted', reason='engine_accepted', correlation='unavailable',
                                     native_ids=[], message_event_ids=[], delivered=False, read=False))
        self.assertEqual(self.puts[0][0], 'PUT')
        self.assertTrue(self.puts[0][1].endswith('/state/com.jkali.imessage_start_chat/%24command'))
        self.assertEqual(self.puts[0][3]['user'], '@imessagebot:test')
        self.assert_no_mapping()
        flat = json.dumps(result)
        self.assertNotIn('person@example.invalid', flat)
        self.assertNotIn('fixture first message', flat)

    def test_existing_thread_history_is_never_this_send(self):
        self.native_result({'id': 'chat', 'type': 'single', 'participants': {'items': []},
                            'messages': {'items': [{'id': 'old-read-message', 'threadID': 'chat',
                                                  'isSender': True, 'isDelivered': True, 'seen': 1234}]}})
        self.assertEqual(self.transaction([self.command()]), 200)
        result = self.publish()
        self.assertEqual(result['status'], 'accepted')
        self.assertFalse(result['read'])
        self.assertFalse(result['delivered'])
        self.assert_no_mapping()
        self.d.publish_native_receipts()
        self.assertEqual(len(self.puts), 1)

    def test_normal_native_echo_keeps_own_receipts_without_claiming_command_link(self):
        self.native_result(True)
        self.transaction([self.command()])
        sends = []
        self.d.send_text = lambda *a, **kw: sends.append(a) or {'event_id': '$native-echo'}
        message = {'id': 'real-native-id', 'threadID': 'chat', 'isSender': True,
                   'text': 'fixture first message', 'isDelivered': True, 'seen': 1234}
        self.d.deliver_inbound('chat', 'Synthetic', False, message)
        self.d.deliver_inbound('chat', 'Synthetic', False, message)
        self.assertEqual(len(sends), 1)
        self.assertEqual(self.d.DB.execute('SELECT event_id,native_id FROM native_receipt').fetchall(),
                         [('$native-echo', 'real-native-id')])
        result = self.publish()
        self.assertEqual(result['message_event_ids'], [])
        self.assertFalse(result['read'])
        self.d.publish_native_receipts()
        self.assertTrue(self.puts[-1][2]['com.jkali.read'])
        self.assertEqual(self.puts[-1][2]['m.relates_to']['event_id'], '$native-echo')

    def test_unknown_shapes_never_masquerade_as_acceptance_or_receipts(self):
        for payload in (None, {}, [], [{'id': 'invented', 'isSender': True}], 1, 'true',
                        {'id': 'chat', 'type': 'single', 'messages': [], 'participants': {}},
                        {'id': 'chat', 'type': 'group', 'messages': {}, 'participants': {}}):
            with self.subTest(payload=payload):
                self.native_result(payload)
                outcome = self.d.engine_create_chat('person@example.invalid', 'fixture')
                self.assertEqual((outcome.state, outcome.reason), ('ambiguous', 'engine_result_unrecognized'))
                self.assertEqual(outcome.messages, [])
                self.assertIsNone(outcome.engine_id)
                self.assertFalse(outcome.delivered)

    def test_false_and_errors_are_ambiguous_not_safe_to_resend(self):
        for payload in (False, {'error': 'synthetic'}, {'success': False}):
            with self.subTest(payload=payload):
                self.native_result(payload)
                result = self.d.engine_create_chat('person@example.invalid', 'fixture')
                self.assertEqual(result.state, 'ambiguous')
                self.assertFalse(result)
                self.assertEqual(result.messages, [])

    def test_repeat_command_and_publish_retry_never_repeat_native_send(self):
        self.native_result(True)
        command = self.command()
        self.assertEqual(self.transaction([command]), 200)
        self.d.mx = lambda *a, **k: (_ for _ in ()).throw(OSError('lost reply'))
        self.d.publish_start_chat_statuses()
        self.assertEqual(self.d.DB.execute('SELECT count(*) FROM published_start_chat_status').fetchone()[0], 0)
        self.assertEqual(self.transaction([command], 'replay'), 200)
        self.assertEqual(self.publish()['status'], 'accepted')
        self.d.publish_start_chat_statuses()
        self.assertEqual(len(self.puts), 1)
        self.assertEqual(len(self.native_calls), 1)

    def test_accepted_restart_preserves_outcome_without_native_replay(self):
        self.native_result(True)
        command = self.command()
        self.assertEqual(self.transaction([command]), 200)
        self.d.initialize(self.config, state_dir=self.tmp.name,
                          runner=lambda *a, **k: self.fail('native replay after restart'))
        self.assertEqual(self.transaction([command], 'restart'), 200)
        self.assertEqual(self.publish()['status'], 'accepted')
        self.assert_no_mapping()

    def test_crash_during_dispatch_publishes_ambiguity_after_restart(self):
        command = self.command()
        def crash(*args, **kwargs):
            self.assertEqual(self.d.DB.execute('SELECT event_id FROM start_chat_command').fetchone()[0], '$command')
            self.assertEqual(self.d.outbound_get('$command')[0], 'dispatching')
            raise KeyboardInterrupt()
        self.d._runner = crash
        with self.assertRaises(KeyboardInterrupt): self.transaction([command])
        self.d.initialize(self.config, state_dir=self.tmp.name,
                          runner=lambda *a, **k: self.fail('ambiguous send retried'))
        self.assertEqual(self.transaction([command], 'restart'), 200)
        result = self.publish()
        self.assertEqual((result['status'], result['reason']), ('ambiguous', 'restart_during_dispatch'))
        self.assert_no_mapping()

    def test_owner_or_management_rebinding_never_publishes_old_account_outcome(self):
        self.native_result(True)
        self.transaction([self.command()])
        self.d.USER_ID = '@new-owner:test'
        self.assertIsNone(self.publish())
        self.d.USER_ID = '@owner:test'
        self.d.meta_set('mgmt_room', '!new-mgmt:test')
        self.assertIsNone(self.publish())

    def test_untrusted_and_stale_commands_do_not_gain_structured_outcomes(self):
        for alteration in ({'sender': '@foreign:test'}, {'origin_server_ts': 1},
                           {'room_id': '!unknown:test'}, {'type': 'com.jkali.crm.send'}):
            command = self.command('$' + str(len(alteration)) + str(alteration))
            command.update(alteration)
            self.assertEqual(self.transaction([command], str(alteration)), 200)
        self.assertEqual(self.d.DB.execute('SELECT count(*) FROM start_chat_command').fetchone()[0], 0)
        self.assertIsNone(self.publish())

    def test_invalid_handle_and_rate_limit_are_not_acceptance(self):
        command = self.command()
        command['content']['body'] = 'start-chat --bad@example.invalid | fixture'
        self.assertEqual(self.transaction([command]), 200)
        self.assertEqual(self.publish()['status'], 'refused')
        self.d.startchat_gate = lambda handle: (False, 3, 3)
        self.assertEqual(self.transaction([self.command('$limited')], 'limit'), 503)
        self.assertEqual(self.publish()['status'], 'retryable')
        self.assert_no_mapping()

    def test_publisher_runs_even_when_history_and_other_statuses_fail(self):
        self.native_result(True)
        self.transaction([self.command()])
        def fail(): raise OSError('offline')
        self.d.poll_once = self.d.publish_message_statuses = self.d.publish_native_receipts = fail
        self.d.poll_native_receipts = self.d.publish_health_state = lambda: None
        self.puts = []
        self.d.mx = lambda *args, **kw: self.puts.append(args) or {}
        with patch.object(self.d.time, 'sleep', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt): self.d.poll_loop()
        self.assertEqual(len(self.puts), 1)
        self.assertEqual(self.puts[0][2]['command_event_id'], '$command')


if __name__ == '__main__':
    unittest.main()
