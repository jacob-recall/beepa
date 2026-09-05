#!/usr/bin/env python3
"""Fault-injected master operations. No Docker, LaunchAgent or Tailscale runs."""
import io
import json
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]/'master'))
import lifecycle


class FakeOps(lifecycle.MasterOps):
    def __init__(self, root):
        self.calls, self.installed = [], []
        self.agents = [('master-enroll','test/enroll'),('master-gateway','test/gateway')]
        self.fail_stop = self.fail_health = self.fail_agent = False
        super().__init__(root, run=self.fake_run, compose=['docker','compose','-p','test'], agent_installer=self.fake_install)

    def fake_run(self, args, **kwargs):
        self.calls.append(list(args))
        if 'bootout' in args:
            self.agents = [pair for pair in self.agents if pair[1] != args[-1]]
        if 'stop' in args and self.fail_stop:
            raise RuntimeError('interrupted during quiesce')
        if 'pg_dump' in args:
            kwargs['stdout'].write(b'isolated pg_dump fixture')
        stdout = b'postgres\nsynapse\nlocal\nlocal-db\n' if 'ps' in args else b''
        return subprocess.CompletedProcess(args, 0, stdout, b'')

    def fake_install(self, root, name, **kwargs):
        if self.fail_agent:
            raise OSError('launchd unavailable')
        self.installed.append(name)

    def running_agents(self):
        return self.agents[:]

    def wait_gateway_drained(self):
        pass

    def stop_tailnet_ingress(self):
        self.ingress_disabled = True

    def wait_health(self):
        if self.fail_health:
            raise RuntimeError('Synapse not healthy')


class LifecycleTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root/'master/synapse').mkdir(parents=True)
        (self.root/'master/.env').write_text('MASTER_POSTGRES_PASSWORD=fixture\n')
        (self.root/'master/synapse/homeserver.yaml').write_text('server_name: master\ndatabase:\n  args:\n    password: old\n')
        self.ops = FakeOps(self.root)

    def external_manifest(self):
        state = self.root/'external-state'
        active = self.root/'active-release'
        (state/'master').mkdir(parents=True)
        (active/'master').mkdir(parents=True)
        (active/'master/lifecycle.py').write_text('# active fixture')
        manifest = {'config_version':1, 'local_localpart':'fixture', 'install_id':'test-install',
                    'state_initialized':True, 'state_root':str(state), 'code_root':str(active)}
        (self.root/'.beepa-install.json').write_text(json.dumps(manifest))
        return state.resolve(), active.resolve()

    def test_external_state_and_installed_code_are_used(self):
        state, active = self.external_manifest()
        ops = lifecycle.MasterOps(self.root, run=self.ops.fake_run, agent_installer=self.ops.fake_install)
        self.assertEqual(ops.root, state)
        self.assertEqual(ops.master, state/'master')
        self.assertEqual(ops.code_root, active)
        self.assertEqual(ops.env['BEEPA_INSTALL_ROOT'], str(state))
        self.assertIn(str(active/'master/docker-compose.master.yml'), ops.compose)
        self.assertIn(str(state/'master/.env'), ops.compose)
        self.assertEqual(ops.maintenance, state/'master/runtime/maintenance')
        with ops.lock():
            self.assertTrue((state/'.beepa-update/operation.lock').exists())
        with patch.object(ops, 'wait_health'):
            ops.resume({'services':[], 'agents':[('master-gateway','test/gateway')]})
        self.assertEqual(self.ops.installed, ['master-gateway'])
        self.assertFalse((self.root/'master/runtime/operation.json').exists())

    def test_persisted_master_override_controls_backup_sources(self):
        state, _active = self.external_manifest()
        custom = self.root/'separate-master'
        (custom/'synapse').mkdir(parents=True)
        (custom/'.env').write_text('MASTER_POSTGRES_PASSWORD=custom-fixture\n')
        (custom/'synapse/homeserver.yaml').write_text('server_name: master\n')
        manifest_path = self.root/'.beepa-install.json'
        manifest = json.loads(manifest_path.read_text())
        manifest['master_state_root'] = str(custom)
        manifest_path.write_text(json.dumps(manifest))
        ops = FakeOps(self.root)
        self.assertEqual(ops.master, custom.resolve())
        self.assertEqual(ops.env['BEEPA_MASTER_STATE_DIR'], str(custom.resolve()))
        self.assertEqual(ops.registry.path, custom.resolve()/'recovery.local.json')
        backup = ops.backup(self.root/'override-backup')
        with tarfile.open(backup/'master.tar.gz') as archive:
            self.assertEqual(archive.extractfile('master/.env').read(), b'MASTER_POSTGRES_PASSWORD=custom-fixture\n')
        self.assertTrue((custom/'runtime/operation.json').exists())
        self.assertFalse((state/'master/runtime/operation.json').exists())
        with patch.dict(lifecycle.os.environ, {'BEEPA_MASTER_STATE_DIR':str(self.root/'explicit-master')}):
            self.assertEqual(FakeOps(self.root).master, (self.root/'explicit-master').resolve())

    def test_checkout_cli_delegates_to_installed_release(self):
        _state, active = self.external_manifest()
        args = ['lifecycle.py','--root',str(self.root),'status']
        with patch.object(sys, 'argv', args), patch.object(lifecycle.os, 'execv', side_effect=SystemExit(0)) as execute:
            with self.assertRaises(SystemExit) as result:
                lifecycle.main()
        self.assertEqual(result.exception.code, 0)
        execute.assert_called_once_with(sys.executable, [sys.executable,str(active/'master/lifecycle.py'),*args[1:]])

    def test_backup_preserves_scope_and_restores_original_services(self):
        backup = self.ops.backup(self.root/'backup')
        self.assertFalse(self.ops.maintenance.exists())
        self.assertEqual(json.loads(self.ops.journal.read_text())['phase'], 'complete')
        self.assertEqual(self.ops.installed, ['master-enroll','master-gateway'])
        stops = [args for args in self.ops.calls if 'stop' in args]
        self.assertEqual(stops[0][-1], 'synapse')
        for args in stops + [args for args in self.ops.calls if 'up' in args]:
            self.assertNotIn('local', args)
            self.assertNotIn('local-db', args)
        self.assertEqual(self.ops.verify_backup(backup)['format'], 1)
        self.assertEqual((backup/'database.dump').stat().st_mode & 0o777, 0o600)

    def test_quiesce_interruption_retains_original_inventory_for_retry(self):
        self.ops.fail_stop = True
        with self.assertRaises(RuntimeError):
            self.ops.quiesce('restore','fixture-backup')
        saved = json.loads(self.ops.journal.read_text())
        self.assertEqual(len(saved['agents']), 2)
        self.assertEqual(saved['services'], ['postgres','synapse'])
        self.assertTrue(self.ops.maintenance.exists())
        self.assertEqual(self.ops.agents, [])
        self.ops.fail_stop = False
        restored = self.ops.quiesce('restore','fixture-backup')
        self.ops.resume(restored)
        self.assertEqual(self.ops.installed, ['master-enroll','master-gateway'])
        self.assertFalse(self.ops.maintenance.exists())

    def test_failed_health_retains_maintenance_until_same_backup_retry(self):
        self.ops.fail_health = True
        with self.assertRaises(RuntimeError):
            self.ops.backup(self.root/'backup')
        self.assertTrue(self.ops.maintenance.exists())
        self.assertEqual(self.ops.pending_operation()['phase'], 'resuming')
        self.ops.fail_health = False
        dumps_before = len([args for args in self.ops.calls if 'pg_dump' in args])
        self.ops.backup(self.root/'backup')
        self.assertEqual(len([args for args in self.ops.calls if 'pg_dump' in args]), dumps_before)
        self.assertFalse(self.ops.maintenance.exists())

    def test_failed_agent_resume_retains_maintenance(self):
        state = self.ops.quiesce()
        self.ops.fail_agent = True
        with self.assertRaises(OSError):
            self.ops.resume(state)
        self.assertTrue(self.ops.maintenance.exists())
        self.assertEqual(self.ops.pending_operation()['phase'], 'resuming')

    def test_restart_and_rebuild_refuse_unsanitized_pending_restore(self):
        self.ops.quiesce('restore','fixture-backup')
        before = len(self.ops.calls)
        with self.assertRaises(ValueError):
            self.ops.restart()
        with self.assertRaises(ValueError):
            self.ops.rebuild_archive()
        self.assertEqual(len(self.ops.calls), before)

    def test_different_operation_cannot_override_pending_restore(self):
        self.ops.quiesce('restore','fixture-backup')
        with self.assertRaises(ValueError):
            self.ops.quiesce('backup','different')
        self.assertEqual(self.ops.pending_operation()['operation'], 'restore')

    def test_preexisting_maintenance_is_not_cleared_by_backup(self):
        self.ops.maintenance.parent.mkdir(parents=True)
        self.ops.maintenance.touch()
        self.ops.backup(self.root/'backup')
        self.assertTrue(self.ops.maintenance.exists())

    def test_archive_rebuild_changes_only_epoch_and_preserves_pairing(self):
        token = 'a'*40
        initial = self.ops.registry.issue('alice','fixture-install-12345678',token)
        changed = self.ops.rebuild_archive()
        self.assertEqual(initial['master_authority_id'], changed['master_authority_id'])
        self.assertNotEqual(initial['master_data_epoch'], changed['master_data_epoch'])
        with self.ops.registry.authorized('fixture-install-12345678',token,changed['master_authority_id']) as (user, _):
            self.assertEqual(user, 'alice')
        self.assertFalse(any('dropdb' in args or 'pg_restore' in args for args in self.ops.calls))
        self.assertTrue(self.ops.ingress_disabled)

    def test_database_readiness_precedes_dump(self):
        self.ops.backup(self.root/'backup')
        ready = next(i for i,args in enumerate(self.ops.calls) if 'pg_isready' in args)
        dump = next(i for i,args in enumerate(self.ops.calls) if 'pg_dump' in args)
        self.assertLess(ready, dump)

    def test_bad_checksum_and_out_of_scope_archive_are_rejected(self):
        backup = self.ops.backup(self.root/'backup')
        original = (backup/'database.dump').read_bytes()
        (backup/'database.dump').write_bytes(b'corruption')
        with self.assertRaises(ValueError):
            self.ops.verify_backup(backup)

        (backup/'database.dump').write_bytes(original)
        with tarfile.open(backup/'master.tar.gz', 'w:gz') as archive:
            item = tarfile.TarInfo('agents/uplink/state.db')
            item.size = 4
            archive.addfile(item, io.BytesIO(b'evil'))
        metadata = json.loads((backup/'backup.json').read_text())
        metadata['checksums']['master.tar.gz'] = lifecycle.digest(backup/'master.tar.gz')
        (backup/'backup.json').write_text(json.dumps(metadata))
        with self.assertRaises(ValueError):
            self.ops.verify_backup(backup)

    def test_master_configuration_merge_state_is_in_snapshot(self):
        config = self.root/'master/.beepa-config/overrides/synapse/homeserver.yaml'
        config.parent.mkdir(parents=True)
        config.write_text('operator_setting: retained\n')
        backup = self.ops.backup(self.root/'backup')
        with tarfile.open(backup/'master.tar.gz') as archive:
            self.assertEqual(archive.extractfile('master/.beepa-config/overrides/synapse/homeserver.yaml').read(),
                             b'operator_setting: retained\n')
        self.ops.verify_backup(backup)

    def test_existing_cluster_password_replaces_only_database_scalar(self):
        yaml = 'server_name: master\ndatabase:\n  name: psycopg2\n  args:\n    password: "old"\n    host: postgres\nmacaroon_secret_key: "preserve"\n'
        result = lifecycle.with_database_password(yaml, 'new"password')
        self.assertIn('password: "new\\"password"', result)
        self.assertIn('macaroon_secret_key: "preserve"', result)
        value = {'database':{'args':{'password':'old','host':'postgres'}},'macaroon_secret_key':'preserve'}
        changed = json.loads(lifecycle.with_database_password(json.dumps(value),'new'))
        self.assertEqual(changed['database']['args']['password'], 'new')
        self.assertEqual(changed['macaroon_secret_key'], 'preserve')

    def test_archive_retirement_failure_resumes_at_saved_step_before_epoch(self):
        identity = self.ops.registry.manifest()
        self.ops.discover_archive_rooms = lambda: [{'user':'alice','owner':'@alice:master','space':'!space:master','room':'!mirror:master','step':0}]
        self.ops.archive_credentials = lambda user: ('fixture-token','@alice:master','@manager:master')
        calls, failed = [], [False]
        def matrix(token, method, path, body=None):
            calls.append((method,path))
            if path.endswith('/kick') and not failed[0]:
                failed[0] = True
                raise OSError('kick unavailable')
            if '/m.room.member/' in path:
                return {'membership':'join'}
            return {}
        self.ops.matrix = matrix
        with self.assertRaises(OSError):
            self.ops.rebuild_archive()
        self.assertEqual(self.ops.registry.manifest()['master_data_epoch'], identity['master_data_epoch'])
        self.assertEqual(self.ops.pending_operation()['retirements'][0]['step'], 1)
        self.assertTrue(self.ops.maintenance.exists())
        self.assertFalse(any(path.endswith('/leave') for _,path in calls))
        result = self.ops.rebuild_archive()
        self.assertEqual(result['retired_rooms'], 1)
        self.assertNotEqual(result['master_data_epoch'], identity['master_data_epoch'])
        self.assertEqual(sum('/m.space.child/' in path for _,path in calls), 1)
        self.assertFalse(self.ops.maintenance.exists())

    def test_restart_actually_restarts_only_master_services(self):
        self.ops.restart()
        restarted = [args for args in self.ops.calls if 'restart' in args]
        self.assertEqual(restarted[0][-2:], ['postgres','synapse'])


if __name__ == '__main__':
    unittest.main()
