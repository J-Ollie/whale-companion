"""Unlimited means no numeric quota; real usage and behavioral gates remain."""
import asyncio
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock,Mock,patch
import test_brain as brain_fixtures
import test_thinking as thought_fixtures
from astrbot_plugin_companion_chat import quota as cq
from astrbot_plugin_companion_phone import quota as pq
from astrbot_plugin_companion_memory import quota as mq
from astrbot_plugin_companion_chat.active.main import eligible
from astrbot_plugin_companion_phone.thinking import Session
from astrbot_plugin_companion_phone.rhythm import TZ
from datetime import datetime

class PolicyCase(unittest.IsolatedAsyncioTestCase):
    def policy(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        path=Path(tmp.name)/'quota-policy.json';path.write_text('{"unlimited_quotas":true}')
        for module in (cq,pq,mq):
            p=patch.object(module,'POLICY_PATH',path);p.start();self.addCleanup(p.stop)
        return path
    def test_true_unlimited_and_iterators_not_a_large_fake_limit(self):
        path=self.policy();self.assertTrue(cq.enabled());self.assertEqual(pq.maximum(6),float('inf'))
        it=pq.steps(3);self.assertEqual([next(it) for _ in range(100)][-1],99)
        path.write_text('{bad');self.assertFalse(cq.enabled())
    def test_semantic_query_and_index_volume_have_no_daily_cap(self):
        self.policy();import sqlite3
        from astrbot_plugin_companion_memory.semantic import SemanticMemory
        semantic=object.__new__(SemanticMemory);semantic.plugin=NS(config={'semantic_daily_queries':0,'semantic_daily_index_docs':0,'semantic_daily_index_chars':0})
        semantic.db=sqlite3.connect(':memory:');self.addCleanup(semantic.db.close)
        semantic.db.execute('CREATE TABLE budget(day TEXT,kind TEXT,units INTEGER,chars INTEGER,PRIMARY KEY(day,kind))')
        self.assertTrue(semantic.reserve('query',10000,10000000))
        self.assertTrue(semantic.reserve('index',10000,10000000))
        self.assertEqual(semantic.db.execute('SELECT sum(units) FROM budget').fetchone()[0],20000)
    def test_unlimited_outreach_keeps_pause_and_contact_hold(self):
        self.policy();now=datetime(2026,11,8,12,tzinfo=TZ).timestamp()
        state={'day':'2026-11-08','daily_count':100000,'last_user_at':now-1,'last_decision_at':now-1}
        config={'enabled':True,'awake_anytime':True,'daily_limit':1,'min_interval_hours':200}
        self.assertTrue(eligible(state,now,config))
        state['paused']=True;self.assertFalse(eligible(state,now,config));state['paused']=False
        state['contact_policy_v1']={'hold':{'mode':'until','until':now+3600}}
        self.assertFalse(eligible(state,now,config))
    def test_session_has_no_call_quota_and_still_checks_cancellation(self):
        self.policy();guard=Mock();reserve=Mock();session=Session(1,guard,reserve)
        for _ in range(40):session.charge()
        self.assertEqual(session.calls,40);self.assertEqual(reserve.call_count,40)
        guard.side_effect=asyncio.CancelledError
        with self.assertRaises(asyncio.CancelledError):session.charge()
    async def test_native_step_override_is_scoped_and_restored(self):
        self.policy()
        from astrbot.core.agent.runners.tool_loop_agent_runner import ToolLoopAgentRunner
        from astrbot.core.platform.astr_message_event import current_message_event
        seen=[]
        async def original(self,maximum):seen.append(maximum);yield 'result'
        with patch.object(ToolLoopAgentRunner,'step_until_done',original):
            owner=NS(ready=True,config={},friends=NS(platform_id='whale'))
            restore=cq.install_agent_steps(owner)
            try:
                for origin,expected in [('whale:FriendMessage:123',float('inf')),('other:FriendMessage:123',3)]:
                    token=current_message_event.set(NS(unified_msg_origin=origin))
                    try:
                        async for _ in ToolLoopAgentRunner.step_until_done(NS(),3):pass
                    finally:current_message_event.reset(token)
                    self.assertEqual(seen[-1],expected)
            finally:restore()
            self.assertIs(ToolLoopAgentRunner.step_until_done,original)

class BrainUnlimited(unittest.IsolatedAsyncioTestCase):
    policy=PolicyCase.policy
    setUp=brain_fixtures.BrainTests.setUp
    def test_brain_daily_counter_not_reset_or_blocked(self):
        self.policy();self.chat.config['brain_dialogue_daily_calls']=0
        for _ in range(5):self.assertTrue(self.brain.reserve())
        with self.brain.connect() as db:self.assertEqual(db.execute('SELECT calls FROM usage').fetchone()[0],5)
    async def test_decision_can_recall_more_than_once_without_crossing_contact(self):
        self.policy();self.chat.config['brain_dialogue_daily_calls']=0
        self.model.side_effect=[NS(completion_text='{"choice":"recall","query":"第一条线索"}'),
            NS(completion_text='{"choice":"recall","query":"另一条线索"}'),NS(completion_text='{"choice":"reply","note":""}')]
        recall=AsyncMock(return_value=NS(content=[NS(text='本人的资料')],isError=False))
        self.plugins['astrbot_plugin_companion_memory']=NS(activated=True,star_cls=NS(tool_recall=recall))
        e=brain_fixtures.BrainTests.event(self)
        value=await self.brain.decide(e,brain_fixtures.BrainTests.request(self))
        self.assertEqual(value['choice'],'reply');self.assertEqual(self.model.await_count,3)
        self.assertEqual(recall.await_count,2);self.assertTrue(all(a.args[0] is e for a in recall.await_args_list))
    def test_optional_reoffer_respects_own_pace_but_responsibility_can_interrupt(self):
        self.policy();import time
        planner=self.brain.planner;planner.last_result='self_chose_idle';at=time.time()+3600;planner.schedule(at)
        planner.submit('growth',AsyncMock(),check=lambda:True)
        self.assertEqual(planner.next_at,at)
        planner.submit('outreach',AsyncMock(),check=lambda:True,priority='responsibility')
        self.assertLess(planner.next_at,at)
    def test_planner_budget_keeps_shared_reservations_without_cap(self):
        self.policy();self.chat.config['brain_background_daily_plans']=0
        phone=NS(thinking=NS(resources=NS(reserve=Mock())))
        for _ in range(4):self.brain.planner.reserve(phone)
        self.assertEqual(phone.thinking.resources.reserve.call_count,4)

class ThinkingUnlimited(unittest.IsolatedAsyncioTestCase):
    policy=PolicyCase.policy
    setUp=thought_fixtures.ThinkingTests.setUp
    def test_shared_resources_unlimited_with_null_remaining_and_real_usage(self):
        self.policy();self.config.update(self_directed_enabled=True,autonomy_daily_model_calls=1)
        for _ in range(8):self.t.resources.reserve()
        status=self.t.resources.status();self.assertIsNone(status['limit']);self.assertIsNone(status['remaining']);self.assertEqual(status['used'],8)
    def test_awake_decision_quota_removed_but_sleep_and_pause_remain(self):
        self.policy();now=thought_fixtures.NOW;self.config['self_directed_enabled']=True
        self.a.rhythm.state.update(decision_day=datetime.fromtimestamp(now,TZ).date().isoformat(),decisions=999)
        self.assertTrue(self.a.due(now,by_brain=True))
        self.a.rhythm.state.update(mode='asleep',until=now+3600)
        self.assertFalse(self.a.due(now,by_brain=True))
        self.a.rhythm.state.update(mode='awake',until=0);self.a.state['paused']=True
        self.assertFalse(self.a.due(now,by_brain=True))
    def test_self_chosen_trials_are_not_limited_to_three(self):
        self.policy()
        ids=[self.t.store.start_trial('方向'+str(i),'自己的小尝试',thought_fixtures.NOW) for i in range(5)]
        self.assertTrue(all(ids));self.assertEqual(len(set(ids)),5)
    def test_reading_daily_quota_removed_enabled_gate_unchanged(self):
        self.policy();self.config.update(public_reading_enabled=True,public_reading_daily_limit=1)
        self.a.reading.state.update(day=datetime.fromtimestamp(thought_fixtures.NOW,TZ).date().isoformat(),used=999)
        self.assertTrue(self.a.reading.available());self.config['public_reading_enabled']=False
        self.assertFalse(self.a.reading.available())

if __name__=='__main__':unittest.main()
