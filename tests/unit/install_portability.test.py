#!/usr/bin/env python3
"""Installer regressions use synthetic state and never launch services."""
import importlib.util
import json
from pathlib import Path
import plistlib
import tempfile
import os
import sqlite3
import subprocess
import unittest
from unittest.mock import patch
import sys
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("install_config", ROOT / "install_config.py")
cfg = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cfg)


class IdentityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve() / "Other's Mac & files"
        self.root.mkdir()

    def test_existing_identity_survives_os_change_and_role_addition(self):
        (self.root / '.env').write_text('LOCAL_LOCALPART=alice\nLOCAL_DISPLAYNAME=Alice Person\n')
        first = cfg.ensure_manifest(self.root, env={})
        with patch.object(cfg.getpass, 'getuser', return_value='different'):
            second = cfg.ensure_manifest(self.root, role='master', env={})
        self.assertEqual(first['install_id'], second['install_id'])
        self.assertEqual(second['local_localpart'], 'alice')
        self.assertEqual(set(second['roles']), {'teammate', 'master'})
        self.assertEqual(cfg.configured_identity(self.root), '@alice:localhost')

    def test_new_install_explicit_or_os_identity(self):
        with patch.object(cfg.getpass, 'getuser', return_value='Casey'):
            data = cfg.ensure_manifest(self.root, env={})
        self.assertEqual(data['local_localpart'], 'casey')
        self.assertEqual(data['compose_project'], 'matrix-wa')

    def test_conflicting_identity_rejected_before_writing(self):
        (self.root / '.env').write_text('LOCAL_LOCALPART=alice\n')
        with self.assertRaisesRegex(ValueError, 'identity'):
            cfg.ensure_manifest(self.root, env={'LOCAL_LOCALPART': 'bob'})
        self.assertFalse((self.root / '.beepa-install.json').exists())

    def test_legacy_token_identity_used_without_author_fallback(self):
        path = self.root / 'agents/uplink/local.env.local'
        path.parent.mkdir(parents=True)
        path.write_text('LOCAL_USER=@casey:localhost\nLOCAL_TOKEN=synthetic\n')
        self.assertEqual(cfg.configured_identity(self.root), '@casey:localhost')
        self.assertFalse((self.root / '.beepa-install.json').exists())

    def test_missing_identity_does_not_create_an_account_from_helper(self):
        with self.assertRaisesRegex(ValueError, 'setup'):
            cfg.configured_identity(self.root)

    def test_conflicting_legacy_sources_rejected(self):
        (self.root / '.env').write_text('LOCAL_LOCALPART=alice\n')
        path = self.root / 'agents/uplink/local.env.local'
        path.parent.mkdir(parents=True)
        path.write_text('LOCAL_USER=@bob:localhost\n')
        with self.assertRaisesRegex(ValueError, 'identity'):
            cfg.ensure_manifest(self.root, env={})

    def test_shell_values_are_parsed_as_data(self):
        path = self.root / '.env'
        path.write_text("LOCAL_LOCALPART='alice'\nLOCAL_DISPLAYNAME=Alice Person\n")
        self.assertEqual(cfg.read_env(path)['LOCAL_DISPLAYNAME'], 'Alice Person')
        with self.assertRaises(ValueError):
            cfg.ensure_manifest(self.root, env={'LOCAL_LOCALPART': "alice'$(touch nope)"})

    def test_launchd_paths_are_escaped_and_native_path_untouched(self):
        manifest = cfg.ensure_manifest(self.root, env={'LOCAL_LOCALPART': 'alice'})
        before = manifest['imessage_cli_path']
        dest = self.root / 'agent.plist'
        cfg.write_plist(self.root, 'session-connect', dest)
        data = plistlib.loads(dest.read_bytes())
        self.assertEqual(data['Label'], 'org.beepa.session-connect')
        self.assertEqual(data['WorkingDirectory'], str(self.root))
        self.assertIn(str(self.root), data['ProgramArguments'][1])
        self.assertEqual(cfg.ensure_manifest(self.root, env={})['imessage_cli_path'], before)

    def test_both_login_helpers_address_installed_user_without_cookie_reads(self):
        (self.root / '.env').write_text('LOCAL_LOCALPART=casey\n')
        for directory in ('gmessages-connect', 'session-connect'):
            with self.subTest(helper=directory):
                sys.path.insert(0, str(ROOT / directory))
                try:
                    spec = importlib.util.spec_from_file_location(directory.replace('-', '_'), ROOT / directory / 'connect.py')
                    helper = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(helper)
                    helper.REPO = str(self.root)
                    with patch.object(helper.subprocess, 'run', return_value=SimpleNamespace(stdout=b'{}')) as run:
                        if directory == 'session-connect':
                            helper.api(helper.NETWORKS['twitter'], '/login/start/cookies', 'synthetic')
                        else:
                            helper.api('/login/start/google', 'synthetic')
                    command = run.call_args.args[0][-1]
                    self.assertIn('user_id=%40casey%3Alocalhost', command)
                    self.assertNotIn('jkali', command)
                finally:
                    sys.path.pop(0)

    def test_new_install_external_state_writes_are_persistent_and_rerunnable(self):
        state = Path(self.tmp.name).resolve() / 'external state'
        logs = Path(self.tmp.name).resolve() / 'external logs'
        data = cfg.ensure_manifest(self.root, env={'LOCAL_LOCALPART': 'casey',
            'BEEPA_STATE_ROOT': str(state), 'BEEPA_LOG_ROOT': str(logs)})
        cfg.initialize_state(self.root)
        for relative, body in {'.env': 'LOCAL_LOCALPART=casey\n',
            'apps/user/session.local.json': '{"user_id":"@casey:localhost","access_token":"synthetic"}',
            'apps/user/connect.local.json': '{"port":8021}'}.items():
            cfg.atomic_write(self.root / relative, body)
            self.assertTrue((self.root / relative).is_symlink())
            self.assertEqual((state / relative).read_text(), body)
        plist = cfg.write_plist(self.root, 'uplink', self.root / 'uplink.plist')
        self.assertEqual(plist['WorkingDirectory'], str(state))
        db = Path(plist['EnvironmentVariables']['UPLINK_DB'])
        connection = sqlite3.connect(db)
        connection.execute('PRAGMA journal_mode=WAL')
        connection.execute('CREATE TABLE evidence (value TEXT)')
        connection.execute("INSERT INTO evidence VALUES ('committed')")
        connection.commit()
        self.assertTrue(Path(str(db) + '-wal').exists())
        self.assertFalse(Path(str(self.root / 'agents/uplink/state.db') + '-wal').exists())
        connection.close()
        rerun = cfg.ensure_manifest(self.root, role='master', env={})
        cfg.initialize_state(self.root)
        self.assertEqual(rerun['install_id'], data['install_id'])
        self.assertEqual(cfg.read_manifest(state)['roles'], ['master', 'teammate'])
        self.assertEqual(cfg.configured_identity(state), '@casey:localhost')
        self.assertEqual(plist['StandardOutPath'], str(logs / 'agents/uplink/logs/uplink.log'))
        self.assertEqual(Path(data['imessage_cli_path']), state / 'imessage/bin/imessage-cli')

    def test_default_state_uses_application_support_and_legacy_native_is_not_moved(self):
        fake_home = Path(self.tmp.name).resolve() / 'new home'
        with patch.object(cfg.Path, 'home', return_value=fake_home):
            data = cfg.ensure_manifest(self.root, env={'LOCAL_LOCALPART': 'casey'})
        self.assertEqual(Path(data['state_root']).parent, fake_home / 'Library/Application Support/Beepa')
        self.assertEqual(Path(data['logs_root']).parent, fake_home / 'Library/Logs/Beepa')
        legacy = Path(self.tmp.name).resolve() / 'old install'
        native = legacy / 'imessage/bin/imessage-cli'
        native.parent.mkdir(parents=True)
        native.write_bytes(b'authorized signed executable')
        inode = native.stat().st_ino
        adopted = cfg.ensure_manifest(legacy, env={})
        cfg.initialize_state(legacy)
        self.assertEqual(adopted['state_root'], str(legacy))
        self.assertEqual(native.stat().st_ino, inode)
        self.assertFalse(native.parent.is_symlink())
        with self.assertRaisesRegex(ValueError, 'relocat'):
            cfg.ensure_manifest(legacy, env={'BEEPA_STATE_ROOT': str(fake_home)})

    def test_uplink_wrapper_reads_state_with_staged_code_and_configured_python(self):
        state = Path(self.tmp.name).resolve() / 'state'
        creds = state / 'agents/uplink/local.env.local'
        creds.parent.mkdir(parents=True)
        creds.write_text('LOCAL_USER=@casey:localhost\nLOCAL_TOKEN=synthetic\n')
        code = self.root / 'agents/uplink'
        code.mkdir(parents=True)
        (code / 'run-uplink.sh').write_bytes((ROOT / 'agents/uplink/run-uplink.sh').read_bytes())
        (code / 'uplink.py').write_text('import os,json; print(json.dumps({k: os.environ[k] for k in ("LOCAL_USER","UPLINK_DB","UPLINK_CONTACTS_DB")}))')
        env = dict(os.environ, BEEPA_INSTALL_ROOT=str(state), BEEPA_PYTHON=sys.executable)
        result = subprocess.run(['bash', str(code / 'run-uplink.sh')], env=env, capture_output=True, text=True, check=True)
        output = json.loads(result.stdout)
        self.assertEqual(output['LOCAL_USER'], '@casey:localhost')
        self.assertEqual(Path(output['UPLINK_DB']), state / 'agents/uplink/state.db')
        self.assertEqual(Path(output['UPLINK_CONTACTS_DB']).resolve(), state / 'agents/contacts/contacts.db')

    def test_runtime_install_is_private_hashed_and_reused(self):
        cfg.ensure_manifest(self.root, env={'LOCAL_LOCALPART': 'casey'})
        lock = self.root / 'requirements-host.txt'
        lock.write_text('phonenumberslite==9.0.38 --hash=sha256:synthetic\n')
        calls = []
        def run(args, **kwargs):
            calls.append(args)
            if 'venv' in args:
                python = Path(args[-1]) / 'bin/python3'
                python.parent.mkdir(parents=True)
                python.write_text('synthetic interpreter')
            return SimpleNamespace(returncode=0)
        with patch.object(cfg.subprocess, 'run', side_effect=run):
            first = cfg.ensure_runtime(self.root)
            second = cfg.ensure_runtime(self.root)
        self.assertEqual(first, second)
        self.assertIn('/.beepa-venvs/', first)
        pip = [command for command in calls if 'pip' in command]
        self.assertEqual(len(pip), 1)
        self.assertIn('--require-hashes', pip[0])
        self.assertEqual(cfg.read_manifest(self.root)['python_path'], first)


if __name__ == '__main__':
    unittest.main()
