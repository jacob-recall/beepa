"""HTTP boundary tests: no live services or credentials."""
import http.client
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import Mock, patch
sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'master'))
import enroll

class ApiTests(unittest.TestCase):
 def setUp(self):
  self.service=Mock();self.service.handle.return_value={'queued':True};self.service.receipt.return_value={'recorded':True}
  for p in [patch.object(enroll,'_manager_mxid',return_value='@manager:test'),patch.object(enroll,'_cs_base',return_value='http://test.invalid'),
            patch.object(enroll,'_whoami',side_effect=lambda base,token:{'manager':'@manager:test','user':'@alice:test'}.get(token)),patch.object(enroll,'_notifications',return_value=self.service)]:
   p.start();self.addCleanup(p.stop)
  self.server=ThreadingHTTPServer(('127.0.0.1',0),enroll._make_handler())
  self.thread=threading.Thread(target=self.server.serve_forever,kwargs={'poll_interval':.01},daemon=True);self.thread.start();self.addCleanup(self.close)
 def close(self):self.server.shutdown();self.server.server_close();self.thread.join(2)
 def request(self,body,token=None,path='/admin/notifications'):
  c=http.client.HTTPConnection('127.0.0.1',self.server.server_port,timeout=3)
  headers={'Content-Type':'application/json'}
  if token:headers['Authorization']='Bearer '+token
  try:
   c.request('POST',path,body,headers);r=c.getresponse();return r.status,json.loads(r.read())
  finally:c.close()
 def test_auth_precedes_body_parse_and_worker(self):
  for token,expected in [(None,401),('invalid',401),('user',403)]:self.assertEqual(self.request('bad json',token)[0],expected)
  self.service.handle.assert_not_called()
 def test_manager_can_invoke_all_actions(self):
  for action in ['status','key','client_status','follow','unfollow','subscribe','unsubscribe','test','sync','delivery','seen','control_enqueue','control_outcomes','control_dispatcher']:
   self.assertEqual(self.request(json.dumps({'action':action}),'manager')[0],200)
   self.assertEqual(self.service.handle.call_args.args[0],{'action':action})
 def test_bad_body_and_internal_error_do_not_leak(self):
  self.assertEqual(self.request('[]','manager')[0],400)
  self.service.handle.side_effect=RuntimeError('SECRET')
  code,body=self.request('{}','manager');self.assertEqual(code,503);self.assertNotIn('SECRET',json.dumps(body))
 def test_receipt_route_has_only_capability_scoped_action(self):
  self.assertEqual(self.request('{"token":"fixture","stage":"shown"}',path='/admin/notifications/receipt')[0],200)
  self.service.handle.assert_not_called();self.service.receipt.assert_called_once()
  self.service.receipt.side_effect=ValueError('private reason')
  self.assertEqual(self.request('{}',path='/admin/notifications/receipt'),(403,{'error':'Invalid notification receipt'}))
if __name__=='__main__':unittest.main()
