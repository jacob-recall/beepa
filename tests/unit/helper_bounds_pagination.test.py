#!/usr/bin/env python3
"""Contact completeness and slow/oversized helper request regressions."""
import email.message
import importlib.util
import io
from pathlib import Path
import sqlite3
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'session-connect'))
import http_limits
import connect_server as helper


class HelperTests(unittest.TestCase):
    def test_more_than_two_thousand_contacts_are_paged_without_unknown_source_starvation(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'contacts.db'
            con = sqlite3.connect(path)
            con.execute('CREATE TABLE contacts(source TEXT,network_id TEXT,kind TEXT,display_name TEXT,deleted INTEGER)')
            con.executemany('INSERT INTO contacts VALUES (?,?,?,?,?)',
                            [('imessage', '+1%010d' % i, 'phone', 'Person %d' % i, 0) for i in range(2501)] +
                            [('aaa-invalid', str(i), 'phone', 'Unknown', 0) for i in range(2500)] +
                            [('imessage', '+deleted', 'phone', 'Deleted', 1)])
            con.commit(); con.close()
            first = helper.contact_page(path)
            self.assertEqual(len(first['contacts']), 2000)
            self.assertTrue(first['next_cursor'])
            second = helper.contact_page(path, first['next_cursor'])
            self.assertEqual(len(second['contacts']), 501)
            self.assertIsNone(second['next_cursor'])
            identities = [c['network_id'] for c in first['contacts'] + second['contacts']]
            self.assertEqual(len(set(identities)), 2501)
            self.assertEqual(sqlite3.connect(path).execute('SELECT count(*) FROM contacts').fetchone()[0], 5002)

    def test_invalid_cursor_cannot_change_query_scope(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                helper.contact_page(Path(tmp) / 'absent.db', "' OR 1=1--")

    def request(self, length, payload=b''):
        headers = email.message.Message(); headers['Content-Length'] = str(length)
        return SimpleNamespace(headers=headers, rfile=io.BytesIO(payload), connection=Mock())

    def test_oversized_body_rejected_before_read(self):
        request = self.request(10**12)
        request.rfile = Mock()
        with self.assertRaises(http_limits.BodyError) as caught:
            http_limits.read_body(request, 65536)
        self.assertEqual(caught.exception.status, 413)
        request.rfile.read.assert_not_called()
        request.rfile.read1.assert_not_called()

    def test_slow_drip_total_deadline_and_truncated_body(self):
        request = self.request(2)
        request.rfile = SimpleNamespace(read=lambda n: b'a', read1=lambda n: b'a')
        with patch.object(http_limits.time, 'monotonic', side_effect=[0, 1, 6]):
            with self.assertRaises(http_limits.BodyError) as caught:
                http_limits.read_body(request, 100, deadline_seconds=5)
        self.assertEqual(caught.exception.status, 408)
        with self.assertRaises(http_limits.BodyError) as caught:
            http_limits.read_body(self.request(2, b'a'), 100)
        self.assertEqual(caught.exception.status, 400)

    def test_duplicate_lengths_and_chunked_bodies_rejected(self):
        request = self.request(2, b'{}')
        request.headers['Content-Length'] = '200'
        with self.assertRaises(http_limits.BodyError):
            http_limits.read_body(request, 100)

    def test_real_handler_never_starts_login_after_oversized_body(self):
        handler = object.__new__(helper._make_handler())
        request = self.request(10**12)
        handler.path = '/connect/instagram/start'
        handler.headers = request.headers
        handler.connection = request.connection
        handler.rfile = Mock()
        handler._diag = Mock()
        handler._authorized = lambda: True
        handler._json = Mock()
        handler._start_provisioning = Mock()
        handler.do_POST()
        handler._start_provisioning.assert_not_called()
        self.assertEqual(handler._json.call_args.args[0], 413)
        request = self.request(2, b'{}'); request.headers['Transfer-Encoding'] = 'chunked'
        with self.assertRaises(http_limits.BodyError):
            http_limits.read_body(request, 100)


if __name__ == '__main__':
    unittest.main()
