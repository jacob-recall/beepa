"""Parallel repair against isolated stores and an in-memory Matrix service."""
import contextlib
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
import urllib.error
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from imessage import repair_timestamps as repair


class ParallelRepair(unittest.TestCase):
    def test_parallel_apply_journals_every_write_and_repeat_is_read_only(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            (root/'imessage').mkdir()
            (root/'agents/uplink').mkdir(parents=True)
            config=dict(user_id='@owner:test',hs_url='http://local',bot_id='@imessagebot:test',
                        domain='test',as_token='fake-as',cli_path='/not-a-native-cli')
            (root/'imessage/daemon.json').write_text(json.dumps(config))
            with sqlite3.connect(root/'imessage/state.db') as db:
                db.executescript("""
                  CREATE TABLE map(chat_id TEXT,room_id TEXT);
                  INSERT INTO map VALUES('chat','!local:test');
                  CREATE TABLE event_map(chat_id TEXT,msg_id TEXT,event_id TEXT);
                  INSERT INTO event_map VALUES('chat','one','$one'),('chat','two','$two');
                """)
            with sqlite3.connect(root/'agents/uplink/state.db') as db:
                db.executescript("""
                  CREATE TABLE meta(k TEXT,v TEXT);
                  CREATE TABLE mirror_rooms(local_room_id TEXT,master_room_id TEXT,source TEXT);
                  INSERT INTO mirror_rooms VALUES('!local:test','!remote:test','imessage');
                  CREATE TABLE mirror_lifecycle(local_room_id TEXT,status TEXT);
                  INSERT INTO mirror_lifecycle VALUES('!local:test','live');
                  CREATE TABLE delivery_map(master_room_id TEXT,local_event_id TEXT,master_event_id TEXT);
                  INSERT INTO delivery_map VALUES('!remote:test','$one','$master-one'),('!remote:test','$two','$master-two');
                """)
            cfg=SimpleNamespace(local_user='@owner:test',local_hs='http://local',local_token='fake-local',
                                master_hs='http://master',master_user='@scoped:master',master_token='fake-master')
            link={'master_hs_url':cfg.master_hs,'master_user':cfg.master_user,'master_token':cfg.master_token}
            states={};writes=[]
            def mx(base,token,method,path,body=None,**kwargs):
                if path.endswith('com.jkali.master_link'):return link
                if path.endswith('com.jkali.share_override'):return {'state':'share'}
                if path.endswith('/state'):return [{'type':'m.room.create','sender':config['bot_id']}]
                if '/event/' in path:
                    eid=urllib.parse.unquote(path.rsplit('/',1)[-1])
                    return {'event_id':eid,'type':'m.room.message','sender':config['bot_id'] if base==cfg.local_hs else cfg.master_user,'content':{'body':'unchanged'}}
                key=(base,path)
                if method=='PUT':
                    journals=list((root/'.beepa-timestamp-repair').glob('*/journal.json'))
                    self.assertEqual(len(journals),1)
                    journal=json.loads(journals[0].read_text())
                    destination='local' if base==cfg.local_hs else 'master'
                    self.assertEqual(journal['entries'][destination+':'+path]['wanted'],body)
                    states[key]=body;writes.append(key);return {'event_id':'$correction'}
                if key not in states:raise urllib.error.HTTPError(path,404,'missing',{},None)
                return states[key]
            def cli(argv,**kwargs):
                self.assertIn('messages',argv)
                items=[{'id':mid,'timestamp':1704110400000+i,'cursor':str(i+1)} for i,mid in enumerate(('one','two'))]
                return subprocess.CompletedProcess(argv,0,json.dumps({'items':items,'hasMore':False}).encode(),b'')
            def run(apply):
                out=io.StringIO()
                argv=['repair','--root',str(root),'--workers','2']+(['--apply'] if apply else [])
                with patch.object(sys,'argv',argv),patch.object(repair,'read_manifest',return_value={}), \
                     patch.object(repair,'read_env',return_value={}),patch.object(repair,'Config',return_value=cfg), \
                     patch.object(repair,'_mx',side_effect=mx),patch('subprocess.run',side_effect=cli), \
                     contextlib.redirect_stdout(out):
                    repair.main()
                return json.loads(out.getvalue())
            before=run(False)
            self.assertEqual(before['local_would_correct'],2)
            self.assertEqual(before['master_would_correct'],2)
            applied=run(True)
            self.assertEqual(applied['local_corrected'],2)
            self.assertEqual(applied['master_corrected'],2)
            self.assertEqual(len(writes),4)
            after=run(False)
            self.assertEqual(after['local_already_correct'],2)
            self.assertEqual(after['master_already_correct'],2)
            self.assertEqual(len(writes),4)


if __name__=='__main__':
    unittest.main()
