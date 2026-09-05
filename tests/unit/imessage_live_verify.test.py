#!/usr/bin/env python3
"""Self-test verification must not count the bridge's outgoing echo as delivery."""
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import unittest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('self_verify', ROOT / 'tests/live/self_send_verify.py')
live = importlib.util.module_from_spec(spec)
spec.loader.exec_module(live)


class VerifyTest(unittest.TestCase):
    def test_outgoing_bot_echo_does_not_confirm_roundtrip(self):
        client = SimpleNamespace(recent_messages=lambda *a: [
            {'sender': '@imessagebot:test', 'content': {'body': 'nonce', 'com.jkali.from_me': True}}])
        kwargs = {'inbound_sender': '@imessage_self:test'} if 'inbound_sender' in live.poll_for_nonce.__code__.co_varnames else {}
        ok, _ = live.poll_for_nonce(client, '!portal:test', 'nonce', .01, 0, '@owner:test', **kwargs)
        self.assertFalse(ok)

    def test_verified_inbound_self_echo_confirms_roundtrip(self):
        client = SimpleNamespace(user='@owner:custom', recent_messages=lambda *a: [
            {'sender': '@imessage_=2b16505550123:custom', 'content': {'body': 'nonce'}}])
        expected = live.imessage_self_mxid(client, '+16505550123')
        self.assertEqual(expected, '@imessage_=2b16505550123:custom')
        ok, _ = live.poll_for_nonce(client, '!portal:custom', 'nonce', .01, 0, client.user, inbound_sender=expected)
        self.assertTrue(ok)

    def test_unmarked_bot_echo_is_still_not_inbound_self(self):
        client = SimpleNamespace(recent_messages=lambda *a: [
            {'sender': '@imessagebot:test', 'content': {'body': 'nonce'}}])
        ok, _ = live.poll_for_nonce(client, '!portal:test', 'nonce', .01, 0, '@owner:test', inbound_sender='@imessage_self:test')
        self.assertFalse(ok)

    def test_untrusted_management_marker_is_refused(self):
        client = SimpleNamespace(user='@owner:test', room_state=lambda *a: [
            {'type': 'com.jkali.bridge.mgmt', 'state_key': 'imessage', 'sender': '@stranger:test'}])
        self.assertFalse(live.verify_imsg_mgmt(client, '!spoof:test'))


if __name__ == '__main__':
    unittest.main()
