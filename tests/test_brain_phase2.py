"""Shared situation, selected-only execution, durable intent and honest usage."""
import asyncio
import json
import time
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, Mock, patch

import test_brain as fixtures
from astrbot_plugin_companion_chat.brain import Brain
from astrbot_plugin_companion_chat.planning import Planner
from astrbot_plugin_companion_chat import cognitive_context as context
from astrbot_plugin_companion_chat.lifecycle import ConsumerContext, ComponentContext
from astrbot_plugin_companion_chat.main import CompanionChat
from astrbot_plugin_companion_chat import grounding
from astrbot_plugin_companion_phone.rhythm import Rhythm
from astrbot_plugin_companion_phone.autonomy import AutonomousBrowser
from astrbot_plugin_companion_phone.social_autonomy import SocialAutonomy
from astrbot_plugin_companion_memory import growth
from astrbot.core.agent.tool import ToolSet,FunctionTool


class PhaseTwoTests(unittest.IsolatedAsyncioTestCase):
    setUp=fixtures.BrainTests.setUp
    event=fixtures.BrainTests.event
    request=fixtures.BrainTests.request

    def phone(self):
        r=Rhythm(self.root,initiative=True)
        r.state.update(mode='awake',energy=.8,updated_at=time.time(),until=0)
        resources=NS(reserve=Mock(),status=lambda:{'remaining':40,'used':32,'limit':72})
        store=NS(meta=lambda *a:False,goal_window=lambda **kw:[])
        p=NS(config={'enabled':True,'self_directed_enabled':True},umo='test:FriendMessage:123',
            context=self.chat.context,phone=NS(state={'paused':False}),
            autonomy=NS(state={'last_user_at':time.time()-1000,'paused':False},rhythm=r),
            thinking=NS(enabled=lambda:True,store=store,resources=resources,common_context=lambda:''))
        self.chat.context.get_config=lambda **kw:{'provider_settings':{'enable':True}}
        self.plugins['astrbot_plugin_companion_phone']=NS(activated=True,star_cls=p)
        return p

    def memory(self):
        state={'revision':7,'mood':1,'emotions':[]}
        self.affect=NS(snapshot=lambda qq,now:{'心情':'高兴'},state=lambda now:dict(state),
            social=NS(summary=lambda qq:{'态度':'熟悉' if qq=='123' else '未知'},
                      relationships=lambda qq,public:[{'事实':'公开关系' if public else '私有关系'}]))
        m=NS(config={},affect=lambda:self.affect,profile=NS(data={'traits':'共同表达'}),
            shared=NS(enabled=lambda:True,group=lambda e:'group:999' if e.get_group_id()=='999' else None))
        self.plugins['astrbot_plugin_companion_memory']=NS(activated=True,star_cls=m)
        return m,state

    def plan_output(self,select='autonomy',**values):
        return NS(completion_text=json.dumps({'select':select,'next_minutes':30,**values},ensure_ascii=False))

    async def test_offer_is_not_execution_and_does_not_reserve_resources(self):
        p=self.phone();a=AsyncMock();b=AsyncMock()
        self.brain.offer('autonomy',a,check=lambda:True)
        self.brain.offer('social',b,check=lambda:True)
        a.assert_not_awaited();b.assert_not_awaited();self.model.assert_not_awaited()
        p.thinking.resources.reserve.assert_not_called()
        self.assertEqual(set(self.brain.planner.pending),{'autonomy','social'})

    async def test_one_shared_choice_executes_only_selected_worker(self):
        p=self.phone();a=AsyncMock();s=AsyncMock()
        self.brain.offer('autonomy',a,check=lambda:True)
        self.brain.offer('social',s,check=lambda:True)
        self.model.return_value=self.plan_output('social')
        await self.brain.planner.select()
        s.assert_awaited_once();a.assert_not_awaited();p.thinking.resources.reserve.assert_called_once_with()
        self.assertIn('autonomy',self.brain.planner.pending)
        self.assertNotIn('social',self.brain.planner.pending)
        self.assertIsNone(self.brain.running)

    async def test_idle_keeps_opportunities_without_advancing_worker_schedule(self):
        self.phone();work=AsyncMock();self.brain.offer('diary',work,check=lambda:True)
        self.model.return_value=self.plan_output('idle')
        await self.brain.planner.select()
        work.assert_not_awaited();self.assertIn('diary',self.brain.planner.pending)
        self.assertEqual(self.brain.planner.last_result,'self_chose_idle')
        self.assertGreater(self.brain.planner.next_at,time.time()+1000)

    async def test_closed_or_foreground_never_runs_a_candidate(self):
        self.phone();work=AsyncMock();self.brain.offer('diary',work,check=lambda:True)
        self.brain.foreground=1
        await self.brain.planner.select();self.model.assert_not_awaited();work.assert_not_awaited()
        self.brain.foreground=0;self.brain.closed=True
        self.assertFalse(self.brain.offer('growth',work,check=lambda:True))

    async def test_guard_is_rechecked_after_model_before_execution(self):
        self.phone();valid=True;work=AsyncMock()
        self.brain.offer('autonomy',work,check=lambda:valid)
        async def model(**kw):
            nonlocal valid
            valid=False;return self.plan_output()
        self.model.side_effect=model
        await self.brain.planner.select();work.assert_not_awaited()
        self.assertEqual(self.brain.planner.last_result,'opportunity_changed')

    async def test_new_message_interrupts_a_pending_plan(self):
        p=self.phone();work=AsyncMock();self.brain.offer('autonomy',work,check=lambda:True)
        async def model(**kw):
            p.autonomy.state['last_user_at']=time.time();return self.plan_output()
        self.model.side_effect=model
        await self.brain.planner.select();work.assert_not_awaited()
        self.assertEqual(self.brain.planner.last_result,'interrupted_by_state_change')

    async def test_existing_responsibility_keeps_original_execution_path(self):
        self.phone();work=AsyncMock()
        self.brain.offer('outreach',work,check=lambda:True,priority='responsibility',fingerprint='task-1')
        await self.brain.planner.select();work.assert_awaited_once();self.model.assert_not_awaited()

    async def test_budget_failure_does_not_fall_back_to_unmetered_execution(self):
        p=self.phone();p.thinking.resources.reserve.side_effect=ValueError('budget')
        work=AsyncMock();self.brain.offer('autonomy',work,check=lambda:True)
        await self.brain.planner.select();work.assert_not_awaited();self.model.assert_not_awaited()
        self.assertIn('autonomy',self.brain.planner.pending)

    async def test_private_plan_text_does_not_cross_to_public_worker(self):
        self.phone();work=AsyncMock();self.brain.offer('social',work,check=lambda:True)
        self.model.return_value=self.plan_output('social',reflection='仅供自己看的反思',
            intention={'action':'create','title':'想弄明白一个问题'})
        await self.brain.planner.select();work.assert_awaited_once_with()
        self.assertEqual(self.brain.intentions.rows()[0]['title'],'想弄明白一个问题')
        with self.brain.connect() as db:
            recorded=json.dumps(db.execute('SELECT * FROM plans').fetchall(),ensure_ascii=False)
        self.assertNotIn('反思',recorded);self.assertNotIn('想弄明白',recorded)

    async def test_intention_can_pause_resume_abandon_and_survives_restart(self):
        key=self.brain.intentions.apply({'action':'create','title':'研究一个有趣问题'})
        self.brain.intentions.apply({'action':'pause','id':key})
        self.assertEqual(self.brain.intentions.rows()[0]['state'],'paused')
        self.brain.intentions.apply({'action':'resume','id':key})
        other=Brain(self.chat)
        self.assertEqual(other.intentions.rows()[0]['id'],key)
        other.intentions.apply({'action':'abandon','id':key});self.assertEqual(other.intentions.rows(),[])

    async def test_restart_never_replays_callbacks_or_unknown_external_actions(self):
        self.phone();work=AsyncMock();self.brain.offer('social',work,check=lambda:True)
        with self.brain.connect() as db:db.execute("INSERT INTO plans VALUES('old',?,'social','running',NULL)",(time.time(),))
        other=Brain(self.chat)
        self.assertEqual(other.planner.pending,{})
        with other.connect() as db:self.assertEqual(db.execute("SELECT status FROM plans WHERE id='old'").fetchone()[0],'interrupted_unknown')
        work.assert_not_awaited()

    async def test_snapshot_reads_one_mood_and_different_person_attitudes_without_updates(self):
        m,state=self.memory();before=json.dumps(state,sort_keys=True)
        a=context.build(self.brain,self.event('123'));b=context.build(self.brain,self.event('456'))
        self.assertEqual(a['self']['emotion'],b['self']['emotion'])
        self.assertNotEqual(a['current_person']['attitude'],b['current_person']['attitude'])
        self.assertEqual(before,json.dumps(state,sort_keys=True));self.assertEqual(a['versions']['emotion'],7)

    async def test_group_excludes_private_material_persona_and_commitments(self):
        self.memory();event=self.event();event.get_group_id=lambda:'999'
        req=self.request();req.system_prompt='仅管理员私聊可见的资料'
        req.extra_user_content_parts=[NS(text='<companion_context>私有原话</companion_context>'),
                                      NS(text='<companion_shared>本群资料</companion_shared>')]
        data=context.build(self.brain,event,req);body=json.dumps(data,ensure_ascii=False)
        self.assertNotIn('私有原话',body);self.assertNotIn('仅管理员',body)
        self.assertIn('公开关系',body);self.assertNotIn('私有关系',body)
        self.assertEqual(data['commitments'],[])

    async def test_unverified_group_is_denied_by_shared_scope_checker(self):
        self.memory();event=self.event();event.get_group_id=lambda:'888'
        with self.assertRaises(ValueError):context.build(self.brain,event,self.request())

    async def test_group_decision_uses_group_recall_and_blocks_new_operations_on_decline(self):
        m,_=self.memory();event=self.event();event.get_group_id=lambda:'999'
        event.unified_msg_origin='test:GroupMessage:999';self.chat.batch.allowed=lambda e:False
        m.shared_recall=AsyncMock(return_value=NS(content=[NS(text='本群原话')],isError=False))
        m.tool_recall=AsyncMock()
        self.model.side_effect=[NS(completion_text='{"choice":"recall","query":"本群约定"}'),
                               NS(completion_text='{"choice":"decline","note":"这次不做"}')]
        req=self.request()
        with patch.object(grounding,'inject',new=AsyncMock()) as inject:
            await CompanionChat.brain_decision(self.chat,event,req)
            inject.assert_not_awaited()
        m.shared_recall.assert_awaited_once();m.tool_recall.assert_not_awaited()
        self.assertNotIn('phone_tap',req.func_tool.names())

    async def test_current_private_intentions_not_exposed_by_foreground_state_or_status(self):
        self.brain.intentions.apply({'action':'create','title':'我自己的私有打算'})
        data=context.build(self.brain,self.event('456'))
        self.assertNotIn('私有打算',json.dumps(data,ensure_ascii=False))
        self.assertNotIn('私有打算',json.dumps(self.brain.status(),ensure_ascii=False))

    async def test_usage_unknown_is_null_and_reported_tokens_are_not_estimated(self):
        await self.brain.observe_model('test',AsyncMock(return_value=NS(completion_text='hello')))
        await self.brain.observe_model('test',AsyncMock(return_value=NS(usage={'prompt_tokens':123,'completion_tokens':9,'total_tokens':132})))
        row=self.brain.usage()['observed_calls'][0]
        self.assertEqual(row['calls'],2);self.assertEqual(row['reported_input_tokens'],123)
        self.assertEqual(row['calls_with_reported_input'],1)
        self.assertIsNone(self.brain.usage()['currency_cost'])

    async def test_usage_adapter_forwards_exact_model_request_without_nested_wrappers(self):
        base=NS(get_registered_star=lambda n:NS(activated=True,star_cls=self.chat),llm_generate=AsyncMock(return_value=NS()))
        consumer=ConsumerContext(ConsumerContext(base),'phone')
        await consumer.llm_generate(prompt='不保存的正文',max_tokens=10)
        base.llm_generate.assert_awaited_once_with(prompt='不保存的正文',max_tokens=10)
        self.assertEqual(self.brain.usage()['observed_calls'][0]['calls'],1)
        with self.brain.connect() as db:
            self.assertNotIn('正文',json.dumps(db.execute('SELECT * FROM model_calls').fetchall(),ensure_ascii=False))

    async def test_selected_worker_cancellation_releases_global_admission(self):
        self.phone()
        async def cancelled():raise asyncio.CancelledError()
        self.brain.offer('autonomy',cancelled,check=lambda:True)
        self.model.return_value=self.plan_output()
        with self.assertRaises(asyncio.CancelledError):await self.brain.planner.select()
        self.assertIsNone(self.brain.running)

    async def test_invalid_plan_never_runs_an_unoffered_capability(self):
        self.phone();work=AsyncMock();self.brain.offer('growth',work,check=lambda:True)
        self.model.return_value=self.plan_output('shell')
        await self.brain.planner.select();work.assert_not_awaited()

    async def test_public_question_focus_reaches_only_its_selected_autonomy_worker(self):
        p=self.phone();question={'id':'question-1','status':'open','question':'一个公开问题','next_step':''}
        p.thinking.store.goal_window=lambda **kw:[question]
        p.thinking.store.goal_by_id=lambda key:question if key==question['id'] else None
        p.thinking.goal_id=''
        async def selected():self.assertEqual(p.thinking.goal_id,'question-1')
        self.brain.offer('autonomy',selected,check=lambda:True)
        self.model.return_value=self.plan_output('autonomy',question_id='question-1')
        await self.brain.planner.select()
        self.assertEqual(self.brain.planner.last_result,'worker_returned')

    async def test_real_autonomy_adapter_only_executes_after_selection_and_tracks_interrupt_task(self):
        p=self.phone();self.plugins['astrbot_plugin_companion_chat']=NS(activated=True,star_cls=self.chat)
        a=NS(thinking=NS(phone=p),session=None,state={},due=lambda now:True)
        async def execute(force,text):
            self.assertIs(a.session,asyncio.current_task());self.assertFalse(force);self.assertEqual(text,'')
        a._consider_session=AsyncMock(side_effect=execute)
        await AutonomousBrowser.consider(a)
        a._consider_session.assert_not_awaited()
        self.model.return_value=self.plan_output('autonomy')
        await self.brain.planner.select();a._consider_session.assert_awaited_once();self.assertIsNone(a.session)

    async def test_real_growth_adapter_does_not_advance_review_before_selection(self):
        self.phone();self.plugins['astrbot_plugin_companion_chat']=NS(activated=True,star_cls=self.chat)
        p=NS(context=self.chat.context,owner='123',umo='test:FriendMessage:123',config={},
             resting=lambda:False,processing=NS(locked=lambda:False),store=NS(get_meta=lambda *a:0))
        with patch.object(growth,'_review',new=AsyncMock()) as work:
            await growth.review(p);work.assert_not_awaited()
            self.model.return_value=self.plan_output('growth')
            await self.brain.planner.select();work.assert_awaited_once_with(p)

    async def test_real_social_adapter_keeps_due_dates_until_selected(self):
        p=self.phone();p.root=self.root;p.config['social_full_autonomy_enabled']=True
        p.social_compat=NS(check=lambda:None)
        self.plugins['astrbot_plugin_companion_chat']=NS(activated=True,star_cls=self.chat)
        social=SocialAutonomy(p);social.state['next_by_category']={k:0 for k in social.DEFAULT_INTERVALS}
        social.tools_for=lambda kinds:ToolSet([FunctionTool(name='qqprofile_set_status',description='',parameters={})])
        social.cycle=AsyncMock()
        social.tick();self.assertEqual(social.state['next_by_category']['status'],0);social.cycle.assert_not_awaited()
        self.model.return_value=self.plan_output('social')
        await self.brain.planner.select();social.cycle.assert_awaited_once()
        self.assertGreater(social.state['next_by_category']['status'],time.time())
        self.assertIsNone(social.task)

    async def test_repeated_hook_reuses_its_one_observed_state_without_applying_emotion_again(self):
        m,state=self.memory();event=self.event();req=self.request()
        with patch.object(grounding,'inject',new=AsyncMock()):
            await CompanionChat.brain_decision(self.chat,event,req)
            state['revision']=99
            await CompanionChat.brain_decision(self.chat,event,req)
        self.assertEqual(event.get_extra('companion_brain_state_versions')['emotion'],7)
        self.model.assert_awaited_once()

    async def test_platform_usage_callback_does_not_double_count_same_response_or_invent_latency(self):
        event=self.event('456');event.set_extra('companion_brain_choice_v1',{'choice':'reply'})
        response=NS(usage={'prompt_tokens':10,'completion_tokens':5})
        await CompanionChat.check_grounded_reply(self.chat,event,response)
        await CompanionChat.check_grounded_reply(self.chat,event,response)
        with self.brain.connect() as db:
            rows=db.execute('SELECT source,elapsed_ms,input_tokens,output_tokens FROM model_calls').fetchall()
        self.assertEqual(rows,[('platform_response',None,10,5)])

    def test_plan_parser_preserves_unoffered_wish_and_rejects_bad_format_or_time(self):
        value=Planner.parse(json.dumps({'select':'social'}),{'autonomy':{}})
        self.assertEqual(value['select'],'self_intention');self.assertEqual(value['wish'],'social')
        for v in ({'select':['idle']},{'select':{'kind':'idle'}},{'select':'idle','next_minutes':False},
                  {'select':'idle','next_minutes':float('inf')},{'select':'rest','rest_minutes':900}):
            with self.assertRaises(ValueError):Planner.parse(json.dumps(v),{'autonomy':{}})


if __name__=='__main__':unittest.main()
