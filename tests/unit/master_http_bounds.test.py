"""Unauthenticated admin callers cannot monopolize the enrollment body reader."""
import email.message
import importlib.util
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'master'))
import enroll


class MasterBodyTests(unittest.TestCase):
    def test_admin_authentication_precedes_body_read(self):
        for path in ('/admin/add-teammate', '/admin/delete-teammate'):
            handler = object.__new__(enroll._make_handler())
            handler.path = path
            handler.headers = email.message.Message()
            handler.headers['Content-Length'] = '65536'
            handler.rfile = Mock()
            handler._json = Mock()
            handler.do_POST()
            handler.rfile.read.assert_not_called()
            self.assertEqual(handler._json.call_args.args[0], 401)
            self.assertTrue(handler.close_connection)

    def test_enrollment_uses_total_read_deadline(self):
        handler = object.__new__(enroll._make_handler())
        handler.headers = email.message.Message()
        handler.headers['Content-Length'] = '2'
        handler.connection = Mock()
        handler.rfile = Mock()
        handler.rfile.read1.return_value = b'a'
        with patch('http_limits.time.monotonic', side_effect=[0, 1, 6]):
            with self.assertRaises(ValueError):
                handler._read_json_body()
        self.assertEqual(handler.rfile.read1.call_count, 1)


if __name__ == '__main__':
    unittest.main()
