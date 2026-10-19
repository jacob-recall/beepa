"""Real isolated Synapse + authenticated notification HTTP API. Push vendor mocked.
Run only via tests/integration/run.sh --notifications; never reads live tokens.
"""
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import threading
from urllib.parse import quote
from unittest.mock import patch
from test_enroll import SANDBOX, TOK, MASTER_HS, http_json, enroll
from notifications import Notifications

sent=[]
owner=TOK['MASTER_ALICE_USER'];manager=TOK['MASTER_MANAGER_USER'];token=TOK['MASTER_MANAGER_TOKEN'];alice=TOK['MASTER_ALICE_TOKEN']
space=TOK['MASTER_SPACE_ALICE']
def matrix(method,path,body=None,who=alice):
 status,data=http_json(MASTER_HS+'/_matrix/client/v3'+path,method,who,body)
 assert 200<=status<300,(method,path,status)
 return data

def state(room,typ,body,key=''):
 return matrix('PUT','/rooms/'+quote(room,safe='')+'/state/'+quote(typ,safe='')+'/'+quote(key,safe=''),body)

room=matrix('POST','/createRoom',{'preset':'private_chat','invite':[manager],
 'creation_content':{'com.jkali.mirror_of':'!native:fixture','m.federate':False},
 'power_level_content_override':{'users':{owner:100,manager:0},'events_default':50},
 'initial_state':[{'type':'com.jkali.share_level','state_key':'','content':{'level':'direct'}}], 'name':'Synthetic followed chat'})['room_id']
for rid in (room,space):matrix('POST','/join/'+quote(rid,safe=''),{},token)
state(space,'m.space.child',{'via':['master']},room)
# Backlog must be ignored even though it is a valid incoming message.
def send(tx,own=False):
 return matrix('PUT','/rooms/'+quote(room,safe='')+'/send/m.room.message/'+tx,{'msgtype':'m.text','body':'Synthetic '+tx,'com.jkali.from_me':own})['event_id']
send('backlog')
server=ThreadingHTTPServer(('127.0.0.1',0),enroll._make_handler());thread=threading.Thread(target=server.serve_forever,kwargs={'poll_interval':.01},daemon=True);thread.start()
base='http://127.0.0.1:'+str(server.server_port)
def api(data,who=token):
 code,body=http_json(base+'/admin/notifications','POST',who,data)
 assert code==200,(code,body)
 return body
try:
 with patch.object(Notifications,'start',lambda self:None),patch.object(Notifications,'_push',lambda self,sub,payload:sent.append(payload) or 201):
  for who,expected in [('invalid',401),(alice,403),(TOK['MASTER_BOB_TOKEN'],403)]:
   assert http_json(base+'/admin/notifications','POST',who,{'action':'status'})[0]==expected
  import base64
  b64=lambda b:base64.urlsafe_b64encode(b).decode().rstrip('=')
  device=api({'action':'subscribe','subscription':{'endpoint':'https://updates.push.services.mozilla.com/wpush/v2/fixture','keys':{'auth':b64(b'a'*16),'p256dh':b64(b'\x04'+b'a'*64)}}})['device']
  api({'action':'follow','room':room});assert api({'action':'sync'})['history']==[]
  incoming=send('incoming');result=api({'action':'sync'});assert len(result['history'])==1
  record=result['history'][0];assert record['event']==incoming;assert record['deliveries'][0]['state']=='accepted';assert len(sent)==1
  assert 'Synthetic incoming' not in json.dumps(sent)
  # Browser display capability, with no manager token; arbitrary token refused.
  receipt={'token':sent[0]['receipt_token'],'stage':'shown'}
  assert http_json(base+'/admin/notifications/receipt','POST',body=receipt)[0]==200
  assert http_json(base+'/admin/notifications/receipt','POST',body={'token':'a'*43,'stage':'shown'})[0]==403
  assert api({'action':'delivery','id':record['id']})['deliveries'][0]['shown_at']
  api({'action':'sync'});assert len(sent)==1
  send('outgoing',True);api({'action':'sync'});assert len(sent)==1
  api({'action':'follow','room':room,'all_messages':True});send('opt-in-outgoing',True);api({'action':'sync'});assert len(sent)==2
  # Persisted cursor/outbox survive a service restart without replay.
  enroll._notifications_service.db.close();enroll._notifications_service=None
  api({'action':'sync'});assert len(sent)==2
  test=api({'action':'test','device':device,'request_id':'retry-safe'});api({'action':'sync'});assert len(sent)==3
  assert api({'action':'test','device':device,'request_id':'retry-safe'})==test
  api({'action':'sync'});assert len(sent)==3
  # Controller adapter keeps exact existing source IDs and exposes delivery
  # evidence without subscriptions or message bodies. Enqueue must not call
  # back into a controller that could be waiting for its acknowledgment.
  adapter_entry={'room':room,'event':incoming,'kind':'message','refs':{'outgoing':False,'rules':[{'id':'fixture-policy','revision':1}]}}
  def no_callback(data):raise AssertionError('Enqueue must not call the controller')
  with patch.object(enroll._notifications_service,'canonical',no_callback):
   for who in (alice,TOK['MASTER_BOB_TOKEN']):assert http_json(base+'/admin/notifications','POST',who,{'action':'control_enqueue','entries':[adapter_entry]})[0]==403
   adapter=api({'action':'control_enqueue','entries':[adapter_entry]});assert adapter['outcomes'][0]['id']==record['id'];assert adapter['outcomes'][0]['state']=='accepted'
   assert len(sent)==3
   evidence=api({'action':'control_outcomes','ids':[record['id']]});assert evidence['outcomes'][0]['state']=='accepted'
   assert all(value not in json.dumps(evidence) for value in ['endpoint','receipt_token','Synthetic incoming'])
  # Run real headless control against these same disposable homeservers.
  import subprocess
  import tempfile
  proposals=matrix('POST','/createRoom',{'preset':'private_chat','invite':[manager],
   'power_level_content_override':{'users':{owner:100,manager:50},'events_default':100,'state_default':100,
    'events':{'com.jkali.proposal':50,'com.jkali.crm.direct_request':50,'com.jkali.monitoring_scope':50}},
   'initial_state':[{'type':'m.room.guest_access','state_key':'','content':{'guest_access':'forbidden'}},
    {'type':'com.jkali.proposals','state_key':'','content':{'enabled':True}},
    {'type':'com.jkali.crm.capability','state_key':'','content':{'schema':1,'owner_mxid':owner,'generation':'fixture-generation',
     'journal_epoch':'fixture-journal','ordered_policy':True,'direct_prepare_commit':True,'conversation_tracking':True}}]})['room_id']
  matrix('POST','/join/'+quote(proposals,safe=''),{},token);state(space,'m.space.child',{'via':['master']},proposals)
  state(room,'com.jkali.source',{'source':'imessage'})
  with patch.object(enroll,'_control_request',return_value={'paired':True}):
   assert http_json(base+'/admin/control','POST',token,{'action':'status'})==(200,{'paired':True})
   for who in [alice,TOK['MASTER_BOB_TOKEN']]:assert http_json(base+'/admin/control','POST',who,{'action':'status'})[0]==403
   assert http_json(base+'/admin/control','POST','invalid',{'action':'status'})[0]==401
  with tempfile.TemporaryDirectory(prefix='beepa-contact-callback-') as callback_dir:
   callback_port=Path(callback_dir)/'port'
   def callback(data):
    if not callback_port.exists():return no_callback(data)
    port=int(callback_port.read_text());assert 1024<port<65536 and port not in (8008,8011,8018,8019,8023)
    code,result=http_json('http://127.0.0.1:'+str(port)+'/control','POST',token,data)
    assert code==200,(code,result)
    return result
   with patch.object(enroll._notifications_service,'canonical',callback):
    for who in (alice,TOK['MASTER_BOB_TOKEN']):assert http_json(base+'/admin/notifications','POST',who,{'action':'control_dispatcher'})[0]==403
    subprocess.run(['node',str(Path(__file__).with_name('telegram_control_client.js'))],input=json.dumps({
     'base':MASTER_HS,'enrollBase':base,'token':token,'manager':manager,'owner':owner,'ownerToken':alice,'space':space,'room':room,'proposals':proposals,'callbackPort':str(callback_port)}),text=True,check=True)
  after_control=len(sent);assert after_control>3
  # Following the handoff, canonical rules own the projection. The synthetic
  # controller has shut down; return the now-empty authorized monitoring scope.
  enroll._notifications_service.canonical=lambda data: {'rules': [], 'updates': [], 'migrated': True}
  state(room,'com.jkali.share_level',{'level':'private'});send('revoked');assert api({'action':'sync'})['follows']==[];assert len(sent)==after_control
  api({'action':'unsubscribe','device':device});assert api({'action':'status'})['devices']==0
 print('notifications: real Synapse owner verification, API auth, backlog skip, incoming/all, provider queue, scoped display receipt, idempotency, restart and revocation passed; native and push vendor not used.')
finally:
 server.shutdown();server.server_close();thread.join(2)
 if enroll._notifications_service:enroll._notifications_service.db.close()
