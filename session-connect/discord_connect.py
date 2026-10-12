"""Discord provisioning over Docker's private network, with no published port."""
import contextlib
import json
import os
import re
import secrets
import socket
import subprocess
import threading
import time
from urllib.parse import quote

import connect

LOGIN_SECONDS = 120
MAX_RESPONSE = 65536
ACTIONS = ('start', 'poll', 'cancel', 'token', 'status', 'reconnect', 'logout')


def command(*args):
    return connect.compose_prefix(connect.REPO, connect.CODE_ROOT) + [
        'exec', '-T', 'mautrix-discord', *args]


def credentials():
    secret = connect.shared_secret('discord/config.yaml').strip('\"\x27')
    if not re.fullmatch(r'[A-Za-z0-9_-]{16,256}', secret):
        raise ValueError('Discord provisioning is not configured.')
    return secret, connect.configured_identity(connect.REPO)


def api(action, body=None):
    routes = {'status': ('GET', 'ping'), 'token': ('POST', 'login/token'),
              'reconnect': ('POST', 'reconnect'), 'logout': ('POST', 'logout')}
    method, path = routes[action]
    secret, identity = credentials()
    url = 'http://127.0.0.1:29334/_matrix/provision/v1/' + path + '?user_id=' + quote(identity, safe='')
    config = {'url': url, 'request': method, 'header': 'Authorization: Bearer ' + secret}
    lines = [key + ' = ' + json.dumps(value) for key, value in config.items()]
    if body is not None:
        lines.extend(['header = "Content-Type: application/json"',
                      'data = ' + json.dumps(json.dumps(body))])
    result = subprocess.run(command('curl', '--silent', '--show-error', '--fail', '--max-time', '15',
                                    '--max-filesize', str(MAX_RESPONSE), '--config', '-'),
                            input=('\n'.join(lines) + '\n').encode(), capture_output=True, timeout=20)
    if result.returncode or len(result.stdout) > MAX_RESPONSE:
        raise ValueError('Discord bridge is unavailable or rejected the request.')
    value = json.loads(result.stdout)
    if not isinstance(value, dict):
        raise ValueError('Invalid Discord response.')
    return value


@contextlib.contextmanager
def websocket_connection():
    import websocket
    secret, identity = credentials()
    client, relay = socket.socketpair()
    process = None
    connection = None
    workers = []
    try:
        process = subprocess.Popen(command('nc', '127.0.0.1', '29334'), stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)

        def to_bridge():
            try:
                while True:
                    data = relay.recv(16384)
                    if not data:
                        break
                    process.stdin.write(data)
                    process.stdin.flush()
            except (OSError, ValueError):
                pass

        def from_bridge():
            try:
                while True:
                    data = os.read(process.stdout.fileno(), 16384)
                    if not data:
                        break
                    relay.sendall(data)
            except (OSError, ValueError):
                pass
            finally:
                with contextlib.suppress(OSError):
                    relay.shutdown(socket.SHUT_RDWR)

        for target in (to_bridge, from_bridge):
            worker = threading.Thread(target=target, daemon=True)
            worker.start()
            workers.append(worker)
        connection = websocket.create_connection(
            'ws://127.0.0.1:29334/_matrix/provision/v1/login/qr?user_id=' + quote(identity, safe=''),
            socket=client, timeout=10, header={'Authorization': 'Bearer ' + secret},
            subprotocols=['com.gitlab.beeper.discord'], enable_multithread=True)
        connection.settimeout(1)
        yield connection
    finally:
        if connection is not None:
            with contextlib.suppress(Exception):
                connection.close(timeout=1)
        for endpoint in (client, relay):
            with contextlib.suppress(OSError):
                endpoint.shutdown(socket.SHUT_RDWR)
            endpoint.close()
        if process is not None:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            for worker in workers:
                worker.join(timeout=2)
            process.stdin.close()
            process.stdout.close()


def connection_status():
    remote = api('status').get('Discord')
    if not isinstance(remote, dict) or not isinstance(remote.get('connected'), bool) or not isinstance(remote.get('logged_in'), bool):
        raise ValueError('Invalid Discord status.')
    connected = remote.get('connected') is True
    logged_in = remote.get('logged_in') is True
    account = remote.get('id')
    return {'status': 'connected' if connected else 'disconnected' if logged_in else 'logged_out',
            'account': account if isinstance(account, str) and re.fullmatch(r'[0-9]{1,32}', account) else ''}


class LoginSession:
    def __init__(self):
        self.identity = secrets.token_urlsafe(24)
        self.expires = time.monotonic() + LOGIN_SECONDS
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.result = {'status': 'starting'}
        self.worker = threading.Thread(target=self.run, daemon=True)

    def set_result(self, result):
        with self.lock:
            if not self.stop.is_set():
                self.result = result

    def snapshot(self):
        with self.lock:
            return dict(self.result, session_id=self.identity,
                        expires_in=max(0, int(self.expires - time.monotonic())))

    def cancel(self):
        self.stop.set()
        self.worker.join(timeout=15)
        if self.worker.is_alive():
            raise ValueError('Login is still stopping. Retry shortly.')
        with self.lock:
            self.result = {'status': 'cancelled'}

    def run(self):
        try:
            import qrcode
            import websocket
            with websocket_connection() as connection:
                while not self.stop.is_set() and time.monotonic() < self.expires:
                    try:
                        raw = connection.recv()
                    except websocket.WebSocketTimeoutException:
                        continue
                    if not isinstance(raw, str) or len(raw) > MAX_RESPONSE:
                        raise ValueError('Invalid Discord login response.')
                    value = json.loads(raw)
                    if not isinstance(value, dict):
                        raise ValueError('Invalid Discord login response.')
                    code = value.get('code')
                    if isinstance(code, str) and re.fullmatch(r'https://discord\.com/ra/[A-Za-z0-9_-]{1,256}', code):
                        qr = qrcode.QRCode(border=4)
                        qr.add_data(code)
                        qr.make(fit=True)
                        self.set_result({'status': 'qr', 'matrix': qr.get_matrix()})
                    elif value.get('success') is True:
                        self.set_result(connection_status())
                        return
                    else:
                        self.set_result({'status': 'failed'})
                        return
                self.set_result({'status': 'expired'})
        except (Exception, SystemExit):
            self.set_result({'status': 'failed'})


class DiscordConnector:
    def __init__(self):
        self.session = None

    def cancel(self):
        if self.session:
            self.session.cancel()
            self.session = None

    def dispatch(self, action, body):
        if action not in ACTIONS or not isinstance(body, dict):
            raise ValueError('Invalid Discord request.')
        if action == 'status':
            return connection_status()
        if action in ('poll', 'cancel'):
            if not self.session or body.get('session_id') != self.session.identity:
                raise ValueError('This login session has expired.')
            if action == 'cancel':
                self.cancel()
                return {'status': 'cancelled'}
            return self.session.snapshot()
        if action == 'token':
            token = body.get('token')
            if not isinstance(token, str) or not re.fullmatch(r'[A-Za-z0-9._-]{20,4096}', token):
                raise ValueError('Invalid token.')
        self.cancel()
        if action == 'start':
            status = connection_status()
            if status['status'] != 'logged_out':
                return status
            self.session = LoginSession()
            self.session.worker.start()
            return self.session.snapshot()
        if action == 'token':
            api('token', {'token': token})
        else:
            api(action)
        return connection_status()


connector = DiscordConnector()
