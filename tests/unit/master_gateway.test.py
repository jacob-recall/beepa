#!/usr/bin/env python3
"""Actual loopback HTTP integration; generated files and fake upstreams only."""
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
import os
from pathlib import Path
import posixpath
import re
import shutil
import socket
import tempfile
import threading
import time
import unittest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('master_gateway', ROOT / 'master/gateway.py')
gateway = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gateway)


class FakeUpstream(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self):
        length = int(self.headers.get('Content-Length', '0'))
        body = self.rfile.read(length)
        self.server.calls.append((self.command, self.path, dict(self.headers), body))
        auth = self.headers.get('Authorization')
        if self.path.startswith('/admin/') and auth != 'Bearer manager-fixture':
            code, data = 403, b'{"error":"manager required"}'
        elif self.path.startswith('/_matrix/') and not auth:
            code, data = 401, b'{"errcode":"M_MISSING_TOKEN"}'
        elif '/media/download/' in self.path:
            code, data = 206, b'fixture-bytes'
        else:
            code, data = 200, json.dumps({'method': self.command, 'path': self.path, 'bytes': len(body)}).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/octet-stream' if code == 206 else 'application/json')
        self.send_header('Content-Length', str(len(data)))
        if code == 206:
            self.send_header('Content-Range', 'bytes 0-12/13')
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(data)

    do_GET = do_HEAD = do_POST = do_PUT = do_DELETE = do_OPTIONS = reply


class GatewayTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for relative in gateway.STATIC_FILES:
            target = self.root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            source = ROOT / relative
            target.write_bytes(source.read_bytes() if source.exists() else b'fixture')
        (self.root / 'apps/master/session.local.json').write_text('{"access_token":"NEVER_SERVE_ME"}')
        (self.root / '.env').write_text('NEVER_SERVE_ME')
        self.upstream = ThreadingHTTPServer(('127.0.0.1', 0), FakeUpstream)
        self.upstream.calls = []
        self.upstream_thread = threading.Thread(target=self.upstream.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
        self.upstream_thread.start()
        self.addCleanup(self.stop, self.upstream, self.upstream_thread)
        address = 'http://127.0.0.1:%d' % self.upstream.server_port
        self.flag = self.root / 'maintenance'
        self.server = gateway.Gateway(('127.0.0.1', 0), self.root, address, address, self.flag, server_name='configured.example')
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop, self.server, self.thread)

    def stop(self, server, thread):
        server.shutdown()
        server.server_close()
        thread.join(2)

    def request(self, path, method='GET', body=None, headers=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=3)
        try:
            conn.request(method, path, body=body, headers=dict({'Host': 'master.private.ts.net'}, **(headers or {})))
            response = conn.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            conn.close()

    def test_console_is_served_independent_of_teammate_views(self):
        status, headers, data = self.request('/apps/master/index.html')
        self.assertEqual(status, 200)
        self.assertIn(b'Beepa Console', data)
        self.assertIn("connect-src 'self';", headers['Content-Security-Policy'])
        self.assertNotIn('127.0.0.1', headers['Content-Security-Policy'])
        self.assertEqual(headers['X-Frame-Options'], 'DENY')
        self.assertEqual(self.request('/')[1]['Location'], '/apps/master/index.html')

    def test_remote_bootstrap_secret_and_all_nonmanifest_paths_are_denied(self):
        for path in ('/apps/master/session.local.json', '/.env', '/master/tokens.local',
                     '/apps/master/', '/apps/user/index.html', '/shared/ui/chat.js',
                     '/apps/master/../../.env', '/apps/master/%2e%2e/%2e%2e/.env',
                     '/apps/master/session%2elocal%2ejson', '/shared/', '/master/gateway.py'):
            status, _, data = self.request(path)
            self.assertNotEqual(status, 200, path)
            self.assertNotIn(b'NEVER_SERVE_ME', data)

    def test_static_symlink_cannot_expose_even_an_inside_root_secret(self):
        path = self.root / 'apps/master/style.css'
        path.unlink()
        path.symlink_to(self.root / '.env')
        self.assertEqual(self.request('/apps/master/style.css')[0], 404)

    def test_public_config_contains_only_server_name(self):
        status, headers, data = self.request('/apps/master/config.js')
        self.assertEqual(status, 200)
        self.assertEqual(headers['Cache-Control'], 'no-store')
        content = data.decode().split('=', 1)[1].strip().rstrip(';')
        self.assertEqual(json.loads(content), {'serverName': 'configured.example'})

    def test_matrix_authentication_response_is_not_bypassed(self):
        self.assertEqual(self.request('/_matrix/client/v3/sync')[0], 401)
        self.assertEqual(self.request('/_matrix/client/v3/sync?since=opaque', headers={'Authorization': 'Bearer scoped-fixture'})[0], 200)
        call = self.upstream.calls[-1]
        self.assertEqual(call[1], '/_matrix/client/v3/sync?since=opaque')
        self.assertEqual(call[2]['Authorization'], 'Bearer scoped-fixture')

    def test_enrollment_admin_auth_and_request_body_are_preserved(self):
        self.assertEqual(self.request('/admin/add-teammate', 'POST', b'{}')[0], 403)
        body = b'{"username":"fixture"}'
        status, _, data = self.request('/admin/add-teammate', 'POST', body,
                                       {'Authorization': 'Bearer manager-fixture', 'Content-Type': 'application/json'})
        self.assertEqual(status, 200)
        self.assertEqual(self.upstream.calls[-1][3], body)
        self.assertEqual(self.request('/enroll/exchange', 'POST', b'{"code":"fixture"}')[0], 200)
        self.assertEqual(self.request('/enroll/recovery', 'POST', b'{}')[0], 200)

    def test_media_range_response_preserves_bytes_and_headers(self):
        status, headers, body = self.request('/_matrix/client/v1/media/download/master/id',
                                             headers={'Authorization': 'Bearer scoped-fixture', 'Range': 'bytes=0-12'})
        self.assertEqual((status, body), (206, b'fixture-bytes'))
        self.assertEqual(headers['Content-Range'], 'bytes 0-12/13')
        self.assertEqual(self.upstream.calls[-1][2]['Range'], 'bytes=0-12')

    def test_maintenance_refuses_new_api_and_static_requests(self):
        self.flag.touch()
        for path in ('/apps/master/index.html', '/_matrix/client/v3/sync', '/enroll/manifest'):
            self.assertEqual(self.request(path)[0], 503)
        status, _, body = self.request('/health')
        self.assertEqual(status, 503)
        self.assertTrue(json.loads(body)['maintenance'])
        self.assertEqual(self.upstream.calls, [])
        self.flag.unlink()
        self.assertEqual(self.request('/health')[0], 200)

    def test_maintenance_started_during_upload_prevents_forwarding(self):
        sock = socket.create_connection(('127.0.0.1', self.server.server_port), timeout=3)
        self.addCleanup(sock.close)
        sock.sendall(b'POST /enroll/exchange HTTP/1.1\r\nHost: master.private.ts.net\r\nContent-Length: 10\r\n\r\n123')
        deadline = time.monotonic() + 2
        while self.server.active_proxies == 0 and time.monotonic() < deadline:
            time.sleep(.005)
        self.assertEqual(self.server.active_proxies, 1)
        self.flag.touch()
        sock.sendall(b'4567890')
        self.assertIn(b'503', sock.recv(4096).split(b'\r\n', 1)[0])
        self.assertEqual(self.upstream.calls, [])

    def test_chunked_upload_is_bounded_and_forwarded_with_length(self):
        conn = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=3)
        self.addCleanup(conn.close)
        conn.request('POST', '/enroll/exchange', body=iter([b'ab', b'cde']), encode_chunked=True)
        response = conn.getresponse()
        self.assertEqual(response.status, 200)
        response.read()
        self.assertEqual(self.upstream.calls[-1][3], b'abcde')
        self.assertEqual(self.upstream.calls[-1][2]['Content-Length'], '5')

    def test_gateway_is_not_a_forward_proxy_or_synapse_admin_endpoint(self):
        self.assertEqual(self.request('http://untrusted.example/enroll/exchange')[0], 400)
        self.assertEqual(self.request('/_synapse/admin/v1/register')[0], 404)
        self.assertEqual(self.request('/_matrix/federation/v1/version')[0], 404)
        self.assertEqual(self.request('/_matrix/client/../../_synapse/admin/v1/register')[0], 400)
        self.assertEqual(self.upstream.calls, [])

    def test_head_does_not_send_static_body(self):
        status, headers, body = self.request('/apps/master/index.html', 'HEAD')
        self.assertEqual(status, 200)
        self.assertEqual(body, b'')
        self.assertGreater(int(headers['Content-Length']), 0)

    def test_served_application_dependency_graph_is_in_manifest(self):
        todo, seen = ['apps/master/index.html'], set()
        while todo:
            relative = todo.pop()
            if relative in seen or relative == 'apps/master/config.js':
                continue
            seen.add(relative)
            self.assertIn(relative, gateway.STATIC_FILES)
            status, _, body = self.request('/' + relative)
            self.assertEqual(status, 200, relative)
            if relative.endswith('.html'):
                links = re.findall(r'(?:src|href)="([^"]+)"', body.decode())
            elif relative.endswith('.js'):
                links = re.findall(r'from\s+[\'"]([^\'"]+)[\'"]', body.decode())
            elif relative.endswith('.css'):
                links = re.findall(r'url\(([^)]+)\)', body.decode())
            else:
                links = []
            for link in links:
                link = link.strip('\'"').split('?', 1)[0]
                if link.startswith(('https:', 'data:', '#')):
                    continue
                todo.append(posixpath.normpath(posixpath.join(posixpath.dirname(relative), link)))


if __name__ == '__main__':
    unittest.main()
