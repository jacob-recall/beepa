"""Monitoring read contracts use synthetic native pages and temporary mappings."""
import importlib.util
import json
from pathlib import Path
import unittest

def fixture(name):
    spec=importlib.util.spec_from_file_location(name,Path(__file__).with_name(name+'.test.py'))
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module

native=fixture('crm_native_history')
provider=fixture('crm_provider_history')

class NativeMonitoringTests(unittest.TestCase):
    msg=native.HistoryTest.msg
    sql=native.HistoryTest.sql
    def setUp(self):native.HistoryTest.setUp(self)
    def read(self,**kwargs):return self.history.conversation_snapshot('!dm:local',**kwargs)

    def test_monitoring_incoming_without_any_crm_outreach(self):
        self.sql('DELETE FROM outbound_event');self.sql('DELETE FROM native_receipt')
        self.page['items']=[self.msg('IN',3)]
        result=self.read()
        self.assertTrue(result['monitoring_ready']);self.assertTrue(result['complete'])
        self.assertEqual(result['outgoing_parts'],{})
        self.assertEqual(result['events'][0]['event_id'],'$incoming')
        self.assertTrue(result['events'][0]['incoming'])
        self.assertNotIn('PRIVATE BODY',json.dumps(result))

    def test_confirmation_aliases_are_exact_and_ambiguous_sends_excluded(self):
        result=self.read()
        self.assertEqual(result['outgoing_parts'],{'$out':['OUT']})
        self.assertEqual(result['events'][1]['outgoing_event_ids'],['$out'])
        self.sql("UPDATE outbound_event SET state='ambiguous'")
        result=self.read()
        self.assertEqual(result['outgoing_parts'],{})
        self.assertEqual(result['events'][1]['outgoing_event_ids'],[])
        # The native observation still proves an outgoing native row, but does
        # not establish that an ambiguous Matrix attempt caused that row.
        self.assertFalse(result['events'][1]['incoming'])

    def test_latest_window_is_not_a_claim_of_full_history(self):
        self.page['hasMore']=True;result=self.read()
        self.assertTrue(result['monitoring_ready']);self.assertTrue(result['window_complete'])
        self.assertFalse(result['complete']);self.assertTrue(result['has_more'])

    def test_empty_history_and_incomplete_empty_page_differ(self):
        self.page={'items':[],'hasMore':False};result=self.read()
        self.assertTrue(result['monitoring_ready']);self.assertTrue(result['complete'])
        self.page['hasMore']=True;result=self.read()
        self.assertFalse(result['monitoring_ready']);self.assertFalse(result['complete'])

    def test_order_ties_and_epoch_changes_do_not_authorize_obligations(self):
        self.page['items'].append(self.msg('OTHER',3))
        self.assertFalse(self.read()['monitoring_ready'])
        self.assertEqual(self.read()['reason'],'native-order-tie')
        self.assertEqual(self.read(bridge_epoch='replaced')['events'],[])

    def test_batch_scope_contract_stays_strict(self):
        result=self.history.snapshot('!dm:local',[])
        self.assertEqual(result['events'],[]);self.assertFalse(result['window_complete'])
        self.assertEqual(self.calls,[])

class ProviderMonitoringTests(unittest.TestCase):
    row=provider.ProviderHistoryTests.row
    def setUp(self):provider.ProviderHistoryTests.setUp(self)
    def read(self,**kwargs):return self.history.conversation_snapshot('linkedin','!native:local',**kwargs)

    def test_incoming_without_a_target_and_no_native_eof_promotion(self):
        self.rows[:]=[self.row('incoming','$incoming',1700000001000000000)]
        result=self.read()
        self.assertTrue(result['monitoring_ready']);self.assertTrue(result['observed_complete'])
        self.assertFalse(result['complete']);self.assertFalse(result['window_complete'])
        self.assertEqual(result['evidence_kind'],'observed_native')
        self.assertEqual(result['events'][0]['event_id'],'$incoming')

    def test_mapped_outgoing_parts_retained_without_guessing(self):
        result=self.read();self.assertEqual(result['outgoing_parts'],{'$out':['out']})
        self.assertEqual(result['events'][0]['outgoing_event_ids'],['$out'])

    def test_bounded_part_cut_removes_entire_boundary_message(self):
        self.rows[:]=[self.row('boundary','$part'+str(i),1700000000000000000,part=str(i)) for i in range(2000)]
        self.rows.append(self.row('newer','$newer',1700000001000000000))
        result=self.read()
        self.assertTrue(result['has_more']);self.assertFalse(result['observed_complete'])
        self.assertTrue(result['monitoring_ready'])
        self.assertEqual([e['event_id'] for e in result['events']],['$newer'])

    def test_backfill_scope_and_ties_still_fail_closed(self):
        self.page['backfill_pending']=True;self.assertEqual(self.read()['events'],[])
        self.page['backfill_pending']=False;self.rows[1]['timestamp_ns']=self.rows[0]['timestamp_ns']
        self.assertFalse(self.read()['monitoring_ready'])
        self.assertEqual(self.read()['reason'],'provider-order-tie')
        self.assertEqual(self.read(bridge_epoch='foreign')['events'],[])

    def test_empty_observed_database_and_batch_scope_remain_distinct(self):
        self.rows.clear();result=self.read()
        self.assertTrue(result['monitoring_ready']);self.assertTrue(result['observed_complete'])
        self.assertFalse(result['complete'])
        batch=self.history.snapshot('linkedin','!native:local',[])
        self.assertEqual(batch['events'],[]);self.assertFalse(batch['observed_window_complete'])

if __name__=='__main__':unittest.main()
