"""Independent native command/status review; injected CLI, no real accounts."""
import copy
import importlib.util
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('start_chat_adversarial_fixture', Path(__file__).with_name('imessage_start_chat_status.test.py'))
fixture = importlib.util.module_from_spec(spec); spec.loader.exec_module(fixture)


class StartChatAdversarial(fixture.StartChatStatusTest):
    def test_retryable_command_can_later_send_and_status_must_change(self):
        command = self.command()
        with patch.object(self.d, 'startchat_gate', return_value=(False, 1, 1)):
            self.assertEqual(self.transaction([command], 'limited'), 503)
        self.assertEqual(self.publish()['status'], 'retryable')
        self.native_result(True)
        self.assertEqual(self.transaction([command], 'retry'), 200)
        self.assertEqual(self.publish()['status'], 'accepted')
        self.assertEqual(len(self.native_calls), 1)
        self.assert_no_mapping()

    def test_successful_command_id_replay_cannot_substitute_body(self):
        self.native_result(True); command = self.command()
        self.transaction([command], 'first')
        altered = copy.deepcopy(command)
        altered['content']['body'] = 'start-chat other@example.invalid | substituted body'
        self.transaction([altered], 'new-as-transaction')
        self.assertEqual(len(self.native_calls), 1)
        self.assertEqual(self.publish()['status'], 'accepted')
        self.assert_no_mapping()

    def test_timeout_after_dispatch_never_becomes_retryable(self):
        command = self.command(); calls = []
        def timeout(argv, **kwargs):
            calls.append(argv); raise subprocess.TimeoutExpired(argv, 60)
        self.d._runner = timeout
        self.assertEqual(self.transaction([command]), 200)
        self.assertEqual(self.publish()['status'], 'ambiguous')
        self.transaction([command], 'again')
        self.assertEqual(len(calls), 1); self.assert_no_mapping()

    def test_nested_stale_thread_success_and_receipts_never_bind_command(self):
        self.native_result({'id': 'old-chat', 'type': 'single', 'participants': {}, 'messages': {
            'items': [{'id': 'old-guid', 'threadID': 'old-chat', 'text': 'fixture first message',
                       'isSender': True, 'isDelivered': True, 'seen': 9999999999}]},
            'sent_event_id': '$forged', 'native_ids': ['old-guid'], 'read': True, 'delivered': True})
        self.transaction([self.command()]); status = self.publish()
        self.assertEqual(status['status'], 'accepted'); self.assertEqual(status['native_ids'], [])
        self.assertEqual(status['message_event_ids'], []); self.assertFalse(status['delivered']); self.assertFalse(status['read'])
        self.assert_no_mapping()

    def test_engine_reported_failure_even_with_thread_shape_is_ambiguous(self):
        self.native_result({'id': 'old-chat', 'type': 'single', 'participants': {}, 'messages': {}, 'success': False})
        self.transaction([self.command()]); self.assertEqual(self.publish()['status'], 'ambiguous')
        self.assert_no_mapping()

    def test_reaction_relation_and_state_event_cannot_create_chat(self):
        self.native_result(True)
        for index, mutation in enumerate((lambda e: e.update(state_key=''),
                lambda e: e['content'].update({'m.relates_to': {'rel_type': 'm.replace', 'event_id': '$old'}}),
                lambda e: e['content'].update(msgtype='m.notice'))):
            command = self.command('$invalid-' + str(index)); mutation(command)
            self.transaction([command], 'invalid-' + str(index))
        self.assertEqual(self.native_calls, [])
        self.assertEqual(self.d.DB.execute('SELECT count(*) FROM start_chat_command').fetchone()[0], 0)


for name in tuple(vars(fixture.StartChatStatusTest)):
    if name.startswith('test_'): setattr(StartChatAdversarial, name, None)
if __name__ == '__main__': unittest.main()
