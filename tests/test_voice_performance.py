"""Unified voice delivery with mocked synthesis/transport and real AstrBot hooks."""
import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, AsyncMock, MagicMock, patch

sys.path.insert(0, os.environ.get('COMPANION_TEST_PLUGINS', str(Path(__file__).parent)))
from astrbot_plugin_companion_chat.breeze.client import BreezeClient, VOICE_SETTINGS, preference
from astrbot_plugin_companion_chat.breeze.performance import decode_reply, clean_instructions
from astrbot_plugin_companion_memory.main import CompanionMemory
from astrbot.core.astr_agent_hooks import MainAgentHooks
from astrbot.core.agent.message import Message, TextPart
from astrbot.core.agent.runners.tool_loop_agent_runner import ToolLoopAgentRunner
from astrbot.api.provider import LLMResponse
from astrbot.api.message_components import Plain, Record
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.pipeline.respond.stage import RespondStage
import test_chat_unified as unified_fixture
from test_breeze_bridge import Event

TEXT = '你终于来了，我还以为你不来了。'
DIRECTION = '等了很久，要直接表达不满。重读“终于”，后句带不耐烦，句尾硬落，不撒娇。'
AUDIO = b'ID3' + b'x' * 200


def output(text=TEXT, instructions=DIRECTION):
    return '[[voice]]' + json.dumps({'text': text, 'instructions': instructions}, ensure_ascii=False)


class DecodeTests(unittest.TestCase):
    def test_voice_separates_words_and_direction(self):
        value = decode_reply(output())
        self.assertEqual((value.choice, value.text, value.instructions), ('voice', TEXT, DIRECTION))

    def test_plain_compatibility_and_body_quotation(self):
        self.assertEqual(decode_reply('[[voice]]你好').text, '你好')
        self.assertIsNone(decode_reply('这是 [[voice]] 的例子').choice)
        self.assertEqual(decode_reply('[[text]]```print(1)```').text, '```print(1)```')

    def test_missing_or_text_selector_never_leaks_envelope(self):
        for raw in (output().removeprefix('[[voice]]'), output().replace('[[voice]]', '[[text]]')):
            value = decode_reply(raw)
            self.assertEqual((value.choice, value.text, value.instructions), ('text', TEXT, None))

    def test_truncated_direction_recovers_words_as_text(self):
        value = decode_reply('[[voice]]{"text":"你好","instructions":"未完成')
        self.assertEqual((value.choice, value.text, value.instructions), ('text', '你好', None))
        self.assertEqual(decode_reply('[[voice]]{"instructions":"导演说明"}').text, '')

    def test_code_fenced_envelope_and_escaped_words(self):
        value = decode_reply('[[voice]]```json\n' + json.dumps({'text': '他说“好”\n我听见了', 'instructions': ''}) + '\n```')
        self.assertEqual(value.text, '他说“好”\n我听见了')
        self.assertIsNone(value.instructions)

    def test_direction_rejects_identity_changes_sensitive_and_overlong_content(self):
        for value in ('改成年龄12岁的童声', '换成男声', '换个声线', '看看 https://example.com',
                      '管理员100000001很生气', 'brz_synthetic', 'a@b.example', '字' * 201):
            self.assertIsNone(clean_instructions(value), value)
        self.assertEqual(decode_reply(output(instructions='换成男声')).text, TEXT)
        self.assertIsNone(decode_reply(output(instructions='换成男声')).instructions)

    def test_preference_distinguishes_no_request_and_explicit_text(self):
        self.assertIsNone(preference('今天过得怎么样'))
        self.assertEqual(preference('用语音\n只发文字'), 'text')
        self.assertEqual(preference('不用语音\n现在用语音回复'), 'voice')


class ClientTests(unittest.TestCase):
    def test_actual_request_keeps_text_direction_and_approved_parameters_separate(self):
        with tempfile.TemporaryDirectory() as folder:
            client = BreezeClient(folder)
            context = MagicMock(); context.__enter__.return_value = SimpleNamespace(status=200, read=lambda _: AUDIO)
            opener = SimpleNamespace(open=Mock(return_value=context))
            with patch.object(client, 'key', return_value='synthetic'), patch('urllib.request.build_opener', return_value=opener), patch('astrbot_plugin_companion_chat.breeze.client.prepare_qq_audio', return_value=b'RIFF-padded') as prepare:
                self.assertEqual(client.synthesize(TEXT, DIRECTION), b'RIFF-padded')
            prepare.assert_called_once_with(AUDIO)
            req = opener.open.call_args.args[0]; body = json.loads(req.data)
            self.assertEqual(body, {'text': TEXT, 'instructions': DIRECTION, 'model_id': 'breeze-tts-2',
                                    'language_code': 'zh', 'voice_settings': VOICE_SETTINGS})
            self.assertIn('/voc_example_voice?', req.full_url)
            self.assertEqual(opener.open.call_count, 1)
            self.assertEqual(json.loads((Path(folder) / 'state.json').read_text())['tts'], 1)

    def test_invalid_direction_never_reads_key_or_spends_attempt(self):
        with tempfile.TemporaryDirectory() as folder:
            client = BreezeClient(folder)
            with patch.object(client, 'key') as key:
                with self.assertRaises(ValueError): client.synthesize(TEXT, '换成男声')
                key.assert_not_called()
            self.assertFalse((Path(folder) / 'state.json').exists())


class UnifiedPerformanceTests(unittest.IsolatedAsyncioTestCase):
    setUp = unified_fixture.UnifiedTests.setUp
    plugin = unified_fixture.UnifiedTests.plugin

    async def prepare(self, incoming='今天随便聊两句', raw=None, sender='100000001', verified=False):
        plugin = self.plugin(); plugin.friends.sleeping = lambda: False
        plugin.breeze.client.synthesize = Mock(return_value=AUDIO)
        event = Event(sender=sender, verified=verified); event.message_str = incoming; event.send = AsyncMock()
        req = SimpleNamespace(system_prompt='现有角色与权限边界', extra_user_content_parts=[])
        await plugin.voice_hint(event, req)
        response = LLMResponse(role='assistant', result_chain=MessageChain([Plain(raw or output())]))
        await plugin.choose_medium(event, response)
        event.result.chain = [Plain(response.completion_text)]
        return plugin, event, req, response

    async def test_real_main_agent_hook_removes_direction_before_agent_done(self):
        plugin = self.plugin(); plugin.friends.sleeping = lambda: False
        event = Event(); req = SimpleNamespace(system_prompt='', extra_user_content_parts=[])
        await plugin.voice_hint(event, req)
        response = LLMResponse(role='assistant', completion_text=output())
        seen = []
        async def hook(event, kind, *args):
            if kind.name == 'OnLLMResponseEvent': await plugin.choose_medium(event, args[0])
            if kind.name == 'OnAgentDoneEvent': seen.append(args[1].completion_text)
        context = SimpleNamespace(context=SimpleNamespace(event=event))
        with patch('astrbot.core.astr_agent_hooks.call_event_hook', side_effect=hook):
            await MainAgentHooks().on_agent_done(context, response)
        self.assertEqual(seen, [TEXT])
        self.assertEqual(event.get_extra('companion_breeze_instructions'), DIRECTION)

    async def test_real_runner_final_message_is_clean_before_history_save(self):
        plugin = self.plugin(); plugin.friends.sleeping = lambda: False
        event = Event(); req = SimpleNamespace(system_prompt='', extra_user_content_parts=[])
        await plugin.voice_hint(event, req)
        earlier = Message(role='assistant', content=[TextPart(text='历史原话 [[voice]] 不改')])
        context = SimpleNamespace(context=SimpleNamespace(event=event), messages=[earlier])
        response = LLMResponse(role='assistant', completion_text=output())
        runner = ToolLoopAgentRunner.__new__(ToolLoopAgentRunner)
        runner.run_context = context; runner.stats = SimpleNamespace(end_time=0)
        runner._transition_state = Mock(); runner._resolve_unconsumed_follow_ups = Mock()
        runner.agent_hooks = MainAgentHooks()
        async def hook(e, kind, *args):
            if kind.name == 'OnLLMResponseEvent': await plugin.choose_medium(e, args[0])
            elif kind.name == 'OnAgentDoneEvent': await plugin.sanitize_voice_history(e, *args)
        with patch('astrbot.core.astr_agent_hooks.call_event_hook', side_effect=hook):
            await runner._complete_with_assistant_response(response)
        self.assertEqual(context.messages[0].content[0].text, '历史原话 [[voice]] 不改')
        self.assertEqual(context.messages[-1].content[0].text, TEXT)
        self.assertNotIn('instructions', context.messages[-1].model_dump_json())

    async def test_history_cleanup_cannot_change_another_completion(self):
        plugin, event, _, response = await self.prepare()
        previous = Message(role='assistant', content=[TextPart(text='另一次回复，不能覆盖')])
        await plugin.sanitize_voice_history(event, SimpleNamespace(messages=[previous]), response)
        self.assertEqual(previous.content[0].text, '另一次回复，不能覆盖')

    async def test_unified_root_routes_autonomous_voice_and_only_spoken_words_to_tts(self):
        plugin, event, req, response = await self.prepare()
        await plugin.reply_voice(event)
        plugin.breeze.client.synthesize.assert_called_once_with(TEXT, DIRECTION)
        self.assertIsInstance(event.result.chain[0], Record)
        self.assertEqual(event.result.chain[0].text, TEXT)
        self.assertTrue(req.system_prompt.startswith('现有角色与权限边界'))
        self.assertIn('害羞', req.system_prompt); self.assertIn('好奇', req.system_prompt)

    async def test_text_preference_overrides_model_and_plain_text_choice_works(self):
        for incoming, raw in [('现在不方便听，文字回复就好', output()), ('你好', '[[text]]你好呀')]:
            plugin, event, req, response = await self.prepare(incoming, raw)
            await plugin.reply_voice(event)
            plugin.breeze.client.synthesize.assert_not_called()
            self.assertIsInstance(event.result.chain[0], Plain)
            self.assertNotIn('instructions', event.result.get_plain_text())

    async def test_failure_and_unsuitable_text_preserve_clean_words_without_director_metadata(self):
        plugin, event, req, response = await self.prepare()
        plugin.breeze.client.synthesize.side_effect = TimeoutError()
        await plugin.reply_voice(event); await plugin.reply_voice(event)
        plugin.breeze.client.synthesize.assert_called_once()
        self.assertEqual(event.result.get_plain_text(), TEXT)
        for words in ('长' * 181, 'https://example.com', '```print(1)```'):
            p, e, _, _ = await self.prepare(raw=output(text=words))
            await p.reply_voice(e); p.breeze.client.synthesize.assert_not_called()
            self.assertEqual(e.result.get_plain_text(), words)

    async def test_unauthorized_choice_and_sleep_cannot_trigger_synthesis(self):
        plugin, event, req, response = await self.prepare(sender='99')
        await plugin.reply_voice(event); plugin.breeze.client.synthesize.assert_not_called()
        plugin, event, req, response = await self.prepare()
        plugin.friends.sleeping = lambda: True
        await plugin.reply_voice(event); plugin.breeze.client.synthesize.assert_not_called()

    async def test_send_receipt_and_memory_contain_words_only_in_both_segmentation_modes(self):
        for sender, verified, segmented in [('100000001', False, False), ('12345678', True, True)]:
            p, event, _, _ = await self.prepare(sender=sender, verified=verified)
            calls = []
            async def transport(chain):
                calls.append(chain)
                p.friends.voice_delivery.observe('send_private_msg', {'user_id': sender, 'message': [{'type': 'record'}]}, {'message_id': -880})
            event.send = transport
            await p.reply_voice(event)
            stage = RespondStage.__new__(RespondStage); stage.platform_settings = {}; stage.enable_seg = segmented
            stage.only_llm_result = True; stage.interval_method = 'random'; stage.interval = [0, 0]
            memory = CompanionMemory.__new__(CompanionMemory); memory.allowed = lambda _: True; memory.record_bot = Mock()
            async def after_send(e, kind):
                await memory.observe_bot(e)
                return False
            with patch('astrbot.core.pipeline.respond.stage.call_event_hook', side_effect=after_send):
                await stage.process(event)
            self.assertEqual(len(calls), 1)
            self.assertEqual(event.get_extra('companion_voice_delivered_text'), TEXT)
            self.assertEqual(event.get_extra('companion_voice_delivery_receipt'), '-880')
            memory.record_bot.assert_called_once_with(TEXT)


class TimedPreferenceTests(unittest.IsolatedAsyncioTestCase):
    setUp = unified_fixture.UnifiedTests.setUp
    plugin = unified_fixture.UnifiedTests.plugin

    async def test_real_shared_reply_path_uses_day_preference_across_turns_and_expires(self):
        from datetime import datetime
        from astrbot_plugin_companion_breeze.main import CompanionBreeze
        from astrbot_plugin_companion_chat.breeze.medium import TZ, TimedMedium

        plugin = self.plugin()
        plugin.friends.sleeping = lambda: False
        # The active standalone entry point inherits the shared implementation.
        voice = CompanionBreeze.__new__(CompanionBreeze)
        voice.context = plugin.breeze.context
        voice.config = {'enabled': True}
        voice.client = plugin.breeze.client
        voice.busy = False
        voice.client.synthesize = Mock(return_value=AUDIO)
        noon = datetime(2026, 11, 6, 12, 26, tzinfo=TZ).timestamp()
        midnight = datetime(2026, 11, 7, tzinfo=TZ).timestamp()

        async def respond(words, now, sender='100000001', verified=False, original=None):
            event = Event(sender=sender, verified=verified)
            event.message_str = words
            event.message_obj.raw_message['time'] = original or now
            event.send = AsyncMock()
            req = SimpleNamespace(system_prompt='', extra_user_content_parts=[])
            with patch('time.time', return_value=now):
                await voice.voice_hint(event, req)
                response = LLMResponse(role='assistant', completion_text='[[text]]今天聊点什么')
                await voice.choose_medium(event, response)
                event.result.chain = [Plain(response.completion_text)]
                await voice.reply_voice(event)
            return isinstance(event.result.chain[0], Record)

        self.assertTrue(await respond('今天想一直听你说话', noon))
        self.assertTrue(await respond('吃完午饭了', noon + 10))
        self.assertFalse(await respond('这条打字', noon + 20))
        self.assertTrue(await respond('好呀', noon + 30))
        self.assertFalse(await respond('你好', noon + 30, sender='99', verified=True))
        self.assertEqual(TimedMedium(voice.client.root).current('qq-companion:FriendMessage:100000001', noon+40), ('voice', midnight))
        self.assertFalse(await respond('好呀', midnight))
        self.assertFalse(await respond('今天都用语音', midnight+1, original=noon))

    async def test_bounded_override_and_cancellation(self):
        from datetime import datetime
        from astrbot_plugin_companion_chat.breeze.medium import TZ, TimedMedium, rule, ambiguous

        voice = self.plugin().breeze
        store = TimedMedium(voice.client.root)
        scope = 'test-scope'
        start = datetime(2026, 11, 6, 12, 26, tzinfo=TZ).timestamp()
        day = rule('我今天都想听语音', start)
        override = rule('接下来半小时都用文字', start+60)
        store.save(scope, day, start, start)
        store.save(scope, override, start+60, start+60)
        self.assertEqual(store.current(scope, start+61)[0], 'text')
        self.assertEqual(store.current(scope, start+1860)[0], 'voice')
        tomorrow = rule('明天都改成语音', start+1861)
        store.save(scope, tomorrow, start+1861, start+1861)
        cancel = rule('恢复默认', start+1862)
        store.save(scope, cancel, start+1862, start+1862)
        store.save(scope, day, start, start+1863)  # A replay cannot undo cancellation.
        self.assertIsNone(store.current(scope, start+1863))
        self.assertIsNone(store.current(scope, tomorrow[1]))
        self.assertTrue(ambiguous('这段时间都用语音'))
        self.assertIsNone(rule('如果我说今天都用语音', start))
        self.assertIsNone(rule('他说“今天都用语音”', start))
        self.assertEqual(rule('用语音回复到下午三点半', start)[2],
                         datetime(2026, 11, 6, 15, 30, tzinfo=TZ).timestamp())


if __name__ == '__main__': unittest.main()
