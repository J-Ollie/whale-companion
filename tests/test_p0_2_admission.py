import asyncio,unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock,Mock
import test_brain as fixtures
from astrbot_plugin_companion_phone.autonomy import AutonomousBrowser

class AdmissionTests(unittest.IsolatedAsyncioTestCase):
 setUp=fixtures.BrainTests.setUp
 def browser(self):
  return NS(thinking=NS(phone=NS(context=self.chat.context)),state={},save=Mock(),_consider_session=AsyncMock(),due=lambda now:True)
 async def test_missing_brain_never_starts_force_or_background(self):
  a=self.browser()
  for force in (False,True):
   await AutonomousBrowser.consider(a,force,'private task')
  a._consider_session.assert_not_awaited();self.assertEqual(a.state['last_result'],'brain_unavailable')
 async def test_requested_admission_returns_result_and_no_model_per_operation(self):
  self.plugins['astrbot_plugin_companion_chat']=NS(activated=True,star_cls=self.chat)
  a=self.browser();self.brain.foreground=1
  async def work(*args):a.state['last_result']='uncertain'
  a._consider_session.side_effect=work
  self.assertTrue(await AutonomousBrowser.consider(a,True,'task'))
  a._consider_session.assert_awaited_once_with(True,'task');self.model.assert_not_awaited()
  with self.brain.connect() as db:self.assertEqual(db.execute("select status from events where kind='autonomy'").fetchone()[0],'uncertain')
 async def test_disabled_brain_does_not_execute_worker(self):
  self.chat.config['brain_enabled']=False;worker=AsyncMock()
  self.assertFalse(await self.brain.run('outreach',worker));worker.assert_not_awaited()
 async def test_requested_does_not_bypass_other_worker(self):
  lease=self.brain.begin('social');worker=AsyncMock()
  self.assertFalse(await self.brain.run('autonomy',worker,requested=True));worker.assert_not_awaited();self.brain.finish(lease,'uncertain')
 async def test_authorized_worker_runs_many_local_operations(self):
  op=AsyncMock()
  async def worker():
   for _ in range(4):await op()
  self.assertTrue(await self.brain.run('autonomy',worker));self.assertEqual(op.await_count,4);self.model.assert_not_awaited()
 async def test_error_releases_lease_without_retry(self):
  op=AsyncMock(side_effect=TimeoutError())
  with self.assertRaises(TimeoutError):await self.brain.run('autonomy',op)
  op.assert_awaited_once();self.assertIsNone(self.brain.running)

 async def test_missing_brain_outreach_does_not_send(self):
  from astrbot_plugin_companion_chat.active.main import CompanionActive
  p=NS(context=NS(get_registered_star=lambda name:None),_consider_tracked=AsyncMock())
  self.assertFalse(await CompanionActive.consider(p));p._consider_tracked.assert_not_awaited()
