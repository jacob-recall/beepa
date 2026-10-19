"""Manager-only control proxy; synthetic credentials and mocked upstream."""
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch

spec=importlib.util.spec_from_file_location('notification_api_fixture',Path(__file__).with_name('master_notification_api.test.py'))
fixture=importlib.util.module_from_spec(spec);spec.loader.exec_module(fixture)

class ControlApiTests(fixture.ApiTests):
 def test_control_requires_manager_before_parsing(self):
  with patch.object(fixture.enroll,'_control_request',return_value={'paired':True}) as control:
   for token,expected in [(None,401),('invalid',401),('user',403)]:
    self.assertEqual(self.request('bad json',token,path='/admin/control')[0],expected)
   control.assert_not_called()
   self.assertEqual(self.request('{"action":"status"}','manager',path='/admin/control'),(200,{'paired':True}))
   control.assert_called_once_with({'action':'status'})
 def test_control_errors_hide_secrets(self):
  with patch.object(fixture.enroll,'_control_request',side_effect=RuntimeError('SECRET')):
   code,value=self.request('{}','manager',path='/admin/control')
   self.assertEqual(code,503);self.assertNotIn('SECRET',str(value))

if __name__=='__main__':unittest.main()
