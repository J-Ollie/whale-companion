"""Opt-in phone permissions preserve payment and manual pause boundaries; no live actions."""
import os,sys,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
sys.path.insert(0,os.environ.get('COMPANION_TEST_PLUGINS',str(Path(__file__).parent)))
from astrbot_plugin_companion_phone.access import enabled_apps
from astrbot_plugin_companion_phone.executor import Executor
from astrbot_plugin_companion_phone.autonomy import AutonomousBrowser
APP='com.example.reader'
class FullAccessTests(unittest.IsolatedAsyncioTestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
 def test_new_apps_require_explicit_full_grant_and_valid_package(self):
  c={'autonomous_apps':[APP,'bad package'],'installed_app_names':{APP:'Example','bad package':'Invalid'}}
  self.assertEqual(enabled_apps(c),{})
  c['full_phone_access']=True
  self.assertEqual(enabled_apps(c),{APP:'Example'})
 def test_device_policy_is_opt_in_and_does_not_resume_manual_pause(self):
  c={'full':False};e=Executor(Path(self.tmp.name),lambda:True,lambda:[APP],lambda:c['full'])
  body={'boot':'synthetic-boot-full','paused':True,'ready':True,'locked':False,'screen_on':True}
  self.assertFalse(e.poll(body)['full_phone_access']);c['full']=True
  reply=e.poll(body);self.assertTrue(reply['full_phone_access']);self.assertFalse(reply['role_active']);self.assertIsNone(reply['job'])
 async def test_send_is_granted_only_in_full_mode_and_payment_remains_blocked(self):
  c={'autonomous_apps':[]};p=SimpleNamespace(root=Path(self.tmp.name),_assert_foreground=AsyncMock(),target_info=AsyncMock(return_value={'text':'发送','_protected_navigation':True}),tap=AsyncMock())
  a=AutonomousBrowser(p,c,AsyncMock(),lambda:'')
  action={'action':'tap','intent':'navigate','x':100,'y':100}
  self.assertFalse(await a.interactive_action(APP,action));p.tap.assert_not_awaited()
  c['full_phone_access']=True
  self.assertTrue(await a.interactive_action(APP,action));self.assertEqual(p.tap.await_count,1)
  for target in [{'text':'付款'},{'text':'删除'},{'text':'账号安全'}]:
   p.target_info.return_value=target
   self.assertFalse(await a.interactive_action(APP,action))
  self.assertEqual(p.tap.await_count,1)
