"""Real SQLite/ControlCore recovery, synthetic credentials, no external services."""
import importlib.util
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT/'master'))
import control_recovery as recovery
from notifications import Notifications

SEED = r'''
import {ControlCore} from './master/control-core.mjs';
import {NotificationRouting} from './master/notification-routing.mjs';
const c=new ControlCore({path:process.argv[2],domain:{},telegram:async()=>{throw Error('No transport in fixture');},model:async()=>{throw Error('No model in fixture');}});
c.bindTransport({id:1,is_bot:true,username:'fixture_bot'});c.set('operator',{user:7,chat:7});c.set('pair',{hash:'old',expires:9999999999999});
for(const state of ['pending','executing','submitted','unknown']){c.draft({id:'draft-'+state,kind:'send',created:1,body:'Synthetic review',room_id:'!room:test'});c.db.prepare('UPDATE drafts SET state=? WHERE id=?').run(state,'draft-'+state);}
for(const state of ['pending','sending','accepted','unknown']){c.enqueue('notice-'+state,'Synthetic alert');c.db.prepare('UPDATE outbox SET state=? WHERE id=?').run(state,'notice-'+state+':0');}
c.enqueueCommand({request_id:'queued',text:'/help'});c.enqueueCommand({request_id:'processing',text:'/lists'});c.db.prepare("UPDATE api_commands SET state='processing' WHERE id='processing'").run();c.set('api:processing',{state:'processing'});
c.ingest([{update_id:1,message:{text:'Synthetic old command'}}]);c.db.prepare("INSERT INTO cursors VALUES('!room:test','before-restore',1)").run();
const router=new NotificationRouting({db:c.db}),source={kind:'message',room:'!room:test',event:'$event',outgoing:false},rules=[{room:source.room,list_id:'list',revision:1,channels:['browser','telegram']}];
const id=router.observe(source,rules);router.claim(router.plan(id,{rules,ready:['list']})[0]);
c.db.close();
'''


class RecoveryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name);self.master = self.root/'master';self.master.mkdir()
        self.control = self.master/'control.local';self.control.mkdir()
        subprocess.run(['node','--input-type=module','-',str(self.control/'control.db')], input=SEED, text=True, cwd=ROOT, check=True, capture_output=True)
        (self.control/'enabled').touch()
        (self.control/'config.json').write_text(json.dumps({'manager':'@old:test','manager_token':'old-fixture-token','telegram_token':'fixture-bot-token','openrouter_key':'fixture-model-key'}))
        (self.control/'enroll.log').write_text('Log must not enter an archive')
        self.notifications = self.master/'notifications.local'
        service = Notifications(self.notifications,lambda _:None,lambda:{},push=lambda *_:201)
        with service.db:
            service.db.execute("INSERT INTO devices VALUES('browser','{}',1)")
            for state in ['pending','sending','accepted','unknown']:
                service.db.execute("INSERT INTO history(id,room,event,title,created) VALUES(?,?,?,?,1)",(state,'!room:test','$event','Synthetic'))
                service.db.execute('INSERT INTO deliveries(id,device,state) VALUES(?,?,?)',(state,'browser',state))
            service.db.execute("INSERT INTO control_metadata VALUES('dispatcher_handoff',?)",(json.dumps({'id':'a'*32,'owner':'control','version':1,'follows':[{'room':'!room:test','cursor':'old','started':1}]}),))
        service.db.close();(self.notifications/'vapid.pem').write_text('fixture-key')

    def stage(self):
        stage = self.root/'stage';stage.mkdir()
        files = recovery.snapshot_features(self.master, self.root/'normalized')
        for source, relative in files:
            target=stage/relative;target.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(source,target)
        return stage, files

    def test_snapshot_excludes_logs_and_sidecars_and_includes_committed_wal(self):
        db=sqlite3.connect(self.control/'control.db');self.addCleanup(db.close)
        db.execute('PRAGMA journal_mode=WAL');db.execute("INSERT INTO kv VALUES('wal-evidence','true')");db.commit()
        stage,files=self.stage();names=[name for _,name in files]
        self.assertNotIn('master/control.local/enroll.log',names)
        self.assertFalse(any(n.endswith(('-wal','-shm')) for n in names))
        copy=sqlite3.connect(stage/'master/control.local/control.db');self.addCleanup(copy.close)
        self.assertEqual(copy.execute("SELECT v FROM kv WHERE k='wal-evidence'").fetchone()[0],'true')
        self.assertTrue(all(path.stat().st_mode & 0o777 == 0o600 for path,_ in files))

    def test_disaster_restore_preserves_evidence_but_requires_fresh_pairing_and_review(self):
        stage,_=self.stage();shutil.rmtree(self.control);shutil.rmtree(self.notifications)
        retained=recovery.feature_inventory(self.master);recovery.restore_features(self.master,stage,retained)
        with sqlite3.connect(self.control/'control.db') as db:
            self.assertIsNone(db.execute("SELECT v FROM kv WHERE k='operator'").fetchone())
            self.assertIsNone(db.execute("SELECT v FROM kv WHERE k='pair'").fetchone())
            self.assertEqual(dict(db.execute('SELECT id,state FROM drafts'))['draft-pending'],'review_required')
            self.assertEqual(dict(db.execute('SELECT id,state FROM drafts'))['draft-executing'],'unknown')
            self.assertEqual(dict(db.execute('SELECT id,state FROM outbox'))['notice-accepted:0'],'accepted')
            self.assertEqual(dict(db.execute('SELECT id,state FROM outbox'))['notice-sending:0'],'unknown')
            self.assertEqual(dict(db.execute('SELECT id,state FROM api_commands')),{'queued':'canceled','processing':'unknown'})
            self.assertEqual(db.execute('SELECT count(*) FROM cursors').fetchone()[0],0)
            self.assertEqual(db.execute('SELECT state FROM notification_route_attempts').fetchone()[0],'unknown')
        with sqlite3.connect(self.notifications/'notifications.db') as db:
            self.assertEqual(db.execute('SELECT enabled FROM devices').fetchone()[0],0)
            self.assertEqual(dict(db.execute('SELECT id,state FROM deliveries')),{'pending':'canceled','sending':'unknown','accepted':'accepted','unknown':'unknown'})
            self.assertEqual(json.loads(db.execute("SELECT value FROM control_metadata WHERE key='dispatcher_handoff'").fetchone()[0])['follows'],[])
        # Exercise production startup and consumers: zero calls to a transport.
        script=r'''
import assert from 'node:assert/strict';import {ControlCore} from './master/control-core.mjs';
const c=new ControlCore({path:process.argv[2],domain:{},telegram:async()=>{throw Error('Restored work was replayed');},model:async()=>{throw Error('Restored command was replayed');}});
assert.equal(c.value('operator'),null);assert.equal(c.nextCommandWork(),null);await c.flush();await c.process();assert.equal((await c.confirm('draft-pending')).state,'review_required');c.db.close();
'''
        subprocess.run(['node','--input-type=module','-',str(self.control/'control.db')],input=script,text=True,cwd=ROOT,check=True,capture_output=True)

    def test_current_state_wins_over_old_snapshot_and_retry_keeps_that_decision(self):
        stage,_=self.stage();retained=recovery.feature_inventory(self.master)
        with sqlite3.connect(self.control/'control.db') as db:
            db.execute("UPDATE outbox SET state='accepted' WHERE id='notice-pending:0'")
            db.execute("UPDATE kv SET v=? WHERE k='operator'",(json.dumps({'user':8,'chat':8}),))
        (self.notifications/'vapid.pem').write_text('newer-fixture-key')
        for _ in range(2): recovery.restore_features(self.master,stage,retained)
        with sqlite3.connect(self.control/'control.db') as db:
            self.assertEqual(json.loads(db.execute("SELECT v FROM kv WHERE k='operator'").fetchone()[0])['user'],8)
            self.assertEqual(db.execute("SELECT state FROM outbox WHERE id='notice-pending:0'").fetchone()[0],'accepted')
        self.assertEqual((self.notifications/'vapid.pem').read_text(),'newer-fixture-key')
        with sqlite3.connect(self.notifications/'notifications.db') as db:self.assertEqual(db.execute('SELECT enabled FROM devices').fetchone()[0],1)

    def test_retry_does_not_trust_a_partly_restored_old_pairing_as_current(self):
        stage,_=self.stage();shutil.rmtree(self.control);shutil.rmtree(self.notifications)
        retained=recovery.feature_inventory(self.master)
        shutil.copytree(stage/'master/control.local',self.control)
        recovery.restore_features(self.master,stage,retained)
        with sqlite3.connect(self.control/'control.db') as db:self.assertIsNone(db.execute("SELECT v FROM kv WHERE k='operator'").fetchone())

    def test_credentials_are_refreshed_without_restoring_old_matrix_auth(self):
        recovery.refresh_manager_credentials(self.master,{'MASTER_MANAGER_USER':'@new:test','MASTER_MANAGER_TOKEN':'new-fixture-token','MASTER_TEAMMATES':'alice','MASTER_ALICE_USER':'@alice:test','MASTER_SPACE_ALICE':'!alice:test'})
        config=json.loads((self.control/'config.json').read_text())
        self.assertEqual(config['manager_token'],'new-fixture-token');self.assertEqual(config['telegram_token'],'fixture-bot-token')
        self.assertEqual(config['roster'],{'@alice:test':'!alice:test'});self.assertEqual((self.control/'config.json').stat().st_mode&0o777,0o600)

    def test_symlinked_feature_state_cannot_escape_backup_or_restore_scope(self):
        outside=self.root/'elsewhere';outside.mkdir();shutil.rmtree(self.control);self.control.symlink_to(outside,target_is_directory=True)
        with self.assertRaisesRegex(ValueError,'symbolic|escapes'):recovery.snapshot_features(self.master,self.root/'snapshot')


if __name__=='__main__':unittest.main()
