import json
from pathlib import Path
import secrets
import sys
import tempfile
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'master'))
from recovery import Registry, RecoveryError


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'recovery.local.json'
        self.registry = Registry(self.path)
        self.token = secrets.token_urlsafe(32)
        self.install = 'fixture-install-12345678'

    def test_restart_and_epoch_keep_pairing(self):
        before = self.registry.issue('alice', self.install, self.token)
        self.assertEqual(before['wire_version'], 1)
        self.assertEqual(before['reads_wire_versions'], [1])
        after = Registry(self.path).bump_epoch()
        self.assertEqual(before['master_authority_id'], after['master_authority_id'])
        self.assertNotEqual(before['master_data_epoch'], after['master_data_epoch'])
        with self.registry.authorized(self.install, self.token, after['master_authority_id']) as (user, meta):
            self.assertEqual(user, 'alice')
            self.assertEqual(meta, after)
        self.assertNotIn(self.token, self.path.read_text())
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_wrong_install_token_authority_and_revocation(self):
        meta = self.registry.issue('alice', self.install, self.token)
        for install, token, authority in [
            ('different-install-1234', self.token, meta['master_authority_id']),
            (self.install, 'x'*40, meta['master_authority_id']),
            (self.install, self.token, 'changed-authority')]:
            with self.assertRaises(RecoveryError), self.registry.authorized(install, token, authority):
                pass
        self.registry.revoke_user('alice')
        with self.assertRaises(RecoveryError), self.registry.authorized(self.install, self.token, meta['master_authority_id']):
            pass
        with self.assertRaises(RecoveryError):
            self.registry.issue('alice', self.install, secrets.token_urlsafe(32))

    def test_cannot_reassign_installation(self):
        self.registry.issue('alice', self.install, self.token)
        with self.assertRaises(RecoveryError):
            self.registry.issue('bob', self.install, self.token)

    def test_lost_registry_is_new_authority(self):
        old = self.registry.manifest()
        other = Registry(self.path.with_name('new.json')).manifest()
        self.assertNotEqual(old['master_authority_id'], other['master_authority_id'])


if __name__ == '__main__':
    unittest.main()
