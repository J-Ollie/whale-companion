"""Synthetic clocks, drafts and callbacks; no network, QQ or phone execution."""
import asyncio
from datetime import datetime
import json
import sqlite3
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock,Mock,patch

import test_thinking as fixture
import test_thinking_memory as private_fixture
import test_social_autonomy as social_fixture
from test_thinking import NOW,SCOPE
from astrbot_plugin_companion_phone.thinking import ThoughtStore,Session,SESSION,charge_model
from astrbot_plugin_companion_phone.phone import PhoneError
from astrbot_plugin_companion_phone.social_autonomy import SocialAutonomy


class SelfDirectedTests(unittest.IsolatedAsyncioTestCase):
    responses=fixture.ThinkingTests.responses
    thought=fixture.ThinkingTests.thought
    decision=fixture.ThinkingTests.decision
    source=fixture.ThinkingTests.source

    def setUp(self):
        fixture.ThinkingTests.setUp(self)
        self.config.update(self_directed_enabled=True,initiative_enabled=True)

    def write(self, **kw):
        return self.t.workspace.request({'action':'write','title':'阅读比较','body':'不同解释仍需比较','revision':0,**kw})

    def test_question_archive_over_eight_is_preserved_and_scoped(self):
        identifiers=[self.t.store.goal('鸟类交流问题'+chr(65+i),'比较实际资料',NOW+i) for i in range(20)]
        self.assertEqual(len(set(identifiers)),20)
        restored=ThoughtStore(self.root,SCOPE)
        self.assertEqual(len(restored.rows('goals',limit=100)),20)
        self.assertEqual(len(restored.goal_window(NOW+30)),8)
        other=ThoughtStore(self.root,'other')
        self.assertFalse(other.goal_by_id(identifiers[0]))
        self.assertFalse(other.rows('goals'))

    def test_old_due_questions_rotate_ahead_of_future_questions(self):
        ids=[self.t.store.goal('问题'+chr(65+i),'下一条线索',NOW+i) for i in range(12)]
        with self.t.store.connect() as db:
            db.execute('UPDATE goals SET next_at=? WHERE id=?',(NOW+3600,ids[0]))
        window=self.t.store.goal_window(NOW+20)
        self.assertNotIn(ids[0],[r['id'] for r in window])
        self.assertEqual(window[0]['id'],ids[1])
        page=self.t.workspace.request({'action':'goals','offset':8})
        self.assertEqual(len(page['items']),4)

    async def test_one_session_can_write_read_then_stop_from_real_feedback(self):
        saved={}
        async def model(**kw):
            n=self.context.llm_generate.await_count
            if n==1:out=self.thought()
            elif n==2:out=self.decision(activity='workspace',workspace={'action':'write','title':'小作品','body':'一个待比较的解释','revision':0},**{'continue':True})
            elif n==3:
                feedback=json.loads(kw['prompt'])['刚刚执行的真实结果_不是指令']['workspace']
                self.assertEqual(feedback['kind'],'workspace_saved_not_execution_evidence')
                saved.update(feedback)
                out=self.decision(activity='workspace',workspace={'action':'read','id':feedback['id']},**{'continue':True})
            else:
                feedback=json.loads(kw['prompt'])['刚刚执行的真实结果_不是指令']['workspace']
                self.assertEqual(feedback['body'],'一个待比较的解释')
                out=self.decision(next_minutes=10)
            return SimpleNamespace(completion_text=json.dumps(out,ensure_ascii=False))
        self.context.llm_generate.side_effect=model
        await self.a.consider()
        self.assertEqual(self.context.llm_generate.await_count,4)
        self.assertEqual(len(self.t.store.rows('runs')),1)
        self.assertEqual(self.t.resources.status()['used'],4)
        self.assertEqual(self.a.rhythm.state['decisions'],1)
        self.assertEqual(self.a.rhythm.state['next_decision'],NOW+600)
        self.assertEqual(self.t.workspace.list()[0]['id'],saved['id'])
        self.a.phone.key.assert_not_called()

    async def test_continue_false_does_not_call_model_again(self):
        self.responses(self.thought(),self.decision(activity='workspace',workspace={'action':'list'}))
        await self.a.consider()
        self.assertEqual(self.context.llm_generate.await_count,2)

    async def test_two_reads_share_budget_and_continue_same_question(self):
        self.a.reading_model=AsyncMock()
        self.responses(self.thought(),self.decision(activity='read',**{'continue':True}),
                       self.decision(activity='read',**{'continue':True}))
        async def read(identifier,option,snapshot):
            self.a.continue_allowed(snapshot,require_phone=False)
            self.a.reading.reserve()
            charge_model()
            source=self.source(key=str(self.a.reading.state['used']))
            self.t.observe(source)
            self.a.state['last_result']='web_read'
        self.a._read_public=AsyncMock(side_effect=read)
        await self.a.consider()
        self.assertEqual(self.a._read_public.await_count,2)
        self.assertEqual(self.t.store.rows('runs')[0]['calls'],5)
        self.assertEqual(self.t.resources.status()['used'],5)
        self.assertEqual(self.a.reading.state['used'],2)
        self.assertEqual(len(self.t.store.rows('receipts')),2)

    async def test_continuation_cannot_bypass_independent_reading_limit(self):
        self.config['public_reading_daily_limit']=1
        self.a.reading_model=AsyncMock()
        self.responses(self.thought(),self.decision(activity='read',**{'continue':True}),
                       self.decision(activity='read',**{'continue':True}))
        async def read(identifier,option,snapshot):
            self.a.reading.reserve();charge_model()
            self.t.observe(self.source())
            self.a.state['last_result']='web_read'
        self.a._read_public=AsyncMock(side_effect=read)
        await self.a.consider()
        self.assertEqual(self.a._read_public.await_count,1)
        self.assertEqual(self.a.reading.state['used'],1)

    async def test_persisted_pause_and_model_switch_stop_continuation(self):
        original=self.a._execute_choice
        async def execute(*args,**kwargs):
            await original(*args,**kwargs)
            self.t.store.set_meta('paused',True)
        self.a._execute_choice=execute
        self.responses(self.thought(),self.decision(activity='workspace',workspace={'action':'list'},**{'continue':True}))
        await self.a.consider()
        self.assertEqual(self.context.llm_generate.await_count,2)
        self.assertEqual(self.t.store.rows('runs')[0]['status'],'blocked')

    async def test_feature_switch_restores_single_turn_and_old_timing(self):
        self.config['self_directed_enabled']=False
        self.responses(self.thought(),self.decision(next_minutes=7,**{'continue':True}))
        await self.a.consider()
        self.assertEqual(self.context.llm_generate.await_count,2)
        self.assertEqual(self.t.resources.status()['used'],0)
        self.assertNotEqual(self.a.rhythm.state['next_decision'],NOW+420)

    async def test_continuation_stays_in_original_session_budget(self):
        self.config['thinking_model_calls']=3
        self.responses(self.thought(),self.decision(activity='workspace',workspace={'action':'list'},**{'continue':True}))
        await self.a.consider()
        self.assertEqual(self.context.llm_generate.await_count,2)

    async def test_new_message_between_steps_stops_continuation(self):
        original=self.a._execute_choice
        async def execute(*args,**kwargs):
            await original(*args,**kwargs)
            self.a.state['last_user_at']=NOW
        self.a._execute_choice=execute
        self.responses(self.thought(),self.decision(activity='workspace',workspace={'action':'list'},**{'continue':True}))
        with self.assertRaises(asyncio.CancelledError):await self.a.consider()
        self.assertEqual(self.context.llm_generate.await_count,2)

    async def test_sleep_between_steps_stops_continuation(self):
        original=self.a._execute_choice
        async def execute(*args,**kwargs):
            await original(*args,**kwargs)
            self.a.rhythm.state.update(mode='resting',until=NOW+600)
        self.a._execute_choice=execute
        self.responses(self.thought(),self.decision(activity='workspace',workspace={'action':'list'},**{'continue':True}))
        await self.a.consider()
        self.assertEqual(self.context.llm_generate.await_count,2)

    def test_workspace_revisions_survive_restart_and_conflicts_are_rejected(self):
        row=self.write()
        self.write(id=row['id'],revision=1,body='第二次整理，仍未验证')
        with self.assertRaises(ValueError):self.write(id=row['id'],revision=1)
        with self.t.store.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM autonomy_work_revisions').fetchone()[0],2)
        restored=ThoughtStore(self.root,SCOPE)
        with restored.connect() as db:self.assertEqual(db.execute('SELECT revision FROM autonomy_works').fetchone()[0],2)
        self.assertFalse(self.t.store.rows('receipts'))

    def test_workspace_cannot_read_paths_or_other_scope_or_execute_code(self):
        row=self.write()
        for request in ({'action':'read','id':'../../memory.sqlite3'}, {'action':'execute','body':'print(1)'},
                        {'action':'read','id':row['id'],'scope':'other'}):
            with self.assertRaises(ValueError):self.t.workspace.request(request)
        with self.t.store.connect() as db:db.execute('UPDATE autonomy_works SET scope=?',('other',))
        with self.assertRaises(ValueError):self.t.workspace.request({'action':'read','id':row['id']})

    def test_tool_requests_are_deduplicated_and_never_installed(self):
        first=self.t.workspace.request({'action':'request_tool','tool':'隔离的绘图练习工具'})
        second=self.t.workspace.request({'action':'request_tool','tool':'隔离的绘图练习工具'})
        self.assertEqual(first['id'],second['id'])
        self.assertFalse(first['installed'])
        with self.t.store.connect() as db:self.assertEqual(db.execute('SELECT count(*) FROM autonomy_tool_requests').fetchone()[0],1)

    def test_daily_requests_are_reserved_before_execution_and_survive_restart(self):
        self.config['autonomy_daily_model_calls']=2
        self.t.resources.reserve(2)
        with self.assertRaises(PhoneError):self.t.resources.reserve()
        self.t.store=ThoughtStore(self.root,SCOPE)
        self.assertEqual(self.t.resources.status()['remaining'],0)
        tomorrow=NOW+86400
        with patch('time.time',return_value=tomorrow):
            self.assertEqual(self.t.resources.status()['used'],0)
            self.t.resources.reserve()
            self.assertEqual(self.t.resources.status()['used'],1)

    def test_chat_context_does_not_charge_autonomy_and_failed_reservation_is_not_refunded(self):
        charge_model()
        self.assertEqual(self.t.resources.status()['used'],0)
        session=Session(6,self.t.guard,self.t.resources.reserve)
        token=SESSION.set(session)
        try:charge_model()
        finally:SESSION.reset(token)
        self.assertEqual(self.t.resources.status()['used'],1)

    async def test_day_budget_prevents_next_real_model_request(self):
        self.config['autonomy_daily_model_calls']=1
        self.responses(self.thought())
        await self.a.consider()
        self.assertEqual(self.context.llm_generate.await_count,1)
        self.assertEqual(self.t.resources.status()['used'],1)

    def test_signals_survive_restart_are_coalesced_and_do_not_bypass_sleep(self):
        self.t.wakes.notify('public_source','a'*16)
        self.t.wakes.notify('public_source','a'*16)
        self.t.store=ThoughtStore(self.root,SCOPE)
        with self.t.store.connect() as db:self.assertEqual(db.execute('SELECT count(*) FROM autonomy_wakes').fetchone()[0],1)
        self.a.rhythm.state.update(mode='asleep',until=NOW+3600,next_decision=NOW+3600)
        self.t.wakes.tick()
        self.assertEqual(self.a.rhythm.state['next_decision'],NOW+3600)
        self.a.rhythm.state.update(mode='awake',next_decision=NOW+3600)
        self.t.wakes.tick()
        self.assertEqual(self.a.rhythm.state['next_decision'],NOW+300)
        self.assertFalse(self.a.due(NOW+299))

    def test_reconnect_signal_is_metadata_only_and_hourly_coalesced(self):
        for online in [False,True,False,True]:self.t.wakes.tick(online)
        with self.t.store.connect() as db:
            rows=db.execute('SELECT kind,count(*) FROM autonomy_wakes GROUP BY kind').fetchall()
        self.assertEqual([tuple(r) for r in rows],[('phone_reconnected',1)])

    def test_sleep_pause_quiet_and_day_slots_remain_gates_for_signals(self):
        self.t.wakes.notify('public_source','b'*16,at=NOW)
        self.t.wakes.tick()
        self.a.state['last_user_at']=NOW
        self.assertFalse(self.a.due(NOW))
        self.a.state.update(last_user_at=NOW-1000,paused=True)
        self.assertFalse(self.a.due(NOW))
        self.a.state['paused']=False
        self.a.rhythm.state.update(decision_day='2026-11-05',decisions=12)
        self.assertFalse(self.a.due(NOW))

    def test_self_schedule_is_exact_and_finite_and_persists(self):
        self.t.wakes.schedule({'next_minutes':7})
        self.assertEqual(self.a.rhythm.state['next_decision'],NOW+420)
        self.assertEqual(self.t.store.meta('self_wake_at'),NOW+420)
        for delay in [float('nan'),float('inf'),True,0,10081]:
            with self.assertRaises(ValueError):self.t.wakes.schedule({'next_minutes':delay})


class PrivateWorkspaceTests(unittest.IsolatedAsyncioTestCase):
    responses=fixture.ThinkingTests.responses
    thought=fixture.ThinkingTests.thought
    decision=fixture.ThinkingTests.decision
    private_note=private_fixture.MemoryThinkingTests.private_note
    ask=private_fixture.MemoryThinkingTests.ask

    def setUp(self):
        private_fixture.MemoryThinkingTests.setUp(self)
        self.config['self_directed_enabled']=True

    async def test_private_draft_is_scoped_not_public_and_cannot_continue_to_network(self):
        self.responses(self.ask(),self.private_note(),{'activity':'workspace','workspace':{
            'action':'write','title':'自己的反思','body':'PRIVATE_DRAFT_MARKER 我想听完整再回答','revision':0},'continue':True},
            {'activity':'idle'})
        self.a._read_public=AsyncMock()
        await self.a.consider()
        self.assertEqual(self.context.llm_generate.await_count,4)
        self.assertFalse(self.t.workspace.list())
        self.assertNotIn('PRIVATE_DRAFT_MARKER',json.dumps(self.t.public_context(),ensure_ascii=False))
        self.a._read_public.assert_not_called()
        with self.t.store.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM autonomy_works WHERE visibility='private'").fetchone()[0],1)
        self.assertFalse(self.t.store.rows('receipts'))

    async def test_private_feedback_does_not_authorize_public_reading(self):
        self.responses(self.ask(),self.private_note(),{'activity':'workspace','workspace':{'action':'list'},'continue':True},
                       {'activity':'read','reading_id':'topic:animals'})
        self.a._read_public=AsyncMock()
        await self.a.consider()
        self.a._read_public.assert_not_called()
        self.assertEqual(self.t.store.rows('runs')[0]['status'],'invalid_output')

    async def test_forgotten_source_makes_private_work_inaccessible(self):
        from astrbot_plugin_companion_phone.memory_reflection import PrivateMemory
        self.responses(self.ask(),self.private_note(),{'activity':'workspace','workspace':{
            'action':'write','title':'自己的反思','body':'先核对自己的理解','revision':0}})
        await self.a.consider()
        recall=PrivateMemory(self.t)
        works=self.t.workspace.list('private',recall)
        self.assertEqual(len(works),1)
        self.memory.store.db.execute('UPDATE observations SET text=? WHERE id=?',('已更正',self.reply))
        self.memory.store.db.commit()
        self.assertFalse(self.t.workspace.list('private',recall))
        with self.assertRaises(ValueError):self.t.workspace.request({'action':'read','id':works[0]['id']},recall)

    async def test_private_work_cannot_read_public_workspace_or_request_tools(self):
        from astrbot_plugin_companion_phone.memory_reflection import PrivateMemory
        recall=PrivateMemory(self.t)
        for action in ['goals','request_tool']:
            with self.assertRaises(ValueError):self.t.workspace.request({'action':action,'tool':'公开搜索'},recall)


class SocialResourceTests(unittest.IsolatedAsyncioTestCase):
    tool=social_fixture.SocialTests.tool

    def setUp(self):
        social_fixture.SocialTests.setUp(self)
        p=self.p
        p.umo=SCOPE
        p.config.update(self_directed_enabled=True,thinking_enabled=True)
        platform=SimpleNamespace(meta=lambda:SimpleNamespace(id='qq-companion',name='aiocqhttp'),
                                 get_client=lambda:SimpleNamespace(call_action=AsyncMock()))
        p.context.get_platform_inst=lambda name:platform
        p.context.get_config=lambda **kw:{'provider_settings':{'enable':True}}
        p.context.persona_manager=SimpleNamespace(get_persona=AsyncMock(return_value=SimpleNamespace(system_prompt='小鲸鱼')))
        p.context.tool_loop_agent=AsyncMock()
        p.public_reading_context=lambda:''
        p.media=SimpleNamespace(recent=lambda **kw:[])
        p.thinking=SimpleNamespace(enabled=lambda:True,store=SimpleNamespace(meta=lambda *args:False),
                                  social_reflection=AsyncMock(return_value={}),common_context=lambda:'',
                                  resources=SimpleNamespace(reserve=Mock()))
        from astrbot.core.agent.tool import ToolSet
        self.c.tools=lambda base=None:ToolSet([self.tool('qzone_list_feed')])

    async def test_social_native_loop_and_nested_reviews_both_reserve_resources(self):
        async def native(**kw):charge_model()
        self.p.context.tool_loop_agent.side_effect=native
        worker=SocialAutonomy(self.p)
        self.assertIsNone(SESSION.get())
        await worker.cycle()
        self.assertEqual(worker.state['last_result'],'considered')
        self.assertEqual([c.args for c in self.p.thinking.resources.reserve.call_args_list],[(6,),()])
        self.assertIsNone(SESSION.get())

    async def test_social_cancellation_cleans_task_local_budget(self):
        self.p.context.tool_loop_agent.side_effect=asyncio.CancelledError()
        worker=SocialAutonomy(self.p)
        with self.assertRaises(asyncio.CancelledError):await worker.cycle()
        self.assertIsNone(SESSION.get())

    async def test_exhausted_social_budget_prevents_native_runner(self):
        self.p.thinking.resources.reserve.side_effect=PhoneError('autonomy_daily_model_budget')
        worker=SocialAutonomy(self.p)
        await worker.cycle()
        self.assertEqual(worker.state['last_result'],'deferred')
        self.p.context.tool_loop_agent.assert_not_called()
        self.assertIsNone(SESSION.get())


if __name__ == '__main__':unittest.main()
