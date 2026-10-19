"""Exercise actual provider-history SELECTs on disposable synthetic PostgreSQL.

Only the integration harness supplies SQL transport. No installed DB discovery,
credentials, native stores, live accounts, or native messages are accessed.
"""
import base64
import copy
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import uuid

sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from agents.enrich.crm_provider_history import ProviderHistoryReader, provider_history_query


def run_fixture(run_sql):
    schema='crm_history_'+uuid.uuid4().hex
    run_sql('CREATE SCHEMA '+schema)
    def execute(sql): return run_sql('SET search_path TO '+schema+';\n'+sql)
    def query(source,proof):
        raw=execute(provider_history_query(source,proof))
        # json_agg may emit newlines between row objects even under psql -At.
        # Decode the entire result; an optional SET tag is transport framing.
        return json.loads(raw[raw.index('{'):])
    proof={'local_owner_mxid':'@owner:fixture','room_id':'!dm:fixture','native_id':'dm','receiver':'login',
           'login_id':'login','counterpart_id':'counterpart','bridge_epoch':'epoch','bridge_epoch_kind':'provider_login_binding'}
    reader=SimpleNamespace(root=Path('/unused'),_provider=lambda source,room_id:[dict(proof,source=source)])
    history=ProviderHistoryReader(room_reader=reader,query=query)
    counts={}
    try:
        execute('''
          CREATE TABLE portal(bridge_id TEXT,id TEXT,receiver TEXT,mxid TEXT,room_type TEXT);
          CREATE TABLE user_portal(bridge_id TEXT,user_mxid TEXT,login_id TEXT,portal_id TEXT,portal_receiver TEXT);
          CREATE TABLE user_login(bridge_id TEXT,user_mxid TEXT,id TEXT,metadata JSONB);
          CREATE TABLE message(bridge_id TEXT,id TEXT,part_id TEXT,mxid TEXT,room_id TEXT,room_receiver TEXT,
            sender_id TEXT,sender_mxid TEXT,timestamp BIGINT,metadata JSONB);
          CREATE TABLE backfill_task(bridge_id TEXT,portal_id TEXT,portal_receiver TEXT,user_login_id TEXT,
            is_done BOOLEAN,queue_done BOOLEAN,dispatched_at TIMESTAMPTZ,completed_at TIMESTAMPTZ);
          INSERT INTO portal VALUES ('','dm','login','!dm:fixture','dm'),
            ('','dm','foreign','!foreign:fixture','dm'),('other','dm','login','!otherbridge:fixture','dm');
          INSERT INTO user_login VALUES ('','@owner:fixture','login','{"secret":"NEVER EXPORT"}'),
            ('','@foreign:fixture','foreign','{"secret":"NEVER EXPORT"}');
          INSERT INTO user_portal VALUES ('','@owner:fixture','login','dm','login'),
            ('','@foreign:fixture','foreign','dm','foreign');
          INSERT INTO message VALUES
            ('','100','','$out','dm','login','login','@owner:fixture',1700000000000000000,'{"orig_mxid":"$original","is_outgoing":true,"secret":"NEVER EXPORT","body":"PRIVATE BODY"}'),
            ('','101','','$reply','dm','login','counterpart','@ghost:fixture',1700000002000000000,'{}'),
            ('','101','attachment','$part','dm','login','counterpart','@ghost:fixture',1700000002000000000,'{}'),
            ('','foreign','','$foreign','dm','foreign','counterpart','@foreign:fixture',1700000003000000000,'{}'),
            ('other','foreign-bridge','','$foreign-bridge','dm','login','counterpart','@ghost:fixture',1700000004000000000,'{}');
        ''')
        for source in ('whatsapp','gmessages','instagram','linkedin','twitter'):
            if source=='instagram':
                def native(stamp,tail):
                    return 'fb:mid.$c'+base64.urlsafe_b64encode(bytes(8)+(stamp-1072915200000).to_bytes(5,'big')+tail.to_bytes(8,'big')).decode().rstrip('=')
                ids=[native(1700000000000,0),native(1700000002000,1)]
            else: ids=['100','101']
            execute("UPDATE message SET id='"+ids[0]+"' WHERE mxid='$out'; UPDATE message SET id='"+ids[1]+"' WHERE mxid IN ('$reply','$part')")
            result=query(source,proof)
            assert result['scope_count']==1 and result['backfill_pending'] is False,source
            assert {r['event_id'] for r in result['rows']}=={'$out','$reply','$part'},source
            assert 'NEVER EXPORT' not in json.dumps(result) and 'PRIVATE BODY' not in json.dumps(result),source
            projected=history.snapshot(source,'!dm:fixture',['$out'],bridge_epoch='epoch')
            assert projected['observed_complete'] and not projected['complete'],(source,projected)
            assert len(projected['events'])==2 and projected['outgoing_parts']=={'$out':[ids[0]]},source
            if source=='gmessages':
                assert history.snapshot(source,'!dm:fixture',['$original'])['observed_complete']
            for field,value in [('local_owner_mxid','@foreign:fixture'),('login_id','foreign'),('receiver','foreign'),('room_id','!foreign:fixture'),('native_id','other')]:
                altered=dict(proof,**{field:value});assert query(source,altered)['scope_count']==0,(source,field)
            counts[source]=len(result['rows'])
        for column,value in [('is_done','false'),('queue_done','false'),('user_login_id',"'foreign'"),('completed_at','NULL')]:
            execute("DELETE FROM backfill_task; INSERT INTO backfill_task VALUES ('','dm','login','login',true,true,'2026-01-01','2026-01-02'); UPDATE backfill_task SET "+column+'='+value)
            assert query('linkedin',proof)['backfill_pending'] is True,column
            assert history.snapshot('linkedin','!dm:fixture',['$out'])['reason']=='provider-backfill-pending',column
        execute("DELETE FROM backfill_task; INSERT INTO backfill_task VALUES ('','other','login','foreign',false,false,NULL,NULL)")
        assert query('linkedin',proof)['backfill_pending'] is False
        execute("INSERT INTO user_portal VALUES ('','@owner:fixture','login','dm','login')")
        assert query('linkedin',proof)['scope_count']==2
        assert not history.snapshot('linkedin','!dm:fixture',['$out'])['observed_complete']
        execute('''DROP TABLE portal,message;
          CREATE TABLE portal(dcid TEXT,receiver TEXT,type INTEGER,dc_guild_id TEXT,mxid TEXT);
          CREATE TABLE "user"(mxid TEXT,dcid TEXT,discord_token TEXT);
          CREATE TABLE message(dcid TEXT,dc_attachment_id TEXT,mxid TEXT,dc_chan_id TEXT,dc_chan_receiver TEXT,
            dc_sender TEXT,sender_mxid TEXT,timestamp BIGINT);
          INSERT INTO "user" VALUES ('@owner:fixture','login','NEVER EXPORT'),('@foreign:fixture','foreign','NEVER EXPORT');
          INSERT INTO portal VALUES ('dm','login',1,NULL,'!dm:fixture'),('dm','foreign',1,NULL,'!foreign:fixture');
          INSERT INTO message VALUES ('100','','$out','dm','login','login','@owner:fixture',1700000000000),
            ('101','','$reply','dm','login','counterpart','@ghost:fixture',1700000002000),
            ('101','attachment','$part','dm','login','counterpart','@ghost:fixture',1700000002000),
            ('102','','$foreign','dm','foreign','counterpart','@foreign:fixture',1700000003000);
        ''')
        result=query('discord',proof)
        assert {r['event_id'] for r in result['rows']}=={'$out','$reply','$part'}
        assert result['rows'][0]['timestamp_ns']>=1700000000000000000
        projected=history.snapshot('discord','!dm:fixture',['$out'])
        assert projected['observed_complete'] and not projected['complete'] and len(projected['events'])==2
        for field,value in [('local_owner_mxid','@foreign:fixture'),('login_id','foreign'),('receiver','foreign'),('room_id','!foreign:fixture')]:
            assert query('discord',dict(proof,**{field:value}))['scope_count']==0,field
        for mutation in ['type=3',"dc_guild_id='guild'"]:
            execute('UPDATE portal SET '+mutation+" WHERE receiver='login'")
            assert query('discord',proof)['scope_count']==0,mutation
            execute("UPDATE portal SET type=1,dc_guild_id=NULL WHERE receiver='login'")
        assert 'NEVER EXPORT' not in json.dumps(result)
        counts['discord']=len(result['rows'])
        return counts
    finally:
        run_sql('DROP SCHEMA '+schema+' CASCADE')
