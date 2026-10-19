"""Independent provider-history boundaries using injected synthetic bridge rows."""
import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import unittest
from types import SimpleNamespace
from unittest.mock import patch

spec=importlib.util.spec_from_file_location('provider_history_fixture',Path(__file__).with_name('crm_provider_history.test.py'))
fixture=importlib.util.module_from_spec(spec);spec.loader.exec_module(fixture)


class ProviderHistoryAdversarial(fixture.ProviderHistoryTests):
    def empty(self,result):
        self.assertFalse(result['complete']);self.assertFalse(result['observed_complete'])
        self.assertFalse(result['observed_window_complete']);self.assertEqual(result['events'],[])
        self.assertEqual(result['outgoing_parts'],{})

    def test_adversarial_wrong_owner_or_receiver_after_query_discards_every_row(self):
        original=copy.deepcopy(self.proof)
        for key in ('local_owner_mxid','receiver','login_id','room_id','counterpart_id'):
            with self.subTest(key=key):
                self.proof=copy.deepcopy(original)
                def query(source,proof):self.proof[key]='foreign';return self.page
                self.history.query=query;self.empty(self.snapshot())

    def test_adversarial_scope_or_backfill_shape_never_silently_becomes_empty_history(self):
        for update in ({'scope_count':0},{'scope_count':2},{'scope_count':True},{'backfill_pending':None},
                       {'backfill_pending':'false'},{'rows':None}):
            with self.subTest(update=update):
                self.page={'scope_count':1,'backfill_pending':False,'rows':copy.deepcopy(self.rows),**update}
                self.empty(self.snapshot())

    def test_adversarial_grouped_native_parts_must_agree_on_direction_and_original_time(self):
        for changes in ({'sender_id':'login','sender_mxid':'@owner:local'}, {'timestamp_ns':1700000003000000000}):
            self.rows[:]=[self.row('out','$out',1700000000000000000,True),self.row('reply','$reply',1700000002000000000)]
            extra=self.row('reply','$attachment',1700000002000000000,part='attachment');extra.update(changes);self.rows.append(extra)
            self.empty(self.snapshot())

    def test_adversarial_exact_outgoing_matrix_target_cannot_be_native_incoming(self):
        self.rows[0].update(sender_id='counterpart',sender_mxid='@ghost:local')
        self.empty(self.snapshot())

    def test_adversarial_gmessages_outgoing_flag_cannot_erase_proven_counterpart_identity(self):
        self.rows[1]['is_outgoing']=True
        self.empty(self.snapshot('gmessages'))

    def test_adversarial_cut_through_oldest_native_parts_does_not_prove_initial_scope(self):
        stamp=1700000000000000000
        self.rows[:]=[self.row('out','$out',stamp,True,part='body')]+[
            self.row('reply-'+str(i),'$reply-'+str(i),stamp+(i+1)*1000000) for i in range(2000)]
        result=self.snapshot();self.assertTrue(result['observed_window_complete']);self.assertFalse(result['observed_complete'])
        self.assertEqual(result['reason'],'provider-window-boundary')
        self.assertTrue(result['has_more'])

    def test_adversarial_bounded_window_with_older_boundary_can_prove_observed_suffix_only(self):
        stamp=1700000000000000000
        self.rows[:]=[self.row('older','$older',stamp-1000000),self.row('out','$out',stamp,True)]+[
            self.row('reply-'+str(i),'$reply-'+str(i),stamp+(i+1)*1000000) for i in range(1999)]
        result=self.snapshot();self.assertTrue(result['observed_complete']);self.assertFalse(result['complete'])
        self.assertTrue(result['has_more']);self.assertEqual(result['evidence_kind'],'observed_native')

    def test_adversarial_over_limit_native_result_fails_closed(self):
        self.rows[:]=[self.row('out','$out',1700000000000000000,True)]*2002
        self.empty(self.snapshot())

    def test_adversarial_equal_order_before_outreach_does_not_invent_order_inside_reply_scope(self):
        self.rows[:0]=[self.row('older-a','$older-a',1699999999000000000),self.row('older-b','$older-b',1699999999000000000)]
        self.assertTrue(self.snapshot()['observed_complete'])
        self.rows[-1]['timestamp_ns']=self.rows[-2]['timestamp_ns'];self.assertEqual(self.snapshot()['reason'],'provider-order-tie')

    def test_adversarial_missing_outgoing_keeps_observed_window_without_claiming_full_scope(self):
        self.rows.pop(0);result=self.snapshot();self.assertTrue(result['observed_window_complete']);self.assertFalse(result['observed_complete'])
        self.assertEqual(result['outgoing_parts'],{'$out':[]})

    def test_adversarial_failed_reader_drops_old_evidence_and_redacts_exception_details(self):
        self.assertTrue(self.snapshot()['observed_complete'])
        for error in (RuntimeError('SECRET credentials'),subprocess.TimeoutExpired('SECRET SQL',1),ValueError('SECRET body')):
            def query(*args):raise error
            self.history.query=query;result=self.snapshot();self.empty(result);self.assertNotIn('SECRET',str(result))

    def test_adversarial_body_credentials_and_unrelated_metadata_never_leave_projection(self):
        self.rows[1].update(body='SECRET BODY',access_token='SECRET TOKEN',metadata={'anything':'SECRET'})
        result=self.snapshot();self.assertTrue(result['observed_complete']);self.assertNotIn('SECRET',str(result))

    def runtime(self, mutate=None):
        from agents.enrich.crm_provider_history import ProviderHistoryReader, _IMAGE_DIGESTS
        calls=[]; counts={}
        def runner(args,**kwargs):
            self.assertIs(kwargs.get('shell'),False);self.assertTrue(kwargs.get('capture_output'));self.assertEqual(kwargs.get('timeout'),20)
            kind='images' if 'images' in args else 'inspect' if 'inspect' in args else 'sql' if 'psql' in args else 'unexpected'
            counts[kind]=counts.get(kind,0)+1;calls.append((kind,args))
            if kind=='images':value=b'a'*64
            elif kind=='inspect':value=json.dumps(['dock.mau.dev/mautrix/linkedin@sha256:'+_IMAGE_DIGESTS['linkedin']]).encode()
            elif kind=='sql':value=json.dumps(self.page).encode()
            else:raise AssertionError(args)
            result=SimpleNamespace(returncode=0,stdout=value)
            if mutate:mutate(kind,counts[kind],result,args)
            return result
        self.history=ProviderHistoryReader(room_reader=self.reader,runner=runner)
        return calls

    def test_adversarial_runtime_multiple_images_never_reach_sql(self):
        for output in (b'a'*64+b'\n'+b'b'*64,b'a'*64+b'\n'+b'a'*64):
            with self.subTest(output=output):
                calls=self.runtime(lambda kind,n,result,args:setattr(result,'stdout',output) if kind=='images' else None)
                self.empty(self.snapshot());self.assertFalse(any(kind=='sql' for kind,args in calls))

    def test_adversarial_runtime_unknown_short_or_malformed_image_never_reaches_sql(self):
        for output in (b'',b'a'*12,b'sha256:'+b'A'*64,b'a'*64+b';touch /tmp/no',b'\xff',b'x'*256):
            with self.subTest(output=output):
                calls=self.runtime(lambda kind,n,result,args:setattr(result,'stdout',output) if kind=='images' else None)
                self.empty(self.snapshot());self.assertFalse(any(kind=='sql' for kind,args in calls))

    def test_adversarial_runtime_inspect_must_return_exact_repository_digest_list(self):
        from agents.enrich.crm_provider_history import _IMAGE_DIGESTS
        expected='dock.mau.dev/mautrix/linkedin@sha256:'+_IMAGE_DIGESTS['linkedin']
        for output in (json.dumps(expected),json.dumps({'RepoDigests':[expected]}),json.dumps([expected+'suffix']),json.dumps(['evil.invalid/linkedin@sha256:'+_IMAGE_DIGESTS['linkedin']]),'null','not json'):
            with self.subTest(output=output):
                calls=self.runtime(lambda kind,n,result,args:setattr(result,'stdout',output.encode()) if kind=='inspect' else None)
                self.empty(self.snapshot());self.assertFalse(any(kind=='sql' for kind,args in calls))

    def test_adversarial_runtime_failed_command_cannot_supply_successful_output(self):
        for failed in ('images','inspect','sql'):
            with self.subTest(command=failed):
                calls=self.runtime(lambda kind,n,result,args:setattr(result,'returncode',1) if kind==failed else None)
                self.empty(self.snapshot())
                if failed!='sql':self.assertFalse(any(kind=='sql' for kind,args in calls))

    def test_adversarial_runtime_failed_post_query_image_check_discards_rows(self):
        for output in (b'',b'a'*64+b'\n'+b'b'*64,b'b'*64):
            with self.subTest(output=output):
                calls=self.runtime(lambda kind,n,result,args:setattr(result,'stdout',output) if kind=='images' and n==2 else None)
                self.empty(self.snapshot());self.assertEqual(sum(kind=='sql' for kind,args in calls),1)

    def test_adversarial_runtime_cached_image_proof_cannot_cross_provider_repository(self):
        calls=self.runtime();self.assertTrue(self.snapshot()['observed_complete'])
        self.empty(self.snapshot('whatsapp'))
        self.assertEqual(sum(kind=='sql' for kind,args in calls),1)
        self.assertEqual(sum(kind=='inspect' for kind,args in calls),2)

    def test_adversarial_runtime_cached_digest_still_checks_service_image_each_query(self):
        calls=self.runtime(lambda kind,n,result,args:setattr(result,'stdout',b'unknown') if kind=='images' and n>=3 else None)
        self.assertTrue(self.snapshot()['observed_complete']);self.empty(self.snapshot())
        self.assertEqual(sum(kind=='sql' for kind,args in calls),1)

    def test_adversarial_runtime_uses_only_fixed_service_and_scoped_readonly_sql(self):
        calls=self.runtime()
        with patch('agents.enrich.crm_provider_history.compose_prefix',return_value=['/fixture/docker','compose','-p','isolated']):
            self.assertTrue(self.snapshot()['observed_complete'])
        images=[args for kind,args in calls if kind=='images'];self.assertTrue(all(args==['/fixture/docker','compose','-p','isolated','images','-q','mautrix-linkedin'] for args in images))
        sql=next(args for kind,args in calls if kind=='sql');self.assertEqual(sql[:10],['/fixture/docker','compose','-p','isolated','exec','-T','postgres','psql','-X','-v'])
        self.assertIn('ON_ERROR_STOP=1',sql);self.assertEqual(sql[-1],fixture.provider_history_query('linkedin',self.proof));self.assertIn('LIMIT 2001',sql[-1]);self.assertNotRegex(sql[-1],r'(?i)\b(INSERT|UPDATE|DELETE|DROP|ALTER)\b')


def load_tests(loader,standard,pattern):
    return unittest.TestSuite(ProviderHistoryAdversarial(name) for name in ProviderHistoryAdversarial.__dict__ if name.startswith('test_adversarial_'))

if __name__=='__main__':unittest.main()
