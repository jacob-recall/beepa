"""Run in the push-helper image when optional provider dependencies are absent."""
import base64
import importlib.util
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

dependencies = all(importlib.util.find_spec(name) for name in ('pywebpush', 'requests', 'cryptography'))
source = Path(os.environ.get('BEEPA_NOTIFICATION_HELPER') or str(Path(__file__).resolve().parents[2]/'master/notifications.py'))
spec = importlib.util.spec_from_file_location('push_helper_fixture', source)
module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)


@unittest.skipUnless(dependencies, 'run in the isolated browser push helper image')
class HelperTests(unittest.TestCase):
    def data(self):
        encoded = lambda value: base64.urlsafe_b64encode(value).decode().rstrip('=')
        return {'action': 'send', 'key': '/unused-fixture-key', 'payload': {'title': 'Fixture'},
                'subscription': {'endpoint': 'https://updates.push.services.mozilla.com/wpush/v2/fixture',
                                 'keys': {'auth': encoded(b'a'*16), 'p256dh': encoded(b'\x04'+b'b'*64)}}}

    def test_no_http_response_remains_unknown(self):
        from pywebpush import WebPushException
        with patch('pywebpush.webpush', side_effect=WebPushException('Lost response', response=None)):
            with self.assertRaisesRegex(RuntimeError, 'outcome is unknown'):
                module.helper(self.data())

    def test_actual_http_failure_preserves_status(self):
        from pywebpush import WebPushException
        for status in (403, 410, 429, 503):
            response = SimpleNamespace(status_code=status, text='Fixture provider response')
            with patch('pywebpush.webpush', side_effect=WebPushException('Rejected', response=response)):
                self.assertEqual(module.helper(self.data()), {'status': status})

    def test_provider_acceptance_is_not_a_browser_display_receipt(self):
        with patch('pywebpush.webpush', return_value=SimpleNamespace(status_code=201)) as send:
            self.assertEqual(module.helper(self.data()), {'status': 201})
            self.assertEqual(send.call_args.kwargs['timeout'], 15)
            self.assertEqual(send.call_args.kwargs['ttl'], 300)


if __name__ == '__main__': unittest.main()
