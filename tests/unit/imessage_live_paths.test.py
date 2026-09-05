"""The opt-in live verifier must require real inbound evidence and explicit use."""
import importlib.util
from pathlib import Path
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'tests/live'))
import imessage_paths_verify as verifier


class LiveVerifierTests(unittest.TestCase):
    def test_local_and_bot_echoes_cannot_count_as_receive(self):
        event = {'type': 'm.room.message', 'sender': '@self:local', 'content': {'body': 'nonce'}}
        self.assertTrue(verifier.inbound_match(event, 'nonce', '@self:local'))
        self.assertFalse(verifier.inbound_match(event, 'different nonce', '@self:local'))
        self.assertFalse(verifier.inbound_match(event, 'nonce', '@bot:local'))
        event['content']['com.jkali.from_me'] = True
        self.assertFalse(verifier.inbound_match(event, 'nonce', '@self:local'))

    def test_default_invocation_cannot_send_or_read_live_config(self):
        result = subprocess.run([sys.executable, str(ROOT / 'tests/live/imessage_paths_verify.py'),
                                 '--root', '/nonexistent-test-root'], capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn('Explicit --i-am-sending-to-myself is required', result.stderr)
        self.assertNotIn('Traceback', result.stderr)


if __name__ == '__main__':
    unittest.main()
