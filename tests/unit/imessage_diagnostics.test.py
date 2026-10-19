#!/usr/bin/env python3
import importlib.util
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('diagnose', Path(__file__).resolve().parents[2] / 'imessage/diagnose-importer.py')
diagnose = importlib.util.module_from_spec(spec)
spec.loader.exec_module(diagnose)


class DiagnosticsTest(unittest.TestCase):
    def test_missing_database_never_created(self):
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaises(ValueError): diagnose.snapshot(root)
            self.assertEqual(list(Path(root).iterdir()), [])

    def test_old_and_new_schema_no_private_values_or_mutations(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'state.db'
            db = sqlite3.connect(path)
            db.executescript('''CREATE TABLE inbound_pending(chat_id,msg_id,status);
              CREATE TABLE inbound_component(status);
              CREATE TABLE outbound_event(state);
              CREATE TABLE map(chat_id,room_id);
              INSERT INTO inbound_pending VALUES ('secret-chat','secret-id','source_unavailable');
              INSERT INTO inbound_pending VALUES ('secret-chat','other-id','private-error-secret');
              INSERT INTO inbound_component VALUES ('refused_path'),('confirmed:private-hash');
              INSERT INTO outbound_event VALUES ('ambiguous');''')
            before = path.read_bytes()
            old = diagnose.snapshot(root)
            self.assertFalse(old['retry_scheduler_installed'])
            self.assertEqual(old['inbound_by_reason'], {'source_unavailable': 1, 'other': 1})
            self.assertEqual(old['terminal_attachment_refusals'], {'refused_path': 1})
            self.assertEqual(old['outbound_by_state'], {'ambiguous': 1})
            self.assertEqual(path.read_bytes(), before)
            db.executescript('''CREATE TABLE inbound_retry(chat_id,msg_id,next_attempt);
              INSERT INTO inbound_retry VALUES ('secret-chat','secret-id',500);''')
            new = diagnose.snapshot(root, now=100)
            self.assertEqual((new['retry_due'], new['retry_deferred']), (1, 1))
            self.assertNotIn('secret', json.dumps(new))
            self.assertNotIn('private', json.dumps(new))
            db.close()

    def test_manifest_state_root_and_explicit_override(self):
        with tempfile.TemporaryDirectory() as root:
            manifest = Path(root) / '.beepa-install.json'
            manifest.write_text(json.dumps({'state_root': root+'/retained', 'state_initialized': True}))
            self.assertEqual(diagnose.resolve_state(root), Path(root+'/retained/imessage'))
            self.assertEqual(diagnose.resolve_state(root, root+'/override'), Path(root+'/override').resolve())


if __name__ == '__main__': unittest.main()
