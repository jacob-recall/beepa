"""Exact decision relay identity, retry, and cross-account history boundaries."""
import json
from pathlib import Path
import sqlite3
import sys
import types
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'agents/uplink'))
from proposal_status import relay, DECISION

class RelayTests(unittest.TestCase):
    def setUp(self):
        self.db=sqlite3.connect(':memory:')
        self.db.execute('CREATE TABLE proposal_map(master_event_id TEXT PRIMARY KEY,local_event_id TEXT,outcome TEXT)')
        self.db.executemany('INSERT INTO proposal_map VALUES (?,?,?)',[('$one','$local1','fallback'),('$two','$local2','refused')])
        self.db.commit()
        self.meta={'local_proposals_room':'!p:local','master_proposals_room':'!p:master'}
        self.state=[];self.writes=[];self.offline=False;self.foreign=False
        self.u=types.SimpleNamespace(db=self.db,cfg=types.SimpleNamespace(local_user='@self:local',manager_mxid='@manager:master'),
            meta_get=self.meta.get,meta_set=self.meta.__setitem__,active_link_for_dispatch=lambda:True,
            local=self.local,master=self.master)
    def local(self,method,path,body=None):
        if method=='GET':return list(self.state)
        self.state.append({'type':'com.jkali.proposal_claim','state_key':body['proposal_event_id'],'sender':'@self:local','content':dict(body)})
        return {'event_id':'$claim'}
    def master(self,method,path,body=None):
        from urllib.parse import unquote
        if method=='GET':return {'event_id':unquote(path.split('/')[-1]),'type':'com.jkali.proposal','sender':'@other:master' if self.foreign else '@manager:master'}
        if self.offline:raise OSError('offline')
        self.writes.append((path,body))
    def decision(self,sender='@self:local'):
        self.state=[{'type':DECISION,'event_id':'$decision1','state_key':'$local1','sender':sender,'content':{'schema':1,'proposal_event_id':'$local1','decision':'accepted','send_state':'sent'}}]
    def test_exact_mapping_and_no_body(self):
        self.decision();relay(self.u)
        self.assertEqual(self.writes[0][1],{'schema':1,'proposal_event_id':'$one','state':'pending','decision':'accepted','send_state':'sent'})
        self.assertEqual(self.writes[1][1]['state'],'refused')
        relay(self.u);self.assertEqual(len(self.writes),2)
    def test_wrong_sender_never_acknowledges(self):
        self.decision('@other:local');relay(self.u)
        self.assertNotIn('decision',self.writes[0][1])
    def test_failed_publish_retries_without_advanced_cursor(self):
        self.decision();self.offline=True
        with self.assertRaises(OSError):relay(self.u)
        self.assertIsNone(self.meta.get('proposal_status_cursor'))
        self.offline=False;relay(self.u);self.assertEqual(len(self.writes),2)
    def test_changed_identity_never_publishes_old_map(self):
        self.foreign=True;self.decision();relay(self.u);self.assertEqual(self.writes,[])
    def test_second_device_cannot_replace_claim_and_database_restore_uses_checkpoint(self):
        self.decision();relay(self.u)
        checkpoint=next(e for e in self.state if e['type']=='com.jkali.proposal_claim')
        self.state=[checkpoint,{'type':DECISION,'event_id':'$second','state_key':'$local1','sender':'@self:local',
            'content':{'schema':1,'proposal_event_id':'$local1','decision':'declined','send_state':'none'}}]
        # Simulate a lost local cache, retaining the Matrix checkpoint.
        for key in list(self.meta):
            if key.startswith('proposal_claim:') or key.startswith('proposal_status:'): del self.meta[key]
        relay(self.u)
        self.assertEqual(self.writes[-2][1]['decision'],'accepted')
        self.assertEqual(checkpoint['content']['decision_event_id'],'$decision1')
    def test_claim_retains_history_when_decision_state_is_missing(self):
        self.decision();relay(self.u)
        self.state=[e for e in self.state if e['type']=='com.jkali.proposal_claim']
        for key in list(self.meta):
            if key.startswith('proposal_status:'):del self.meta[key]
        relay(self.u)
        self.assertEqual(self.writes[-2][1]['decision'],'accepted')
    def test_disconnect_stops_all_reads_and_writes(self):
        self.u.active_link_for_dispatch=lambda:False
        self.u.local=lambda *args: self.fail_test()
        relay(self.u);self.assertEqual(self.writes,[])
    def fail_test(self):self.fail('unexpected local read')

if __name__=='__main__':unittest.main()
