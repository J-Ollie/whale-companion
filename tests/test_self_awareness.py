"""The brain recalls scoped actions, choices and uncertainty, without exporting secrets."""
import asyncio
from datetime import datetime
import json
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock,patch
from zoneinfo import ZoneInfo

import test_brain as fixtures
from astrbot_plugin_companion_chat.brain import Brain
from astrbot_plugin_companion_chat.main import CompanionChat
from astrbot_plugin_companion_chat.self_activity import SelfActivity,result_stage,window,CURRENT_TOOL
from astrbot_plugin_companion_chat.activity_hooks import ActivityHooks,observed_agent
from astrbot_plugin_companion_chat.lifecycle import ConsumerContext
from astrbot_plugin_companion_chat import grounding
from astrbot.core.agent.tool import FunctionTool


class AwarenessTests(unittest.IsolatedAsyncioTestCase):
    setUp=fixtures.BrainTests.setUp
    event=fixtures.BrainTests.event
    request=fixtures.BrainTests.request

    def set_group(self,event,group='999'):
        event.get_group_id=lambda:group
        self.plugins['astrbot_plugin_companion_memory']=NS(activated=True,star_cls=NS(config={},
            shared=NS(enabled=lambda:True,group=lambda e:'group:999' if e.get_group_id()=='999' else None)))
        return event

    def result(self,**values):
        return NS(content=[NS(text=json.dumps(values))],isError=False)

    async def test_actual_choice_is_remembered_as_choice_not_task_completion(self):
        e=self.event();self.model.return_value=NS(completion_text='{"choice":"decline","note":"这次不接"}')
        await self.brain.decide(e,self.request())
        rows=self.brain.activities.recall(e)['events']
        self.assertEqual(rows[0]['stage'],'decline');self.assertNotIn('完成',rows[0]['meaning'])

    async def test_all_generic_tools_get_attempt_and_return_with_correlated_turn(self):
        e=self.event();tool=NS(name='unknown_external_tool')
        self.brain.activities.tool_start(e,tool,{'secret':'never_persist'})
        self.brain.activities.tool_end(e,tool,{'secret':'never_persist'},self.result(status='ok',password='never_persist'))
        rows=self.brain.activities.recall(e)['events']
        self.assertEqual(len(rows),1);self.assertEqual(rows[0]['stage'],'reported_success')
        self.assertIn('attempted',rows[0]['observed_stages'])
        with self.brain.activities.connect() as db:
            self.assertNotIn('never_persist',json.dumps(db.execute('SELECT * FROM entries').fetchall()))

    async def test_missing_callback_is_uncertain_not_failed_or_success(self):
        e=self.event();tool=NS(name='external')
        self.brain.activities.tool_start(e,tool,{})
        self.brain.activities.finish_tools(e)
        row=self.brain.activities.recall(e)['events'][0]
        self.assertEqual(row['stage'],'uncertain');self.assertIn('未确认',row['meaning'])

    async def test_explicit_error_receipt_is_failure(self):
        e=self.event();tool=NS(name='external')
        self.brain.activities.tool_start(e,tool,{})
        self.brain.activities.tool_end(e,tool,{},NS(isError=True,content=[NS(text='private error body')]))
        self.assertEqual(self.brain.activities.recall(e)['events'][0]['stage'],'failed')

    async def test_private_tool_records_do_not_cross_contacts(self):
        a=self.event('123');b=self.event('456');tool=NS(name='phone_tap')
        self.brain.activities.tool_start(a,tool,{})
        self.brain.activities.tool_end(a,tool,{},self.result(status='ok'))
        self.assertEqual(self.brain.activities.recall(b)['events'],[])
        self.assertEqual(len(self.brain.activities.recall(a)['events']),1)

    async def test_group_and_private_scopes_do_not_merge_for_same_person(self):
        p=self.event('123');g=self.set_group(self.event('123'))
        self.brain.remember('p','private_action','thought',scope='contact:'+self.brain.scope('123'),note='私人具体内容')
        self.brain.remember('g','group_action','thought',scope='group:'+self.brain.scope('999'),note='本群内容')
        self.assertNotIn('私人具体内容',json.dumps(self.brain.activities.recall(g),ensure_ascii=False))
        self.assertNotIn('本群内容',json.dumps(self.brain.activities.recall(p),ensure_ascii=False))

    async def test_self_private_thought_is_available_internally_and_not_to_other_people(self):
        self.brain.remember('plan','planning_thought','thought',note='只给自己看的完整方向')
        self.assertIn('完整方向',json.dumps(self.brain.activities.recall(internal=True),ensure_ascii=False))
        self.assertNotIn('完整方向',json.dumps(self.brain.activities.recall(self.event()),ensure_ascii=False))

    async def test_public_receipt_can_be_recalled_in_another_allowed_place(self):
        self.brain.remember('post','qzone_publish_post','api_acknowledged',scope='public',source='social_receipt')
        row=self.brain.activities.recall(self.event('456'))['events'][0]
        self.assertEqual(row['stage'],'api_acknowledged');self.assertIn('不证明对外可见',row['meaning'])

    async def test_generic_return_never_overwrites_stronger_platform_receipt(self):
        e=self.event();tool=NS(name='qzone_publish_post')
        self.brain.activities.tool_start(e,tool,{})
        episode,name,_=CURRENT_TOOL.get()
        self.brain.remember(episode,name,'api_acknowledged',scope='public',source='social_receipt')
        self.brain.activities.tool_end(e,tool,{},self.result(status='ok'))
        rows=self.brain.activities.recall(e)['events']
        self.assertEqual(len(rows),1);self.assertEqual(rows[0]['stage'],'api_acknowledged')
        self.assertIn('reported_success',rows[0]['observed_stages'])

    async def test_native_loop_uses_hooks_and_preserves_original_callbacks(self):
        event=self.event();original=NS(on_tool_start=AsyncMock(),on_tool_end=AsyncMock(),on_agent_begin=AsyncMock(),on_agent_done=AsyncMock())
        async def execute(**kwargs):
            hooks=kwargs['agent_hooks'];tool=NS(name='native_external')
            await hooks.on_agent_begin(None)
            await hooks.on_tool_start(None,tool,{})
            await hooks.on_tool_end(None,tool,{},self.result(status='success'))
            await hooks.on_agent_done(None,NS())
            return 'exact result'
        result=await observed_agent(self.brain,execute,{'event':event,'agent_hooks':original,'max_steps':6})
        self.assertEqual(result,'exact result');original.on_tool_start.assert_awaited_once()
        self.assertEqual(self.brain.activities.recall(event)['events'][0]['kind'],'native_external')

    async def test_native_exception_does_not_retry_and_closes_unknown_attempt(self):
        event=self.event();calls=0
        async def execute(**kw):
            nonlocal calls
            calls+=1;await kw['agent_hooks'].on_tool_start(None,NS(name='native_write'),{})
            raise RuntimeError('operation outcome unknown')
        with self.assertRaises(RuntimeError):await observed_agent(self.brain,execute,{'event':event})
        self.assertEqual(calls,1);self.assertEqual(self.brain.activities.recall(event)['events'][0]['stage'],'uncertain')

    async def test_proxy_native_runner_does_not_mutate_arguments_or_double_wrap(self):
        event=self.event();seen=[]
        async def call(**kw):seen.append(kw);return 'ok'
        base=NS(get_registered_star=lambda n:NS(activated=True,star_cls=self.chat),tool_loop_agent=call)
        proxy=ConsumerContext(ConsumerContext(base),'phone')
        self.assertEqual(await proxy.tool_loop_agent(event=event,max_steps=6,contexts=[]),'ok')
        self.assertIsInstance(seen[0]['agent_hooks'],ActivityHooks);self.assertEqual(seen[0]['max_steps'],6)

    async def test_platform_hooks_record_unknown_tool_even_without_owner_grounding(self):
        event=self.event('456');tool=NS(name='friend_read_tool')
        await CompanionChat.remember_tool_start(self.chat,event,tool,{})
        await CompanionChat.collect_grounding_tools(self.chat,event,tool,{},self.result(status='ok'))
        self.assertEqual(self.brain.activities.recall(event)['events'][0]['kind'],'friend_read_tool')

    async def test_existing_agent_hooks_are_not_counted_twice(self):
        event=self.event();tool=NS(name='native')
        original=NS(on_tool_start=lambda *a:CompanionChat.remember_tool_start(self.chat,event,tool,{}),
                    on_tool_end=lambda *a:CompanionChat.collect_grounding_tools(self.chat,event,tool,{},self.result(status='ok')))
        hooks=ActivityHooks(self.brain,event,original)
        await hooks.on_tool_start(None,tool,{})
        await hooks.on_tool_end(None,tool,{},self.result(status='ok'))
        with self.brain.activities.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM entries WHERE stage='attempted'").fetchone()[0],1)

    async def test_scoped_read_tool_is_available_after_reply_choice(self):
        self.model.return_value=NS(completion_text='{"choice":"reply","note":""}')
        event=self.event();req=self.request()
        with patch.object(grounding,'inject',new=AsyncMock()):await CompanionChat.brain_decision(self.chat,event,req)
        self.assertIn('companion_self_recall',req.func_tool.names());self.assertNotIn('phone_tap',req.func_tool.names())

    async def test_recall_tool_respects_sleep_and_per_turn_limits(self):
        event=self.event()
        for _ in range(3):self.assertFalse((await CompanionChat.self_recall(self.chat,event)).isError)
        self.assertTrue((await CompanionChat.self_recall(self.chat,event)).isError)
        self.chat.friends.sleeping=lambda:True
        self.assertTrue((await CompanionChat.self_recall(self.chat,self.event())).isError)

    async def test_public_practice_readthrough_verifies_program_but_not_image_review(self):
        now=__import__('time').time()
        source=NS(thoughts=lambda *a:[],private_runs=lambda *a:[],practices=lambda *a:[
            {'id':'practice:1','timestamp':now,'program_executed':True,'status':'succeeded','declared_title':'练习画图'}])
        self.plugins['astrbot_plugin_companion_phone']=NS(activated=True,star_cls=NS(umo=self.chat.active.umo,self_experience=source))
        row=self.brain.activities.recall(self.event('456'))['events'][0]
        self.assertEqual(row['stage'],'confirmed_execution');self.assertIn('不证明自己看过图',row['limit'])

    async def test_private_diary_body_and_reason_are_never_read_into_recall(self):
        now=__import__('time').time()
        books=NS(daily={'decisions':{'private:2026-11-07':{'at':now,'status':'written','reason':'private secret'}}},read=AsyncMock())
        self.plugins['astrbot_plugin_companion_phone']=NS(activated=True,star_cls=NS(umo=self.chat.active.umo,books=books))
        own=self.brain.activities.recall(self.event());other=self.brain.activities.recall(self.event('456'))
        self.assertEqual(own['events'][0]['kind'],'private_diary');self.assertEqual(other['events'],[])
        self.assertNotIn('secret',json.dumps(own));books.read.assert_not_called()

    async def test_restart_preserves_choices_and_marks_incomplete_attempt_as_unknown(self):
        event=self.event();self.brain.activities.tool_start(event,NS(name='write'),{})
        other=Brain(self.chat)
        row=other.activities.recall(event)['events'][0]
        self.assertEqual(row['stage'],'interrupted_unknown')

    async def test_secrets_are_redacted_and_plain_tool_output_never_saved(self):
        self.brain.remember('x','thought','thought',note='password=supersecret https://private.example',scope='self')
        self.assertNotIn('supersecret',json.dumps(self.brain.activities.recall(internal=True)))

    async def test_paging_is_explicit_and_records_keep_original_dates(self):
        for n in range(15):self.brain.remember(str(n),'public_event','thought',scope='public',note=str(n))
        a=self.brain.activities.recall(self.event(),limit=12)
        b=self.brain.activities.recall(self.event(),offset=a['next_offset'],limit=12)
        self.assertTrue(a['more']);self.assertEqual(len(b['events']),3)
        self.assertEqual(set(r['id'] for r in a['events'])&set(r['id'] for r in b['events']),set())

    async def test_message_receipt_records_platform_acceptance_but_not_empty_submission(self):
        event=self.event();event.set_extra('companion_reply_state_id','r1')
        row={'id':'r1','state':'submitted','receipts':'[]'}
        self.chat.reply_state=NS(latest=lambda *a:[row])
        self.chat.outcomes_sent=lambda e:None
        self.chat.friends.unread_answered=AsyncMock();self.chat.active.replied=AsyncMock();self.chat.batch.replied=AsyncMock()
        self.chat.followups=NS(sent=lambda e:None)
        await CompanionChat.message_sent(self.chat,event)
        self.assertEqual(self.brain.activities.recall(event)['events'],[])
        row['receipts']='["platform-message-id"]'
        await CompanionChat.message_sent(self.chat,event)
        record=self.brain.activities.recall(event)['events'][0]
        self.assertEqual(record['stage'],'submitted');self.assertIn('不证明已展示或已读',record['meaning'])

    async def test_forgotten_source_cannot_return_via_old_decision_reason(self):
        event=self.event();event.set_extra('companion_observation_id',42)
        self.plugins['astrbot_plugin_companion_memory']=NS(activated=True,star_cls=NS(
            affect=lambda:NS(social=NS(valid_source=lambda qq,identifier:False))))
        self.brain.remember('choice','dialogue_choice','decline',scope='contact:'+self.brain.scope('123'),
            note='应随原来源忘记的私人细节',source='own_decision',ref=self.brain.activities.turn(event))
        rows=self.brain.activities.recall(event)['events']
        self.assertEqual(rows[0]['stage'],'decline');self.assertEqual(rows[0]['note'],'')

    def test_relative_day_resolution_uses_beijing_original_time(self):
        now=datetime(2026,11,7,12,tzinfo=ZoneInfo('Asia/Shanghai')).timestamp()
        start,end=window(query='昨天做了什么',now=now)
        self.assertEqual(datetime.fromtimestamp(start,ZoneInfo('Asia/Shanghai')).day,6)
        self.assertEqual(end-start,86400)

    def test_result_status_does_not_turn_arbitrary_return_into_verified_completion(self):
        self.assertEqual(result_stage(NS(isError=False,content=[NS(text='done')])),'tool_returned')
        self.assertEqual(result_stage(self.result(ok=True)),'reported_success')
        self.assertEqual(result_stage(None),'uncertain')


if __name__=='__main__':unittest.main()
