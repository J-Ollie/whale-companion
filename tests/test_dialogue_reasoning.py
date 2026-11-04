"""Request isolation, native serialization and lifecycle checks, no API calls."""
import asyncio
import copy
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock

sys.path.insert(0, os.environ.get('COMPANION_TEST_PLUGINS', str(Path(__file__).parent)))
from astrbot.core.provider.entities import ProviderRequest
from astrbot.core.provider.sources.openai_source import ProviderOpenAIOfficial
from astrbot.core.agent.runners.tool_loop_agent_runner import ToolLoopAgentRunner
from astrbot_plugin_companion_chat.deliberation import REQUEST_MARKER, for_dialogue, install
from astrbot_plugin_companion_chat.main import CompanionChat


def provider():
    p = ProviderOpenAIOfficial.__new__(ProviderOpenAIOfficial)
    p.provider_config = {'id': 'deepseek/deepseek-flash', 'model': 'deepseek-flash',
                         'max_context_tokens': 16384, 'modalities': ['text', 'tool_use'],
                         'custom_extra_body': {'thinking': {'type': 'disabled'}, 'max_tokens': 2048}}
    p.client = SimpleNamespace(base_url=SimpleNamespace(host='api.deepseek.com'))
    p.get_model = lambda: p.provider_config['model']
    return p


class IsolationTests(unittest.TestCase):
    def test_provider_and_limits_are_not_mutated(self):
        p = provider(); before = copy.deepcopy(p.provider_config)
        scoped = for_dialogue(p)
        self.assertIsNot(scoped, p)
        self.assertIs(scoped.client, p.client)
        self.assertEqual(p.provider_config, before)
        self.assertEqual(scoped.provider_config['custom_extra_body'],
                         {'thinking': {'type': 'enabled'}, 'reasoning_effort': 'low', 'max_tokens': 2048})
        self.assertEqual(scoped.provider_config['max_context_tokens'], 16384)
        scoped.provider_config['modalities'].append('image')
        self.assertNotIn('image', p.provider_config['modalities'])

    def test_other_provider_model_endpoint_are_untouched(self):
        for field, value in [('id', 'other'), ('model', 'deepseek-reasoner')]:
            p = provider(); p.provider_config[field] = value
            self.assertIs(for_dialogue(p), p)
        p = provider(); p.client.base_url.host = 'proxy.example'
        self.assertIs(for_dialogue(p), p)
        p = provider(); self.assertIs(for_dialogue(p, 'different-model'), p)
        other = SimpleNamespace(provider_config=p.provider_config)
        self.assertIs(for_dialogue(other), other)

    def test_native_reasoning_and_tool_round_serialization(self):
        from astrbot.core.agent.message import Message, TextPart, ThinkPart
        p = provider()
        payload = {'model': 'deepseek-flash', 'messages': [
            Message(role='assistant', content=[ThinkPart(think='synthetic-private-reasoning'),
                    TextPart(text='visible answer')]).model_dump(),
            {'role': 'assistant', 'content': None, 'reasoning_content': 'tool reasoning',
             'tool_calls': [{'id': 'test', 'type': 'function', 'function': {'name': 'test', 'arguments': '{}'}}]},
            {'role': 'tool', 'tool_call_id': 'test', 'content': 'result'},
            {'role': 'assistant', 'content': 'old nonthinking answer'}]}
        p._finally_convert_payload(payload)
        rows = payload['messages']
        self.assertEqual(rows[0]['reasoning_content'], 'synthetic-private-reasoning')
        self.assertNotIn('synthetic-private-reasoning', str(rows[0]['content']))
        self.assertEqual(rows[1]['reasoning_content'], 'tool reasoning')
        self.assertEqual(rows[2]['tool_call_id'], 'test')
        self.assertEqual(rows[3]['reasoning_content'], '')


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_concurrent_native_runners_and_background_keep_separate_configs(self):
        restore = install(); self.addCleanup(restore)
        p = provider(); compressor = provider()
        async def make(marked):
            runner = ToolLoopAgentRunner(); request = ProviderRequest()
            if marked: setattr(request, REQUEST_MARKER, True)
            await runner.reset(p, request, SimpleNamespace(), None, None, llm_compress_provider=compressor)
            await asyncio.sleep(0)
            return runner
        marked, background, second = await asyncio.gather(make(True), make(False), make(True))
        self.assertIs(background.provider, p)
        self.assertIsNot(marked.provider, second.provider)
        self.assertIs(marked.llm_compress_provider, compressor)
        self.assertEqual(marked.provider.provider_config['custom_extra_body']['thinking']['type'], 'enabled')
        self.assertEqual(p.provider_config['custom_extra_body']['thinking']['type'], 'disabled')

    async def test_fallback_preserves_other_models_and_unmarked_requests(self):
        restore = install(); self.addCleanup(restore)
        primary = provider(); primary.provider_config['id'] = 'other'
        fallback = provider(); request = ProviderRequest(); setattr(request, REQUEST_MARKER, True)
        runner = ToolLoopAgentRunner()
        await runner.reset(provider=primary, request=request, run_context=SimpleNamespace(),
                           tool_executor=None, agent_hooks=None, fallback_providers=[fallback])
        self.assertIs(runner.provider, primary)
        self.assertIsNot(runner.fallback_providers[0], fallback)
        self.assertEqual(fallback.provider_config['custom_extra_body']['thinking']['type'], 'disabled')

    async def test_reload_restore_does_not_overwrite_a_later_wrapper(self):
        original = ToolLoopAgentRunner.reset; restore = install()
        installed = ToolLoopAgentRunner.reset
        try:
            later = AsyncMock(); ToolLoopAgentRunner.reset = later
            restore(); self.assertIs(ToolLoopAgentRunner.reset, later)
            ToolLoopAgentRunner.reset = installed
            restore(); self.assertIs(ToolLoopAgentRunner.reset, original)
        finally: ToolLoopAgentRunner.reset = original

    async def test_only_verified_awake_noncommand_requests_are_marked(self):
        for allowed, sleeping, enabled, text, expected in [
            (True, False, True, '你好', True), (False, False, True, '你好', False),
            (True, True, True, '你好', False), (True, False, False, '你好', False),
            (True, False, True, '/help', False)]:
            with self.subTest(allowed=allowed, sleeping=sleeping, enabled=enabled, text=text):
                chat = CompanionChat.__new__(CompanionChat)
                chat.ready = True; chat.config = {'dialogue_low_reasoning': enabled}
                chat.friends = SimpleNamespace(sleeping=lambda: sleeping)
                chat.batch = SimpleNamespace(allowed=lambda _: allowed, task_truth_hint=AsyncMock())
                chat.materials = SimpleNamespace(inject=Mock())
                req = ProviderRequest(); event = SimpleNamespace(message_str=text, set_extra=Mock())
                await chat.task_truth_hint(event, req)
                self.assertEqual(getattr(req, REQUEST_MARKER, False), expected)

    async def test_native_provider_outgoing_body_uses_scoped_settings(self):
        from openai.types.chat import ChatCompletion
        p = provider(); scoped = for_dialogue(p)
        out = ChatCompletion(id='synthetic', created=0, model='deepseek-flash', object='chat.completion',
                             choices=[{'index':0, 'finish_reason':'stop', 'message':{'role':'assistant','content':'ok'}}])
        scoped.default_params = {'messages', 'model'}
        create = AsyncMock(return_value=out)
        scoped.client.chat = SimpleNamespace(completions=SimpleNamespace(create=create))
        scoped._parse_openai_completion = AsyncMock()
        # Real adapter performs encoding and parsing; no network is involved.
        await scoped._query({'model':'deepseek-flash','messages':[{'role':'user','content':'synthetic'}]}, None, request_max_retries=1)
        self.assertEqual(create.call_args.kwargs['extra_body']['reasoning_effort'], 'low')
        self.assertEqual(create.call_args.kwargs['extra_body']['max_tokens'], 2048)
        self.assertEqual(p.provider_config['custom_extra_body']['thinking']['type'], 'disabled')


if __name__ == '__main__': unittest.main()
