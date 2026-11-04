import os
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock
sys.path.insert(0,os.environ.get('COMPANION_TEST_PLUGINS',str(Path(__file__).parent)))
from astrbot_plugin_companion_chat.deliberation import for_dialogue,require_visible_output
from astrbot.core.exceptions import EmptyModelOutputError
from astrbot.api.provider import LLMResponse
from astrbot.api.message_components import Plain,Image
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.agent.runners.tool_loop_agent_runner import ToolLoopAgentRunner
from astrbot.core.provider.entities import ProviderRequest
from test_dialogue_reasoning import provider

class OutputTests(unittest.TestCase):
    def test_reasoning_only_and_selector_only_are_not_visible_replies(self):
        for body in ['', ' ', '[[text]]','[[voice]]']:
            response=LLMResponse(role='assistant',completion_text=body,reasoning_content='synthetic reasoning')
            with self.assertRaises(EmptyModelOutputError):require_visible_output(response)

    def test_text_tools_errors_and_media_are_preserved(self):
        for response in [LLMResponse(role='assistant',completion_text='知道了'),
                         LLMResponse(role='tool',tools_call_name=['read_status']),
                         LLMResponse(role='err',completion_text='error'),
                         LLMResponse(role='assistant',result_chain=MessageChain([Image.fromURL('https://example.test/a.png')]))]:
            self.assertIs(require_visible_output(response),response)

class NativeRetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_reasoning_only_native_response_retries_then_delivers_one_final(self):
        from openai.types.chat import ChatCompletion
        p=provider();p.api_keys=['synthetic'];p.reasoning_key='reasoning_content'
        p.default_params={'model','messages','tool_choice','tools'}
        outputs=[ChatCompletion(id='synthetic1',created=0,model='deepseek-flash',object='chat.completion',
                  choices=[{'index':0,'finish_reason':'stop','message':{'role':'assistant','content':'','reasoning_content':'synthetic reasoning'}}]),
                 ChatCompletion(id='synthetic2',created=0,model='deepseek-flash',object='chat.completion',
                  choices=[{'index':0,'finish_reason':'stop','message':{'role':'assistant','content':'这次有正文'}}])]
        create=AsyncMock(side_effect=outputs);p.client.chat=SimpleNamespace(completions=SimpleNamespace(create=create))
        scoped=for_dialogue(p);runner=ToolLoopAgentRunner()
        await runner.reset(scoped,ProviderRequest(contexts=[{'role':'user','content':'synthetic'}]),SimpleNamespace(),None,None)
        runner.EMPTY_OUTPUT_RETRY_WAIT_MIN_S=.001;runner.EMPTY_OUTPUT_RETRY_WAIT_MAX_S=.001
        results=[r async for r in runner._iter_llm_responses_with_fallback()]
        self.assertEqual(len(results),1);self.assertEqual(results[0].completion_text,'这次有正文')
        self.assertEqual(create.await_count,2)
        self.assertEqual(p.provider_config['custom_extra_body']['thinking']['type'],'disabled')

    async def test_provider_guard_is_scoped_and_keeps_existing_parser(self):
        p=provider();p._parse_openai_completion=AsyncMock(return_value=LLMResponse(role='assistant',reasoning_content='synthetic'))
        original=p._parse_openai_completion;scoped=for_dialogue(p)
        with self.assertRaises(EmptyModelOutputError):await scoped._parse_openai_completion(None,None)
        self.assertIs(p._parse_openai_completion,original)

if __name__=='__main__':unittest.main()
