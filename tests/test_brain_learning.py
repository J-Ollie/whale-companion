"""No canned personality or shortcut without current context and actual sources."""
import json
import sqlite3
import time
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock,patch
import test_brain as fixtures
import test_brain_phase2 as phase2
from astrbot_plugin_companion_chat import cognitive_context
from astrbot_plugin_companion_memory.memory import MemoryStore
from astrbot_plugin_companion_chat.brain import KEY
from astrbot_plugin_companion_chat.main import CompanionChat

class LearningTests(unittest.IsolatedAsyncioTestCase):
    setUp=fixtures.BrainTests.setUp
    event=fixtures.BrainTests.event
    request=fixtures.BrainTests.request
    def view(self):return {'scope':'private','conversation':[{'role':'user','content':'说说花的颜色'},{'role':'assistant','content':'这朵是蓝色的。'}],
        'self':{'rhythm':{'mode':'awake'},'emotion':{'心情':'平静'}},'versions':{'common_style':'a','common_intention':'a','attitude':'a','relationships':'a'},'commitments':[]}
    def memory(self):
        if not hasattr(self,'memory_store'):
            self.memory_store=MemoryStore(self.root/'test-memory');self.addCleanup(self.memory_store.close)
        self.valid=True
        self.plugins['astrbot_plugin_companion_memory']=NS(activated=True,star_cls=NS(store=self.memory_store,
            affect=lambda:NS(social=NS(valid_source=lambda *a:self.valid))))
    def observe(self,i,at=None,choice='reply',source='self_choice'):
        if not hasattr(self,'memory_store'):self.memory()
        e=self.event(text='谢谢')
        with self.memory_store.atomic():self.memory_store.db.execute('INSERT OR IGNORE INTO observations(id,qq,role,text,at,origin) VALUES(?,?,?,?,?,?)',(i,'123','user','谢谢',at or time.time(),'test:FriendMessage:123'))
        e.set_extra('companion_observation_id',i)
        self.brain.learning.choose(e,self.view())
        decision={'source':source,'choice':choice};e.set_extra(KEY,decision)
        self.brain.learning.observe_decision(e,decision)
        if at is None:self.brain.learning.record_reply(e,'嗯嗯，花色很漂亮。',True)
        else:
            with patch('time.time',return_value=at):self.brain.learning.record_reply(e,'嗯嗯，花色很漂亮。',True)
        return e
    def activate(self):
        self.memory();now=time.time()
        for i in range(1,6):self.observe(i,now-86400 if i<3 else now)
        offered=self.brain.learning.review_context()
        # Six can be offered, but no more than three assessments per review.
        for chunk in (offered['samples'][:3],offered['samples'][3:]):
            self.brain.learning.apply_review({'ratings':[{'id':r['id'],'verdict':'fit','quote':'花色很漂亮'} for r in chunk]},offered)
        scope='contact:'+self.brain.scope('123');self.brain.learning.phase(scope,'thanks','shadow')
        self.observe(7);self.observe(8)
        self.brain.learning.phase(scope,'thanks','active')
        return scope
    def test_ambiguous_words_new_requests_groups_and_sensitive_context_always_slow(self):
        for text in ('哦','嗯','好','行','谢谢，帮我发空间','谢谢你明天提醒我'):
            self.assertIsNone(self.brain.learning.choose(self.event(text=text),self.view()))
        for content in ('我很难过','是否发布？','他只说“谢谢”','我答应明天发'):
            v=self.view();v['conversation'][0]['content']=content
            self.assertIsNone(self.brain.learning.choose(self.event(text='谢谢'),v))
        v=self.view();v['self']['emotion']={'心情':'生气'}
        self.assertIsNone(self.brain.learning.choose(self.event(text='谢谢'),v))
        e=self.event(text='谢谢');e.get_group_id=lambda:'group'
        self.assertIsNone(self.brain.learning.choose(e,self.view()))
    def test_no_receipt_is_not_sample_and_candidate_is_not_active(self):
        e=self.event(text='谢谢');self.brain.learning.choose(e,self.view())
        self.brain.learning.record_reply(e,'嗯嗯',False)
        self.assertEqual(self.brain.learning.mirror(e)['confirmed_sent_samples'],0)
        self.observe(1)
        self.assertIsNone(self.brain.learning.choose(self.event(text='谢谢'),self.view()))
        with self.assertRaises(ValueError):self.brain.learning.phase('contact:'+self.brain.scope('123'),'thanks','active')
    def test_real_source_cross_day_and_shadow_then_self_choice_enable(self):
        scope=self.activate();e=self.event(text='谢谢')
        self.assertEqual(self.brain.learning.choose(e,self.view())['source'],'learned_habit')
        other=self.event('456',text='谢谢');self.assertIsNone(self.brain.learning.choose(other,self.view()))
        self.valid=False
        self.assertIsNone(self.brain.learning.choose(self.event(text='谢谢'),self.view()))
        self.assertEqual(self.brain.learning.status()['phases']['paused'],1)
    def test_missing_current_state_or_forgotten_shadow_never_uses_habit(self):
        self.activate();v=self.view();v['versions'].pop('relationships')
        self.assertIsNone(self.brain.learning.choose(self.event(text='谢谢'),v))
        with self.brain.learning.connect() as db:db.execute("UPDATE habit_hits SET turn=replace(turn,'|obs:','|forgotten:') WHERE phase='shadow'")
        self.assertFalse(self.brain.learning.supported('contact:'+self.brain.scope('123'),'thanks'))
    def test_changed_current_style_invalidates_habit(self):
        self.activate();v=self.view();v['versions']['common_style']='new'
        self.assertIsNone(self.brain.learning.choose(self.event(text='谢谢'),v))
    def test_counterexample_and_fake_quote_do_not_enable(self):
        self.memory();self.observe(1);offered=self.brain.learning.review_context();r=offered['samples'][0]
        with self.assertRaises(ValueError):self.brain.learning.apply_review({'ratings':[{'id':r['id'],'verdict':'fit','quote':'不存在的原文'}]},offered)
        self.brain.learning.apply_review({'ratings':[{'id':r['id'],'verdict':'counterexample','quote':'嗯嗯'}]},offered)
        self.assertFalse(self.brain.learning.supported('contact:'+self.brain.scope('123'),'thanks'))
    def test_multiple_bubbles_from_one_observation_are_not_independent_events(self):
        scope=self.activate()
        with self.brain.learning.connect() as db:
            db.execute("UPDATE samples SET ref='turn:repeat|obs:1'")
        self.assertFalse(self.brain.learning.supported(scope,'thanks'))
    def test_stats_scoped_and_unknown_average_is_not_zero(self):
        self.assertIsNone(self.brain.learning.mirror(self.event())['average_characters'])
        self.observe(1)
        self.assertEqual(self.brain.learning.mirror(self.event('456'))['confirmed_sent_samples'],0)
    async def test_active_habit_skips_only_decision_model_and_its_reservation(self):
        self.activate();e=self.event(text='谢谢')
        with patch.object(self.brain,'situation',return_value=self.view()):value=await self.brain.decide(e,self.request())
        self.assertEqual(value['source'],'learned_habit');self.model.assert_not_awaited()
        with self.brain.connect() as db:self.assertEqual(db.execute('SELECT count(*) FROM model_calls').fetchone()[0],0)
    def test_source_correction_in_place_invalidates_habit(self):
        scope=self.activate()
        with self.brain.learning.connect() as db:old_ids={r[0] for r in db.execute("SELECT id FROM samples WHERE ref LIKE '%|obs:1' OR ref LIKE '%|obs:2' OR ref LIKE '%|obs:3'")}
        self.assertTrue(old_ids)
        with self.memory_store.atomic():self.memory_store.db.execute("UPDATE observations SET text='修正后的原话' WHERE id IN (1,2,3)")
        self.assertFalse(self.brain.learning.supported(scope,'thanks'))
        self.assertFalse(old_ids & {r['id'] for r in self.brain.learning.review_context()['samples']})
    def test_future_commitment_quotes_attachments_and_truncated_context_require_slow(self):
        e=self.event(text='谢谢');v=self.view();v['commitments']=[{'status':'pending','not_before':time.time()+86400}]
        self.assertIsNone(self.brain.learning.choose(e,v))
        v=self.view();v['conversation_complete_for_habits']=False
        self.assertIsNone(self.brain.learning.choose(e,v))
        e.message_obj=NS(message=[NS(type='Reply')])
        self.assertIsNone(self.brain.learning.choose(e,self.view()))
    def test_explicit_acknowledgement_can_be_candidate_but_ambiguous_ack_stays_slow(self):
        self.assertEqual(self.brain.learning.route(self.event(text='我明白了'),self.view())[0],'acknowledgement')
        for text in ('嗯','哦','好','行'):
            self.assertIsNone(self.brain.learning.route(self.event(text=text),self.view()))
    def test_fast_replies_never_train_their_own_support(self):
        self.memory();e=self.observe(1,source='learned_habit')
        self.assertEqual(self.brain.learning.review_context()['samples'],[])
        self.assertEqual(self.brain.learning.mirror(e)['confirmed_sent_samples'],1)
    def test_shadow_requires_real_slow_decision_and_disagreement_pauses(self):
        scope=self.activate();self.brain.learning.phase(scope,'thanks','shadow')
        before=self.brain.learning.readiness(scope,'thanks')['shadow_matches']
        self.observe(9,source='learned_habit')
        self.assertEqual(self.brain.learning.readiness(scope,'thanks')['shadow_matches'],before)
        self.observe(10,choice='clarify')
        self.assertEqual(self.brain.learning.status()['phases']['paused'],1)
        self.assertIn('shadow_disagreed',self.brain.learning.readiness(scope,'thanks')['missing'])
    def test_verdict_can_be_revised_and_counterexample_pauses_immediately(self):
        scope=self.activate();offered=self.brain.learning.review_context();row=next(r for r in offered['samples'] if r['previous_verdict']=='fit')
        for verdict in ('unknown','fit','counterexample'):
            self.brain.learning.apply_review({'ratings':[{'id':row['id'],'verdict':verdict,'quote':'花色很漂亮'}]},offered)
        self.assertEqual(self.brain.learning.status()['phases']['paused'],1)
        with self.brain.learning.connect() as db:
            self.assertEqual(db.execute('SELECT verdict FROM assessments WHERE sample=?',(row['id'],)).fetchone()[0],'counterexample')
            self.assertGreaterEqual(db.execute('SELECT count(*) FROM assessment_history WHERE sample=?',(row['id'],)).fetchone()[0],4)
    def test_failed_decision_or_unconfirmed_send_does_not_form_candidate(self):
        self.memory();self.observe(1,choice='decision_unavailable',source='decision_error')
        self.assertEqual(self.brain.learning.review_context()['samples'],[])
    def test_old_policy_observations_do_not_satisfy_new_habit(self):
        scope=self.activate()
        with self.brain.learning.connect() as db:db.execute("UPDATE habit_shadow_evidence SET policy='old'")
        self.assertFalse(self.brain.learning.supported(scope,'thanks'))
    async def test_self_review_tool_is_contact_scoped_and_pause_is_immediate(self):
        scope=self.activate();e=self.event(text='普通聊天')
        self.assertFalse((await CompanionChat.habits(self.chat,e,'status')).isError)
        result=await CompanionChat.habits(self.chat,e,'review',json.dumps({'habit':{'route':'thanks','phase':'paused'}}))
        self.assertFalse(result.isError)
        self.assertIsNone(self.brain.learning.choose(self.event(text='谢谢'),self.view()))
        other=self.event('456');view=await CompanionChat.habits(self.chat,other,'samples')
        self.assertEqual(json.loads(view.content[0].text)['samples'],[])
        self.assertTrue((await CompanionChat.habits(self.chat,other,'review',json.dumps({'habit':{'route':'thanks','phase':'active'}}))).isError)
    async def test_self_review_tool_respects_sleep_group_and_turn_limits(self):
        self.chat.friends.sleeping=lambda:True
        self.assertTrue((await CompanionChat.habits(self.chat,self.event(),'status')).isError)
        self.chat.friends.sleeping=lambda:False;e=self.event();e.get_group_id=lambda:'group'
        self.assertTrue((await CompanionChat.habits(self.chat,e,'samples')).isError)
        e=self.event()
        for _ in range(3):self.assertFalse((await CompanionChat.habits(self.chat,e,'status')).isError)
        self.assertTrue((await CompanionChat.habits(self.chat,e,'status')).isError)
    def test_native_plain_text_parts_are_eligible_but_images_and_tool_history_are_not(self):
        req=self.request();req.contexts=[{'role':'user','content':[{'type':'text','text':'花是什么颜色'}]},
            {'role':'assistant','content':[{'type':'text','text':'这朵是蓝色的。'}]}]
        view=cognitive_context.build(self.brain,self.event(text='谢谢'),req)
        self.assertTrue(view['conversation_complete_for_habits'])
        eligible=self.view();eligible.update(conversation=view['conversation'],conversation_complete_for_habits=True)
        self.assertIsNotNone(self.brain.learning.route(self.event(text='谢谢'),eligible))
        req.contexts[0]['content'].append({'type':'image_url','image_url':{'url':'not_read'}})
        self.assertFalse(cognitive_context.build(self.brain,self.event(),req)['conversation_complete_for_habits'])
        req.contexts=[{'role':'assistant','content':'这朵是蓝色的。','tool_calls':[{'id':'call'}]}]
        self.assertFalse(cognitive_context.build(self.brain,self.event(),req)['conversation_complete_for_habits'])
    def test_optional_context_trims_without_losing_current_message_or_core_state(self):
        v=self.view();v['current_message']='当前请求';v['self_observation']={'samples':[{'id':str(i),'text':'长材料'*1000} for i in range(8)]}
        out=cognitive_context.bounded(v,3000)
        self.assertEqual(out['current_message'],'当前请求');self.assertEqual(out['self']['emotion'],v['self']['emotion'])
        self.assertLess(len(json.dumps(out,ensure_ascii=False).encode()),3000)

class PlannerReviewTests(unittest.IsolatedAsyncioTestCase):
    setUp=fixtures.BrainTests.setUp
    phone=phase2.PhaseTwoTests.phone
    plan_output=phase2.PhaseTwoTests.plan_output
    async def test_invalid_optional_review_does_not_cancel_real_choice(self):
        self.phone();work=AsyncMock();self.brain.offer('growth',work,check=lambda:True)
        self.model.return_value=self.plan_output('growth',learning_review={'ratings':[{'id':'invented','verdict':'fit','quote':'fake'}]})
        await self.brain.planner.select();work.assert_awaited_once()
        self.assertEqual(self.brain.planner.learning_result,'invalid_optional_review')
    async def test_unrelated_optional_question_does_not_cancel_valid_growth(self):
        p=self.phone();work=AsyncMock();self.brain.offer('growth',work,check=lambda:True)
        self.model.return_value=self.plan_output('growth',question_id='unoffered')
        await self.brain.planner.select();work.assert_awaited_once()
        self.assertEqual(self.brain.planner.ignored_optional,['question_not_applicable'])
        self.assertFalse(getattr(p.thinking,'goal_id',''))
    async def test_unoffered_optional_goal_never_reaches_worker_as_reference(self):
        p=self.phone();work=AsyncMock();self.brain.offer('autonomy',work,check=lambda:True)
        self.model.return_value=self.plan_output('autonomy',question_id='unoffered')
        await self.brain.planner.select();work.assert_awaited_once()
        self.assertFalse(getattr(p.thinking,'goal_id',''))
        self.assertEqual(self.brain.planner.ignored_optional,['question_not_offered'])
    async def test_malformed_plan_gets_one_metered_format_correction(self):
        p=self.phone();work=AsyncMock();self.brain.offer('growth',work,check=lambda:True)
        self.model.side_effect=[NS(completion_text='{bad json'),self.plan_output('growth')]
        await self.brain.planner.select();work.assert_awaited_once()
        self.assertEqual(self.model.await_count,2);self.assertEqual(p.thinking.resources.reserve.call_count,2)
        self.assertEqual(self.brain.planner.repair_result,'validated')
    async def test_malformed_correction_never_gets_third_call_or_unoffered_worker(self):
        self.phone();work=AsyncMock();self.brain.offer('growth',work,check=lambda:True)
        self.model.return_value=NS(completion_text='{bad json')
        await self.brain.planner.select();work.assert_not_awaited()
        self.assertEqual(self.model.await_count,2);self.assertEqual(self.brain.planner.repair_result,'rejected')
    async def test_repair_cannot_bypass_background_budget(self):
        self.phone();work=AsyncMock();self.brain.offer('growth',work,check=lambda:True)
        self.chat.config['brain_background_daily_plans']=1;self.model.return_value=NS(completion_text='{bad json')
        await self.brain.planner.select();work.assert_not_awaited();self.model.assert_awaited_once()
        self.assertEqual(self.brain.planner.last_result,'planning_budget_limited')
    async def test_state_change_prevents_repair_and_preserves_opportunity(self):
        p=self.phone();work=AsyncMock();self.brain.offer('growth',work,check=lambda:True)
        async def model(**kw):
            p.autonomy.state['last_user_at']+=1
            return NS(completion_text='{bad json')
        self.model.side_effect=model
        await self.brain.planner.select();work.assert_not_awaited();self.model.assert_awaited_once()
        self.assertEqual(self.brain.planner.last_result,'interrupted_by_state_change')
    async def test_self_chosen_thinking_does_not_require_timer_opportunity(self):
        p=self.phone();p.autonomy.due=lambda *args,**kwargs:kwargs.get('by_brain') is True
        p.autonomy._consider_session=AsyncMock()
        self.model.return_value=self.plan_output('学习',wish='想学一点新东西')
        await self.brain.planner.select()
        p.autonomy._consider_session.assert_awaited_once_with(False,'',by_brain=True)
        self.assertEqual(self.brain.intentions.rows()[0]['title'],'想学一点新东西')
        self.assertEqual(self.brain.planner.last_result,'worker_returned')
    async def test_no_reminder_list_can_still_trigger_due_self_consideration(self):
        p=self.phone();p.autonomy.due=lambda *a,**kw:True;p.autonomy._consider_session=AsyncMock()
        self.brain.planner.next_at=0;self.model.return_value=self.plan_output('idle')
        self.brain.planner.tick();task=self.brain.planner.task
        self.assertIsNotNone(task);await task
        self.assertEqual(self.brain.planner.last_result,'self_chose_idle')
        p.autonomy._consider_session.assert_not_awaited()
    async def test_new_wish_is_preserved_not_rejected_or_executed_as_code(self):
        self.phone();work=AsyncMock();self.brain.offer('growth',work,check=lambda:True)
        self.model.return_value=self.plan_output('画一部自己的动画',wish='想画一部自己的动画')
        await self.brain.planner.select();work.assert_not_awaited();self.model.assert_awaited_once()
        self.assertEqual(self.brain.planner.last_result,'intention_saved_capability_unavailable')
        self.assertEqual(self.brain.intentions.rows()[0]['title'],'想画一部自己的动画')
        with self.brain.connect() as db:
            self.assertNotIn('动画',json.dumps(db.execute('SELECT * FROM plans').fetchall(),ensure_ascii=False))
    async def test_explicit_entry_binds_free_wish_to_available_executor(self):
        p=self.phone();p.autonomy.due=lambda *a,**kw:True;p.autonomy._consider_session=AsyncMock()
        self.model.return_value=self.plan_output('研究一种新想法',entry='autonomy',wish='研究一种新想法')
        await self.brain.planner.select();p.autonomy._consider_session.assert_awaited_once()
    async def test_self_initiated_capability_is_unavailable_while_paused_or_asleep(self):
        p=self.phone();p.autonomy.due=lambda *a,**kw:True;p.autonomy._consider_session=AsyncMock()
        p.autonomy.state['paused']=True
        self.assertEqual(self.brain.planner.capabilities(p),{})
        p.autonomy.state['paused']=False;p.autonomy.rhythm.state['mode']='asleep'
        self.assertEqual(self.brain.planner.capabilities(p),{})
    async def test_unknown_old_intention_reference_does_not_cancel_self_choice(self):
        self.phone();work=AsyncMock();self.brain.offer('growth',work,check=lambda:True)
        self.model.return_value=self.plan_output('growth',intention={'action':'pause','id':'intent:ffffffffffffffff'})
        await self.brain.planner.select();work.assert_awaited_once()
        self.assertEqual(self.brain.intentions.rows(),[])
        self.assertIn('intention_change_not_applied',self.brain.planner.ignored_optional)
    async def test_unpersistable_optional_wish_does_not_cancel_valid_choice_or_claim_saved(self):
        self.phone();work=AsyncMock();self.brain.offer('growth',work,check=lambda:True)
        self.model.return_value=self.plan_output('growth',wish='未摘要的编号12345678')
        await self.brain.planner.select();work.assert_awaited_once()
        self.assertIn('new_intention_not_saved',self.brain.planner.ignored_optional)
        self.brain.offer('growth',work,check=lambda:True)
        self.model.return_value=self.plan_output('新功能',wish='未摘要的编号12345678')
        await self.brain.planner.select()
        self.assertEqual(self.brain.planner.last_result,'intention_not_saved')
    async def test_diagnostics_never_export_private_error_message(self):
        self.phone();work=AsyncMock();self.brain.offer('growth',work,check=lambda:True)
        self.model.side_effect=OSError('PRIVATE_PROVIDER_SECRET')
        await self.brain.planner.select()
        self.assertEqual(self.brain.planner.last_failure['stage'],'model')
        self.assertNotIn('PRIVATE_PROVIDER_SECRET',json.dumps(self.brain.status()))

if __name__=='__main__':unittest.main()
