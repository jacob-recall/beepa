#!/usr/bin/env python3
"""Native history proof from a temporary bridge DB + injected pinned CLI pages."""
import copy
import importlib.util
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from agents.enrich.crm_native_history import NativeHistoryReader

spec = importlib.util.spec_from_file_location('resolver_fixture', Path(__file__).with_name('crm_resolver.test.py'))
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)


class HistoryTest(unittest.TestCase):
    def setUp(self):
        fixture.ResolverTest.setUp(self)
        self.cfg = self.reader.imessage_config
        self.cfg.write_text(json.dumps({'domain':'local', 'user_id':'@user:local', 'bot_id':'@native:local', 'cli_path':'/fixture/signed/imessage'}))
        with sqlite3.connect(self.native) as db:
            db.execute('CREATE TABLE outbound_event(event_id TEXT PRIMARY KEY, room_id TEXT, state TEXT)')
            db.execute('CREATE TABLE native_receipt(event_id TEXT, room_id TEXT, chat_id TEXT, native_id TEXT, PRIMARY KEY(event_id,native_id))')
            db.execute('CREATE TABLE event_map(chat_id TEXT, msg_id TEXT, event_id TEXT, PRIMARY KEY(chat_id,msg_id))')
            db.execute("INSERT INTO outbound_event VALUES('$out','!dm:local','confirmed')")
            db.execute("INSERT INTO native_receipt VALUES('$out','!dm:local','any;-;person@example.test','OUT')")
            db.execute("INSERT INTO event_map VALUES('any;-;person@example.test','IN','$incoming')")
        self.page = {'items':[self.msg('OLDER',1), self.msg('OUT',2,True), self.msg('IN',3)], 'hasMore':False}
        self.calls, self.hook = [], lambda:None
        def runner(argv, **kwargs):
            self.calls.append((argv,kwargs))
            self.hook()
            return subprocess.CompletedProcess(argv,0,json.dumps(self.page).encode(),b'')
        self.history = NativeHistoryReader(room_reader=self.reader,runner=runner)

    def msg(self, native, step, sender=False, **changes):
        order = 800000000000000000 + step * 1000000
        return dict({'id':native,'cursor':str(order),'timestamp':order//1000000+978307200000,
                     'isSender':sender,'threadID':'any;-;person@example.test', 'text':'PRIVATE BODY'},**changes)

    def snapshot(self, outgoing=('$out',), **kwargs):
        return self.history.snapshot('!dm:local',outgoing,**kwargs)

    def sql(self, statement, args=()):
        with sqlite3.connect(self.native) as db: db.execute(statement,args)

    def assert_unavailable(self, result):
        self.assertFalse(result['complete'])
        self.assertFalse(result['window_complete'])
        self.assertEqual(result['events'],[])
        self.assertEqual(result['outgoing_parts'],{})

    def test_real_resolver_maps_and_latest_native_page_exact_contract(self):
        result=self.snapshot()
        self.assertTrue(result['complete'])
        self.assertTrue(result['window_complete'])
        self.assertEqual(result['outgoing_parts'],{'$out':['OUT']})
        self.assertEqual(result['events'][-1],dict(native_id='IN',order='800000000003000000',timestamp=1778307200003,
                                                  incoming=True,event_id='$incoming',outgoing_event_ids=[]))
        self.assertEqual(result['events'][1]['outgoing_event_ids'],['$out'])
        self.assertEqual(result['bridge_epoch_kind'],'local_bridge_incarnation')
        self.assertNotIn('PRIVATE BODY',json.dumps(result))
        self.assertEqual(self.calls,[(['/fixture/signed/imessage','--no-events','--json','messages','any;-;person@example.test'],
                                      {'capture_output':True,'timeout':20,'shell':False})])

    def test_more_history_still_proves_suffix_with_strictly_older_boundary(self):
        self.page['hasMore']=True
        self.assertTrue(self.snapshot()['complete'])

    def test_boundary_equal_to_outgoing_is_not_a_complete_initial_scope(self):
        self.page.update(items=self.page['items'][1:],hasMore=True)
        result=self.snapshot()
        self.assertFalse(result['complete'])
        self.assertTrue(result['window_complete'])
        self.assertEqual(result['reason'],'native-window-boundary')

    def test_short_full_history_needs_no_older_boundary(self):
        self.page['items']=self.page['items'][1:]
        self.assertTrue(self.snapshot()['complete'])

    def test_old_outgoing_outside_window_keeps_exact_parts_for_consumer_overlap(self):
        self.page.update(items=[self.msg('LATER',20),self.msg('LATEST',21)],hasMore=True)
        result=self.snapshot()
        self.assertFalse(result['complete'])
        self.assertTrue(result['window_complete'])
        self.assertEqual(result['outgoing_parts'],{'$out':['OUT']})
        self.assertEqual(len(result['events']),2)

    def test_distinct_native_nanos_order_survives_same_millisecond(self):
        self.page['items'][-1]['cursor']='800000000002000001'
        self.page['items'][-1]['timestamp']=1778307200002
        self.assertTrue(self.snapshot()['complete'])

    def test_ties_in_attribution_scope_never_use_id_or_arrival_tiebreak(self):
        self.page['items'][-1]=self.msg('IN',2)
        result=self.snapshot()
        self.assertFalse(result['complete'])
        self.assertEqual(result['reason'],'native-order-tie')

    def test_ties_before_tracked_outreach_do_not_poison_suffix(self):
        self.page['items'].insert(0,self.msg('OTHER-OLDER',1))
        self.page['hasMore']=True
        self.assertTrue(self.snapshot()['complete'])

    def test_all_outgoing_parts_required_and_never_guess_suffix(self):
        self.sql("INSERT INTO native_receipt VALUES('$out','!dm:local','any;-;person@example.test','OUT_1')")
        result=self.snapshot()
        self.assertFalse(result['complete'])
        self.assertEqual(result['outgoing_parts'],{'$out':['OUT','OUT_1']})
        self.page['items'].append(self.msg('OUT_1',2,True))
        self.assertEqual(self.snapshot()['reason'],'native-order-tie')

    def test_absent_echo_mapping_keeps_native_id_without_fabricating_event(self):
        self.sql('DELETE FROM event_map')
        result=self.snapshot()
        self.assertTrue(result['complete'])
        self.assertIsNone(result['events'][-1]['event_id'])

    def test_unconfirmed_or_foreign_receipt_target_never_counts(self):
        for statement in ("UPDATE outbound_event SET state='ambiguous'", "UPDATE outbound_event SET room_id='!foreign:local'",
                          "UPDATE native_receipt SET chat_id='foreign'", "UPDATE native_receipt SET room_id='!foreign:local'"):
            with self.subTest(statement=statement):
                with sqlite3.connect(self.native) as db:
                    old=list(db.iterdump())
                self.sql(statement)
                self.assert_unavailable(self.snapshot())
                self.native.unlink()
                with sqlite3.connect(self.native) as db: db.executescript('\n'.join(old))

    def test_no_body_matching_when_exact_native_id_absent(self):
        self.page['items'][1]['id']='UNRELATED-SAME-BODY'
        self.assertFalse(self.snapshot()['complete'])

    def test_outgoing_direction_requires_native_sender_flag(self):
        self.page['items'][1]['isSender']=False
        self.assert_unavailable(self.snapshot())

    def test_actions_unsends_and_hidden_items_cannot_be_responses(self):
        for flag in ('isAction','isDeleted','isHidden','action'):
            with self.subTest(flag=flag):
                self.page['items'][-1]=self.msg('IN',3,**{flag:True})
                self.assertEqual([e['native_id'] for e in self.snapshot()['events']],['OLDER','OUT'])

    def test_malformed_messages_fail_closed(self):
        changes=[{'cursor':'1.5'},{'cursor':2},{'cursor':'9223372036854775808'},{'timestamp':False},
                 {'timestamp':1778307200003.5},{'timestamp':1778307200999},{'isSender':'false'},
                 {'threadID':'foreign'},{'id':'OUT'},{'isAction':'false'}]
        for change in changes:
            with self.subTest(change=change):
                self.page['items'][-1]=self.msg('IN',3,**change)
                self.assert_unavailable(self.snapshot())

    def test_malformed_native_page_and_exception_text_never_escape(self):
        for page in ({'items':[]},{'items':[],'hasMore':0},[],{'items':[None],'hasMore':False}):
            self.page=page
            self.assert_unavailable(self.snapshot())
        def broken(*args,**kw):raise subprocess.TimeoutExpired('SECRET body',20)
        self.history.runner=broken
        result=self.snapshot()
        self.assert_unavailable(result)
        self.assertNotIn('SECRET',str(result))

    def test_scope_and_epoch_validation_before_read(self):
        for scope in ([],['not-event'],[None],['$out']*201):
            self.assert_unavailable(self.snapshot(scope))
        self.assert_unavailable(self.snapshot(bridge_epoch='foreign'))
        self.assertFalse(self.calls)

    def test_bridge_health_and_portal_proof_failure_is_contained(self):
        self.states['!management:local'][-1]['content']['poll_ok']=False
        self.assert_unavailable(self.snapshot())
        self.assertFalse(self.calls)

    def test_bridge_authority_change_during_read_discards_window(self):
        self.hook=lambda:self.states['!management:local'][-1]['content'].update(poll_ok=False)
        self.assert_unavailable(self.snapshot())

    def test_changed_mapping_or_config_during_read_discards_window(self):
        for hook in (lambda:self.sql("UPDATE event_map SET event_id='$changed'"),
                     lambda:self.cfg.write_text(json.dumps({'user_id':'@user:local','bot_id':'@native:local','cli_path':'/changed'}))):
            self.hook=hook
            self.assert_unavailable(self.snapshot())

    def test_missing_native_database_is_never_created(self):
        self.native.unlink()
        self.assert_unavailable(self.snapshot())
        self.assertFalse(self.native.exists())

    def test_metadata_only_native_edits_do_not_create_another_incoming_id(self):
        self.page['items'][-1].update(editedTimestamp=1778307999999,editHistory=[{'text':'EDIT SECRET'}],reactions=[{'id':'rxn'}])
        result=self.snapshot()
        self.assertTrue(result['complete'])
        self.assertEqual(len([event for event in result['events'] if event['native_id']=='IN']),1)
        self.assertNotIn('EDIT SECRET',json.dumps(result))


if __name__=='__main__':unittest.main()
