"""Synthetic source provenance, durable followup claims and recall relevance."""
import asyncio
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

sys.path.insert(0, os.environ.get('COMPANION_TEST_PLUGINS', str(Path(__file__).parent)))
from astrbot.api.message_components import Plain, Reply
from astrbot_plugin_companion_chat.context_material import MaterialContext
from astrbot_plugin_companion_chat.followup import Followups
from astrbot_plugin_companion_chat.friends.sleep_inbox import SleepInbox
from astrbot_plugin_companion_chat.behavior import recent_reply_hint
from astrbot_plugin_companion_chat.batch.main import CompanionBatch
from astrbot_plugin_companion_memory.memory import MemoryStore
from astrbot_plugin_companion_memory.cognition import Cognition
from astrbot_plugin_companion_memory.recall_index import weak_recall
from astrbot_plugin_companion_memory.main import CompanionMemory

A='12345678'; B='87654321'


class Event:
    def __init__(self, text='等一下，我还没说完', sender=A, mid=1, parts=None):
        self.message_str=text; self.sender=sender
        self.unified_msg_origin='qq-companion:FriendMessage:'+sender
        self.message_obj=SimpleNamespace(message_id=str(mid),message=parts or [Plain(text)],
            raw_message={'user_id':int(sender),'message_id':mid,'message_type':'private','message':[{'type':'text','data':{'text':text}}]})
        self.extra={};self.result=None
    def get_extra(self,key,default=None):return self.extra.get(key,default)
    def set_extra(self,key,value):self.extra[key]=value
    def get_sender_id(self):return self.sender
    def get_result(self):return self.result
    def get_platform_id(self):return 'qq-companion'
    def get_group_id(self):return ''
    def should_call_llm(self,value):self.suppressed=value
    def stop_event(self):self.stopped=True


class SourceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.f=SimpleNamespace(policy=SimpleNamespace(root=Path(self.tmp.name)),sleeping=lambda:False,call=AsyncMock())
        self.p=MaterialContext(self.f)
    async def test_quote_preserves_self_and_original_separately(self):
        e=Event('帮我看看 我生病了',parts=[Plain('帮我看看'),Reply(id='7',message_str='我生病了',sender_nickname='第三人')])
        await self.p.prepare(e)
        data=e.extra['companion_material_context']
        self.assertEqual(data['self_text'],'帮我看看')
        self.assertEqual(data['materials'][0]['text'],'我生病了')
        self.assertEqual(data['materials'][0]['author'],'第三人')
        self.assertEqual(e.message_str,'帮我看看 我生病了')
        req=SimpleNamespace(extra_user_content_parts=[]);self.p.inject(e,req)
        self.assertEqual(len(req.extra_user_content_parts),1)
        self.f.call.assert_not_awaited()
        with sqlite3.connect(self.p.path) as db:self.assertEqual(db.execute('select count(*) from materials').fetchone()[0],1)
    async def test_missing_quote_fetches_only_message_and_handles_wrapper(self):
        self.f.call.return_value={'retcode':0,'data':{'sender':{'user_id':999},'message':[{'type':'text','data':{'text':'第三方原文'}}]}}
        e=Event(parts=[Reply(id='7')]);await self.p.prepare(e)
        self.f.call.assert_awaited_once_with('get_msg',message_id=7)
        self.assertEqual(e.extra['companion_material_context']['materials'][0]['text'],'第三方原文')
    async def test_forward_authors_and_nested_limits(self):
        nodes=[{'sender':{'nickname':'作者'+str(n)},'content':[{'type':'text','data':{'text':'内容'+str(n)}}]} for n in range(40)]
        e=Event(parts=[SimpleNamespace(type='forward',id='synthetic')]);self.f.call.return_value={'messages':nodes}
        await self.p.prepare(e);rows=e.extra['companion_material_context']['materials']
        self.assertLessEqual(len(rows),24);self.assertEqual(rows[0]['author'],'作者0');self.assertEqual(rows[0]['text'],'内容0')
    async def test_unknown_contents_are_not_guessed(self):
        self.f.call.side_effect=RuntimeError('unavailable')
        e=Event(parts=[Reply(id='7')]);await self.p.prepare(e)
        self.assertIn('未能读取',e.extra['companion_material_context']['materials'][0]['text'])
    async def test_plain_message_does_not_write_material_archive(self):
        e=Event('普通文字');await self.p.prepare(e)
        self.assertFalse(e.extra);self.assertFalse(self.p.path.exists())
    async def test_owner_observation_uses_only_self_authored_text(self):
        store=MemoryStore(Path(self.tmp.name)/'memory');self.addCleanup(store.close)
        p=CompanionMemory.__new__(CompanionMemory);p.store=store;p.owner=A;p.allowed=lambda _:True
        e=Event('帮我看看 我生病了');e.extra['companion_material_context']={'self_text':'帮我看看','materials':[{'text':'我生病了'}]}
        await p.observe_user(e)
        rows=store.latest_messages(A)
        self.assertEqual(rows[0]['text'],'帮我看看');self.assertNotIn('生病',rows[0]['text'])


class FollowupTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.inbox=SleepInbox(Path(self.tmp.name));self.sleep=False
        self.f=SimpleNamespace(sleep_inbox=self.inbox,sleeping=lambda:self.sleep)
        self.p=Followups(self.f)
    async def start_wait(self,event=None):
        e=event or Event();finished=asyncio.Event();result=asyncio.Future()
        async def pipeline():
            e.set_extra('companion_pipeline_task',asyncio.current_task())
            try:result.set_result(await self.p.wait(e,1))
            except Exception as exc:result.set_exception(type(exc)(str(exc)))
            await finished.wait()
        task=asyncio.create_task(pipeline());await asyncio.sleep(.01)
        return e,task,finished,result
    async def end(self,task,finished):
        finished.set();await task;await asyncio.sleep(0)
    async def test_new_text_enters_same_wait_once_and_completes_after_receipt(self):
        e,task,finished,result=await self.start_wait()
        second=Event('更正一下，是明天',mid=2)
        self.assertTrue(self.p.offer(second));self.assertTrue(self.p.offer(second))
        data=await result;self.assertEqual(len(data['messages']),1)
        self.assertFalse(self.inbox.pending())
        e.result=SimpleNamespace(is_model_result=lambda:True,get_plain_text=lambda:'明天，记住了')
        self.p.sent(e);await self.end(task,finished)
        self.assertFalse(self.inbox.pending())
        with self.inbox.connect() as db:self.assertEqual(db.execute('select state from unread').fetchone()[0],'completed')
    async def test_other_contact_and_command_and_media_are_not_absorbed(self):
        e,task,finished,result=await self.start_wait()
        self.assertFalse(self.p.offer(Event('另一个人',sender=B,mid=3)))
        self.assertFalse(self.p.offer(Event('/状态',mid=4)))
        self.assertFalse(self.p.offer(Event('引用',mid=5,parts=[Reply(id='7')])))
        self.p.offer(Event('真正补充',mid=6));await result
        await self.end(task,finished)
        self.assertEqual(len(self.inbox.pending()[A]),1)
    async def test_pipeline_failure_requeues_without_resetting_existing_unread(self):
        self.inbox.add(Event('之前的未读',sender=B,mid=4).message_obj.raw_message)
        e,task,finished,result=await self.start_wait();self.p.offer(Event('补充',mid=2));await result
        await self.end(task,finished)
        self.assertEqual(set(self.inbox.pending()),{A,B})
    async def test_voice_without_delivery_does_not_complete(self):
        e,task,finished,result=await self.start_wait();self.p.offer(Event('补充',mid=2));await result
        e.extra['companion_voice_prepared']=True;e.result=SimpleNamespace(is_model_result=lambda:True,get_plain_text=lambda:'')
        self.p.sent(e);await self.end(task,finished)
        self.assertIn(A,self.inbox.pending())
    async def test_sleep_during_wait_leaves_new_text_unread(self):
        e,task,finished,result=await self.start_wait();self.p.offer(Event('补充',mid=2));self.sleep=True
        with self.assertRaises(ValueError):await result
        await self.end(task,finished);self.assertIn(A,self.inbox.pending())
    async def test_second_wait_and_out_of_range_duration_refused(self):
        e=Event();e.set_extra('companion_pipeline_task',asyncio.current_task())
        for d in (0,9,float('nan'),True):
            with self.assertRaises(ValueError):await self.p.wait(e,d)
        e.set_extra('companion_followup_wait_used',True)
        with self.assertRaises(ValueError):await self.p.wait(e,1)
    async def test_recovery_requeues_only_our_dispatched_claims(self):
        a=self.inbox.reserve_followup(Event('未完成',mid=7).message_obj.raw_message)
        self.inbox.add(Event('普通未读',sender=B,mid=8).message_obj.raw_message)
        self.inbox.recover_followups()
        self.assertEqual(set(self.inbox.pending()),{A,B})
        self.inbox.finish_followups([a],'completed')
        self.assertEqual(self.inbox.pending()[A][0][0],a)
    async def test_actual_batch_suppresses_only_consumed_followup_pipeline(self):
        e,task,finished,result=await self.start_wait()
        batch=CompanionBatch(None,{'owner_qq':A,'quiet_seconds':.01,'natural_closure':False})
        batch.followups=self.p
        second=Event('还有一句',mid=2)
        await batch.merge(second)
        self.assertTrue(second.stopped and second.suppressed)
        self.assertFalse(batch.pending)
        self.assertEqual((await result)['messages'][0]['text'],'还有一句')
        await self.end(task,finished)
    async def test_cancelling_pipeline_releases_durable_claim(self):
        e,task,finished,result=await self.start_wait();self.p.offer(Event('补充',mid=2))
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):await task
        await asyncio.sleep(0)
        self.assertIn(A,self.inbox.pending())


class RecallTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.store=MemoryStore(self.tmp.name);self.addCleanup(self.store.close);self.c=Cognition(self.store)
    def fact(self,text,topic,qq=A):
        source=self.store.observe(qq,'user',text,at=time.time()-100)
        return self.store.put_entry(qq,'fact',topic,text,[source])
    def test_different_wording_retrieves_related_fact_and_injects_it(self):
        ref=self.fact('上次进山采样时仪器坏了','外业设备')
        self.assertEqual(self.store.retrieve(A,'野外设备')[0]['id'],ref)
        self.assertIn(ref,self.c.render(A,'野外设备',include_recent=False))
    def test_concept_retrieval_keeps_person_and_forget_filters(self):
        other=self.fact('上次进山采样时仪器坏了','外业设备',B)
        self.assertFalse(self.c.search(A,'野外设备',category='facts')['matches'])
        own=self.fact('上次进山采样时仪器坏了','外业设备')
        self.store.forget(A,own)
        self.assertFalse(self.c.search(A,'野外设备',category='facts')['matches'])
    def test_explicit_recall_is_not_blocked_by_cross_turn_cooldown(self):
        self.fact('外业仪器坏了','外业设备')
        self.assertTrue(self.c.search(A,'野外设备',category='facts')['matches'])
        self.assertTrue(self.c.search(A,'野外设备',category='facts')['matches'])
    def test_generic_only_hit_can_trigger_existing_rewrite_fallback(self):
        self.assertTrue(weak_recall('野外回来论文设备检查安排',[{'text':'论文'}]))
        self.assertFalse(weak_recall('香菜',[{'text':'我不吃香菜'}]))
    def test_no_repeated_reminder_requires_no_reply_deletion(self):
        text=recent_reply_hint([{'role':'assistant','content':'早点休息，鞋子晾干'},{'role':'user','content':'再说一次'}])
        self.assertIn('早点休息',text);self.assertIn('仍正常回应',text)
        self.assertEqual(recent_reply_hint([{'role':'tool','content':'secret'}]),'')
    def test_wait_tool_schema_is_registered_with_only_wait_argument(self):
        from astrbot_plugin_companion_chat.main import CompanionChat
        from astrbot.core.provider.register import llm_tools
        tool=next(t for t in llm_tools.func_list if t.name=='companion_wait_for_followup')
        self.assertEqual(set(tool.parameters['properties']),{'seconds'})
        self.assertIn(tool.parameters['properties']['seconds']['type'],('number','integer'))


if __name__=='__main__':unittest.main()
