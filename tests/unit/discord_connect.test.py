import contextlib
import email.message
import io
import json
from pathlib import Path
import sys
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'session-connect'))
import discord_connect as discord
import connect_server as server


class DiscordTests(unittest.TestCase):
    def test_real_websocket_handshake_over_process_tunnel(self):
        script = '''import sys, base64, hashlib, json
headers = {}
while True:
    line = sys.stdin.buffer.readline()
    if line == b'\\r\\n':
        break
    if b':' in line:
        key, value = line.decode().split(':', 1)
        headers[key.lower()] = value.strip()
assert headers['authorization'] == 'Bearer synthetic-secret'
accept = base64.b64encode(hashlib.sha1((headers['sec-websocket-key'] + '258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest()).decode()
sys.stdout.buffer.write(('HTTP/1.1 101 Switching Protocols\\r\\nUpgrade: websocket\\r\\nConnection: Upgrade\\r\\nSec-WebSocket-Accept: ' + accept + '\\r\\nSec-WebSocket-Protocol: com.gitlab.beeper.discord\\r\\n\\r\\n').encode())
payload = json.dumps({'code':'https://discord.com/ra/synthetic'}).encode()
sys.stdout.buffer.write(bytes([129,len(payload)]) + payload)
sys.stdout.buffer.flush()
sys.stdin.buffer.read(8)
'''
        with patch.object(discord, 'credentials', return_value=('synthetic-secret', '@casey:localhost')), \
                patch.object(discord, 'command', return_value=[sys.executable, '-u', '-c', script]):
            with discord.websocket_connection() as connection:
                self.assertEqual(json.loads(connection.recv())['code'], 'https://discord.com/ra/synthetic')

    def test_status_uses_legacy_capitalized_discord_field(self):
        for connected, logged_in, expected in [(True, True, 'connected'), (False, True, 'disconnected'), (False, False, 'logged_out')]:
            with patch.object(discord, 'api', return_value={'Discord': {'id': '123', 'connected': connected, 'logged_in': logged_in}, 'token': 'secret'}):
                self.assertEqual(discord.connection_status(), {'status': expected, 'account': '123'})
        with patch.object(discord, 'api', return_value={}), self.assertRaises(ValueError):
            discord.connection_status()

    def test_credentials_go_to_stdin_not_argv(self):
        with patch.object(discord, 'credentials', return_value=('synthetic-provision-secret', '@casey:localhost')), \
                patch.object(discord, 'command', side_effect=lambda *args: ['docker', *args]), \
                patch.object(discord.subprocess, 'run', return_value=SimpleNamespace(returncode=0, stdout=b'{"success":true}')) as run:
            discord.api('token', {'token': 'synthetic-account-token'})
            argv = run.call_args.args[0]
            self.assertNotIn('synthetic', repr(argv))
            payload = run.call_args.kwargs['input'].decode()
            self.assertIn('synthetic-provision-secret', payload)
            self.assertIn('synthetic-account-token', payload)
            self.assertIn('%40casey%3Alocalhost', payload)
            self.assertNotIn('shell', run.call_args.kwargs)

    def test_actions_are_fixed_and_sessions_are_bound(self):
        connector = discord.DiscordConnector()
        with patch.object(discord, 'api') as api:
            for action, body in [('guilds', {}), ('token', {'token': 'bad\nheader'}), ('poll', {'session_id': 'unknown'})]:
                with self.assertRaises(ValueError):
                    connector.dispatch(action, body)
            api.assert_not_called()

    def test_logout_cancels_before_calling_bridge(self):
        connector = discord.DiscordConnector()
        connector.session = Mock()
        sequence = []
        connector.session.cancel.side_effect = lambda: sequence.append('cancel')
        with patch.object(discord, 'api', side_effect=lambda action: sequence.append(action)), \
                patch.object(discord, 'connection_status', return_value={'status': 'logged_out'}):
            self.assertEqual(connector.dispatch('logout', {}), {'status': 'logged_out'})
        self.assertEqual(sequence, ['cancel', 'logout'])

    def test_qr_is_allowlisted_and_returns_pixels_only(self):
        for code, expected in [('https://discord.com/ra/abc_123', 'qr'), ('https://evil.example/ra/abc', 'failed')]:
            session = discord.LoginSession()
            received = []
            connection = Mock()
            def receive():
                if not received:
                    received.append(True)
                    return json.dumps({'code': code, 'token': 'must-not-return'})
                session.stop.set()
                return json.dumps({'success': False})
            connection.recv.side_effect = receive
            with patch.object(discord, 'websocket_connection', return_value=contextlib.nullcontext(connection)):
                session.run()
            snapshot = session.snapshot()
            self.assertEqual(snapshot['status'], expected)
            self.assertNotIn('must-not-return', json.dumps(snapshot))
            if expected == 'qr':
                self.assertTrue(all(isinstance(cell, bool) for row in snapshot['matrix'] for cell in row))

    def test_expired_session_never_waits_for_provider(self):
        session = discord.LoginSession()
        session.expires = time.monotonic() - 1
        connection = Mock()
        with patch.object(discord, 'websocket_connection', return_value=contextlib.nullcontext(connection)):
            session.run()
        self.assertEqual(session.snapshot()['status'], 'expired')
        connection.recv.assert_not_called()

    def test_bridge_errors_never_echo_response(self):
        with patch.object(discord, 'credentials', return_value=('synthetic-secret', '@casey:localhost')), \
                patch.object(discord, 'command', return_value=['synthetic']), \
                patch.object(discord.subprocess, 'run', return_value=SimpleNamespace(returncode=22, stdout=b'secret', stderr=b'secret')):
            with self.assertRaises(ValueError) as caught:
                discord.api('status')
        self.assertNotIn('secret', str(caught.exception))

    def handler(self, origin='http://127.0.0.1:8011', host='127.0.0.1:8021', payload=b'{}'):
        handler = object.__new__(server._make_handler())
        handler.path = '/connect/discord/start'
        handler.headers = email.message.Message()
        for key, value in {'Origin': origin, 'Host': host, 'Content-Type': 'application/json',
                           'X-Beepa-Connect': '1', 'Content-Length': str(len(payload))}.items():
            handler.headers[key] = value
        handler.connection = Mock()
        handler.rfile = io.BytesIO(payload)
        handler._json = Mock()
        handler._diag = Mock()
        return handler

    def test_http_gates_before_any_side_effect(self):
        for kwargs in [{'origin': 'https://evil.example'}, {'host': 'evil.example'}, {'payload': b'[]'}]:
            handler = self.handler(**kwargs)
            with patch.object(discord.connector, 'dispatch') as dispatch:
                handler.do_POST()
                dispatch.assert_not_called()
        handler = self.handler()
        with patch.object(discord.connector, 'dispatch', return_value={'status': 'starting'}) as dispatch:
            handler.do_POST()
            dispatch.assert_called_once_with('start', {})
            self.assertEqual(handler._json.call_args.args[0], 200)


if __name__ == '__main__':
    unittest.main()
