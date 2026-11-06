"""Message commitments, real wake events, voice receipts and evidence boundaries."""
import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch
from datetime import datetime

sys.path.insert(0, os.environ.get('COMPANION_TEST_PLUGINS', str(Path(__file__).parent)))
from astrbot_plugin_companion_chat.commitments import Commitments, TZ
from astrbot_plugin_companion_chat.active.main import CompanionActive, eligible, apply_control, OutreachRejected
from astrbot_plugin_companion_chat import grounding
from astrbot_plugin_companion_chat.breeze.performance import decode_reply
from astrbot.api.provider import LLMResponse
from astrbot.core.agent.message import Message, TextPart
from test_breeze_bridge import Event

NOW = datetime(2026, 11, 5, 6, tzinfo=TZ).timestamp()


def operation(trigger='wake', repeat='daily', quote='以后你醒来就给我发早安', **kw):
    return {'ops': [dict(op='upsert', id='', quote=quote, title='醒来问候', message='向管理员问早',
                         trigger=trigger, repeat=repeat,
                         not_before=datetime.fromtimestamp(NOW, TZ).isoformat(), deadline='', **kw)]}


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.ledger = Commitments(self.root, 'owner'); self.addCleanup(lambda: self.ledger.db.close())

    def add(self, value=None, source='1'):
        value = value or operation(); quote = value['ops'][0]['quote']
        return self.ledger.apply(value, quote, source, NOW)['receipts'][0]['id']

    def test_new_wake_only_not_deploy_or_rest(self):
        self.add()
        self.assertEqual(self.ledger.due(NOW+60, NOW-3600), [])
        self.assertEqual(self.ledger.due(NOW+60, NOW), [])
        self.assertEqual(self.ledger.due(NOW+60, None), [])
        self.assertEqual(len(self.ledger.due(NOW+61, NOW+60)), 1)

    def test_daily_once_even_multiple_wakes_and_reload(self):
        self.add(); row = self.ledger.due(NOW+100, NOW+60)[0]
        self.assertTrue(self.ledger.reserve(row, NOW+100)); self.ledger.finish(row, 'submitted', '101')
        fresh = Commitments(self.root, 'owner'); self.addCleanup(fresh.db.close)
        self.assertEqual(fresh.due(NOW+3600, NOW+1800), [])
        self.assertEqual(len(fresh.due(NOW+86500, NOW+86460)), 1)

    def test_unknown_send_not_replayed_after_restart(self):
        self.add(); row = self.ledger.due(NOW+100, NOW+60)[0]
        self.assertTrue(self.ledger.reserve(row, NOW+100))
        fresh = Commitments(self.root, 'owner'); self.addCleanup(fresh.db.close)
        self.assertEqual(fresh.due(NOW+1000, NOW+60), [])
        self.assertEqual(fresh.evidence()[0]['attempts'][0]['status'], 'uncertain')

    def test_cancel_is_scoped_and_prevents_reserved_send(self):
        identifier = self.add(); row = self.ledger.due(NOW+100, NOW+60)[0]
        self.ledger.apply({'ops':[{'op':'cancel','id':identifier,'quote':'取消早安'}]}, '取消早安', '2', NOW+120)
        self.assertFalse(self.ledger.reserve(row, NOW+121)); self.assertEqual(self.ledger.due(NOW+200, NOW+60), [])
        other = Commitments(self.root, 'other'); self.addCleanup(other.db.close)
        with self.assertRaises(ValueError):other.apply({'ops':[{'op':'cancel','id':identifier,'quote':'取消'}]}, '取消', 'x', NOW+120)

    def test_input_idempotency(self):
        self.add(); self.add()
        self.assertEqual(len(self.ledger.rows()), 1)

    def test_media_preference_does_not_create_task_or_unnecessary_question(self):
        result=self.ledger.apply({'intent':'none','ops':[],'needs_clarification':'请确认是否每天早安'},'以后问候用语音','medium',NOW)
        self.assertEqual(result['needs_clarification'],'')
        self.assertEqual(self.ledger.rows(),[])

    def test_unanchored_quote_invalid_target_or_schedule_rejected_atomically(self):
        for mutate in (lambda o:o.update(quote='别人的话'), lambda o:o.update(id='other'),
                       lambda o:o.update(not_before='2026-11-05T06:00:00'),
                       lambda o:o.update(trigger='phone'), lambda o:o.update(repeat='hourly')):
            value = operation(); mutate(value['ops'][0])
            with self.assertRaises(ValueError):self.ledger.apply(value, '以后你醒来就给我发早安', '1', NOW)
        self.assertEqual(self.ledger.rows(), [])

    def test_expired_oneoff_not_completed_and_not_sent_late(self):
        value = operation(repeat='once'); value['ops'][0]['deadline'] = datetime.fromtimestamp(NOW+3600, TZ).isoformat()
        self.add(value)
        self.assertEqual(self.ledger.due(NOW+3601, NOW+2000), [])
        self.assertEqual(self.ledger.rows()[0]['status'], 'missed')

    def test_blocked_task_survives_and_exposes_actual_reason(self):
        self.add(); row = self.ledger.due(NOW+100, NOW+60)[0]
        self.ledger.block(row, 'owner_do_not_disturb', NOW+100, 600)
        self.assertEqual(self.ledger.due(NOW+200, NOW+60), [])
        self.assertEqual(len(self.ledger.due(NOW+701, NOW+60)), 1)
        self.assertEqual(self.ledger.evidence()[0]['last_reason'], 'owner_do_not_disturb')

    def test_at_once_and_daily_dont_replay_missed_days(self):
        value = operation(trigger='at', repeat='daily'); self.add(value)
        rows = self.ledger.due(NOW+3*86400+60)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['occurrence'], '2026-11-08')
        self.assertTrue(self.ledger.reserve(rows[0], NOW+3*86400+60))
        self.ledger.finish(rows[0], 'submitted', 'ok')
        self.assertEqual(self.ledger.due(NOW+3*86400+120), [])


class GateTests(unittest.TestCase):
    def test_awake_anytime_removes_only_hour_limit(self):
        state = {'last_user_at':NOW-10000}
        cfg = {'awake_anytime':True}
        self.assertTrue(eligible(state, NOW, cfg))
        self.assertFalse(eligible(state, NOW, {}))
        for extra in ({'paused':True}, {'snooze_until':NOW+1}, {'unanswered':1},
                      {'day':'2026-11-05','daily_count':4}, {'last_bot_at':NOW-10}):
            self.assertFalse(eligible(state|extra, NOW, cfg))

    def test_goodnight_not_implicit_nine_am_block_but_explicit_dnd_remains(self):
        state = {}
        self.assertIsNone(apply_control(state, '晚安，期待明天问候', NOW, True))
        self.assertNotIn('snooze_until', state)
        self.assertEqual(apply_control(state, '不要吵醒我', NOW, True), 'sleep')
        self.assertGreater(state['snooze_until'], NOW)
        self.assertEqual(apply_control(state, '忙，别打扰我', NOW, True), 'busy')


class FlowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.ledger = Commitments(self.root, 'qq-companion:FriendMessage:100000001'); self.addCleanup(self.ledger.db.close)
        self.plugin = object.__new__(CompanionActive)
        self.plugin.config = {'awake_anytime':True}
        self.plugin.state_path = self.root/'state.json'
        self.plugin.state = {'last_user_at':NOW-10000,'paused':False,'unanswered':0}
        self.plugin.commitments = self.ledger
        self.rhythm = SimpleNamespace(healthy=True, advance=Mock(), state={'mode':'awake','awake_since':NOW+10,'updated_at':NOW+20})
        self.phone = SimpleNamespace(autonomy=SimpleNamespace(rhythm=self.rhythm), outreach_allowed=lambda:True,
                                     outreach_sent=Mock(), recent_activity=lambda:'')
        self.voice = SimpleNamespace(config={'enabled':True}, busy=False, client=SimpleNamespace(synthesize=Mock(return_value=b'ID3'+b'x'*200)))
        self.transport = SimpleNamespace(call_action=AsyncMock(return_value={'message_id':123}))
        def lookup(name):
            values={'astrbot_plugin_companion_phone':self.phone,'astrbot_plugin_companion_breeze':self.voice}
            return SimpleNamespace(activated=True,star_cls=values[name]) if name in values else None
        manager = SimpleNamespace(get_curr_conversation_id=AsyncMock(return_value='c'),
                                  get_conversation=AsyncMock(return_value=SimpleNamespace(history='[]')),
                                  update_conversation=AsyncMock())
        self.plugin.context = SimpleNamespace(get_registered_star=lookup, get_platform_inst=lambda _:SimpleNamespace(get_client=lambda:self.transport),
            get_config=lambda **kw:{'provider_settings':{'enable':True},'platform_settings':{}},
            conversation_manager=manager, persona_manager=SimpleNamespace(get_persona=AsyncMock(return_value=SimpleNamespace(system_prompt=''))),
            llm_generate=AsyncMock(return_value=SimpleNamespace(completion_text='{"send":true,"message":"早呀","instructions":"自然亲近"}')))
        self.ledger.apply(operation(), operation()['ops'][0]['quote'], '1', NOW)

    def due(self):return self.ledger.due(NOW+20, NOW+10)[0]

    async def test_actual_due_path_sends_voice_and_requires_receipt(self):
        with patch('astrbot_plugin_companion_chat.active.main.time.time', return_value=NOW+20):
            await self.plugin.fulfill(self.due(), self.phone, {})
        args = self.transport.call_action.await_args
        self.assertEqual(args.args, ('send_private_msg',))
        self.assertEqual(args.kwargs['message'][0]['type'], 'record')
        self.assertTrue(args.kwargs['message'][0]['data']['file'].startswith('base64://'))
        self.assertEqual(self.ledger.evidence()[0]['attempts'][0]['status'], 'submitted')
        self.assertEqual(self.plugin.state['daily_count'], 1)
        self.assertEqual(self.plugin.context.conversation_manager.update_conversation.await_args.kwargs['history'][-1]['content'], '早呀')

    async def test_voice_failure_keeps_pending_without_text_or_send_count(self):
        self.voice.client.synthesize.side_effect=RuntimeError('synthetic')
        with patch('astrbot_plugin_companion_chat.active.main.time.time', return_value=NOW+20):
            await self.plugin.fulfill(self.due(), self.phone, {})
        self.transport.call_action.assert_not_awaited()
        self.assertEqual(self.ledger.rows()[0]['status'], 'pending')
        self.assertEqual(self.ledger.rows()[0]['last_reason'], 'voice_unavailable')
        self.assertNotIn('daily_count', self.plugin.state)

    async def test_sleep_during_tts_aborts_send_without_consuming_message_slot(self):
        def synth(*args):self.rhythm.state['mode']='asleep';return b'ID3'+b'x'*200
        self.voice.client.synthesize.side_effect=synth
        with patch('astrbot_plugin_companion_chat.active.main.time.time', return_value=NOW+20):
            await self.plugin.fulfill(self.due(), self.phone, {})
        self.transport.call_action.assert_not_awaited(); self.assertNotIn('daily_count', self.plugin.state)

    async def test_unknown_transport_never_marks_submitted_or_retries(self):
        self.transport.call_action.side_effect=TimeoutError('synthetic')
        with patch('astrbot_plugin_companion_chat.active.main.time.time', return_value=NOW+20):
            await self.plugin.fulfill(self.due(), self.phone, {})
        self.assertEqual(self.ledger.evidence()[0]['attempts'][0]['status'], 'uncertain')
        self.assertEqual(self.ledger.due(NOW+1000, NOW+10), [])
        self.plugin.context.conversation_manager.update_conversation.assert_not_awaited()

    async def test_missing_or_rejected_receipt_cannot_count_as_success(self):
        self.transport.call_action.return_value={'status':'ok','data':{}}
        with self.assertRaises(RuntimeError):await self.plugin.send_voice('synthetic', '早呀')
        self.transport.call_action.return_value={'status':'failed','retcode':100}
        with self.assertRaises(OutreachRejected):await self.plugin.send_voice('synthetic', '早呀')

    async def test_browsing_is_awake_but_rest_sleep_and_broken_state_are_not(self):
        for mode, expected in [('awake',True),('browsing',True),('resting',False),('asleep',False)]:
            self.rhythm.state['mode']=mode; self.assertEqual(self.plugin.awake(), expected)
        self.rhythm.healthy=False;self.assertFalse(self.plugin.awake())

    async def test_fabricated_reason_replaced_before_voice_and_history(self):
        self.plugin.context.llm_generate.return_value=SimpleNamespace(completion_text='{"supported":false,"text":"我没做到，原因还不能确认。"}')
        event=Event();event.message_str='我还等着你的问候呢'
        raw='[[voice]]'+json.dumps({'text':'早，我起晚了点，让你等着了','instructions':'自然'},ensure_ascii=False)
        response=LLMResponse(role='assistant',completion_text=raw)
        await grounding.audit(self.plugin,event,response)
        self.assertNotIn('起晚',decode_reply(response.completion_text).text)
        self.assertEqual(decode_reply(response.completion_text).choice,'voice')
        final=decode_reply(response.completion_text).text;response.completion_text=final
        history=SimpleNamespace(messages=[Message(role='assistant',content=[TextPart(text=raw)])])
        grounding.sanitize(event,history,response)
        self.assertEqual(history.messages[-1].content[0].text,final)

    async def test_audit_failure_withholds_risky_claim_and_plain_greeting_no_call(self):
        event=Event();event.message_str='早'
        await grounding.audit(self.plugin,event,LLMResponse(role='assistant',completion_text='早呀'))
        self.plugin.context.llm_generate.assert_not_awaited()
        self.plugin.context.llm_generate.side_effect=TimeoutError('synthetic')
        response=LLMResponse(role='assistant',completion_text='我忘记发了')
        await grounding.audit(self.plugin,event,response)
        self.assertNotIn('忘记',response.completion_text)

    async def test_rewrite_cannot_smuggle_an_unregistered_promise(self):
        event=Event();event.message_str='明早问候我'
        self.plugin.context.llm_generate.side_effect=[
            SimpleNamespace(completion_text='{"supported":false,"text":"明早我会给你发消息，语音不保证"}'),
            SimpleNamespace(completion_text='{"supported":false}')]
        response=LLMResponse(role='assistant',completion_text='明早一定发给你')
        await grounding.audit(self.plugin,event,response)
        self.assertNotIn('明早',response.completion_text)
        self.assertNotIn('还没核实',response.completion_text)
        self.assertEqual(self.plugin.context.llm_generate.await_count,2)

    async def test_verified_correction_keeps_the_actual_words(self):
        event=Event();event.message_str='怎么没发'
        self.plugin.context.llm_generate.side_effect=[
            SimpleNamespace(completion_text='{"supported":false,"text":"我没做到，原因还不能确认。"}'),
            SimpleNamespace(completion_text='{"supported":true}')]
        response=LLMResponse(role='assistant',completion_text='我起晚了')
        await grounding.audit(self.plugin,event,response)
        self.assertEqual(response.completion_text,'我没做到，原因还不能确认。')

    async def test_implicit_acceptance_uses_context_and_source_dates_are_preserved(self):
        self.plugin.context.llm_generate.return_value=SimpleNamespace(completion_text='{"ops":[],"needs_clarification":""}')
        event=Event();event.message_str='好啊';event.message_obj=SimpleNamespace(message_id='ack',raw_message={'time':NOW-86400,'message':[{'type':'text','data':{'text':'好啊'}}]})
        await self.ledger.prepare(self.plugin.context,'p',event,SimpleNamespace(contexts=[{'role':'assistant','content':'明早给你发问候，可以吗'}]))
        args=self.plugin.context.llm_generate.await_args.kwargs
        payload=json.loads(args['prompt'])
        self.assertEqual(payload['source_times'][0]['at'],'2026-11-04T06:00:00+08:00')

    async def test_extractor_failure_cannot_claim_registered(self):
        self.plugin.context.llm_generate.side_effect=TimeoutError('synthetic')
        event=Event();event.message_str='明早提醒我';event.message_obj=SimpleNamespace(message_id='x')
        result=await self.ledger.prepare(self.plugin.context,'p',event,SimpleNamespace(contexts=[]))
        self.assertFalse(result['receipts']);self.assertIn('未能可靠登记',result['needs_clarification'])

    async def test_due_commitment_respects_existing_quota_and_records_block(self):
        self.plugin.state.update(day='2026-11-05',daily_count=4)
        with patch('astrbot_plugin_companion_chat.active.main.time.time',return_value=NOW+20):
            await self.plugin.consider()
        self.plugin.context.llm_generate.assert_not_awaited()
        self.assertEqual(self.ledger.rows()[0]['last_reason'],'daily_limit')

    async def test_spontaneous_path_also_delivers_only_voice(self):
        # Keep the existing wake rule from being due in this unrelated branch.
        self.rhythm.state['awake_since']=NOW-1000
        manager=self.plugin.context.conversation_manager
        manager.get_conversation.return_value=SimpleNamespace(history='[{"role":"user","content":"昨天聊过"}]')
        goals=SimpleNamespace(context=lambda value:[],offers=lambda *args:[],feedback_context=lambda:[])
        self.plugin.goal_ledger=lambda:goals
        self.plugin.context.llm_generate.return_value=SimpleNamespace(completion_text='{"send":true,"message":"在干嘛","exploration_id":""}')
        with patch('astrbot_plugin_companion_chat.active.main.time.time',return_value=NOW+20):
            await self.plugin.consider()
        args=self.transport.call_action.await_args
        self.assertEqual(args.kwargs['message'][0]['type'],'record')
        self.assertEqual(self.plugin.state['last_outreach_result']['result'],'submitted_voice')


if __name__ == '__main__':unittest.main()
