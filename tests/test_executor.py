"""Native executor protocol and no-replay boundaries; no real phone or network."""
import asyncio
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from contextlib import closing
from unittest.mock import Mock,AsyncMock

sys.path.insert(0,os.environ.get('COMPANION_TEST_PLUGINS',str(Path(__file__).parent)))
from astrbot_plugin_companion_phone.executor import Executor,ExecutorError
from astrbot_plugin_companion_phone.native_transport import NativeTransport
from astrbot_plugin_companion_phone.phone import Phone,PhoneError

APP='com.coolapk.market'
def heartbeat(**extra):return {'boot':'synthetic-boot-one','paused':False,'ready':True,'locked':False,'screen_on':True,
    'foreground':APP,'model':'V2046A','android':'13','sdk':33,**extra}


class ExecutorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        self.guard=Mock(return_value=True);self.ex=Executor(self.root,self.guard,lambda:[APP,'com.tencent.mobileqq'])
        self.ex.poll(heartbeat())

    async def queued(self,op='tap',args=None):
        task=asyncio.create_task(self.ex.rpc(op,args or {'x':10,'y':20,'package':APP}))
        await asyncio.sleep(0)
        return task

    async def test_unique_lease_returns_once_and_duplicate_result_does_not_repeat(self):
        task=await self.queued();job=self.ex.poll(heartbeat())['job'];self.assertIsNotNone(job)
        self.assertIsNone(self.ex.poll(heartbeat())['job'])
        reply={'id':job['id'],'lease':job['lease'],'boot':heartbeat()['boot'],'result':{'ok':True}}
        self.ex.result(reply);self.assertTrue((await task)['ok']);self.assertTrue(self.ex.result(reply)['duplicate'])

    async def test_wrong_credentials_and_wrong_lease_cannot_get_or_finish_job(self):
        self.assertFalse(self.ex.authenticate('not-the-key'));self.assertTrue(self.ex.authenticate(self.ex.token))
        task=await self.queued();job=self.ex.poll(heartbeat())['job']
        with self.assertRaises(ExecutorError):self.ex.result({'id':job['id'],'lease':'wrong','boot':heartbeat()['boot'],'result':{'ok':True}})
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):await task

    async def test_sleep_and_human_pause_never_issue_pending_action(self):
        task=await self.queued();self.guard.return_value=False
        self.assertIsNone(self.ex.poll(heartbeat())['job'])
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):await task
        self.guard.return_value=True;self.ex.poll(heartbeat(paused=True))
        with self.assertRaises(ExecutorError):await self.ex.rpc('tap',{})

    async def test_lost_or_cancelled_action_never_requeues_but_late_receipt_is_saved(self):
        task=await self.queued();job=self.ex.poll(heartbeat())['job'];task.cancel()
        with self.assertRaises(asyncio.CancelledError):await task
        self.assertIsNone(self.ex.poll(heartbeat())['job'])
        self.ex.result({'id':job['id'],'lease':job['lease'],'boot':heartbeat()['boot'],'result':{'ok':True}})
        with closing(self.ex.db()) as db:self.assertEqual(db.execute('SELECT status FROM jobs').fetchone()[0],'done')

    async def test_server_restart_keeps_identity_and_marks_reserved_jobs_uncertain(self):
        task=await self.queued();job=self.ex.poll(heartbeat())['job'];task.cancel()
        with self.assertRaises(asyncio.CancelledError):await task
        restored=Executor(self.root,self.guard,lambda:[APP]);self.assertEqual(restored.token,self.ex.token)
        self.assertTrue(restored.paired);self.assertFalse(restored.online())
        self.assertIsNone(restored.poll(heartbeat())['job'])
        with closing(restored.db()) as db:self.assertEqual(db.execute('SELECT status FROM jobs').fetchone()[0],'uncertain')

    async def test_phone_restart_does_not_replay_inflight_job(self):
        task=await self.queued();job=self.ex.poll(heartbeat())['job']
        self.assertIsNone(self.ex.poll(heartbeat(boot='synthetic-boot-two'))['job'])
        with closing(self.ex.db()) as db:self.assertEqual(db.execute('SELECT status FROM jobs').fetchone()[0],'uncertain')
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):await task

    async def test_lock_and_unknown_operations_are_rejected_before_enqueue(self):
        self.ex.poll(heartbeat(locked=True))
        for op in ('tap','screenshot','shell','install'):
            with self.assertRaises(ExecutorError):await self.ex.rpc(op,{})
        with closing(self.ex.db()) as db:self.assertEqual(db.execute('SELECT count(*) FROM jobs').fetchone()[0],0)

    async def test_private_qq_app_is_not_a_native_capability(self):
        self.assertNotIn('com.tencent.mobileqq',self.ex.allowed_apps())
        self.ex.poll(heartbeat(foreground='com.tencent.mobileqq'));self.assertEqual(self.ex.status['foreground'],'')

    async def test_no_adb_fallback_after_pairing_pause_or_disconnect(self):
        phone=Phone(self.root);phone.executor=self.ex;phone.native=NativeTransport(self.ex);phone._run=AsyncMock()
        self.ex.poll(heartbeat(paused=True))
        with self.assertRaises(PhoneError):phone.ready()
        with self.assertRaises(PhoneError):await phone._shell('input','tap','10','20')
        self.ex.poll(heartbeat());self.ex.seen=0
        with self.assertRaises(PhoneError):phone.ready()
        phone._run.assert_not_called()

    async def test_unknown_native_shell_template_does_not_reach_adb(self):
        phone=Phone(self.root);phone.executor=self.ex;phone.native=NativeTransport(self.ex);phone._run=AsyncMock()
        with self.assertRaises(PhoneError):await phone._shell('sh','-c','anything')
        phone._run.assert_not_called()

    async def test_pause_persists_across_reload(self):
        self.ex.poll(heartbeat(paused=True));restored=Executor(self.root,self.guard,lambda:[APP])
        phone=Phone(self.root);phone.executor=restored
        with self.assertRaises(PhoneError):phone.ready()

    async def test_raw_image_is_not_persisted_to_job_database(self):
        task=await self.queued('screenshot',{'package':APP});job=self.ex.poll(heartbeat())['job']
        self.ex.result({'id':job['id'],'lease':job['lease'],'boot':heartbeat()['boot'],'result':{'ok':True,'image':'SYNTHETIC_IMAGE_BYTES'}})
        await task
        with closing(self.ex.db()) as db:result=db.execute('SELECT result FROM jobs').fetchone()[0]
        self.assertNotIn('SYNTHETIC_IMAGE_BYTES',result)


if __name__=='__main__':unittest.main()
