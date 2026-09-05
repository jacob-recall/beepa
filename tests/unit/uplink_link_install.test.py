#!/usr/bin/env python3
"""Run link.sh against fake loopback APIs and a fake service installer."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
import threading

ROOT = Path(__file__).resolve().parents[2]
saved = []


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        assert self.path == '/enroll/exchange'
        self.rfile.read(int(self.headers['Content-Length']))
        payload = json.dumps({'master_hs_url': BASE, 'master_user': '@fixture:master',
            'master_token': "synthetic'quoted", 'manager_mxid': '@manager:master',
            'master_space': '!space:master'}).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(payload)

    def do_PUT(self):
        assert self.path.endswith('/account_data/com.jkali.master_link')
        saved.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'{}')


server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
BASE = 'http://127.0.0.1:' + str(server.server_port)
thread = threading.Thread(target=server.serve_forever, daemon=True)
thread.start()
try:
    with tempfile.TemporaryDirectory(prefix='beepa-link-') as temporary:
        root = Path(temporary) / 'checkout with spaces'
        state = Path(temporary) / 'persistent state'
        relative = Path('agents/uplink/uplink.env.local')
        (root / relative.parent).mkdir(parents=True)
        (state / relative.parent).mkdir(parents=True)
        (state / relative).write_text('previous synthetic credentials')
        (root / relative).symlink_to(state / relative)
        (root / '.beepa-install.json').write_text(json.dumps({'state_root': str(state), 'state_initialized': True}))
        # Production service-generation behavior is independently tested. This
        # stub makes it impossible for this shell test to modify real launchd.
        (root / 'install_config.py').write_text('import json\nfrom pathlib import Path\n'
            'def read_manifest(root): return json.loads((Path(root)/".beepa-install.json").read_text())\n')
        for name in ('link.sh', 'enroll_client.py'):
            shutil.copy2(ROOT / 'agents/uplink' / name, root / 'agents/uplink' / name)
        env = dict(os.environ, LOCAL_HS_URL=BASE, LOCAL_USER='@fixture:local', LOCAL_TOKEN='fixture')
        env.pop('BEEPA_INSTALL_ROOT', None)
        result = subprocess.run(['/bin/bash', str(root / 'agents/uplink/link.sh'), BASE, 'synthetic-code'],
                                env=env, capture_output=True, text=True, timeout=20)
        assert result.returncode == 0, result.stderr
        assert (root / relative).is_symlink(), 'atomic credential replacement broke checkout projection'
        fields = dict(line.split('=', 1) for line in (state / relative).read_text().splitlines())
        assert shlex.split(fields['MASTER_TOKEN']) == ["synthetic'quoted"]
        assert (state / relative).stat().st_mode & 0o777 == 0o600
        assert saved[0]['master_token'] == "synthetic'quoted"
        assert saved[0]['master_enroll_url'] == BASE
        print('PASS: link installer preserves external credential symlink, safely quotes credentials, writes explicit local pairing')
finally:
    server.shutdown()
    server.server_close()
    thread.join()
