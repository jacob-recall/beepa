"""Staged iMessage code must read contacts from the retained installation."""
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'imessage'))
import daemon


class ContactsPathTests(unittest.TestCase):
    def test_staged_code_uses_external_state_and_explicit_override(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for relative, name in [('state/agents/contacts/contacts.db', 'Retained contact'),
                                   ('override/contacts.db', 'Explicit contact')]:
                path = root / relative
                path.parent.mkdir(parents=True)
                with sqlite3.connect(path) as db:
                    db.execute('CREATE TABLE contacts(network_id TEXT, display_name TEXT, deleted INTEGER)')
                    db.execute('INSERT INTO contacts VALUES (?,?,0)', ('fixture@example.invalid', name))
            with patch.dict(os.environ, {'BEEPA_INSTALL_ROOT': str(root / 'state'), 'CONTACTS_DB': ''}), \
                 patch.object(daemon, 'BASE', str(root / 'release/imessage')):
                self.assertEqual(daemon.local_contact_name('fixture@example.invalid'), 'Retained contact')
                with patch.dict(os.environ, {'CONTACTS_DB': str(root / 'override/contacts.db')}):
                    self.assertEqual(daemon.local_contact_name('fixture@example.invalid'), 'Explicit contact')

    def test_legacy_install_keeps_its_existing_location(self):
        with patch.dict(os.environ, {'BEEPA_INSTALL_ROOT': '', 'CONTACTS_DB': ''}):
            self.assertEqual(daemon.contacts_db_path(), os.path.realpath(os.path.join(
                daemon.BASE, '..', 'agents', 'contacts', 'contacts.db')))


if __name__ == '__main__':
    unittest.main()
