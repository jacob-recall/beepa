#!/usr/bin/env python3
"""Offline update lifecycle: isolated fake services, real files and journals."""
import importlib.util
import json
from pathlib import Path
import tempfile
import sqlite3
import subprocess
import os
import tarfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('beepa_update', ROOT / 'beepa_update.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class UpdateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_incompatible_or_downgrade_is_refused(self):
        current = {'state_version': 2, 'release': '2.0'}
        with self.assertRaisesRegex(ValueError, 'compatible'):
            m.check_compatibility(current, {'release': '1.0', 'state_version': 1, 'reads_state_versions': [1]})

    def test_inventory_tracks_secret_hashes_not_secret_values(self):
        (self.root / '.env').write_text('POSTGRES_PASSWORD=synthetic-do-not-print\n')
        data = m.inventory(self.root)
        self.assertIn('.env', data)
        self.assertNotIn('synthetic-do-not-print', json.dumps(data))

    def test_journal_resumes_completed_steps_without_repeating_dispatch(self):
        calls = []
        journal = m.Journal(self.root / 'journal.json', {'target': 'one'})
        journal.step('backup', lambda: calls.append('backup'))
        with self.assertRaises(RuntimeError):
            journal.step('activate', lambda: (_ for _ in ()).throw(RuntimeError('crash')))
        journal = m.Journal(self.root / 'journal.json', {'target': 'one'})
        journal.step('backup', lambda: calls.append('backup'))
        journal.step('activate', lambda: calls.append('activate'))
        self.assertEqual(calls, ['backup', 'activate'])

    def test_new_target_cannot_overwrite_unfinished_journal(self):
        m.Journal(self.root / 'journal.json', {'target': 'one'})
        with self.assertRaisesRegex(ValueError, 'unfinished'):
            m.Journal(self.root / 'journal.json', {'target': 'two'})

    def test_runtime_overlay_keeps_session_tokens_in_state_not_release(self):
        path = self.root / 'apps/user/session.local.json'
        path.parent.mkdir(parents=True); path.write_text('{"access_token":"synthetic"}')
        overlay = m.views_overlay(self.root, self.root / 'release')
        volumes = overlay['services']['views']['volumes']
        self.assertTrue(any(v['source'] == str((self.root / 'apps').resolve()) and v['target'] == '/usr/share/nginx/runtime/apps' for v in volumes))
        self.assertFalse(any(v['source'] == str(path) for v in volumes))
        self.assertFalse(any('synthetic' in str(v) for v in volumes))

    def test_config_change_selects_only_relevant_services(self):
        self.assertEqual(m.changed_config_services(['whatsapp/config.yaml', 'synapse/homeserver.yaml']),
                         ['mautrix-whatsapp', 'synapse'])

    def fixture(self):
        (self.root / '.env').write_text('POSTGRES_PASSWORD=synthetic\nLOCAL_LOCALPART=casey\n')
        for relative, body in {
            'release.json': json.dumps({'release': 'test', 'update_format': 1, 'state_version': 1, 'reads_state_versions': [1]}),
            'docker-compose.yml': 'services: {}\n',
            'hub/render-hub.sh': '#!/bin/bash\nexit 0\n',
            'imessage/bin/imessage-cli': 'untouched synthetic binary',
            'agents/uplink/local.env.local': 'LOCAL_USER=@casey:localhost\nLOCAL_TOKEN=synthetic\n',
            'synapse/localhost.signing.key': 'synthetic unchanged signing identity',
        }.items():
            p = self.root / relative; p.parent.mkdir(parents=True, exist_ok=True); p.write_text(body)
        m.ensure_manifest(self.root, env={})
        self.commands = []
        self.fail_activation = False
        self.sha = '0123456789abcdef0123456789abcdef01234567'

        def run(argv, **kwargs):
            self.commands.append(argv)
            out = b''
            if argv[:3] == ['git', 'rev-parse', 'HEAD']:
                out = (self.sha + '\n').encode()
            elif argv[:2] == ['git', 'archive']:
                with tarfile.open(fileobj=kwargs['stdout'], mode='w:') as archive:
                    for relative in ('release.json', 'docker-compose.yml', 'hub/render-hub.sh', 'requirements-host.txt'):
                        if (self.root / relative).exists():
                            archive.add(self.root / relative, arcname=relative)
            elif '--services' in argv:
                out = b'postgres\nsynapse\nviews\n'
            elif 'pg_dumpall' in argv:
                kwargs['stdout'].write(b'-- synthetic consistent database dump\n')
            elif 'up' in argv and self.fail_activation:
                self.fail_activation = False
                raise RuntimeError('simulated process crash during activation')
            return SimpleNamespace(stdout=out, returncode=0)
        return m.Updater(self.root, run=run)

    def test_apply_twice_preserves_credentials_native_binary_and_backup(self):
        updater = self.fixture()
        before = m.inventory(self.root)
        with patch.object(m.sys, 'platform', 'linux'), patch.object(updater, 'health'):
            updater.apply()
            updater.apply()
        self.assertEqual(m.inventory(self.root), before)
        self.assertEqual(sum('pg_dumpall' in cmd for cmd in self.commands), 1)
        self.assertFalse(any('down' in cmd or 'logout' in cmd or 'provision' in str(cmd) for cmd in self.commands))
        installed = json.loads(updater.installed_path.read_text())
        self.assertTrue(Path(installed['code_root'], '.staged').exists())
        journal = json.loads((updater.meta / 'journal.json').read_text())
        self.assertTrue(journal['complete'])
        checksums = json.loads(Path(journal['backup'], 'checksums.json').read_text())
        self.assertEqual(checksums['runtime.tar.gz'], m.hash_file(Path(journal['backup'], 'runtime.tar.gz')))

    def test_crash_resumes_without_replacing_snapshot_or_native_identity(self):
        updater = self.fixture()
        before = m.inventory(self.root)
        self.fail_activation = True
        with patch.object(m.sys, 'platform', 'linux'), patch.object(updater, 'health'):
            with self.assertRaises(RuntimeError):
                updater.apply()
            updater.apply()
        self.assertEqual(sum('pg_dumpall' in cmd for cmd in self.commands), 1)
        self.assertEqual(m.inventory(self.root), before)

    def test_resume_uses_persisted_dependency_interpreter_after_prepare(self):
        updater = self.fixture()
        (self.root / 'requirements-host.txt').write_text('synthetic pinned lock')
        python = str(self.root / '.beepa-venvs/fixture/bin/python3')
        def runtime(root, requirements):
            manifest = m.read_manifest(root)
            manifest['python_path'] = python
            m.save_manifest(root, manifest)
            return python
        seen = []
        activate = updater.activate
        def capture(release, manifest, compositions, journal):
            seen.append(manifest['python_path'])
            return activate(release, manifest, compositions, journal)
        self.fail_activation = True
        with patch.object(m.sys, 'platform', 'linux'), patch.object(updater, 'health'), \
                patch.object(m, 'ensure_runtime', side_effect=runtime) as ensure, \
                patch.object(updater, 'activate', side_effect=capture):
            with self.assertRaises(RuntimeError):
                updater.apply()
            updater.apply()
        self.assertEqual(ensure.call_count, 1)
        self.assertEqual(seen, [python, python])
        self.assertEqual(m.read_manifest(self.root)['python_path'], python)

    def test_dirty_tree_stops_before_any_docker_or_runtime_write(self):
        updater = m.Updater(self.root, run=lambda *args, **kwargs: SimpleNamespace(stdout=b' M file.py\n'))
        with self.assertRaisesRegex(ValueError, 'local changes'):
            updater.apply()
        self.assertFalse(updater.meta.exists())

    def test_upgrade_rollback_and_reapply_use_current_ledgers_and_fresh_backup(self):
        updater = self.fixture()
        with patch.object(m.sys, 'platform', 'linux'), patch.object(updater, 'health'):
            updater.apply()
            first = json.loads(updater.installed_path.read_text())
            self.sha = 'abcdef0123456789abcdef0123456789abcdef01'
            target = json.loads((self.root / 'release.json').read_text())
            target['release'] = 'next'
            (self.root / 'release.json').write_text(json.dumps(target))
            updater.apply()
            latest = json.loads(updater.installed_path.read_text())
            ledger = self.root / 'agents/uplink/state.db'
            with sqlite3.connect(ledger) as db:
                db.execute('CREATE TABLE outcomes (value TEXT)')
                db.execute("INSERT INTO outcomes VALUES ('current send outcomes must survive')")
            ledger_bytes = ledger.read_bytes()
            updater.rollback()
            self.assertEqual(json.loads(updater.installed_path.read_text())['git_sha'], first['git_sha'])
            self.assertEqual(ledger.read_bytes(), ledger_bytes)
            updater.apply()
            self.assertEqual(json.loads(updater.installed_path.read_text())['git_sha'], latest['git_sha'])
            self.assertEqual(ledger.read_bytes(), ledger_bytes)
        self.assertEqual(len(list((updater.meta / 'backups').iterdir())), 3)


    def test_backup_includes_committed_sqlite_wal_rows(self):
        updater = self.fixture()
        ledger = self.root / 'agents/uplink/state.db'
        connection = sqlite3.connect(ledger)
        self.addCleanup(connection.close)
        connection.execute('PRAGMA journal_mode=WAL')
        connection.execute('PRAGMA wal_autocheckpoint=0')
        connection.execute('CREATE TABLE outcomes (value TEXT)')
        connection.execute("INSERT INTO outcomes VALUES ('durable')")
        connection.commit()
        self.assertGreater(Path(str(ledger) + '-wal').stat().st_size, 0)
        with patch.object(m.sys, 'platform', 'linux'), patch.object(updater, 'health'):
            updater.apply()
        journal = json.loads((updater.meta / 'journal.json').read_text())
        with tarfile.open(Path(journal['backup']) / 'runtime.tar.gz') as archive:
            snapshot = self.root / 'restored.db'
            snapshot.write_bytes(archive.extractfile('agents/uplink/state.db').read())
        with sqlite3.connect(snapshot) as restored:
            self.assertEqual(restored.execute('SELECT value FROM outcomes').fetchone()[0], 'durable')

    def test_real_git_fork_fast_forward_keeps_tenant_state_and_refuses_divergence(self):
        canonical = self.root / 'canonical'
        fork = self.root / 'tenant'
        canonical.mkdir()
        def git(where, *args, check=True):
            return subprocess.run(['git', '-C', str(where), '-c', 'user.name=Fixture',
                '-c', 'user.email=fixture@example.invalid', *args], check=check, capture_output=True, text=True)
        git(canonical, 'init', '-b', 'stable')
        (canonical / '.gitignore').write_text('.env\n.beepa-install.json\n.beepa-update/\n')
        (canonical / 'release.json').write_text(json.dumps({'release':'one','state_version':1,'reads_state_versions':[1]}))
        git(canonical, 'add', '.')
        git(canonical, 'commit', '-m', 'fixture baseline')
        git(self.root, 'clone', str(canonical), str(fork))
        (fork / '.env').write_text('LOCAL_LOCALPART=casey\nLOCAL_TOKEN=tenant-secret\n')
        manifest = m.ensure_manifest(fork, env={})
        before = m.inventory(fork)
        (canonical / 'release.json').write_text(json.dumps({'release':'two','state_version':1,'reads_state_versions':[1]}))
        git(canonical, 'add', 'release.json'); git(canonical, 'commit', '-m', 'fixture release')
        git(fork, 'pull', '--ff-only')
        updater = m.Updater(fork)
        sha, release, _ = updater.inspect()
        self.assertEqual(release['release'], 'two')
        staged = updater.stage(sha)
        self.assertFalse((staged / '.env').exists())
        self.assertEqual(m.inventory(fork), before)
        self.assertEqual(m.read_manifest(fork)['install_id'], manifest['install_id'])
        (fork / 'tenant-source.txt').write_text('divergent tenant source')
        git(fork, 'add', 'tenant-source.txt'); git(fork, 'commit', '-m', 'fixture divergence')
        (canonical / 'next.txt').write_text('next release')
        git(canonical, 'add', 'next.txt'); git(canonical, 'commit', '-m', 'fixture next')
        old_head = git(fork, 'rev-parse', 'HEAD').stdout
        rejected = git(fork, 'pull', '--ff-only', check=False)
        self.assertNotEqual(rejected.returncode, 0)
        self.assertEqual(git(fork, 'rev-parse', 'HEAD').stdout, old_head)
        self.assertEqual(m.inventory(fork), before)


if __name__ == '__main__':
    unittest.main()
