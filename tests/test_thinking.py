"""Isolated thought-first execution and self-directed growth, no real services."""
import asyncio
from datetime import datetime
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from astrbot_plugin_companion_phone.autonomy import AutonomousBrowser
from astrbot_plugin_companion_phone.thinking import Thinking, ThoughtStore, THINK, DECIDE, Session, SESSION, charge_model, bounded
from astrbot_plugin_companion_phone.development import adopt
from astrbot_plugin_companion_memory.memory import MemoryStore, TZ
from astrbot_plugin_companion_memory.profile import SharedProfile

NOW = datetime(2026,11,5,14,0,tzinfo=TZ).timestamp()
SCOPE = 'qq-companion:FriendMessage:owner'


class ThinkingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.clock = patch('time.time',return_value=NOW)
        self.clock.start()
        self.addCleanup(self.clock.stop)
        self.addCleanup(self.tmp.cleanup)
        self.config = {'enabled':True,'thinking_enabled':True,'autonomous_enabled':True,
                       'autonomous_screen_consent':False,'autonomous_apps':['com.coolapk.market'],
                       'public_reading_enabled':True,'autonomous_quiet_seconds':900}
        device = SimpleNamespace(root=self.root,state={'paused':False},ready=Mock(),status=AsyncMock(),
                                 key=AsyncMock(),open_app=AsyncMock(),screenshot=AsyncMock())
        self.memory = SimpleNamespace(owner='owner',umo=SCOPE,config={'enabled':True,'growth_enabled':True},
            store=MemoryStore(self.root/'memory'),profile=SharedProfile(self.root/'memory'),
            resting=lambda:False,processing=asyncio.Lock())
        self.addCleanup(self.memory.store.close)
        self.context = SimpleNamespace(get_registered_star=lambda name:SimpleNamespace(activated=True,star_cls=self.memory),
            get_config=lambda **kw:{'provider_settings':{'enable':True}},llm_generate=AsyncMock())
        self.a = AutonomousBrowser(device,self.config,AsyncMock(),Mock(side_effect=AssertionError('private background read')))
        self.p = SimpleNamespace(root=self.root,config=self.config,umo=SCOPE,owner='owner',autonomy=self.a,
                                 context=self.context,public_notebook=lambda:SimpleNamespace(lookup=Mock(return_value=[])))
        self.t = self.a.thinking = Thinking(self.p)
        self.t.snapshot = NOW-1000
        self.a.state.update(last_user_at=NOW-1000)
        self.a.rhythm.state.update(mode='awake',updated_at=NOW,mode_since=NOW,awake_since=NOW,energy=.8,next_decision=NOW-1)

    def responses(self,*objects):
        self.context.llm_generate.side_effect=[SimpleNamespace(completion_text=json.dumps(x,ensure_ascii=False)) for x in objects]

    def thought(self,**values):
        return {'focus':'想弄清楚一个问题','question':'鸟类如何交流','next_step':'声音与行为',
                'growth_wish':'','practice':'','trial_id':'','attitude':'consider',**values}

    def decision(self,**values):
        return {'activity':'idle','growth_action':'none',**values}

    def source(self,at=NOW,key='a'):
        return {'id':'web:'+hashlib.sha256(key.encode()).hexdigest()[:12], 'timestamp':at,
                'app':'公开网页','kind':'web_text','url':'https://example.org/'+key,'title':'公开资料',
                'scope':'extracted_text','content_hash':hashlib.sha256(key.encode()).hexdigest(),
                'truncated':False,'note':'实际读取的一段公开资料','views':1,'public_content':True}

    def evidence(self):
        trial=self.t.store.start_trial('更愿意比较不同解释后再下判断','阅读时留意说法的依据',NOW-3*86400)
        ids=[]
        for day,key in [(2,'first'),(1,'second')]:
            at=NOW-day*86400
            run=self.t.store.begin(at)
            source=self.source(at,key)
            self.assertTrue(self.t.store.receipt(run,trial,source))
            self.a.reading.state['receipts'].append(source)
            self.t.store.update_run(run,status='completed')
        ids=[r['id'] for r in self.t.store.rows('receipts')]
        return trial,ids

    async def test_brain_start_skips_only_reminder_time_and_still_thinks_first(self):
        self.config['self_directed_enabled']=True
        self.a.rhythm.state['next_decision']=NOW+3600
        self.assertFalse(self.a.due(NOW));self.assertTrue(self.a.due(NOW,by_brain=True))
        self.responses(self.thought(),self.decision())
        await self.a._consider_session(False,'',by_brain=True)
        self.assertEqual(self.context.llm_generate.await_count,2)
        self.assertEqual(self.context.llm_generate.await_args_list[0].kwargs['system_prompt'],THINK)
        self.a.phone.key.assert_not_called()
    def test_brain_start_keeps_sleep_quiet_pause_and_decision_quota(self):
        self.config['self_directed_enabled']=True
        self.a.rhythm.state['next_decision']=NOW+3600
        self.a.state['last_user_at']=NOW
        self.assertFalse(self.a.due(NOW,by_brain=True));self.a.state['last_user_at']=NOW-1000
        self.a.state['paused']=True
        self.assertFalse(self.a.due(NOW,by_brain=True));self.a.state['paused']=False
        self.a.rhythm.state.update(mode='asleep',until=NOW+3600)
        self.assertFalse(self.a.due(NOW,by_brain=True));self.a.rhythm.state.update(mode='awake',until=0)
        self.a.rhythm.state.update(decision_day=datetime.fromtimestamp(NOW,TZ).date().isoformat(),decisions=12)
        self.assertFalse(self.a.due(NOW,by_brain=True))
    def test_brain_start_keeps_shared_model_request_budget(self):
        self.config.update(self_directed_enabled=True,autonomy_daily_model_calls=1)
        self.t.resources.reserve()
        self.assertFalse(self.a.due(NOW,by_brain=True))
    async def test_thought_finishes_before_decision_and_no_tools_in_first_stage(self):
        observed=[]
        async def generate(**kw):
            observed.append(kw)
            self.assertNotIn('tools',kw)
            if len(observed)==1:
                self.assertFalse(self.t.store.rows('goals'))
                self.assertFalse(self.t.store.rows('trials'))
                return SimpleNamespace(completion_text=json.dumps(self.thought(),ensure_ascii=False))
            latest=self.t.store.rows('runs')[0]
            self.assertEqual(latest['phase'],'thought_complete')
            self.assertEqual(latest['focus'],'想弄清楚一个问题')
            return SimpleNamespace(completion_text=json.dumps(self.decision()))
        self.context.llm_generate.side_effect=generate
        await self.a.consider()
        self.assertEqual([r['system_prompt'] for r in observed],[THINK,DECIDE])
        self.assertEqual(self.t.store.rows('runs')[0]['calls'],2)
        self.a.phone.key.assert_not_called()
        self.a.background.assert_not_called()

    async def test_empty_thought_does_not_force_decision_or_task(self):
        self.responses({})
        await self.a.consider()
        self.assertEqual(self.context.llm_generate.await_count,1)
        self.assertFalse(self.t.store.rows('goals'))
        self.assertFalse(self.t.store.rows('trials'))

    async def test_invalid_growth_field_gets_one_budgeted_repair_before_any_action(self):
        calls = 0
        async def generate(**kw):
            nonlocal calls
            calls += 1
            if calls == 1:
                value = self.thought()
            elif calls == 2:
                value = self.decision(growth_action='start|continue')
            else:
                self.assertFalse(self.t.store.rows('goals'))
                self.assertFalse(self.t.store.rows('trials'))
                value = {'growth_action':'none','activity':'browse','app':'unauthorized.package'}
            return SimpleNamespace(completion_text=json.dumps(value))
        self.context.llm_generate.side_effect = generate
        await self.a.consider()
        run = self.t.store.rows('runs')[0]
        self.assertEqual((run['status'],run['calls'],run['activity']), ('completed',3,'idle'))
        self.a.phone.open_app.assert_not_called()
        with self.t.store.connect() as db:
            events = [r['kind'] for r in db.execute('SELECT kind FROM process_events ORDER BY id')]
        self.assertEqual(events.count('validation_failed'), 1)

    async def test_repeated_invalid_growth_stops_with_specific_safe_diagnostic(self):
        self.a.outcome_callback = Mock()
        self.responses(self.thought(), self.decision(growth_action='PRIVATE_BAD_ACTION'), {'growth_action':None})
        await self.a.consider()
        self.assertEqual(self.context.llm_generate.await_count, 3)
        self.assertEqual(self.a.state['last_failure']['code'], 'invalid_development_action')
        self.assertEqual(self.t.store.rows('runs')[0]['status'], 'invalid_output')
        self.assertFalse(self.t.store.rows('goals'))
        self.assertFalse(self.t.store.rows('trials'))
        self.a.outcome_callback.assert_called_once_with('autonomy','activity','growth_action_invalid',error_type='ValueError')
        with self.t.store.connect() as db:
            events = [r['payload'] for r in db.execute('SELECT payload FROM process_events')]
        self.assertNotIn('PRIVATE_BAD_ACTION', ''.join(events))
        self.assertIn('invalid_development_action', events[-1])

    async def test_growth_repair_does_not_expand_the_shared_model_budget(self):
        self.config['thinking_model_calls'] = 2
        self.responses(self.thought(), self.decision(growth_action='not_allowed'))
        await self.a.consider()
        self.assertEqual(self.context.llm_generate.await_count, 2)
        self.assertEqual(self.t.store.rows('runs')[0]['status'], 'invalid_output')
        self.assertFalse(self.t.store.rows('goals'))

    async def test_arrival_during_growth_repair_interrupts_before_execution(self):
        values = iter([self.thought(), self.decision(growth_action=[]), {'growth_action':'none'}])
        calls = 0
        async def generate(**kw):
            nonlocal calls
            calls += 1
            if calls == 3:
                self.a.state['last_user_at'] = NOW
            return SimpleNamespace(completion_text=json.dumps(next(values)))
        self.context.llm_generate.side_effect = generate
        with self.assertRaises(asyncio.CancelledError):
            await self.a.consider()
        self.assertEqual(self.t.store.rows('runs')[0]['status'], 'interrupted')
        self.assertFalse(self.t.store.rows('goals'))
        self.a.phone.open_app.assert_not_called()

    async def test_first_stage_tool_like_fields_never_execute(self):
        self.responses({'activity':'browse','app':'com.coolapk.market','growth_action':'start'})
        await self.a.consider()
        self.a.phone.key.assert_not_called()
        self.assertEqual(self.context.llm_generate.await_count,1)

    async def test_new_problem_survives_restart_without_claimed_progress(self):
        self.responses(self.thought(),self.decision())
        await self.a.consider()
        restarted=ThoughtStore(self.root,SCOPE)
        goal=restarted.rows('goals')[0]
        self.assertEqual(goal['question'],'鸟类如何交流')
        self.assertEqual(goal['progress'],'')
        self.assertEqual(goal['status'],'open')

    async def test_pure_thought_works_with_offline_phone_and_reading_exhausted(self):
        self.config['public_reading_enabled']=False
        self.responses(self.thought(),self.decision())
        self.assertTrue(self.a.due(NOW))
        await self.a.consider()
        self.assertEqual(self.context.llm_generate.await_count,2)
        self.a.phone.status.assert_not_called()

    async def test_sleep_and_pause_do_not_read_or_call(self):
        self.a.rhythm.state['mode']='asleep'
        self.a.rhythm.state['until']=NOW+1000
        await self.a.consider()
        self.context.llm_generate.assert_not_called()
        self.a.rhythm.state['mode']='awake'
        self.t.store.set_meta('paused',True)
        await self.a.consider()
        self.context.llm_generate.assert_not_called()

    async def test_sleep_during_thought_blocks_decision_and_growth(self):
        async def complete(**kw):
            self.a.rhythm.state.update(mode='resting',until=NOW+600)
            return SimpleNamespace(completion_text=json.dumps(self.thought()))
        self.context.llm_generate.side_effect=complete
        await self.a.consider()
        self.assertEqual(self.context.llm_generate.await_count,1)
        self.assertFalse(self.t.store.rows('goals'))
        self.assertFalse(self.memory.store.entries('owner','growth',now=NOW))

    async def test_new_message_during_thought_interrupts_without_deciding(self):
        async def complete(**kw):
            self.a.state['last_user_at']=NOW
            return SimpleNamespace(completion_text=json.dumps(self.thought()))
        self.context.llm_generate.side_effect=complete
        with self.assertRaises(asyncio.CancelledError):await self.a.consider()
        self.assertEqual(self.t.store.rows('runs')[0]['status'],'interrupted')
        self.assertEqual(self.context.llm_generate.await_count,1)

    async def test_model_pause_and_whitelist_rechecked(self):
        self.context.get_config=lambda **kw:{'provider_settings':{'enable':False}}
        await self.a.consider()
        self.context.llm_generate.assert_not_called()
        self.a.rhythm.state['next_decision']=NOW-1
        self.context.get_config=lambda **kw:{'provider_settings':{'enable':True},'platform_settings':{'enable_id_white_list':True,'id_whitelist':[]}}
        await self.a.consider()
        self.context.llm_generate.assert_not_called()

    async def test_only_own_public_context_is_supplied(self):
        self.memory.store.observe('other','user','OTHER_CONTACT_PRIVATE',at=NOW)
        self.memory.store.observe('owner','user','OWNER_PRIVATE',at=NOW)
        self.a.recent=lambda:[{'id':'phone:x','timestamp':NOW,'app':'QQ','views':1,'note':'PRIVATE_SCREEN','public_content':False}]
        self.responses(self.thought(),self.decision())
        await self.a.consider()
        texts=''.join(x.kwargs['prompt'] for x in self.context.llm_generate.await_args_list)
        self.assertNotIn('PRIVATE',texts)
        self.a.background.assert_not_called()

    async def test_model_error_retains_thought_without_action(self):
        self.context.llm_generate.side_effect=[SimpleNamespace(completion_text=json.dumps(self.thought())),TimeoutError()]
        await self.a.consider()
        self.assertEqual(self.t.store.meta('last_thought')['question'],'鸟类如何交流')
        self.assertFalse(self.t.store.rows('trials'))
        self.a.phone.key.assert_not_called()

    async def test_invented_tool_activity_is_rejected(self):
        self.responses(self.thought(),self.decision(activity='send_private_msg'))
        await self.a.consider()
        self.assertEqual(self.t.store.rows('runs')[0]['status'],'invalid_output')

    async def test_growth_direction_starts_without_any_user_messages(self):
        self.responses(self.thought(growth_wish='更愿意耐心核对不同解释',practice='阅读时比较不同说法'),self.decision(growth_action='start'))
        await self.a.consider()
        trial=self.t.store.rows('trials')[0]
        self.assertEqual(trial['state'],'trying')
        self.assertEqual(trial['direction'],'更愿意耐心核对不同解释')
        self.assertFalse(self.memory.store.latest_messages('owner'))
        self.assertFalse(self.memory.store.entries('owner','growth',now=NOW))

    def test_repeated_ideas_or_one_day_cannot_become_stable_growth(self):
        trial=self.t.store.start_trial('更喜欢比较解释','阅读时比较说法',NOW-100)
        self.assertEqual(adopt(self.memory,self.t,trial,[],NOW),'insufficient_practice')
        run=self.t.store.begin(NOW-60)
        for key in ('a','b'):self.t.store.receipt(run,trial,self.source(NOW,key))
        ids=[r['id'] for r in self.t.store.rows('receipts')]
        self.assertIsNone(self.t.store.practice_evidence(trial,ids,NOW))

    def test_actual_cross_day_practice_can_grow_without_user_evidence(self):
        trial,ids=self.evidence()
        self.assertEqual(adopt(self.memory,self.t,trial,ids,NOW),'accepted')
        growth=self.memory.store.entries('owner','growth',now=NOW)[0]
        self.assertEqual(json.loads(growth['evidence']),[])
        evidence=self.memory.store.get_meta('self_growth_evidence:'+growth['id'])
        self.assertEqual(evidence['kind'],'self_chosen_practice')
        self.assertEqual(len(evidence['sources']),2)
        self.assertFalse(self.memory.store.latest_messages('owner'))

    def test_other_scope_receipts_are_never_eligible(self):
        trial,ids=self.evidence()
        with self.t.store.connect() as db:db.execute('UPDATE receipts SET scope=? WHERE id=?',('other',ids[0]))
        self.assertEqual(adopt(self.memory,self.t,trial,ids,NOW),'insufficient_practice')

    def test_removed_sources_cannot_support_growth(self):
        trial,ids=self.evidence()
        self.a.reading.state['receipts']=[]
        self.assertEqual(adopt(self.memory,self.t,trial,ids,NOW),'source_unavailable')

    def test_pre_intent_receipts_cannot_support_a_trial(self):
        trial,ids=self.evidence()
        with self.t.store.connect() as db:db.execute('UPDATE trials SET created=? WHERE id=?',(NOW,trial))
        self.assertIsNone(self.t.store.practice_evidence(trial,ids,NOW))

    def test_source_replay_not_counted_as_new_practice(self):
        trial=self.t.store.start_trial('更喜欢比较解释','阅读时比较说法',NOW-10)
        run=self.t.store.begin(NOW-1)
        self.assertTrue(self.t.store.receipt(run,trial,self.source()))
        self.assertFalse(self.t.store.receipt(run,trial,self.source()))

    def test_cooldown_growth_switch_and_pinned_traits_preserved(self):
        trial,ids=self.evidence()
        self.memory.config['growth_enabled']=False
        self.assertEqual(adopt(self.memory,self.t,trial,ids,NOW),'growth_unavailable')
        self.memory.config['growth_enabled']=True
        self.memory.store.set_meta('last_growth:owner',NOW-60)
        self.assertEqual(adopt(self.memory,self.t,trial,ids,NOW),'growth_cooldown')
        self.memory.store.set_meta('last_growth:owner',0)
        self.memory.store.put_entry('owner','growth','expression','保留固定习惯',[],pinned=True,now=NOW)
        self.assertEqual(adopt(self.memory,self.t,trial,ids,NOW),'protected')

    async def test_can_choose_to_abandon_a_trial(self):
        trial=self.t.store.start_trial('更喜欢比较解释','阅读时比较说法',NOW-10)
        self.responses(self.thought(trial_id=trial,attitude='abandon'),self.decision(growth_action='abandon',trial_id=trial))
        await self.a.consider()
        self.assertEqual(self.t.store.rows('trials')[0]['state'],'abandoned')
        self.assertFalse(self.memory.store.entries('owner','growth',now=NOW))

    async def test_can_revise_own_direction_without_reusing_old_practice(self):
        trial,ids=self.evidence()
        self.responses(self.thought(trial_id=trial,attitude='adjust',growth_wish='更愿意提出具体问题',practice='阅读后留下一个可以查证的问题'),
                       self.decision(growth_action='revise',trial_id=trial))
        await self.a.consider()
        trials=self.t.store.rows('trials')
        new=next(r for r in trials if r['id']!=trial)
        self.assertEqual(new['state'],'trying')
        self.assertEqual(next(r for r in trials if r['id']==trial)['state'],'abandoned')
        self.assertIsNone(self.t.store.practice_evidence(new['id'],ids,NOW))

    async def test_decision_cannot_adopt_when_thought_wants_to_abandon(self):
        trial,ids=self.evidence()
        self.responses(self.thought(trial_id=trial,attitude='abandon'),
                       self.decision(growth_action='adopt',trial_id=trial,evidence_ids=ids))
        await self.a.consider()
        self.assertFalse(self.memory.store.entries('owner','growth',now=NOW))
        self.assertEqual(self.t.store.rows('runs')[0]['status'],'invalid_output')

    async def test_budget_limits_downstream_observation_too(self):
        self.config['thinking_model_calls']=2
        self.responses(self.thought(),self.decision(activity='browse',app='com.coolapk.market'))
        # The absent screen grant is enforced before any device action.
        await self.a.consider()
        self.a.phone.key.assert_not_called()
        self.assertEqual(self.context.llm_generate.await_count,2)

    async def test_disabled_growth_keeps_thinking_but_does_not_start_trial(self):
        self.memory.config['growth_enabled']=False
        self.responses(self.thought(growth_wish='更愿意比较解释',practice='阅读时比较不同说法'),self.decision(growth_action='start'))
        await self.a.consider()
        self.assertTrue(self.t.store.rows('runs'))
        self.assertFalse(self.t.store.rows('trials'))

    async def test_own_paused_question_can_be_reopened(self):
        goal=self.t.store.goal('鸟类如何交流','声音与行为',NOW-100)
        with self.t.store.connect() as db:db.execute("UPDATE goals SET status='paused' WHERE id=?",(goal,))
        self.responses(self.thought(),self.decision(goal_id=goal,goal_status='open'))
        await self.a.consider()
        self.assertEqual(self.t.store.rows('goals')[0]['status'],'open')
        self.assertIn(goal,self.t.reading_options())

    def test_unrelated_public_observation_does_not_overwrite_question(self):
        goal=self.t.store.goal('鸟类如何交流','声音与行为',NOW-10)
        self.t.run_id=self.t.store.begin(NOW-1);self.t.goal_id=goal
        source=self.source()
        self.a.exploration.observe(source,{'question':'摄影如何构图','progress':'其他问题的暂时理解'})
        self.t.observe(source)
        self.assertEqual(self.t.store.rows('goals')[0]['progress'],'')

    def test_future_receipt_cannot_be_mixed_into_other_valid_evidence(self):
        trial,ids=self.evidence()
        run=self.t.store.begin(NOW)
        self.t.store.receipt(run,trial,self.source(NOW+60,'future'))
        ids=[r['id'] for r in self.t.store.rows('receipts')]
        self.assertIsNone(self.t.store.practice_evidence(trial,ids,NOW))

    async def test_real_read_is_called_only_after_both_stages_and_retains_progress(self):
        thought=self.thought(growth_wish='更愿意看原文再判断',practice='先读原文再留下一句理解')
        body='鸟类的声音和行为会传达不同信息。'*20
        article={'material':'web_text','url':'https://example.org/birds','title':'鸟类交流',
                 'text':body,'content_hash':hashlib.sha256(body.encode()).hexdigest(),
                 'retrieved_at':NOW,'truncated':False,'scope':'extracted_text','links':[]}
        observation={'useful':True,'note':'读到了声音与行为的不同作用',
                     'exploration':{'question':'鸟类如何交流','progress':'本页提到声音和行为都有作用','next_step':'比较另一种鸟类','status':'open'},'knowledge':None}
        self.responses(thought,self.decision(activity='read',growth_action='start'),observation)
        async def read_model(prompt,image):
            charge_model()
            response=await self.context.llm_generate(system_prompt='observation',prompt=prompt)
            return response.completion_text
        self.a.reading_model=read_model
        def search(query):
            self.assertEqual(self.context.llm_generate.await_count,2)
            self.assertLessEqual(len(query),80)
            return [{'url':article['url']}]
        with patch('astrbot_plugin_companion_phone.reading.search_pages',side_effect=search),patch('astrbot_plugin_companion_phone.reading.read_page',return_value=article):
            await self.a.consider()
        self.assertEqual(self.a.state['last_result'],'web_read')
        self.assertEqual(self.t.store.rows('goals')[0]['progress'],'本页提到声音和行为都有作用')
        self.assertEqual(len(self.t.store.rows('receipts')),1)
        self.assertEqual(self.t.store.rows('runs')[0]['calls'],3)
        self.assertFalse(self.memory.store.entries('owner','growth',now=NOW))

    def test_total_request_budget_and_context_isolation(self):
        session=Session(2,lambda:None)
        token=SESSION.set(session)
        try:
            charge_model();charge_model()
            with self.assertRaises(Exception):charge_model()
        finally:SESSION.reset(token)
        charge_model()
        self.assertEqual(session.calls,2)

    def test_interrupted_runs_are_not_replayed_on_restart(self):
        run=self.t.store.begin(NOW)
        store=ThoughtStore(self.root,SCOPE)
        self.assertEqual(store.rows('runs')[0]['status'],'interrupted_unknown')
        self.assertFalse(store.rows('receipts'))

    def test_corrupt_store_disables_thought_without_legacy_fallback(self):
        self.t.store.healthy=False
        self.assertFalse(self.a.due(NOW))

    def test_input_budget_thins_context_without_cutting_fields(self):
        payload={'自己的问题':[{'question':'鸟'*80,'progress':'鲸'*160} for _ in range(12)]}
        body=bounded(THINK,payload)
        self.assertLessEqual(len((THINK+body).encode()),11000)
        self.assertEqual(json.loads(body)['自己的问题'][0]['question'],'鸟'*80)

    async def test_social_activity_gets_separate_thought_before_tools(self):
        self.responses(self.thought())
        guard=Mock()
        result=await self.t.social_reflection(guard)
        self.assertEqual(result['question'],'鸟类如何交流')
        self.assertGreaterEqual(guard.call_count,2)
        self.assertEqual(self.t.store.rows('runs')[0]['phase'],'thought_complete')
        self.a.phone.key.assert_not_called()


if __name__=='__main__':unittest.main()
