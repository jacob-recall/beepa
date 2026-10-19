import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from urllib.parse import unquote, urlsplit, parse_qs
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[2]
spec=importlib.util.spec_from_file_location('master_notifications', ROOT/'master/notifications.py')
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
OWNER='@user:master';ROOM='!chat:master';SPACE='!space:master'
def ev(kind,content,key=''):return {'type':kind,'sender':OWNER,'state_key':key,'content':content}
def message(n=1,own=False):return {'event_id':'$event'+str(n),'sender':OWNER,'origin_server_ts':100010,'type':'m.room.message','content':{'msgtype':'m.text','body':'private body','com.jkali.from_me':own}}
def subscription():return {'endpoint':'https://updates.push.services.mozilla.com/wpush/v2/test','keys':{'auth':m.base64.urlsafe_b64encode(b'a'*16).decode().rstrip('='),'p256dh':m.base64.urlsafe_b64encode(b'\x04'+b'a'*64).decode().rstrip('=')}}
class NotificationsTest(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.sent=[];self.events=[];self.clock=100.;self.shared=True
  self.service=m.Notifications(self.tmp.name,self.matrix,lambda:{OWNER:SPACE},push=self.push,clock=lambda:self.clock)
  self.addCleanup(self.service.db.close)
 def matrix(self,path):
  if path.endswith('/state'):
   if SPACE in unquote(path):return [ev('m.room.create',{'type':'m.space'}),ev('m.space.child',{'via':['master']} if self.shared else {},ROOM)]
   return [ev('m.room.create',{'com.jkali.mirror_of':'!source:local'}),ev('m.room.name',{'name':'Test person'}),ev('com.jkali.share_level',{'level':'direct'})]
  q=parse_qs(urlsplit(path).query)
  if q['dir']==['b']:return {'start':str(len(self.events)),'chunk':self.events[-1:]}
  i=int(q['from'][0]);return {'chunk':self.events[i:], 'end':str(len(self.events))}
 def push(self,sub,payload):self.sent.append((sub,payload));return 201
 def follow(self,all_messages=False):self.service.handle({'action':'follow','room':ROOM,'all_messages':all_messages})
 def subscribe(self):return self.service.handle({'action':'subscribe','subscription':subscription()})['device']
 def test_incoming_push_and_no_body_in_payload(self):
  self.subscribe();self.follow();self.events.append(message());self.service.poll_once()
  self.assertEqual(len(self.sent),1);self.assertNotIn('private body',json.dumps(self.sent));self.assertIn('#notification=',self.sent[0][1]['url'])
  self.assertEqual(len(self.service.handle({'action':'status'})['history']),1)
 def test_dedup_replay_and_restart(self):
  self.subscribe();self.follow();self.events.extend([message(),message()]);self.service.poll_once();self.service.poll_once();self.assertEqual(len(self.sent),1)
  second=m.Notifications(self.tmp.name,self.matrix,lambda:{OWNER:SPACE},push=self.push,clock=lambda:self.clock);self.addCleanup(second.db.close);second.poll_once();self.assertEqual(len(self.sent),1)
 def test_follow_skips_old_backlog(self):
  self.events.append(message());self.subscribe();self.follow();self.service.poll_once();self.assertEqual(self.sent,[])
 def test_old_timestamp_is_not_alerted(self):
  self.subscribe();self.follow();e=message();e['origin_server_ts']=1;self.events.append(e);self.service.poll_once();self.assertEqual(self.sent,[])
 def test_outgoing_opt_in(self):
  self.subscribe();self.follow();self.events.append(message(1,True));self.service.poll_once();self.assertEqual(self.sent,[])
  self.follow(True);self.events.append(message(2,True));self.service.poll_once();self.assertEqual(len(self.sent),1)
 def test_unfollow_stops_alerts(self):
  self.subscribe();self.follow();self.service.handle({'action':'unfollow','room':ROOM});self.events.append(message());self.service.poll_once();self.assertEqual(self.sent,[])
 def test_share_revoked_suppresses_notification(self):
  self.subscribe();self.follow();self.events.append(message());self.shared=False;self.service.poll_once();self.assertEqual(self.sent,[]);self.assertEqual(self.service.handle({'action':'status'})['follows'],[])
 def test_untrusted_sender_and_edits_ignored(self):
  self.subscribe();self.follow();a=message();a['sender']='@attacker:master';b=message(2);b['content']['m.relates_to']={'rel_type':'m.replace'};self.events=[a,b];self.service.poll_once();self.assertEqual(self.sent,[])
 def test_retry_uses_existing_notification(self):
  self.subscribe();self.follow();self.events.append(message());self.service.push=lambda *_:503;self.service.poll_once();self.clock+=10;self.service.push=self.push;self.service.poll_once();self.assertEqual(len(self.sent),1);self.assertEqual(len(self.service.handle({'action':'status'})['history']),1)
 def test_unfollow_cancels_retry(self):
  self.subscribe();self.follow();self.events.append(message());self.service.push=lambda *_:503;self.service.poll_once();self.service.handle({'action':'unfollow','room':ROOM});self.clock+=10;self.service.push=self.push;self.service.deliver();self.assertEqual(self.sent,[])
 def test_expired_subscription_disabled(self):
  self.subscribe();self.follow();self.events.append(message());self.service.push=lambda *_:410;self.service.poll_once();self.assertEqual(self.service.handle({'action':'status'})['devices'],0)
 def test_notification_seen_does_not_touch_matrix(self):
  self.subscribe();self.follow();self.events.append(message());self.service.poll_once();nid=self.service.handle({'action':'status'})['history'][0]['id'];self.service.matrix=lambda _:self.fail('unexpected read/write');self.service.handle({'action':'seen','id':nid});self.assertEqual(self.service.handle({'action':'status'})['history'][0]['seen'],1)
 def test_arbitrary_push_destinations_rejected(self):
  for url in ['http://localhost/a','https://127.0.0.1/a','https://updates.push.services.mozilla.com.evil.test/a','https://user@web.push.apple.com/a','https://fcm.googleapis.com:444/a']:
   with self.subTest(url=url):
    sub=subscription();sub['endpoint']=url
    with self.assertRaises(ValueError):self.service.handle({'action':'subscribe','subscription':sub})
 def test_invalid_keys_rejected(self):
  sub=subscription();sub['keys']['auth']='wrong'
  with self.assertRaises(ValueError):self.service.handle({'action':'subscribe','subscription':sub})
 def test_unmanaged_room_rejected(self):
  self.service.roster=lambda:{}
  with self.assertRaises(ValueError):self.follow()
 def test_test_push_requires_registered_device(self):
  with self.assertRaises(ValueError):self.service.handle({'action':'test','device':'unknown'})
  device=self.subscribe();self.assertTrue(self.service.handle({'action':'test','device':device})['queued'])
 def test_disabled_device_not_sent(self):
  device=self.subscribe();self.follow();self.service.handle({'action':'unsubscribe','device':device});self.events.append(message());self.service.poll_once();self.assertEqual(self.sent,[])
 def test_secret_permissions(self):
  self.assertEqual(Path(self.tmp.name).stat().st_mode&0o777,0o700);self.assertEqual((Path(self.tmp.name)/'notifications.db').stat().st_mode&0o777,0o600)
 def test_test_trigger_is_durable_idempotent_and_visible(self):
  device=self.subscribe();data={'action':'test','device':device,'request_id':'one-test'}
  nid=self.service.handle(data)['notification_id'];self.assertEqual(self.sent,[])
  self.assertEqual(self.service.handle(data)['notification_id'],nid)
  second=m.Notifications(self.tmp.name,self.matrix,lambda:{OWNER:SPACE},push=self.push,clock=lambda:self.clock);self.addCleanup(second.db.close)
  second.handle({'action':'sync'});self.assertEqual(len(self.sent),1)
  record=second.handle({'action':'delivery','id':nid});self.assertEqual(record['kind'],'test');self.assertEqual(record['deliveries'][0]['state'],'accepted')
  self.assertNotIn('receipt_token',json.dumps(second.handle({'action':'status'})))
  self.assertNotIn('endpoint',json.dumps(second.handle({'action':'status'})))
  second.handle(data);second.poll_once();self.assertEqual(len(self.sent),1)
 def test_scoped_browser_receipts_are_idempotent_and_not_native_reads(self):
  self.subscribe();nid=self.service.handle({'action':'test'})['notification_id'];self.service.poll_once()
  token=self.sent[0][1]['receipt_token'];self.service.matrix=lambda _:self.fail('no native/Matrix read or write')
  self.service.receipt({'token':token,'stage':'shown'});self.clock+=1;self.service.receipt({'token':token,'stage':'shown'})
  record=self.service.handle({'action':'delivery','id':nid});self.assertEqual(record['deliveries'][0]['shown_at'],100000);self.assertEqual(record['seen'],0)
  self.service.receipt({'token':token,'stage':'clicked'});self.assertEqual(self.service.handle({'action':'delivery','id':nid})['deliveries'][0]['clicked_at'],101000)
  for value in [{'token':'a'*43,'stage':'shown'},{'token':token,'stage':'send'},{'token':None,'stage':'shown'}]:
   with self.assertRaises(ValueError):self.service.receipt(value)
  self.clock+=86401
  with self.assertRaises(ValueError):self.service.receipt({'token':token,'stage':'shown'})
 def test_disabled_device_cannot_acknowledge(self):
  device=self.subscribe();self.service.handle({'action':'test'});self.service.poll_once();token=self.sent[0][1]['receipt_token']
  self.service.handle({'action':'unsubscribe','device':device})
  with self.assertRaises(ValueError):self.service.receipt({'token':token,'stage':'shown'})
 def test_unavailable_room_does_not_stall_tests_or_other_rooms(self):
  self.subscribe();self.follow();self.service.handle({'action':'test'})
  self.service.matrix=lambda _:(_ for _ in ()).throw(OSError('offline'))
  self.service.poll_once();self.assertEqual(len(self.sent),1);self.assertIsNotNone(self.service.error)
  self.assertEqual(len(self.service.snapshot()['follows']),1)
 def test_unfollow_during_validation_cancels_send(self):
  self.subscribe();self.follow();self.events.append(message());self.service.push=lambda *_:503;self.service.poll_once();self.clock+=10
  real=self.service.verified
  def verified(room):
   result=real(room);self.service.handle({'action':'unfollow','room':room});return result
  self.service.verified=verified;self.service.push=self.push;self.service.deliver();self.assertEqual(self.sent,[])
 def test_client_diagnostics_are_bounded_and_secret_free(self):
  data={'action':'client_status','client':'12345678-1234-1234-1234-123456789abc','stage':'subscription','permission':'granted','error_code':'AbortError'}
  self.service.handle(data);self.assertEqual(self.service.snapshot()['clients'][0]['stage'],'subscription')
  self.assertEqual(self.service.snapshot()['clients'][0]['error_code'],'AbortError')
  for key,value in [('client','invalid'),('stage','send'),('permission','arbitrary'),('error_code','https://secret-endpoint')]:
   with self.assertRaises(ValueError):self.service.handle(dict(data,**{key:value}))
 def test_malformed_event_does_not_stall_valid_message(self):
  self.subscribe();self.follow();self.events.extend([{'content':[]},dict(message(),event_id=123),message()]);self.service.poll_once();self.assertEqual(len(self.sent),1)
 def test_browser_ack_stops_ambiguous_provider_retry(self):
  self.subscribe();self.service.handle({'action':'test'});self.service.push=lambda sub,payload:self.sent.append((sub,payload)) or 503;self.service.poll_once()
  self.service.receipt({'token':self.sent[0][1]['receipt_token'],'stage':'shown'});self.clock+=10;self.service.poll_once();self.assertEqual(len(self.sent),1)
 def test_fast_browser_receipt_preserves_attempt_count_and_acceptance(self):
  self.subscribe();nid=self.service.handle({'action':'test'})['notification_id']
  def fast_push(sub,payload):
   self.service.receipt({'token':payload['receipt_token'],'stage':'shown'});return 503
  self.service.push=fast_push;self.service.poll_once()
  delivery=self.service.handle({'action':'delivery','id':nid})['deliveries'][0]
  self.assertEqual(delivery['state'],'accepted');self.assertEqual(delivery['attempts'],1)
class CanonicalListsTest(NotificationsTest):
 def canonical(self, data):
  self.calls.append(data)
  if data['action']=='rules':return {'rules':self.rules}
  if data['action']=='updates':return {'updates':self.updates}
  if data['action']=='unfollow':self.rules=[]
  return {}
 def enable(self):
  self.calls=[];self.updates=[];self.rules=[{'list_id':'list','room':ROOM,'owner':OWNER,'title':'Test','channels':['browser'],'all_messages':False}]
  self.service.canonical=self.canonical
 def test_migrates_once_preserving_cursor_then_uses_list_rules(self):
  self.follow(True);self.events.append(message());self.enable();self.subscribe();self.service.poll_once()
  self.assertEqual(len(self.sent),1)
  migration=[d for d in self.calls if d['action']=='migrate_follows'];self.assertEqual(len(migration),1);self.assertEqual(migration[0]['follows'][0]['all_messages'],1)
  self.service.poll_once();self.assertEqual(len([d for d in self.calls if d['action']=='migrate_follows']),1)
  self.rules=[];self.service.poll_once();self.assertEqual(self.service.snapshot()['follows'],[])
 def test_browser_deadlines_only_when_explicit_fresh_and_caught_up(self):
  self.enable();self.subscribe();self.service.poll_once()
  obligation={'event_id':'$incoming','due_at':None,'waiting':'waiting_for_us'}
  row={'list_id':'list','id':ROOM,'owner':OWNER,'title':'Test','fresh':True,'obligations':[obligation]}
  self.updates=[row];self.service.poll_once();self.assertEqual(len(self.sent),0)
  obligation['due_at']=90000;row['fresh']=False;self.service.poll_once();self.assertEqual(len(self.sent),0)
  row['fresh']=True;self.service.poll_once();self.service.poll_once();self.assertEqual(len(self.sent),1);self.assertIn('deadline',self.sent[0][1]['body'])
 def test_message_level_reminders_cancel_answered_and_snoozed_jobs(self):
  self.enable();self.subscribe();self.service.poll_once()
  a={'event_id':'$older','due_at':90000,'waiting':'waiting_for_them'}
  b={'event_id':'$newer','due_at':90000,'waiting':'waiting_for_us','snooze_until':200000}
  self.updates=[{'list_id':'list','id':ROOM,'owner':OWNER,'title':'Test','fresh':True,'obligations':[a,b]}]
  self.service.push=lambda *_:503;self.service.poll_once()
  a['waiting']='answered';b['snooze_until']=None;self.clock+=90;self.service.push=self.push;self.service.poll_once()
  self.assertEqual(len(self.sent),1)
  history=self.service.snapshot()['history']
  self.assertEqual({h['event'] for h in history},{'$older','$newer'})
 def test_list_service_failure_suppresses_delivery(self):
  self.enable();self.subscribe();self.service.poll_once();self.events.append(message())
  self.service.canonical=lambda _:(_ for _ in ()).throw(OSError('offline'))
  with self.assertRaises(OSError):self.service.poll_once()
  self.assertEqual(self.sent,[])
 def test_mode_change_cancels_pending_outgoing(self):
  self.enable();self.rules[0]['all_messages']=True;self.subscribe();self.service.poll_once();self.events.append(message(1,True));self.service.push=lambda *_:503;self.service.poll_once()
  self.rules[0]['all_messages']=False;self.clock+=90;self.service.push=self.push;self.service.poll_once();self.assertEqual(self.sent,[])
 def test_quiet_hours_defer_the_same_job_until_open(self):
  self.clock=1772488800. # Monday 2026-03-02 22:00 UTC
  self.enable();self.rules[0]['schedule']={'timezone':'UTC','mode':'quiet','days':[1,2,3,4,5,6,7],'start_minute':1320,'end_minute':480}
  self.subscribe();self.service.poll_once();e=message();e['origin_server_ts']=int(self.clock*1000);self.events.append(e);self.service.poll_once();self.assertEqual(self.sent,[])
  history=self.service.snapshot()['history'];self.assertEqual(len(history),1);self.assertEqual(history[0]['deliveries'][0]['state'],'pending')
  self.clock+=10*3600;self.service.poll_once();self.assertEqual(len(self.sent),1);self.service.poll_once();self.assertEqual(len(self.sent),1)
 def test_activity_off_still_allows_explicit_repeating_reminders(self):
  self.enable();self.rules[0].update(activity_enabled=False,repeat_ms=60000);self.subscribe();self.service.poll_once();self.events.append(message())
  obligation={'event_id':'$incoming','due_at':100000,'waiting':'waiting_for_us'}
  self.updates=[{'list_id':'list','id':ROOM,'owner':OWNER,'title':'Test','fresh':True,'obligations':[obligation]}]
  self.service.poll_once();self.assertEqual(len(self.sent),1);self.assertIn('deadline',self.sent[-1][1]['body'])
  self.service.poll_once();self.assertEqual(len(self.sent),1);self.clock+=600;self.service.poll_once();self.assertEqual(len(self.sent),2)
  obligation['waiting']='answered';self.clock+=60;self.service.poll_once();self.assertEqual(len(self.sent),2)
 def test_overlapping_lists_independently_gate_delivery(self):
  self.enable();self.rules[0].update(list_name='Quiet',schedule={'timezone':'UTC','mode':'quiet','days':[1,2,3,4,5,6,7],'start_minute':0,'end_minute':1440})
  self.rules.append(dict(self.rules[0],list_id='open',list_name='Open',schedule=None));self.subscribe();self.service.poll_once();self.events.append(message());self.service.poll_once()
  self.assertEqual(len(self.sent),1);self.assertIn('Lists: Open',self.sent[0][1]['body']);self.assertNotIn('Quiet',self.sent[0][1]['body'])
 def test_revision_change_cancels_queued_notification(self):
  self.enable();self.rules[0]['revision']=1;self.subscribe();self.service.poll_once();self.events.append(message());self.service.push=lambda *_:503;self.service.poll_once()
  self.rules[0]['revision']=2;self.clock+=10;self.service.push=self.push;self.service.poll_once();self.assertEqual(self.sent,[])
  self.assertEqual(self.service.snapshot()['history'][0]['deliveries'][0]['state'],'canceled')
 def test_receipt_lifetime_starts_at_delivery_after_long_deferral(self):
  self.enable();self.rules[0]['schedule']={'timezone':'UTC','mode':'working','days':[1,2,3,4,5,6,7],'start_minute':0,'end_minute':1440}
  self.subscribe();self.service.poll_once()
  with self.service.db:self.service.enqueue('deferred',ROOM,'$deferred','Deferred conversation',rule_refs={'outgoing':False,'rules':[{'id':'list','revision':None}]})
  self.clock+=172800;self.service.deliver();self.assertEqual(len(self.sent),1)
  self.service.receipt({'token':self.sent[0][1]['receipt_token'],'stage':'shown'})
  self.assertIsNotNone(self.service.snapshot()['history'][0]['deliveries'][0]['shown_at'])
 def test_overlapping_repeat_intervals_combine_and_quiet_repeat_does_not_leak(self):
  self.enable();self.rules[0]['repeat_ms']=60000;self.rules.append(dict(self.rules[0],list_id='other',repeat_ms=120000));self.subscribe();self.service.poll_once()
  self.updates=[{'list_id':r['list_id'],'id':ROOM,'owner':OWNER,'title':'Test','fresh':True,'obligations':[{'event_id':'$incoming','due_at':10000,'waiting':'waiting_for_us'}]} for r in self.rules]
  self.service.poll_once();self.assertEqual(len(self.sent),1)
  self.rules[0]['schedule']={'timezone':'UTC','mode':'quiet','days':[1,2,3,4,5,6,7],'start_minute':0,'end_minute':1440}
  self.rules[1]['repeat_ms']=None
  self.clock+=600;self.service.poll_once();count=len(self.sent)
  self.clock+=600;self.service.poll_once();self.assertEqual(len(self.sent),count,'a quiet repeat must not make the once-only list repeat')

if __name__=='__main__':unittest.main()
