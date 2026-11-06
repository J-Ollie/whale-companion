"""Factual checks preserve conversation without releasing unverified operations."""
import json
import os
from pathlib import Path
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock,patch

sys.path.insert(0,os.environ.get('COMPANION_TEST_PLUGINS',str(Path(__file__).parent)))
from astrbot_plugin_companion_chat import grounding


class DialogueTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.extra={}
        self.event=SimpleNamespace(message_str='感觉空落落的，少了点什么，毕竟之前你每天都来找我',
            get_extra=lambda key,default=None:self.extra.get(key,default),set_extra=lambda key,value:self.extra.__setitem__(key,value))
        self.active=SimpleNamespace(config={},context=SimpleNamespace(llm_generate=AsyncMock()))
        self.evidence=patch.object(grounding,'evidence',return_value={'commitments':[]});self.evidence.start()
    def tearDown(self):self.evidence.stop()
    def response(self,text):return SimpleNamespace(role='assistant',tools_call_name=None,completion_text=text,result_chain=None)

    async def test_plain_affection_is_not_replaced_or_audited(self):
        response=self.response('原来你也习惯我来找你了呀')
        await grounding.audit(self.active,self.event,response)
        self.assertEqual(response.completion_text,'原来你也习惯我来找你了呀')
        self.active.context.llm_generate.assert_not_awaited()

    async def test_unrecorded_candidate_trials_cannot_use_prior_self_claim_as_proof(self):
        self.event.message_str = '名字想好了没，别一直说在想'
        self.extra['companion_grounding_context'] = [{'role':'assistant','text':'我试过几个了'}]
        self.active.context.llm_generate.return_value = SimpleNamespace(completion_text='{"supported":true}')
        response = self.response('还没有，试过几个都不太顺口')
        await grounding.audit(self.active, self.event, response)
        self.active.context.llm_generate.assert_awaited_once()
        self.assertIn('曾试过', self.active.context.llm_generate.await_args.kwargs['system_prompt'])

    async def test_current_thought_is_allowed_without_claiming_background_work(self):
        response = self.response('我想叫岚，你觉得呢')
        await grounding.audit(self.active, self.event, response)
        self.assertEqual(response.completion_text, '我想叫岚，你觉得呢')
        self.active.context.llm_generate.assert_not_awaited()

    async def test_supported_current_feeling_keeps_original_words(self):
        self.active.context.llm_generate.return_value=SimpleNamespace(completion_text='{"supported":true}')
        response=self.response('所以你是有点想我了呀')
        await grounding.audit(self.active,self.event,response)
        self.assertEqual(response.completion_text,'所以你是有点想我了呀')

    async def test_failure_preserves_safe_original_clause(self):
        self.active.context.llm_generate.side_effect=TimeoutError('isolated')
        response=self.response('我起晚了，你这么说我也有点在意。')
        await grounding.audit(self.active,self.event,response)
        self.assertIn('你这么说我也有点在意',response.completion_text)
        self.assertNotIn('起晚',response.completion_text);self.assertNotIn('核实',response.completion_text)
        self.assertEqual(self.active.context.llm_generate.await_count,1)

    async def test_rejected_promise_keeps_contextual_safe_alternative(self):
        safe='少了平时那声招呼，确实会有点空。'
        self.active.context.llm_generate.side_effect=[
            SimpleNamespace(completion_text=json.dumps({'supported':False,'text':'明天我一定发消息','safe_text':safe},ensure_ascii=False)),
            SimpleNamespace(completion_text='{"supported":false}')]
        response=self.response('以后我一定每天找你')
        await grounding.audit(self.active,self.event,response)
        self.assertEqual(response.completion_text,safe)
        self.assertLessEqual(self.active.context.llm_generate.await_count,2)

    async def test_unsafe_alternative_cannot_smuggle_a_promise_or_progress(self):
        self.active.context.llm_generate.side_effect=[
            SimpleNamespace(completion_text='{"supported":false,"text":"明天一定发","safe_text":"我找到乐谱了"}'),
            SimpleNamespace(completion_text='{"supported":false,"safe_text":"明早我会联系你"}')]
        response=self.response('明早一定发给你')
        await grounding.audit(self.active,self.event,response)
        self.assertNotIn('明早',response.completion_text);self.assertNotIn('找到',response.completion_text)
        self.assertNotIn('核实',response.completion_text)

    async def test_fact_check_cannot_replace_emotional_turn_with_audit_speech(self):
        self.active.context.llm_generate.side_effect=[
            SimpleNamespace(completion_text='{"supported":false,"text":"这件事尚未核实，不能保证。","safe_text":"平时那声招呼，对你挺重要的呀。"}')]
        response=self.response('以后我一定每天找你')
        await grounding.audit(self.active,self.event,response)
        self.assertEqual(response.completion_text,'平时那声招呼，对你挺重要的呀。')
        self.assertEqual(self.active.context.llm_generate.await_count,1)

    async def test_initial_audit_speech_in_a_feeling_turn_is_repaired(self):
        self.active.context.llm_generate.return_value=SimpleNamespace(completion_text='{"text":"平时那声招呼，对你挺重要的呀。"}')
        response=self.response('这件事我还没核实，不能随便给你一个原因或保证。')
        await grounding.audit(self.active,self.event,response)
        self.assertEqual(response.completion_text,'平时那声招呼，对你挺重要的呀。')
        self.assertEqual(self.active.context.llm_generate.await_count,1)

    async def test_explicit_diagnostic_question_keeps_necessary_uncertainty(self):
        self.event.message_str='请核对记录，我虽然有点空落，但想先看日志'
        response=self.response('这件事我还没核实，不能保证。')
        await grounding.audit(self.active,self.event,response)
        self.assertEqual(response.completion_text,'这件事我还没核实，不能保证。')
        self.active.context.llm_generate.assert_not_awaited()

    async def test_actorless_success_claim_is_not_a_safe_alternative(self):
        self.assertEqual(grounding.safe_dialogue('消息发送成功。'),'')

    async def test_available_second_call_repairs_instead_of_reporting_internal_failure(self):
        self.active.context.llm_generate.side_effect=[TimeoutError('isolated'),
            SimpleNamespace(completion_text='{"text":"原来平时那声招呼对你挺重要。"}')]
        response=self.response('我忘记发了')
        await grounding.audit(self.active,self.event,response)
        self.assertEqual(response.completion_text,'原来平时那声招呼对你挺重要。')
        self.assertEqual(self.active.context.llm_generate.await_count,2)
        self.assertLessEqual(sum(c.kwargs['max_tokens'] for c in self.active.context.llm_generate.await_args_list),730)

    async def test_voice_choice_is_preserved_on_safe_repair(self):
        self.active.context.llm_generate.side_effect=TimeoutError('isolated')
        response=self.response('[[voice]]'+json.dumps({'text':'我起晚了，你这么说我也有点在意。','instructions':'自然，轻声'},ensure_ascii=False))
        await grounding.audit(self.active,self.event,response)
        decoded=grounding.decode_reply(response.completion_text)
        self.assertEqual(decoded.choice,'voice');self.assertIn('有点在意',decoded.text)

    async def test_actual_confirmed_operation_is_not_replaced(self):
        self.active.context.llm_generate.return_value=SimpleNamespace(completion_text='{"supported":true}')
        response=self.response('刚才那条已经发出了')
        await grounding.audit(self.active,self.event,response)
        self.assertEqual(response.completion_text,'刚才那条已经发出了')

    async def test_total_provider_failure_does_not_release_claim_or_fixed_audit_speech(self):
        self.active.context.llm_generate.side_effect=TimeoutError('isolated')
        response=self.response('明早一定发给你')
        await grounding.audit(self.active,self.event,response)
        self.assertNotIn('明早',response.completion_text);self.assertNotIn('核实',response.completion_text)
        self.assertNotIn('保证',response.completion_text)
        self.assertEqual(self.active.context.llm_generate.await_count,2)


if __name__=='__main__':unittest.main()
