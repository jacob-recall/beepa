#!/usr/bin/env python3
import importlib.util
from pathlib import Path
import tempfile
import unittest
import shutil
import subprocess
import os

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('managed_config', ROOT / 'hub/managed_config.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class ConfigTests(unittest.TestCase):
    def test_operator_change_survives_upstream_disjoint_change(self):
        self.assertEqual(m.merge('history: 500\nport: 10\n', 'history: 9000\nport: 10\n',
                                 'history: 500\nport: 20\n'), 'history: 9000\nport: 20\n')

    def test_conflicting_changes_are_not_silently_overwritten(self):
        with self.assertRaises(m.ConfigConflict):
            m.merge('history: 500\n', 'history: 9000\n', 'history: 200\n')

    def test_adoption_preserves_legacy_config_and_repeat_is_stable(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); stage = root / 'stage'; stage.mkdir()
            (stage / 'bridge.yaml').write_text('history: 500\nport: 10\n')
            (root / 'bridge.yaml').write_text('history: 9000\nport: 10\n')
            m.activate(root, stage)
            self.assertEqual((root / 'bridge.yaml').read_text(), 'history: 9000\nport: 10\n')
            (stage / 'bridge.yaml').write_text('history: 500\nport: 20\n')
            m.activate(root, stage)
            m.activate(root, stage)
            self.assertEqual((root / 'bridge.yaml').read_text(), 'history: 9000\nport: 20\n')

    def test_conflict_does_not_apply_other_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); stage = root / 'stage'; stage.mkdir()
            for name in ('a.yaml', 'b.yaml'):
                (stage / name).write_text('x: 1\n')
            m.activate(root, stage)
            (root / 'b.yaml').write_text('x: 3\n')
            for name in ('a.yaml', 'b.yaml'):
                (stage / name).write_text('x: 2\n')
            with self.assertRaises(m.ConfigConflict):
                m.activate(root, stage)
            self.assertEqual((root / 'a.yaml').read_text(), 'x: 1\n')
            self.assertEqual((root / 'b.yaml').read_text(), 'x: 3\n')

    def test_same_edit_and_insertions(self):
        self.assertEqual(m.merge('a\nb\n', 'a\nc\n', 'a\nc\n'), 'a\nc\n')
        self.assertEqual(m.merge('a\nb\n', 'extra\na\nb\n', 'a\nb\nnew\n'), 'extra\na\nb\nnew\n')

    def test_real_renderer_rerun_preserves_customized_history_and_credentials(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            shutil.copytree(ROOT / 'hub', root / 'hub', ignore=shutil.ignore_patterns('.local-user.local'))
            shutil.copy2(ROOT / 'install_config.py', root / 'install_config.py')
            (root / '.env').write_text('POSTGRES_PASSWORD=synthetic\nLOCAL_LOCALPART=casey\n')
            (root / 'synapse').mkdir()
            (root / 'synapse/localhost.signing.key').write_text('synthetic-preserved-signing-key')
            env = dict(os.environ, BEEPA_INSTALL_ROOT=str(root), OUT_ROOT=str(root))
            command = ['/bin/bash', str(root / 'hub/render-hub.sh')]
            subprocess.run(command, env=env, check=True, capture_output=True)
            config = root / 'whatsapp/config.yaml'
            original = config.read_text()
            config.write_text(original + '\n# operator history policy retained\n')
            secrets = (root / 'synapse/.hub-secrets.local').read_bytes()
            subprocess.run(command, env=env, check=True, capture_output=True)
            subprocess.run(command, env=env, check=True, capture_output=True)
            self.assertEqual(config.read_text(), original + '\n# operator history policy retained\n')
            self.assertEqual((root / 'synapse/.hub-secrets.local').read_bytes(), secrets)
            self.assertIn('@casey:localhost', config.read_text())


    def test_master_renderer_preserves_edits_keys_and_applies_new_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            code = Path(tmp) / 'release'
            state = Path(tmp) / 'external state'
            (code / 'master').mkdir(parents=True)
            (code / 'hub').mkdir()
            shutil.copy2(ROOT / 'master/setup.sh', code / 'master/setup.sh')
            shutil.copy2(ROOT / 'hub/managed_config.py', code / 'hub/managed_config.py')
            shutil.copy2(ROOT / 'install_config.py', code / 'install_config.py')
            (state / 'master/synapse').mkdir(parents=True)
            (state / 'master/.env').write_text('MASTER_POSTGRES_PASSWORD=synthetic\n')
            key = state / 'master/synapse/master.signing.key'
            key.write_text('synthetic-signing-identity')
            env = dict(os.environ, BEEPA_INSTALL_ROOT=str(state))
            env.pop('BEEPA_MASTER_STATE_DIR', None)
            command = ['/bin/bash', str(code / 'master/setup.sh')]
            subprocess.run(command, env=env, check=True, capture_output=True)
            config = state / 'master/synapse/homeserver.yaml'
            config.write_text(config.read_text().replace('burst_count: 200', 'burst_count: 777'))
            secrets = (state / 'master/synapse/.secrets.local').read_bytes()
            source = code / 'master/setup.sh'
            source.write_text(source.read_text().replace('sync_response_cache_duration: 0', 'sync_response_cache_duration: 1s'))
            env['BEEPA_UPDATE'] = '1'
            env['TEAMMATE_PASSWORD_KEY'] = 'must-not-rotate-during-update'
            subprocess.run(command, env=env, check=True, capture_output=True)
            self.assertIn('burst_count: 777', config.read_text())
            self.assertIn('sync_response_cache_duration: 1s', config.read_text())
            self.assertEqual((state / 'master/synapse/.secrets.local').read_bytes(), secrets)
            self.assertEqual(key.read_text(), 'synthetic-signing-identity')
            self.assertFalse((code / 'master/synapse').exists())
            before = config.read_bytes()
            source.write_text(source.read_text().replace('burst_count: 200', 'burst_count: 400'))
            failed = subprocess.run(command, env=env, capture_output=True)
            self.assertNotEqual(failed.returncode, 0)
            self.assertEqual(config.read_bytes(), before)
            key.unlink()
            failed = subprocess.run(command, env=env, capture_output=True)
            self.assertNotEqual(failed.returncode, 0)
            self.assertFalse(key.exists())


if __name__ == '__main__':
    unittest.main()
