"""Isolated exact-component repair: no native executable or live Matrix."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location('attachment_repair', ROOT/'imessage/repair_attachments.py')
repair = importlib.util.module_from_spec(spec)
spec.loader.exec_module(repair)


class AttachmentRepair(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root/'imessage').mkdir()
        (self.root/'agents/uplink').mkdir(parents=True)
        self.image = self.root/'image.png'
        self.image.write_bytes(b'fixture')
        self.config = dict(user_id='@owner:test',hs_url='http://test.invalid',
                           cli_path='/no-native-cli',domain='test',bot_id='@imessagebot:test',
                           as_token='fake',attachment_allow_prefixes=[str(self.root)])
        (self.root/'imessage/daemon.json').write_text(json.dumps(self.config))
        self.db = sqlite3.connect(self.root/'imessage/state.db')
        self.addCleanup(self.db.close)
        self.db.executescript("""
          CREATE TABLE map(chat_id TEXT,room_id TEXT);
          INSERT INTO map VALUES('chat','!room:test');
          CREATE TABLE inbound_component(room_id TEXT,msg_id TEXT,component TEXT,event_id TEXT,status TEXT,
             PRIMARY KEY(room_id,msg_id,component));
          INSERT INTO inbound_component VALUES('!room:test','native1','attachment:a1','','refused_invalid_path');
          CREATE TABLE event_map(chat_id TEXT,msg_id TEXT,event_id TEXT,sender TEXT,body_hash TEXT,
             PRIMARY KEY(chat_id,msg_id));
          CREATE TABLE outbound_event(event_id TEXT,state TEXT);
          INSERT INTO outbound_event VALUES('$protected','dispatching');
          CREATE TABLE ledger(h TEXT,ts REAL);
          INSERT INTO ledger VALUES('protected',1);
        """)
        with sqlite3.connect(self.root/'agents/uplink/state.db') as up:
            up.executescript("""
              CREATE TABLE mirror_rooms(local_room_id TEXT,master_room_id TEXT,source TEXT);
              INSERT INTO mirror_rooms VALUES('!room:test','!master:test','imessage');
              CREATE TABLE mirror_lifecycle(local_room_id TEXT,status TEXT);
              INSERT INTO mirror_lifecycle VALUES('!room:test','live');
            """)
        self.message = dict(id='native1',timestamp=1704110400000,isSender=True,
                            attachments=[dict(id='a1',srcURL=self.image.as_uri())])
        self.level = 'share'
        self.sent = []

    def run_repair(self, apply=False):
        def matrix(method,path,body=None,**kwargs):
            if path.endswith('/state'):
                return [{'type':'m.room.create','sender':'@imessagebot:test'}]
            if '/send/' in path:
                journals=list((self.root/'.beepa-repair-verification').glob('attachments-*/journal.json'))
                self.assertTrue(journals)
                self.assertEqual(json.loads(journals[-1].read_text())['entries'][0]['outcome'],
                                 'dispatching_local_component')
                self.sent.append(body)
                return {'event_id':'$recovered'}
            if '/event/' in path:
                return {'content':self.sent[-1]}
            self.fail('unexpected Matrix mutation: '+path)
        argv=['repair','--root',str(self.root)]+(['--apply'] if apply else [])
        out=io.StringIO()
        with patch.object(sys,'argv',argv), \
             patch.object(repair.r,'read_manifest',return_value={'state_root':str(self.root),'owner_uid':os.getuid()}), \
             patch.object(repair.r,'read_env',return_value={}), \
             patch.object(repair.r,'Config',return_value=SimpleNamespace(local_user='@owner:test',local_hs='http://test.invalid',local_token='fake')), \
             patch.object(repair.r,'_mx',side_effect=lambda *a,**k:{'state':self.level}), \
             patch.object(repair.d,'cli_json',return_value={'items':[self.message],'hasMore':False}), \
             patch.object(repair.d,'mx',side_effect=matrix), \
             patch.object(repair.d,'upload_media',return_value='mxc://test/fixture'), \
             patch.object(repair.subprocess,'run',return_value=SimpleNamespace(returncode=1)), \
             patch.object(repair.d,'initialize',side_effect=AssertionError('outbound recovery forbidden')), \
             contextlib.redirect_stdout(out):
            repair.main()
        if repair.d.DB is not None:
            repair.d.DB.close()
            repair.d.DB=None
        return json.loads(out.getvalue())

    def test_audit_apply_and_repeat_preserve_ledgers_and_native_date(self):
        self.assertEqual(self.run_repair()['counts'],{'would_recover':1})
        self.assertFalse(self.sent)
        self.assertEqual(self.run_repair(True)['counts'],{'recovered':1})
        self.assertEqual(self.sent[0]['com.jkali.origin_ts'],1704110400000)
        self.assertEqual(self.run_repair()['counts'],{})
        self.assertEqual(len(self.sent),1)
        self.assertEqual(self.db.execute('SELECT * FROM outbound_event').fetchall(),[('$protected','dispatching')])
        self.assertEqual(self.db.execute('SELECT * FROM ledger').fetchall(),[('protected',1.0)])
        self.assertEqual(self.db.execute('SELECT event_id,status FROM inbound_component').fetchone(),('$recovered','confirmed'))

    def test_private_and_loading_components_are_skipped(self):
        self.level='private'
        self.assertEqual(self.run_repair()['counts'],{'private_or_not_live':1})
        self.level='share'
        self.message['attachments'][0].update(srcURL='',loading=True)
        self.assertEqual(self.run_repair()['counts'],{'native_attachment_loading':1})
        self.assertFalse(self.sent)

    def test_path_refusals_are_recoverable_only_inside_reviewed_roots(self):
        self.db.execute("UPDATE inbound_component SET status='refused_path'")
        self.db.commit()
        self.assertEqual(self.run_repair()['counts'],{'would_recover':1})
        self.message['attachments'][0]['srcURL']='file:///etc/hosts'
        self.assertEqual(self.run_repair()['counts'],{'path_not_allowed':1})
        self.assertFalse(self.sent)


if __name__=='__main__':
    unittest.main()
