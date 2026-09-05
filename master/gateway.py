#!/usr/bin/env python3
"""Loopback master web gateway for Tailscale Serve; no credentials or auth bypass.

Static files are an explicit app manifest, never a directory listing. Matrix and
enrollment enforce their existing authentication on forwarded requests. Run as
the install's LaunchAgent, independently of the teammate Docker/views stack.
"""
import argparse
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
import os
from pathlib import Path
import re
import socket
import tempfile
import threading
import time
from urllib.parse import unquote, urlsplit


STATIC_FILES = frozenset({
    'apps/master/index.html', 'apps/master/main.js', 'apps/master/style.css',
    'apps/master/invites.js', 'apps/master/hidden.js', 'apps/master/transport.js',
    'shared/matrix/client.js', 'shared/state.js', 'shared/ui/el.js',
    'shared/model/source_catalog.js', 'shared/model/message_preview.js',
    'shared/style/organic.css', 'shared/style/beepa.css',
    'shared/assets/motherload_master.svg', 'shared/assets/motherload_master.png',
    'shared/assets/logo-whatsapp.png', 'shared/assets/logo-imessage.png',
    'shared/assets/logo-gmessages.png', 'shared/assets/logo-instagram.png',
    'shared/assets/logo-linkedin.png', 'shared/assets/logo-twitter.png',
})
CSP = ("default-src 'self'; connect-src 'self'; img-src 'self' blob:; media-src 'self' blob:; "
       "style-src 'self' https://fonts.googleapis.com; font-src 'self' https://fonts.gstatic.com; "
       "script-src 'self'; object-src 'none'; base-uri 'none'; form-action 'none'; "
       "frame-ancestors 'none'; require-trusted-types-for 'script'; trusted-types 'none'")
FORWARD_HEADERS = frozenset({'authorization', 'content-type', 'accept', 'range',
                             'if-none-match', 'if-modified-since', 'if-range', 'origin'})
RESPONSE_HEADERS = frozenset({'content-type', 'content-length', 'cache-control', 'etag',
                              'last-modified', 'retry-after', 'content-disposition',
                              'content-encoding', 'accept-ranges', 'content-range', 'vary'})
MAX_REQUEST_BYTES = 256 * 1024 * 1024


def upstream_url(value):
    parsed = urlsplit(value)
    if (parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username
            or parsed.password or parsed.query or parsed.fragment or parsed.path not in ('', '/')):
        raise ValueError('upstream must be an HTTP(S) origin without credentials')
    return parsed


class Gateway(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, code_root, matrix_upstream, enroll_upstream, maintenance,
                 server_name='master', max_requests=32):
        self.code_root = Path(code_root).resolve()
        self.matrix_upstream = upstream_url(matrix_upstream)
        self.enroll_upstream = upstream_url(enroll_upstream)
        self.maintenance = Path(maintenance)
        if not re.fullmatch(r'[A-Za-z0-9.-]+(?::[0-9]+)?', server_name):
            raise ValueError('invalid Matrix server name')
        self.server_name_config = server_name
        self.slots = threading.BoundedSemaphore(max_requests)
        self.active_proxies = 0
        self.proxy_lock = threading.Lock()
        super().__init__(address, Handler)

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            try:
                request.settimeout(1)
                request.sendall(b'HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\nConnection: close\r\nRetry-After: 1\r\n\r\n')
            finally:
                self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()


class Handler(BaseHTTPRequestHandler):
    server_version = 'BeepaGateway'
    sys_version = ''
    protocol_version = 'HTTP/1.1'

    def setup(self):
        super().setup()
        self.connection.settimeout(30)

    def log_message(self, *args):
        # Query strings may carry legacy access tokens; no request access log.
        pass

    def _headers(self, status, content_type, length=None, extra=()):
        self.send_response(status)
        if content_type:
            self.send_header('Content-Type', content_type)
        if length is not None:
            self.send_header('Content-Length', str(length))
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('X-Frame-Options', 'DENY')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Security-Policy', CSP)
        self.send_header('Connection', 'close')
        for key, value in extra:
            self.send_header(key, value)
        self.end_headers()
        self.close_connection = True

    def _json(self, status, value, extra=()):
        data = json.dumps(value, separators=(',', ':')).encode()
        self._headers(status, 'application/json', len(data), extra=extra)
        if self.command != 'HEAD':
            self.wfile.write(data)

    def _request_path(self):
        parsed = urlsplit(self.path)
        decoded = unquote(parsed.path)
        if (parsed.scheme or parsed.netloc or parsed.fragment or not parsed.path.startswith('/')
                or '\\' in decoded or any(ord(c) < 32 for c in decoded)
                or any(part in ('.', '..') for part in decoded.split('/'))):
            raise ValueError('invalid request path')
        return parsed, decoded

    def _read_body(self):
        # Spool large media requests to disk, not an unbounded per-client array.
        transfer = self.headers.get('Transfer-Encoding')
        lengths = self.headers.get_all('Content-Length', [])
        if len(lengths) > 1 or (transfer and lengths):
            raise ValueError('ambiguous request framing')
        if transfer and transfer.lower() != 'chunked':
            raise ValueError('unsupported request framing')
        length = int(lengths[0]) if lengths else 0
        if length < 0 or length > MAX_REQUEST_BYTES:
            raise ValueError('request too large')
        spool = tempfile.TemporaryFile()
        total, deadline = 0, time.monotonic() + 120
        try:
            while True:
                if transfer:
                    line = self.rfile.readline(64)
                    if not line.endswith(b'\r\n'):
                        raise ValueError('invalid chunk framing')
                    count = int(line.split(b';', 1)[0].strip(), 16)
                    if count == 0:
                        if self.rfile.readline(4) != b'\r\n':
                            raise ValueError('trailers are not supported')
                        break
                else:
                    count = length
                if count < 0 or total + count > MAX_REQUEST_BYTES:
                    raise ValueError('request too large')
                while count:
                    if time.monotonic() > deadline:
                        raise TimeoutError('request deadline')
                    data = self.rfile.read(min(count, 65536))
                    if not data:
                        raise ValueError('incomplete request body')
                    spool.write(data)
                    count -= len(data)
                    total += len(data)
                if not transfer:
                    break
                if self.rfile.read(2) != b'\r\n':
                    raise ValueError('invalid chunk framing')
            spool.seek(0)
            return spool, total
        except BaseException:
            spool.close()
            raise

    def _proxy(self, upstream):
        with self.server.proxy_lock:
            self.server.active_proxies += 1
        try:
            spool, size = self._read_body()
        except (ValueError, OSError):
            with self.server.proxy_lock:
                self.server.active_proxies -= 1
            return self._json(400, {'error': 'Invalid request body'})
        if self.server.maintenance.exists():
            spool.close()
            with self.server.proxy_lock:
                self.server.active_proxies -= 1
            return self._json(503, {'error': 'Master maintenance in progress'}, extra=(('Retry-After', '5'),))
        connection_class = http.client.HTTPSConnection if upstream.scheme == 'https' else http.client.HTTPConnection
        conn = connection_class(upstream.hostname, upstream.port, timeout=90)
        response_started = False
        try:
            headers = {key: value for key, value in self.headers.items() if key.lower() in FORWARD_HEADERS}
            headers['Content-Length'] = str(size)
            conn.putrequest(self.command, self.path, skip_accept_encoding=True)
            for key, value in headers.items():
                conn.putheader(key, value)
            conn.endheaders()
            while True:
                chunk = spool.read(65536)
                if not chunk:
                    break
                conn.send(chunk)
            response = conn.getresponse()
            outgoing = [(key, value) for key, value in response.getheaders() if key.lower() in RESPONSE_HEADERS]
            # Same-origin browser traffic needs no CORS widening. Authorization
            # and upstream 401/403 responses pass through without interpretation.
            self._headers(response.status, None, extra=outgoing)
            response_started = True
            if self.command != 'HEAD':
                while True:
                    chunk = response.read(65536)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
        except (OSError, http.client.HTTPException):
            if not response_started:
                self._json(502, {'error': 'Master upstream unavailable'})
        finally:
            spool.close()
            conn.close()
            with self.server.proxy_lock:
                self.server.active_proxies -= 1

    def _dispatch(self):
        try:
            parsed, decoded = self._request_path()
        except ValueError:
            return self._json(400, {'error': 'Invalid path'})
        if decoded == '/health':
            paused = self.server.maintenance.exists()
            with self.server.proxy_lock:
                active = self.server.active_proxies
            return self._json(503 if paused else 200,
                              {'service': 'master-gateway', 'maintenance': paused, 'active_upstreams': active})
        if self.server.maintenance.exists():
            return self._json(503, {'error': 'Master maintenance in progress'}, extra=(('Retry-After', '5'),))
        if parsed.path.startswith(('/_matrix/client/', '/_matrix/media/')):
            return self._proxy(self.server.matrix_upstream)
        if parsed.path.startswith(('/enroll/', '/admin/')):
            return self._proxy(self.server.enroll_upstream)
        if self.command not in ('GET', 'HEAD'):
            return self._json(405, {'error': 'Method not allowed'})
        if decoded in ('/', '/apps/master', '/apps/master/'):
            self._headers(302, None, 0, extra=(('Location', '/apps/master/index.html'),))
            return
        if decoded == '/apps/master/config.js':
            data = ('globalThis.BEEPA_MASTER_CONFIG = ' + json.dumps({'serverName': self.server.server_name_config}) + ';\n').encode()
            self._headers(200, 'text/javascript; charset=utf-8', len(data), extra=(('Cache-Control', 'no-store'),))
            if self.command != 'HEAD':
                self.wfile.write(data)
            return
        relative = decoded.lstrip('/')
        if relative not in STATIC_FILES:
            return self._json(404, {'error': 'Not found'})
        path = self.server.code_root / relative
        # A deployment mistake must not turn an allowlisted file into a symlink
        # to a local credential. No directory browsing or extension wildcard.
        if path.resolve() != path or not path.is_file():
            return self._json(404, {'error': 'Not found'})
        try:
            with path.open('rb') as source:
                size = os.fstat(source.fileno()).st_size
                content_type = mimetypes.guess_type(str(path))[0] or 'application/octet-stream'
                self._headers(200, content_type, size, extra=(('Cache-Control', 'no-cache'),))
                if self.command != 'HEAD':
                    while True:
                        chunk = source.read(65536)
                        if not chunk:
                            break
                        self.wfile.write(chunk)
        except OSError:
            self.close_connection = True

    do_GET = do_HEAD = do_POST = do_PUT = do_DELETE = do_OPTIONS = _dispatch


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=int(os.environ.get('MASTER_GATEWAY_PORT', '8017')))
    args = parser.parse_args(argv)
    code_root = Path(os.environ.get('BEEPA_CODE_ROOT', Path(__file__).resolve().parents[1]))
    install_root = Path(os.environ.get('BEEPA_INSTALL_ROOT', code_root))
    gateway = Gateway(('127.0.0.1', args.port), code_root,
                      os.environ.get('MASTER_MATRIX_UPSTREAM', 'http://127.0.0.1:8018'),
                      os.environ.get('MASTER_ENROLL_UPSTREAM', 'http://127.0.0.1:8019'),
                      install_root / 'master/runtime/maintenance',
                      server_name=os.environ.get('MASTER_SERVER_NAME', 'master'))
    gateway.serve_forever()


if __name__ == '__main__':
    main()
