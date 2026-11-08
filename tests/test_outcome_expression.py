"""Real receipts, optional expression, scope isolation and failure-channel fallback."""
import json
import os
import sqlite3
from pathlib import Path
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock,Mock,patch
sys.path.insert(0,os.environ.get('COMPANION_TEST_PLUGINS',str(Path(__file__).parent)))
from astrbot_plugin_companion_chat.outcomes import Outcomes,RULE
from astrbot_plugin_companion_chat.outreach_evidence import OutreachEvidence
from astrbot_plugin_companion_chat.main import CompanionChat
from astrbot_plugin_companion_chat import grounding
from astrbot_plugin_companion_chat.breeze.medium import TimedMedium
from astrbot_plugin_companion_chat.reply_state import KEY
from mcp.types import CallToolResult,TextContent
from test_breeze_bridge import Event
import test_commitments as flow
import test_social_autonomy as social_fixture
import test_thinking as thinking_fixture

NOW=flow.NOW+20


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.s=Outcomes(self.root,'owner')

    def fail(self,source='one',now=NOW-600):
        self.s.record('greeting','message','voice_synthesis_failed',source=source,error_type='TypeError',now=now)

    def test_coalesces_real_failures_and_receipt_dedup_survives_restart(self):
        self.fail();self.fail();self.fail('two')
        value=Outcomes(self.root,'owner').snapshot(now=NOW)
        self.assertEqual(len(value),1);self.assertEqual(value[0]['count'],2)
        self.assertEqual(Outcomes(self.root,'other').snapshot(now=NOW),[])

    def test_recovery_keeps_old_obstruction_without_claiming_original_task_done(self):
        self.fail();self.s.recover('greeting','message','platform_accepted',now=NOW-100)
        value=self.s.snapshot(now=NOW)[0]
        self.assertEqual(value['state'],'available_again');self.assertEqual(value['error_type'],'TypeError')
        self.assertIn('后来一次',value['later_evidence'])

    def test_uncertain_notice_submission_never_automatically_repeats(self):
        self.fail();item=self.s.snapshot(proactive=True,now=NOW)[0]
        self.assertTrue(self.s.reserve(item,NOW));self.assertFalse(self.s.reserve(item,NOW))
        self.s.finish(item,'uncertain',NOW)
        self.assertEqual(self.s.snapshot(proactive=True,now=NOW+8000),[])
        self.assertTrue(self.s.snapshot(now=NOW+8000))

    def test_notice_receipt_cannot_mark_a_newer_experience_version_told(self):
        self.fail()
        item = self.s.snapshot(now=NOW)[0]
        self.assertTrue(self.s.reserve(item, NOW))
        self.fail('new_failure', NOW+1)
        self.s.finish(item, 'submitted', NOW+2)
        row = self.s.snapshot(now=NOW+2)[0]
        self.assertIsNone(row['last_said_at'])
        self.assertEqual(row['proactive_state'], 'submitted')

    def test_notice_failures_and_text_receipts_do_not_feed_back_as_outreach_experiences(self):
        self.fail()
        events = OutreachEvidence(self.root, 'owner')
        for stage, result, reason in [('decision','selected',''), ('voice','unavailable','synthesis_failed'),
                                      ('send','submitted','')]:
            events.record(stage, result, kind='outcome_notice', reason=reason, run_id='d'*16, now=NOW-100)
        with patch('time.time', return_value=NOW):
            self.s.import_outreach(events.path)
        rows = self.s.snapshot(now=NOW)
        self.assertEqual([(row['feature'], row['state']) for row in rows], [('greeting', 'open')])

    def test_real_mentions_allow_later_requests_without_forced_permanent_silence(self):
        self.fail();item=self.s.snapshot(now=NOW)[0]
        text='今天本来想来问候你，但语音没准备好'
        self.s.said([{'id':item['id'],'version':item['version'],'quote':text}],text,NOW)
        self.assertIsNotNone(self.s.snapshot(now=NOW)[0]['last_said_at'])
        self.assertFalse(self.s.snapshot(proactive=True,now=NOW+60))
        self.assertTrue(self.s.snapshot(proactive=True,now=NOW+8000))

    def test_wrong_quote_or_changed_version_cannot_claim_notice_was_communicated(self):
        self.fail();item=self.s.snapshot(now=NOW)[0]
        mention={'id':item['id'],'version':item['version'],'quote':'今天本来想问候你但语音卡住'}
        self.s.said([mention],'今天午饭怎么样',NOW)
        self.assertIsNone(self.s.snapshot(now=NOW)[0]['last_said_at'])
        self.fail('new',NOW-10);self.s.said([mention],mention['quote'],NOW)
        self.assertIsNone(self.s.snapshot(now=NOW)[0]['last_said_at'])

    def test_import_uses_execution_records_and_ignores_ticks_without_selected_intent(self):
        events=OutreachEvidence(self.root,'owner')
        events.record('voice','unavailable',reason='synthesis_failed',run_id='a'*16,now=NOW-900)
        events.record('decision','selected',kind='commitment',run_id='b'*16,now=NOW-800)
        events.record('voice','unavailable',kind='commitment',reason='synthesis_failed',error_type='TypeError',run_id='b'*16,now=NOW-700)
        events.record('send','submitted',kind='commitment',run_id='c'*16,now=NOW-100)
        with patch('time.time',return_value=NOW):
            self.s.import_outreach(events.path);self.s.import_outreach(events.path)
        value=self.s.snapshot(now=NOW)
        self.assertEqual(len(value),1);self.assertEqual(value[0]['count'],1)

    def test_raw_error_messages_credentials_and_free_form_fields_are_not_stored(self):
        with self.assertRaises(ValueError):self.s.record('greeting','PRIVATE_TOOL','execution_error')
        self.s.record('phone','operate','execution_error',error_type='token=PRIVATE_SECRET',source='PRIVATE_SOURCE',now=NOW)
        raw=self.s.path.read_bytes()
        self.assertNotIn(b'PRIVATE_SECRET',raw);self.assertNotIn(b'PRIVATE_SOURCE',raw)

    def test_later_greeting_proves_shared_voice_channel_recovered_for_old_outreach(self):
        events=OutreachEvidence(self.root,'owner')
        events.record('decision','selected',run_id='a'*16,now=NOW-900)
        events.record('voice','unavailable',reason='synthesis_failed',error_type='TypeError',run_id='a'*16,now=NOW-800)
        events.record('send','submitted',kind='commitment',run_id='b'*16,now=NOW-100)
        with patch('time.time',return_value=NOW):self.s.import_outreach(events.path)
        row=self.s.snapshot(now=NOW)[0]
        self.assertEqual(row['state'],'available_again');self.assertEqual(row['feature'],'outreach')
        self.assertIn('后来一次',row['later_evidence'])

    def test_older_success_cannot_clear_a_newer_failure(self):
        self.fail(now=NOW-10)
        self.s.recover('greeting','message','platform_accepted',now=NOW-100)
        self.assertEqual(self.s.snapshot(now=NOW)[0]['state'],'open')

    def thinking_run(self, at, *, scope='owner', status='completed', phase='executing', activity='practice'):
        path = self.root/'thinking.sqlite3'
        with sqlite3.connect(path) as db:
            db.execute('CREATE TABLE IF NOT EXISTS runs(scope TEXT,at REAL,status TEXT,phase TEXT,activity TEXT)')
            db.execute('INSERT INTO runs VALUES(?,?,?,?,?)',(scope,at,status,phase,activity))
        return path

    def test_later_completed_decision_recovers_validation_only_and_is_idempotent(self):
        self.s.record('autonomy','activity','model_invalid',now=NOW-500)
        self.s.record('autonomy','activity','execution_timeout',now=NOW-500)
        path = self.thinking_run(NOW-100)
        before = path.read_bytes()
        self.s.import_thinking(path, now=NOW)
        states = {r['reason']:r for r in self.s.snapshot(now=NOW)}
        self.assertEqual(states['model_invalid']['state'], 'available_again')
        self.assertEqual(states['execution_timeout']['state'], 'open')
        self.assertIn('不证明原活动已补做', states['model_invalid']['later_evidence'])
        version = states['model_invalid']['version']
        self.s.import_thinking(path, now=NOW)
        self.assertEqual({r['reason']:r for r in self.s.snapshot(now=NOW)}['model_invalid']['version'], version)
        self.assertEqual(path.read_bytes(), before)

    def test_other_scope_private_thought_running_and_failed_runs_are_not_recovery(self):
        self.s.record('autonomy','activity','growth_action_invalid',now=NOW-500)
        path = self.thinking_run(NOW-100, scope='other')
        self.thinking_run(NOW-90, phase='private_complete', activity='private_reflection')
        self.thinking_run(NOW-80, status='running')
        self.thinking_run(NOW-70, status='invalid_output')
        self.thinking_run(NOW+10)
        self.s.import_thinking(path, now=NOW)
        self.assertEqual(self.s.snapshot(now=NOW)[0]['state'], 'open')

    def test_old_success_does_not_close_new_failure_and_missing_store_is_safe(self):
        self.s.record('autonomy','activity','model_invalid',now=NOW-50)
        path = self.thinking_run(NOW-100)
        self.s.import_thinking(path, now=NOW)
        self.assertEqual(self.s.snapshot(now=NOW)[0]['state'], 'open')
        path.write_text('broken database')
        self.s.import_thinking(path, now=NOW)
        self.s.import_thinking(self.root/'missing.sqlite3', now=NOW)
        self.assertEqual(self.s.snapshot(now=NOW)[0]['state'], 'open')


class ProactiveTests(unittest.IsolatedAsyncioTestCase):
    due=flow.FlowTests.due

    def setUp(self):
        flow.FlowTests.setUp(self)
        self.plugin.state_path = self.root/'companion_active/state.json'
        self.plugin.state_path.parent.mkdir()
        self.preferences = TimedMedium(self.root/'companion_breeze')

    def experience(self, feature='greeting', operation='message', reason='voice_synthesis_failed'):
        with patch('time.time',return_value=NOW):
            store=self.plugin.outcome_ledger()
            store.record(feature,operation,reason,source='test',now=NOW-600)
            return store.snapshot(proactive=True,now=NOW)[0]

    def draft(self, item, text, audit=None):
        evidence = {'unrelated': [], 'supported': True, 'experience_id': item['id'], 'quote': text}
        self.plugin.context.llm_generate.side_effect = [
            NS(completion_text=json.dumps({'send': True, 'message': text, 'instructions': '有些无奈'}, ensure_ascii=False)),
            NS(completion_text=json.dumps(evidence if audit is None else audit, ensure_ascii=False)),
        ]

    async def tell(self,transport=None):
        self.plugin.prepare_voice=AsyncMock(side_effect=AssertionError('Do not depend on failed voice channel'))
        if transport:self.transport.call_action.side_effect=transport
        item=self.experience()
        self.draft(item, '早上想问候你，语音卡住了，真有点烦，你快帮我弄弄。')
        with patch('time.time',return_value=NOW):await self.plugin.tell_outcome(item,self.phone)
        return item

    async def test_voice_failure_notice_can_use_text_with_actual_receipt_and_existing_quota(self):
        await self.tell()
        args=self.transport.call_action.await_args
        self.assertEqual(args.kwargs['message'][0]['type'],'text')
        self.assertIn('你快帮我',args.kwargs['message'][0]['data']['text'])
        self.assertEqual(self.plugin.state['daily_count'],1)
        self.assertEqual(self.plugin.state['unanswered'],1)
        self.assertEqual(self.plugin.state['last_outreach_result']['result'],'submitted_outcome_notice')
        self.assertEqual(self.ledger.rows()[0]['status'],'pending')
        self.plugin.prepare_voice.assert_not_awaited()

    async def test_old_exam_reply_with_boolean_only_approval_cannot_send_or_mark_notice(self):
        manager = self.plugin.context.conversation_manager
        manager.get_conversation.return_value = NS(history='[{"role":"user","content":"我准备去考试了"}]')
        item = self.experience('autonomy', 'activity', 'model_invalid')
        self.draft(item, '考试顺利，去吧。', {'supported': True})
        with patch('time.time', return_value=NOW):
            await self.plugin.tell_outcome(item, self.phone)
        generated, checked = self.plugin.context.llm_generate.await_args_list
        self.assertNotIn('考试', generated.kwargs['prompt'])
        self.assertEqual(json.loads(checked.kwargs['prompt'])['own_experience']['id'], item['id'])
        manager.get_conversation.assert_not_awaited()
        self.transport.call_action.assert_not_awaited()
        self.assertNotIn('daily_count', self.plugin.state)
        self.assertIsNone(self.plugin.outcome_ledger().snapshot(now=NOW)[0]['last_said_at'])

    async def test_isolated_incident_cannot_become_continuous_outage_even_if_checker_agrees(self):
        item=self.experience('autonomy','activity','model_invalid')
        self.draft(item,'我这个操作一直卡着，今天什么都没干。')
        with patch('time.time',return_value=NOW):await self.plugin.tell_outcome(item,self.phone)
        self.transport.call_action.assert_not_awaited()
        self.assertEqual(self.plugin.context.llm_generate.await_count,1)

    async def test_incident_guard_preserves_complaint_and_request_for_help(self):
        item=self.experience('autonomy','activity','model_invalid')
        text='这次决定怎么做时没通过，真烦，你快帮我看看。'
        self.draft(item,text)
        with patch('time.time',return_value=NOW):await self.plugin.tell_outcome(item,self.phone)
        self.transport.call_action.assert_awaited_once()

    async def test_audit_must_quote_the_sent_draft_and_bind_it_to_the_selected_experience(self):
        item = self.experience()
        for identifier, quote in ((item['id'], '语音没弄出来'), ('another', '语音卡了')):
            self.draft(item, '语音卡了', {'unrelated': [], 'supported': True, 'experience_id': identifier, 'quote': quote})
            with patch('time.time', return_value=NOW):
                await self.plugin.tell_outcome(item, self.phone)
        self.transport.call_action.assert_not_awaited()

    async def test_valid_excerpt_does_not_override_unrelated_text_in_the_same_notice(self):
        item = self.experience()
        self.draft(item, '语音卡了。考试顺利，去吧。',
                   {'unrelated': ['考试顺利，去吧。'], 'supported': True, 'experience_id': item['id'], 'quote': '语音卡了'})
        with patch('time.time', return_value=NOW):
            await self.plugin.tell_outcome(item, self.phone)
        self.transport.call_action.assert_not_awaited()

    async def test_effective_voice_preference_reuses_voice_delivery_and_receipt(self):
        item = self.experience()
        self.preferences.save(self.plugin.umo, ('voice', NOW-300, NOW+300), NOW-300, NOW)
        text = '想问候你的语音刚才没弄出来。'
        self.draft(item, text)
        with patch('time.time', return_value=NOW):
            await self.plugin.tell_outcome(item, self.phone)
        self.voice.client.synthesize.assert_called_once_with(text, '有些无奈')
        self.assertEqual(self.transport.call_action.await_args.kwargs['message'][0]['type'], 'record')
        self.assertIsNotNone(self.plugin.outcome_ledger().snapshot(now=NOW)[0]['last_said_at'])

    async def test_voice_preparation_failure_allows_one_text_notice_despite_voice_preference(self):
        item = self.experience('autonomy', 'activity', 'model_invalid')
        self.preferences.save(self.plugin.umo, ('voice', NOW-300, NOW+300), NOW-300, NOW)
        text = '我想自己安排点事，结果卡在决定怎么做那一步了。'
        self.draft(item, text)
        self.voice.client.synthesize.side_effect = RuntimeError('synthetic')
        with patch('time.time', return_value=NOW):
            await self.plugin.tell_outcome(item, self.phone)
        self.voice.client.synthesize.assert_called_once()
        self.transport.call_action.assert_awaited_once()
        self.assertEqual(self.transport.call_action.await_args.kwargs['message'], [{'type': 'text', 'data': {'text': text}}])
        self.assertEqual(self.plugin.state['daily_count'], 1)
        self.assertEqual(self.preferences.current(self.plugin.umo, NOW)[0], 'voice')
        self.assertIsNotNone(self.plugin.outcome_ledger().snapshot(now=NOW)[0]['last_said_at'])

    async def test_uncertain_voice_submission_does_not_fall_back_to_text(self):
        item = self.experience('autonomy', 'activity', 'model_invalid')
        self.preferences.save(self.plugin.umo, ('voice', NOW-300, NOW+300), NOW-300, NOW)
        self.draft(item, '我自己安排的活动卡在决定怎么做那一步了。')
        self.transport.call_action.side_effect = TimeoutError('synthetic')
        with patch('time.time', return_value=NOW):
            await self.plugin.tell_outcome(item, self.phone)
        self.transport.call_action.assert_awaited_once()
        self.assertEqual(self.transport.call_action.await_args.kwargs['message'][0]['type'], 'record')
        row = self.plugin.outcome_ledger().snapshot(now=NOW)[0]
        self.assertEqual(row['proactive_state'], 'uncertain')
        self.assertIsNone(row['last_said_at'])

    async def test_text_preference_takes_precedence_and_expired_text_defaults_to_voice(self):
        item = self.experience('autonomy', 'activity', 'model_invalid')
        self.preferences.save(self.plugin.umo, ('text', NOW-300, NOW+300), NOW-300, NOW)
        self.assertEqual(self.plugin.outcome_medium(item, NOW+300), 'voice')
        self.draft(item, '刚想做点自己的事，决定怎么做的那一步没过。')
        with patch('time.time', return_value=NOW):
            await self.plugin.tell_outcome(item, self.phone)
        self.voice.client.synthesize.assert_not_called()
        self.assertEqual(self.transport.call_action.await_args.kwargs['message'][0]['type'], 'text')

    async def test_voice_preference_change_during_synthesis_blocks_stale_delivery(self):
        item = self.experience('autonomy', 'activity', 'model_invalid')
        self.draft(item, '自己安排的活动卡在决定怎么做那一步了。')
        async def change_preference(*args):
            self.preferences.save(self.plugin.umo, ('text', NOW, NOW+300), NOW, NOW)
            return 'fake_audio'
        self.plugin.prepare_voice = change_preference
        with patch('time.time', return_value=NOW):
            await self.plugin.tell_outcome(item, self.phone)
        self.transport.call_action.assert_not_awaited()
        self.assertNotIn('daily_count', self.plugin.state)

    async def test_missing_send_receipt_does_not_mark_experience_told_or_retry_original(self):
        await self.tell(TimeoutError('synthetic'))
        row=self.plugin.outcome_ledger().snapshot(now=NOW)[0]
        self.assertEqual(row['proactive_state'],'uncertain');self.assertIsNone(row['last_said_at'])
        self.plugin.context.conversation_manager.update_conversation.assert_not_awaited()
        self.assertEqual(self.ledger.rows()[0]['status'],'pending')

    async def test_she_can_choose_not_to_send_and_no_message_quota_is_consumed(self):
        item=self.experience();self.plugin.context.llm_generate.return_value=NS(completion_text='{"send":false}')
        with patch('time.time',return_value=NOW):await self.plugin.tell_outcome(item,self.phone)
        self.transport.call_action.assert_not_awaited();self.assertNotIn('daily_count',self.plugin.state)

    async def test_sleep_pause_unanswered_and_quota_still_gate_proactive_notice(self):
        for state in ({'paused':True},{'unanswered':1},{'day':'2026-11-05','daily_count':4}):
            self.plugin.state={'last_user_at':NOW-10000,'paused':False,'unanswered':0}|state
            self.plugin.tell_outcome=AsyncMock()
            with patch('time.time',return_value=NOW):await self.plugin.consider()
            self.plugin.tell_outcome.assert_not_awaited()
        self.plugin.state={'last_user_at':NOW-10000,'paused':False,'unanswered':0}
        self.rhythm.state['mode']='asleep'
        with patch('time.time',return_value=NOW):await self.plugin.consider()
        self.plugin.tell_outcome.assert_not_awaited()

    async def test_due_commitment_keeps_priority_over_talking_about_an_old_obstruction(self):
        self.plugin.fulfill=AsyncMock();self.plugin.tell_outcome=AsyncMock()
        self.plugin.outcome_context=Mock(return_value=[{'id':'old'}])
        with patch('time.time',return_value=NOW):await self.plugin.consider()
        self.plugin.fulfill.assert_awaited_once();self.plugin.tell_outcome.assert_not_awaited()


class ReplyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.store=Outcomes(self.tmp.name,'qq-companion:FriendMessage:100000001')
        self.store.record('greeting','message','voice_synthesis_failed',source='test')
        self.item=self.store.snapshot()[0];self.event=Event()
        self.active=NS(config={},context=NS(llm_generate=AsyncMock()))
        self.facts={'own_outcomes':[self.item]}

    async def test_complaint_and_request_to_fix_remain_her_words(self):
        text='今天本来想问候你，但语音没弄好，真烦，你快帮我修修'
        self.active.context.llm_generate.return_value=NS(completion_text=json.dumps({'supported':True,'mentions':[{'id':self.item['id'],'quote':text}]},ensure_ascii=False))
        response=NS(role='assistant',tools_call_name=None,completion_text=text,result_chain=None)
        with patch.object(grounding,'evidence',return_value=self.facts):await grounding.audit(self.active,self.event,response)
        self.assertEqual(response.completion_text,text)
        self.assertEqual(self.event.get_extra('companion_outcome_mentions')[0]['id'],self.item['id'])
        self.assertIn('催',RULE);self.assertIn('不知道',RULE)

    async def test_experience_available_does_not_force_disclosure_in_unrelated_reply(self):
        response=NS(role='assistant',tools_call_name=None,completion_text='吃过啦',result_chain=None)
        with patch.object(grounding,'evidence',return_value=self.facts):await grounding.audit(self.active,self.event,response)
        self.active.context.llm_generate.assert_not_awaited()
        self.assertEqual(response.completion_text,'吃过啦')

    async def test_real_obstruction_may_be_mentioned_when_user_is_expressing_feelings(self):
        self.event.message_str='早上没见你，有点想你'
        text='早上本来想问候你，但语音那边出了问题，有点烦'
        self.active.context.llm_generate.return_value=NS(completion_text='{"supported":true,"mentions":[]}')
        response=NS(role='assistant',tools_call_name=None,completion_text=text,result_chain=None)
        with patch.object(grounding,'evidence',return_value=self.facts):await grounding.audit(self.active,self.event,response)
        self.assertEqual(response.completion_text,text)

    async def test_mention_generation_alone_never_means_it_was_communicated(self):
        text='今天本来想来问候你，但语音没准备好'
        mention={'id':self.item['id'],'version':self.item['version'],'quote':text}
        self.event.set_extra('companion_outcome_mentions',[mention]);self.event.set_extra(KEY,'turn')
        self.event.result.chain[0].text=text
        root=object.__new__(CompanionChat);root.active=NS(umo=self.event.unified_msg_origin,outcome_ledger=lambda:self.store)
        root.friends=NS(owner='100000001')
        row={'id':'turn','state':'generated','receipts':'[]'}
        root.reply_state=NS(latest=lambda *a:[row])
        root.outcomes_sent(self.event)
        self.assertIsNone(self.store.snapshot()[0]['last_said_at'])
        row.update(state='submitted',receipts='["receipt"]');root.outcomes_sent(self.event)
        self.assertIsNotNone(self.store.snapshot()[0]['last_said_at'])

    async def test_voice_mention_requires_voice_delivery_receipt(self):
        text='今天本来想问候你，但语音没准备好'
        self.event.set_extra('companion_outcome_mentions',[{'id':self.item['id'],'version':self.item['version'],'quote':text}])
        self.event.set_extra('companion_voice_prepared',True)
        root=object.__new__(CompanionChat);root.active=NS(umo=self.event.unified_msg_origin,outcome_ledger=lambda:self.store);root.friends=NS(owner='100000001')
        root.outcomes_sent(self.event);self.assertIsNone(self.store.snapshot()[0]['last_said_at'])
        self.event.set_extra('companion_voice_delivery_receipt','receipt');self.event.set_extra('companion_voice_delivered_text',text)
        root.outcomes_sent(self.event);self.assertIsNotNone(self.store.snapshot()[0]['last_said_at'])


class ToolReceiptTests(unittest.IsolatedAsyncioTestCase):
    setUp=flow.FlowTests.setUp

    async def test_error_is_retained_without_raw_content_and_plain_return_is_not_recovery(self):
        root=object.__new__(CompanionChat);root.ready=True;root.active=self.plugin;root.friends=NS(owner='100000001')
        event=Event();tool=NS(name='companion_phone_tap')
        error=CallToolResult(content=[TextContent(type='text',text='PRIVATE_SENTINEL')],isError=True)
        await root.collect_grounding_tools(event,tool,{'x':100},error)
        store=self.plugin.outcome_ledger();self.assertEqual(store.snapshot()[0]['state'],'open')
        self.assertNotIn(b'PRIVATE_SENTINEL',store.path.read_bytes())
        plain=CallToolResult(content=[TextContent(type='text',text='没有确认是否成功')])
        await root.collect_grounding_tools(event,tool,{},plain)
        self.assertEqual(store.snapshot()[0]['state'],'open')


class SocialReceiptTests(unittest.IsolatedAsyncioTestCase):
    setUp=social_fixture.SocialTests.setUp
    call=social_fixture.SocialTests.call

    async def test_social_timeout_keeps_uncertain_receipt_and_own_experience(self):
        self.p.report_own_outcome=Mock()
        self.send.side_effect=TimeoutError('PRIVATE_ERROR_CONTENT')
        with self.assertRaises(TimeoutError):await self.call('/post',content='合成公开动态')
        self.assertEqual(self.c.state['actions'][-1]['result'],'uncertain')
        args=self.p.report_own_outcome.call_args
        self.assertEqual(args.args,('social','operate','social_unconfirmed'))
        self.assertEqual(args.kwargs,{'error_type':'TimeoutError'})
        with self.assertRaises(ValueError):await self.call('/post',content='合成公开动态')
        self.send.assert_awaited_once()

    async def test_social_later_api_return_is_only_stage_recovery_evidence(self):
        self.p.report_own_outcome=Mock()
        await self.call('/post',content='合成公开动态')
        self.p.report_own_outcome.assert_called_once_with('social','operate','',recovery='later_same_kind_succeeded')
        self.assertEqual(self.c.state['actions'][-1]['result'],'api_acknowledged')


class ActivityReceiptTests(unittest.IsolatedAsyncioTestCase):
    setUp=thinking_fixture.ThinkingTests.setUp

    async def test_background_timeout_becomes_owned_unknown_cause_experience(self):
        self.a.outcome_callback=Mock()
        self.a._consider=AsyncMock(side_effect=TimeoutError('PRIVATE_ERROR_CONTENT'))
        await self.a.consider()
        self.assertEqual(self.a.outcome_callback.call_args.args,('autonomy','activity','execution_timeout'))
        self.assertNotIn('PRIVATE_ERROR_CONTENT',str(self.a.outcome_callback.call_args))

    async def test_user_interruption_is_not_turned_into_a_fault(self):
        import asyncio
        self.a.outcome_callback=Mock();self.a._consider=AsyncMock(side_effect=asyncio.CancelledError())
        with self.assertRaises(asyncio.CancelledError):await self.a.consider()
        self.a.outcome_callback.assert_not_called()
