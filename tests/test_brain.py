"""Cognitive choice precedes commitments/tools; shared admission preserves scopes."""
import asyncio
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, patch

from astrbot.core.agent.tool import ToolSet, FunctionTool
from astrbot_plugin_companion_chat.brain import Brain, KEY, parse
from astrbot_plugin_companion_chat.main import CompanionChat
from astrbot_plugin_companion_chat import grounding
from astrbot_plugin_companion_phone.rhythm import Rhythm
from astrbot_plugin_companion_phone.social_autonomy import SocialAutonomy


class BrainTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.plugins={}
        self.model=AsyncMock(return_value=NS(completion_text='{"choice":"accept","note":"愿意试试"}'))
        self.chat=NS(config={},ready=True,gate=NS(holding=False),
            context=NS(llm_generate=self.model,get_registered_star=lambda n:self.plugins.get(n)),
            friends=NS(policy=NS(root=self.root/'companion_friends'),platform_id='test',owner='123',sleeping=lambda:False),
            active=NS(config={},umo='test:FriendMessage:123'),batch=NS(allowed=lambda e:True))
        self.brain=self.chat.brain=Brain(self.chat)
        self.patch=patch('astrbot_plugin_companion_chat.lifecycle.pipeline_tasks',return_value=set())
        self.patch.start();self.addCleanup(self.patch.stop)

    def event(self,sender='123',text='请帮我查看公开资料'):
        extra={}
        return NS(message_str=text,unified_msg_origin='test:FriendMessage:'+sender,get_sender_id=lambda:sender,
                  get_group_id=lambda:'',get_extra=lambda k,d=None:extra.get(k,d),set_extra=lambda k,v:extra.__setitem__(k,v))

    def request(self):
        return NS(contexts=[],system_prompt='同一个小鲸鱼。',extra_user_content_parts=[],
                  func_tool=ToolSet([FunctionTool(name='phone_tap',description='',parameters={}),
                                     FunctionTool(name='companion_memory_recall',description='',parameters={})]))

    async def test_choice_cached_per_real_event_not_per_contact(self):
        event=self.event();req=self.request()
        a=await self.brain.decide(event,req);b=await self.brain.decide(event,req)
        self.assertEqual(a,b);self.model.assert_awaited_once()
        await self.brain.decide(self.event(),self.request())
        self.assertEqual(self.model.await_count,2)

    async def test_decline_blocks_operation_before_commitment(self):
        self.model.return_value=NS(completion_text='{"choice":"decline","note":"现在不想做这个"}')
        event=self.event();req=self.request()
        with patch.object(grounding,'inject',new=AsyncMock()) as inject:
            await CompanionChat.brain_decision(self.chat,event,req)
            self.assertEqual(set(req.func_tool.names()),{'companion_memory_recall','companion_self_recall','companion_work_read','companion_habits'})
            self.assertFalse(inject.await_args.kwargs['register'])
        self.assertEqual(event.get_extra(KEY)['source'],'self_choice')

    async def test_accept_decision_precedes_registration_and_keeps_tools(self):
        event=self.event();req=self.request()
        async def inject(*a,**kw):
            self.assertEqual(event.get_extra(KEY)['choice'],'accept')
            self.assertTrue(kw['register'])
        with patch.object(grounding,'inject',new=AsyncMock(side_effect=inject)):
            await CompanionChat.brain_decision(self.chat,event,req)
        self.assertIn('phone_tap',req.func_tool.names())

    async def test_provider_error_is_not_voluntary_refusal(self):
        self.model.side_effect=OSError('offline')
        event=self.event();req=self.request()
        with patch.object(grounding,'inject',new=AsyncMock()) as inject:
            await CompanionChat.brain_decision(self.chat,event,req)
        self.assertEqual(event.get_extra(KEY)['choice'],'decision_unavailable')
        self.assertFalse(inject.await_args.kwargs['register'])
        self.assertNotIn('phone_tap',req.func_tool.names())

    async def test_pause_and_privacy_controls_bypass_voluntary_veto(self):
        value=await self.brain.decide(self.event(text='停止操作，撤销授权'),self.request())
        self.assertEqual(value['source'],'control_passthrough');self.model.assert_not_awaited()

    async def test_budget_exhaustion_never_becomes_personality(self):
        self.chat.config['brain_dialogue_daily_calls']=0
        value=await self.brain.decide(self.event(),self.request())
        self.assertEqual(value['source'],'budget_exhausted');self.model.assert_not_awaited()

    async def test_optional_recall_uses_current_real_sender_before_final_choice(self):
        self.model.side_effect=[NS(completion_text='{"choice":"recall","query":"上次的约定"}'),
                               NS(completion_text='{"choice":"clarify","note":"想确认具体是哪一项"}')]
        recall=AsyncMock(return_value=NS(content=[NS(text='当前联系人范围回忆')],isError=False))
        self.plugins['astrbot_plugin_companion_memory']=NS(activated=True,star_cls=NS(tool_recall=recall))
        event=self.event('456');value=await self.brain.decide(event,self.request())
        self.assertEqual(value['choice'],'clarify');self.assertIs(recall.await_args.args[0],event)
        self.assertEqual(recall.await_args.kwargs['query'],'上次的约定')
        self.assertEqual(self.model.await_count,2)
        other=self.event('789');self.assertIsNone(other.get_extra('companion_brain_recall'))

    async def test_private_text_and_reasons_never_enter_shared_ledger(self):
        self.model.return_value=NS(completion_text='{"choice":"decline","note":"秘密原因甲乙丙"}')
        await self.brain.decide(self.event(text='秘密聊天甲乙丙'),self.request())
        with self.brain.connect() as db:rows=db.execute('SELECT * FROM events').fetchall()
        rendered=json.dumps(rows,ensure_ascii=False)
        self.assertNotIn('秘密',rendered);self.assertNotIn('123',rendered)
        self.assertEqual(self.brain.scope('123'),self.brain.scope('123'))
        self.assertNotEqual(self.brain.scope('123'),self.brain.scope('456'))

    async def test_foreground_and_other_activity_defer_without_starting_worker(self):
        work=AsyncMock();lease=self.brain.begin('autonomy')
        self.assertIsNotNone(lease)
        self.assertFalse(await self.brain.run('outreach',work));work.assert_not_awaited()
        self.brain.finish(lease,'chose_not_to_play')
        self.brain.foreground=1
        self.assertFalse(await self.brain.run('growth',work));work.assert_not_awaited()

    async def test_cancelled_worker_releases_admission_and_records_interruption(self):
        async def cancel():raise asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):await self.brain.run('diary',cancel)
        self.assertIsNone(self.brain.running)
        with self.brain.connect() as db:self.assertEqual(db.execute('SELECT status FROM events').fetchone()[0],'interrupted')
        await self.brain.run('growth',AsyncMock())

    async def test_closed_service_never_falls_through_to_independent_scheduler(self):
        self.brain.closed=True;work=AsyncMock()
        self.assertFalse(await self.brain.run('autonomy',work));work.assert_not_awaited()

    async def test_social_deferral_keeps_due_categories_and_quotas(self):
        phone=NS(config={'social_full_autonomy_enabled':True},root=self.root,
                 context=self.chat.context,social_compat=NS(check=lambda:None))
        worker=SocialAutonomy(phone);worker.state['next_by_category']={k:0 for k in worker.DEFAULT_INTERVALS}
        worker.tools_for=lambda due:ToolSet([FunctionTool(name='qqprofile_set_status',description='',parameters={})])
        self.plugins['astrbot_plugin_companion_chat']=NS(activated=True,star_cls=self.chat)
        self.brain.foreground=1;before=dict(worker.state['next_by_category'])
        worker.tick();self.assertEqual(before,worker.state['next_by_category']);self.assertIsNone(worker.task)

    def test_model_sleep_choice_is_not_rewritten_as_daytime_nap(self):
        rhythm=Rhythm(self.root);rhythm.brain_managed=True
        now=rhythm.state['updated_at'];rhythm.state.update(mode='awake',energy=.9,awake_since=now)
        rhythm.choose_rest('sleep',180,now)
        self.assertEqual(rhythm.state['mode'],'asleep')
        self.assertEqual(rhythm.state['until'],now+180*60)
        self.assertEqual(rhythm.state['sleep_origin'],'self_choice')
        self.assertEqual(rhythm.summary()['last_recorded_rest_origin'],'self_choice')

    def test_clock_suggests_sleep_when_thought_capacity_exists(self):
        rhythm=Rhythm(self.root,initiative=True);rhythm.brain_managed=True
        now=rhythm.state['updated_at'];rhythm.state.update(mode='awake',energy=.1,next_decision=now+10000,decisions=0)
        rhythm.advance(now+1)
        self.assertEqual(rhythm.state['mode'],'awake');self.assertTrue(rhythm.state['sleep_advisory'])
        self.assertLess(rhythm.state['next_decision'],now+10000)

    def test_invalid_choice_cannot_smuggle_operation(self):
        for raw in ('{"choice":"publish"}','{"choice":"recall","query":""}',
                    '{"choice":"accept","note":"[[silent:pause]]"}'):
            with self.assertRaises(ValueError):parse(raw)


if __name__=='__main__':unittest.main()
