"""Synthetic observed-native provider evidence; no installed accounts or SQL."""
import base64
import copy
import json
from pathlib import Path
import sys
import unittest
from types import SimpleNamespace

sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from agents.enrich.crm_provider_history import ProviderHistoryReader, provider_history_query


class ProviderHistoryTests(unittest.TestCase):
    def setUp(self):
        self.proof = {'local_owner_mxid':'@owner:local','room_id':'!native:local','native_id':'room-native',
            'receiver':'login','login_id':'login','counterpart_id':'counterpart','bridge_epoch':'current',
            'bridge_epoch_kind':'provider_login_binding','source':'linkedin'}
        self.rows = [self.row('out', '$out', 1700000000000000000, own=True), self.row('reply','$reply',1700000002000000000)]
        self.page = {'scope_count':1,'backfill_pending':False,'rows':self.rows}
        self.reader = SimpleNamespace(_provider=lambda source,room_id:[copy.deepcopy(self.proof)], root=Path('/unused'))
        self.history = ProviderHistoryReader(room_reader=self.reader,query=lambda source,proof:copy.deepcopy(self.page))

    def row(self, native, event, timestamp, own=False, part=''):
        return {'native_id':native,'part_id':part,'event_id':event,'sender_id':'login' if own else 'counterpart',
            'sender_mxid':'@owner:local' if own else '@ghost:local','timestamp_ns':timestamp,
            'original_event_id':None,'is_outgoing':None}

    def snapshot(self, source='linkedin'):
        self.proof['source'] = source
        return self.history.snapshot(source, '!native:local', ['$out'],bridge_epoch='current')

    def test_exact_native_order_supports_observed_projection_without_eof_claim(self):
        result = self.snapshot()
        self.assertTrue(result['observed_complete'])
        self.assertEqual(result['evidence_kind'],'observed_native')
        self.assertFalse(result['complete']); self.assertFalse(result['window_complete'])
        self.assertEqual(result['outgoing_parts'],{'$out':['out']})
        self.assertEqual([event['event_id'] for event in result['events']],['$out','$reply'])

    def test_all_six_native_order_contracts_common_case(self):
        for source in ('linkedin','whatsapp','gmessages','discord','twitter','instagram'):
            with self.subTest(source=source):
                self.rows[:] = [self.row('out','$out',1700000000000000000,True),self.row('reply','$reply',1700000002000000000)]
                if source in ('twitter','discord'):
                    self.rows[0]['native_id']='100'; self.rows[1]['native_id']='101'
                elif source=='instagram':
                    for i,row in enumerate(self.rows):
                        raw = bytes(8) + (row['timestamp_ns']//1000000-1072915200000).to_bytes(5,'big') + i.to_bytes(8,'big')
                        row['native_id']='fb:mid.$c'+base64.urlsafe_b64encode(raw).decode().rstrip('=')
                self.assertTrue(self.snapshot(source)['observed_complete'])

    def test_pending_backfill_cannot_become_complete(self):
        self.page['backfill_pending']=True
        result=self.snapshot()
        self.assertFalse(result['observed_complete']); self.assertEqual(result['reason'],'provider-backfill-pending')

    def test_scope_change_across_query_discards_all_evidence(self):
        def query(source,proof):
            self.proof['bridge_epoch']='replaced'
            return self.page
        self.history.query=query
        result=self.snapshot()
        self.assertEqual(result['events'],[]); self.assertEqual(result['reason'],'provider-proof-changed')

    def test_native_parts_are_one_response_with_exact_navigation(self):
        self.rows.append(self.row('reply','$attachment',1700000002000000000,part='attachment'))
        result=self.snapshot()
        self.assertTrue(result['observed_complete']); self.assertEqual(len(result['events']),2)
        self.assertEqual(result['events'][1]['event_id'],'$reply')

    def test_duplicate_rows_idempotent_but_conflicting_part_is_rejected(self):
        self.rows.append(copy.deepcopy(self.rows[1]))
        self.assertTrue(self.snapshot()['observed_complete'])
        self.rows[-1]['event_id']='$different'
        self.assertFalse(self.snapshot()['observed_complete'])

    def test_equal_source_time_is_not_broken_by_ids_or_arrival(self):
        self.rows[1]['timestamp_ns']=self.rows[0]['timestamp_ns']
        self.assertEqual(self.snapshot()['reason'],'provider-order-tie')

    def test_one_matrix_event_cannot_alias_two_native_messages(self):
        self.rows.append(self.row('other','$reply',1700000003000000000))
        self.assertEqual(self.snapshot()['reason'],'provider-matrix-map-conflict')

    def test_unknown_sender_and_pending_native_id_block(self):
        self.rows[1]['sender_id']='unknown'
        self.assertFalse(self.snapshot()['observed_complete'])
        self.rows[1]['sender_id']='counterpart'; self.rows[0]['native_id']='~txn:out'
        self.assertEqual(self.snapshot()['reason'],'provider-native-id-unresolved')

    def test_missing_exact_outgoing_mapping_does_not_guess_by_time(self):
        self.rows[0]['event_id']='$unrelated'
        self.assertEqual(self.snapshot()['reason'],'provider-outgoing-outside-window')

    def test_gmessages_resolved_original_matrix_id_is_exact(self):
        self.rows[0].update(event_id='$remote-echo',original_event_id='$out',is_outgoing=True)
        self.assertTrue(self.snapshot('gmessages')['observed_complete'])
        self.assertFalse(self.snapshot('linkedin')['observed_complete'])

    def test_wrong_direction_original_id_is_rejected(self):
        self.rows[1]['original_event_id']='$out'
        self.assertFalse(self.snapshot('gmessages')['observed_complete'])

    def test_sql_is_scoped_no_secrets_and_values_cannot_become_sql(self):
        self.proof['native_id']="'; DROP TABLE message; --"
        for source in ('linkedin','whatsapp','gmessages','instagram','twitter','discord'):
            sql=provider_history_query(source,self.proof)
            self.assertNotIn('DROP TABLE',sql)
            self.assertIn('LIMIT 2001',sql)
            for forbidden in ('access_token','password','text_hash','status_text','SELECT * FROM user_login'):
                self.assertNotIn(forbidden,sql)
            self.assertIn('scope_count',sql)

    def image_runner(self, digest=None, replacement=False):
        from agents.enrich.crm_provider_history import _IMAGE_DIGESTS
        calls=[]; images=[0]
        def runner(args, **kwargs):
            calls.append(args)
            if 'images' in args:
                images[0]+=1
                identity = 'b' if replacement and images[0]>1 else 'a'
                output=identity*64
            elif 'inspect' in args:
                output=json.dumps(['dock.mau.dev/mautrix/linkedin@sha256:'+(digest or _IMAGE_DIGESTS['linkedin'])])
            elif 'psql' in args:
                output=json.dumps(self.page)
            else:
                raise AssertionError(args)
            return SimpleNamespace(returncode=0,stdout=output.encode())
        self.history=ProviderHistoryReader(room_reader=self.reader,runner=runner)
        return calls

    def test_default_transport_requires_exact_audited_image_before_sql(self):
        calls=self.image_runner(digest='f'*64)
        self.assertEqual(self.snapshot()['reason'],'provider-image-unverified')
        self.assertFalse(any('psql' in args for args in calls))

    def test_image_replacement_during_query_discards_evidence(self):
        self.image_runner(replacement=True)
        self.assertEqual(self.snapshot()['reason'],'provider-image-changed')

    def test_audited_immutable_image_uses_fixed_scoped_query(self):
        calls=self.image_runner()
        self.assertTrue(self.snapshot()['observed_complete'])
        self.assertEqual(sum('psql' in args for args in calls),1)
        self.assertEqual(sum('images' in args for args in calls),2)
        self.assertEqual(sum('inspect' in args for args in calls),1)

    def test_parser_errors_never_expose_response_body(self):
        self.history.query=lambda source,proof: (_ for _ in ()).throw(ValueError('private body secret'))
        self.assertEqual(self.snapshot()['reason'],'provider-evidence-unavailable')

    def test_twitter_client_uuid_cannot_supply_native_order(self):
        self.rows[0]['native_id']='client-uuid'; self.rows[1]['native_id']='123'
        self.assertEqual(self.snapshot('twitter')['reason'],'provider-native-id-unresolved')

    def test_instagram_timestamp_fallback_is_rejected(self):
        raw=bytes(8)+(1700000000000-1072915200000).to_bytes(5,'big')+bytes(8)
        self.rows[0]['native_id']='fb:mid.$c'+base64.urlsafe_b64encode(raw).decode().rstrip('=')
        self.rows[0]['timestamp_ns']+=1000000
        self.assertEqual(self.snapshot('instagram')['reason'],'provider-native-timestamp-mismatch')

    def test_no_content_is_copied_into_evidence(self):
        self.rows[1]['body']='secret message text'
        self.rows[1]['metadata']={'token':'secret'}
        result=json.dumps(self.snapshot())
        self.assertNotIn('secret',result); self.assertNotIn('body',result)

if __name__=='__main__': unittest.main()
