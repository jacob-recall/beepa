#!/usr/bin/env python3
"""Two synthetic users; no Messages, Docker, credentials or live sends."""
import copy
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import multi_account as m
import install_config as cfg
from beepa_update import Updater, views_overlay

class MultiAccountTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)

    def account(self, name, slot, uid):
        root = self.base / name
        root.mkdir()
        with patch.dict(os.environ, {'BEEPA_STATE_ROOT': str(root / 'state'), 'BEEPA_LOG_ROOT': str(root / 'logs')}), patch.object(m.os, 'getuid', return_value=uid), patch.object(cfg.getpass, 'getuser', return_value=name):
            data = m.prepare(root, slot, name + '@example.test')
        return root, data

    def test_two_accounts_have_disjoint_endpoints_state_and_projects(self):
        a, first = self.account('alice', 1, 501)
        b, second = self.account('bob', 2, 502)
        self.assertFalse(set(first['ports'].values()) & set(second['ports'].values()))
        self.assertNotEqual(first['compose_project'], second['compose_project'])
        self.assertNotEqual(first['install_id'], second['install_id'])
        self.assertNotEqual((a / '.env').read_text(), (b / '.env').read_text())
        for root, data in ((a, first), (b, second)):
            with patch.object(m.os, 'getuid', return_value=data['owner_uid']):
                command = dict(Updater(root).compositions(data, ROOT))['teammate']
                self.assertIn(data['compose_project'], command)
                self.assertIn(str(ROOT / 'docker-compose.imessage.yml'), command)
                overlay = json.loads(Path(command[-1]).read_text())
                self.assertEqual(set(overlay['services']), {'synapse', 'postgres', 'views'})
                volumes = overlay['services']['views']['volumes']
                source = {v['target']: Path(v['source']) for v in volumes}
                entry = source['/usr/share/nginx/html/apps/user/instance.js'].read_text()
                index = source['/usr/share/nginx/html/apps/user/index.html'].read_text()
                self.assertIn(data['local_cs_base'], entry)
                self.assertIn(data['install_id'], entry)
                self.assertIn(data['local_cs_base'], index)
                self.assertNotIn('http://127.0.0.1:8021', index)
                self.assertNotIn('http://127.0.0.1:8008', index)
                self.assertIn('instance.js', index)
                self.assertEqual(source['/usr/share/nginx/runtime/apps'], Path(data['state_root']) / 'apps')

    def test_enabling_all_networks_retains_account_and_native_state(self):
        root, data = self.account('alice', 1, 501)
        state = Path(data['state_root'])
        cfg.atomic_write(state / 'imessage/daemon.json', '{"receive_only": true, "self_handle": "alice@example.test"}')
        original_native = (state / 'imessage/daemon.json').read_bytes()
        password = cfg.read_env(state / '.env')['POSTGRES_PASSWORD']
        with patch.object(m.os, 'getuid', return_value=501):
            m.enable_networks(root)
            m.enable_networks(root)
            expanded = cfg.read_manifest(root)
            self.assertEqual(expanded['install_id'], data['install_id'])
            self.assertEqual(expanded['compose_project'], data['compose_project'])
            self.assertEqual(expanded['local_localpart'], data['local_localpart'])
            self.assertEqual(expanded['local_cs_base'], data['local_cs_base'])
            self.assertEqual(cfg.read_env(state / '.env')['POSTGRES_PASSWORD'], password)
            self.assertEqual((state / 'imessage/daemon.json').read_bytes(), original_native)
            command = dict(Updater(root).compositions(expanded, ROOT))['teammate']
            self.assertIn(str(ROOT / 'docker-compose.yml'), command)
            self.assertIn('bridge', command)
            overlay = json.loads(Path(command[command.index('--profile') - 1]).read_text())
            self.assertIn('mautrix-twitter', overlay['services'])
            volumes = overlay['services']['views']['volumes']
            source = {v['target']: Path(v['source']) for v in volumes}
            entry = source['/usr/share/nginx/html/apps/user/instance.js'].read_text()
            index = source['/usr/share/nginx/html/apps/user/index.html'].read_text()
            self.assertIn('"profile": "full"', entry)
            self.assertIn('http://127.0.0.1:8120', entry)
            self.assertIn('http://127.0.0.1:8121', index)
            self.assertNotIn('http://127.0.0.1:18021', index)
            plist = cfg.write_plist(root, 'session-connect', root / 'session.plist')
            self.assertEqual(plist['EnvironmentVariables']['BEEPA_APP_PORT'], '8111')
            self.assertEqual(plist['EnvironmentVariables']['BEEPA_SESSION_PORT'], '8121')

    def test_owner_and_slot_changes_refused_without_mutating_state(self):
        root, data = self.account('alice', 1, 501)
        before = (root / '.beepa-install.json').read_bytes()
        with patch.object(m.os, 'getuid', return_value=502), self.assertRaisesRegex(ValueError, 'owning'):
            m.prepare(root, 1)
        with patch.object(m.os, 'getuid', return_value=501), self.assertRaisesRegex(ValueError, 'slot'):
            m.prepare(root, 2)
        self.assertEqual(before, (root / '.beepa-install.json').read_bytes())

    def test_repeat_retains_secrets_handle_and_instance(self):
        root, data = self.account('alice', 1, 501)
        before = (root / '.env').read_bytes()
        with patch.object(m.os, 'getuid', return_value=501):
            again = m.prepare(root, 1)
        self.assertEqual(again['install_id'], data['install_id'])
        self.assertEqual(again['self_handle'], data['self_handle'])
        self.assertEqual(before, (root / '.env').read_bytes())

    def test_failed_enrollment_command_does_not_echo_one_time_code(self):
        import contextlib
        import io
        error = subprocess.CalledProcessError(1, ['link.sh', 'https://example.test', 'synthetic-secret-code'])
        output = io.StringIO()
        with patch.object(sys, 'argv', ['multi_account.py', '--root', str(self.base), 'install']), patch.object(m, 'install', side_effect=error), contextlib.redirect_stderr(output):
            with self.assertRaises(SystemExit):
                m.main()
        self.assertNotIn('synthetic-secret-code', output.getvalue())
        self.assertIn('exit 1', output.getvalue())

    def test_occupied_port_is_not_adopted(self):
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            port = listener.getsockname()[1]
            with self.assertRaisesRegex(ValueError, 'occupied'):
                m.validate_ports({'ports': {'imessage': port}})

    def test_new_update_artifacts_do_not_replace_running_files(self):
        root, data = self.account('alice', 1, 501)
        with patch.object(m.os, 'getuid', return_value=501):
            old = m.instance_overlay(data, ROOT, views_overlay(Path(data['state_root']), ROOT))
            before = {v['source']: Path(v['source']).read_bytes() for v in old['services']['views']['volumes'] if '/instances/' in v['source']}
            changed = copy.deepcopy(data)
            changed['local_cs_base'] = 'http://127.0.0.1:9108'
            m.instance_overlay(changed, ROOT, views_overlay(Path(data['state_root']), ROOT))
            for path, contents in before.items():
                self.assertEqual(Path(path).read_bytes(), contents)

    def test_real_render_matches_daemon_and_only_registers_imessage(self):
        root, data = self.account('alice', 1, os.getuid())
        # Run the actual release renderer with isolated state and a synthetic
        # signing key, so no Docker or downloaded signer is needed.
        state = Path(data['state_root'])
        (state / 'synapse/localhost.signing.key').write_text('synthetic')
        env = dict(os.environ, BEEPA_INSTALL_ROOT=str(state), OUT_ROOT=str(state))
        result = subprocess.run(['bash', str(ROOT / 'hub/render-hub.sh')], env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        daemon = json.loads((state / 'imessage/daemon.json').read_text())
        self.assertEqual(daemon['port'], data['ports']['imessage'])
        self.assertEqual(daemon['hs_url'], data['local_cs_base'])
        registration = (state / 'synapse/imessage-registration.yaml').read_text()
        self.assertIn('host.docker.internal:%d' % daemon['port'], registration)
        self.assertIn(daemon['as_token'], registration)
        self.assertIn(daemon['hs_token'], registration)
        self.assertIn('    aliases:', registration)
        self.assertIn('^#imessage_.*:localhost$', registration)
        homeserver = (state / 'synapse/homeserver.yaml').read_text()
        self.assertIn('/data/imessage-registration.yaml', homeserver)
        self.assertNotIn('/data/meta-registration.yaml', homeserver)
        self.assertNotIn('/data/registration.yaml', homeserver)

if __name__ == '__main__':
    unittest.main()
