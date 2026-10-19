"""Candidate CRM release contract exercised through the real isolated updater."""
import importlib.util
import json
from pathlib import Path
import sqlite3
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('updater_fixture', Path(__file__).with_name('update_lifecycle.test.py'))
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)
m=fixture.m
CRM_RELEASE=json.loads((Path(__file__).resolve().parents[2]/'release.json').read_text())


class CRMReleaseTest(fixture.UpdateTests):
    def test_crm_old_to_new_applies_but_both_downgrade_paths_stop_before_runtime(self):
        self.assertEqual(CRM_RELEASE['state_version'],2)
        self.assertEqual(CRM_RELEASE['reads_state_versions'],[1,2])
        self.assertEqual(CRM_RELEASE['wire_version'],1)
        self.assertEqual(CRM_RELEASE['reads_wire_versions'],[1])
        updater=self.fixture()
        old_release=json.loads((self.root/'release.json').read_text())
        with patch.object(m.sys,'platform','linux'),patch.object(updater,'health'):
            updater.apply()
            old_installed=json.loads(updater.installed_path.read_text())
            self.sha='abcdef0123456789abcdef0123456789abcdef01'
            (self.root/'release.json').write_text(json.dumps(CRM_RELEASE))
            updater.apply()
            installed=json.loads(updater.installed_path.read_text())
            self.assertEqual(installed['state_version'],2)
            self.assertEqual(installed['previous']['state_version'],1)
            self.assertEqual(installed['wire_version'],1)
            ledger=self.root/'agents/uplink/state.db'
            with sqlite3.connect(ledger) as db:
                db.execute('CREATE TABLE crm_attempts(item_id TEXT PRIMARY KEY,state TEXT)')
                db.execute("INSERT INTO crm_attempts VALUES('immutable-item','outcome_unknown')")
            preserved=ledger.read_bytes()
            manifest=m.read_manifest(self.root)
            before=len(self.commands)
            with self.assertRaisesRegex(ValueError,'not compatible'):
                updater.rollback()
            self.assertEqual(len(self.commands),before,'incompatible rollback cannot stop or activate any service')
            self.assertFalse((updater.meta/'rollback.json').exists())
            self.sha=old_installed['git_sha']
            (self.root/'release.json').write_text(json.dumps(old_release))
            with self.assertRaisesRegex(ValueError,'not compatible'):
                updater.apply()
            self.assertEqual(self.commands[before:],[['git','status','--porcelain','--untracked-files=no'],['git','rev-parse','HEAD']])
            self.assertEqual(json.loads(updater.installed_path.read_text()),installed)
            self.assertEqual(m.read_manifest(self.root),manifest)
            self.assertEqual(ledger.read_bytes(),preserved)


def load_tests(loader,standard,pattern):
    return unittest.TestSuite([CRMReleaseTest('test_crm_old_to_new_applies_but_both_downgrade_paths_stop_before_runtime')])

if __name__=='__main__':unittest.main()
