"""Approved clips use the existing protected private voice transport."""
import base64
from pathlib import Path
import os
from time import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from astrbot.api.message_components import Plain, Record
from astrbot.core.agent.tool import ToolSet
from astrbot.core.message.message_event_result import MessageChain, MessageEventResult
from astrbot.core.provider.register import llm_tools
from astrbot_plugin_companion_chat import acapella
from test_breeze_bridge import Event
import test_chat_unified as fixture


class AcapellaTests(unittest.IsolatedAsyncioTestCase):
    setUp = fixture.UnifiedTests.setUp
    plugin = fixture.UnifiedTests.plugin

    def prepared(self, **event_options):
        plugin = self.plugin()
        plugin.friends.sleeping = Mock(return_value=False)
        plugin.acapella_ready = True
        plugin.acapella_root = Path(os.environ['COMPANION_TEST_PLUGINS']) / 'rvc-acapella-v1'
        event = Event(**event_options)
        event.chain_result = lambda chain: MessageEventResult(chain=chain)
        event.plain_result = lambda text: event.chain_result([Plain(text)])
        event.is_stopped = lambda: event.stopped
        event.set_result = lambda result: setattr(event, 'result', result)
        event.send = AsyncMock()
        return plugin, event

    async def test_natural_request_uses_verified_wav_and_requires_receipt(self):
        plugin, event = self.prepared()
        event.message_str = '给我清唱一段《小半》吧'
        transport = plugin.friends.voice_delivery
        calls = []

        async def confirmed(chain):
            calls.append(chain)
            transport.observe('send_private_msg', {'user_id': event.sender, 'message': [{'type': 'record'}]},
                              value={'message_id': 51})

        event.send = confirmed
        handler = plugin.acapella_request(event)
        result = await anext(handler)
        self.assertEqual(len(result.chain), 1)
        self.assertIsInstance(result.chain[0], Record)
        self.assertIsNone(event.get_extra('companion_voice_delivery_receipt'))
        record = result.chain[0]
        self.assertEqual(base64.b64decode(record.file.removeprefix('base64://')),
                         acapella.read_clip(plugin.acapella_root, '小半'))
        await event.send(MessageChain(result.chain))
        await event.send(MessageChain(result.chain))
        self.assertEqual(len(calls), 1)
        self.assertEqual(event.get_extra('companion_voice_delivery_receipt'), '51')
        with self.assertRaises(StopAsyncIteration):
            await anext(handler)
        self.assertTrue(event.stopped)

    async def test_send_rejection_falls_back_to_truthful_text(self):
        plugin, event = self.prepared()
        sent = []

        async def reject_voice(chain):
            voice = isinstance(chain.chain[0], Record)
            sent.append(chain.chain[0])
            plugin.friends.voice_delivery.observe(
                'send_private_msg', {'user_id': event.sender, 'message': [{'type': 'record' if voice else 'text'}]},
                value={'status': 'failed', 'retcode': 100} if voice else {'message_id': 52},
            )

        event.send = reject_voice
        handler = plugin.acapella_command(event, '去年夏天')
        result = await anext(handler)
        await event.send(MessageChain(result.chain))
        self.assertEqual(len(sent), 2)
        self.assertEqual(sent[1].text, '这段清唱语音没有发出去。')
        self.assertEqual(event.get_extra('companion_voice_delivered_as'), 'text')
        await handler.aclose()

    async def test_asleep_defers_and_text_preference_produces_no_audio(self):
        plugin, event = self.prepared()
        plugin.friends.sleeping.return_value = True
        plugin.breeze.defer = Mock()
        self.assertEqual([x async for x in plugin.acapella_command(event, '小半')], [])
        plugin.breeze.defer.assert_called_once_with(event)
        plugin.friends.sleeping.return_value = False
        plugin.breeze.wanted_medium = Mock(return_value='text')
        replies = [x async for x in plugin.acapella_command(event, '小半')]
        self.assertIsInstance(replies[0].chain[0], Plain)
        event.send.assert_not_awaited()

    async def test_groups_other_contacts_and_ordinary_conversation_are_excluded(self):
        for options in ({'sender': '31415', 'verified': True}, {'group': '12345'}, {'platform': 'other'}):
            plugin, event = self.prepared(**options)
            self.assertEqual([x async for x in plugin.acapella_command(event, '小半')], [])
        plugin, event = self.prepared()
        for text in ('不要唱小半', '你喜欢小半吗', '请唱别的歌', '我想听你唱小半\n先用文字'):
            event.message_str = text
            self.assertEqual([x async for x in plugin.acapella_request(event)], [])
        req = SimpleNamespace(system_prompt='', func_tool=None)
        with patch.object(plugin, 'external_breeze', return_value=plugin.breeze):
            await plugin.isolate_tools(event, req)
            await plugin.voice_hint(event, req)
        self.assertIn(acapella.TOOL, req.system_prompt)

    async def test_general_request_exposes_tool_and_decorates_final_with_one_clip(self):
        plugin, event = self.prepared()
        event.message_str = '随便唱一首清唱吧'
        req = SimpleNamespace(system_prompt='', func_tool=None, extra_user_content_parts=[])
        await plugin.isolate_tools(event, req)
        await plugin.voice_hint(event, req)
        self.assertIn(acapella.TOOL, req.func_tool.names())
        self.assertNotIn('当前未接通按旋律演唱功能', req.system_prompt)
        self.assertIn('自己选一首', req.system_prompt)
        queued = await plugin.choose_acapella(event, '去年夏天')
        self.assertFalse(queued.isError)
        self.assertIn('当前尚未发送', queued.content[0].text)
        await plugin.choose_acapella(event, '小半')
        self.assertEqual(event.get_extra(acapella.SELECTION), '去年夏天')
        response = SimpleNamespace(role='assistant', tools_call_name=None, completion_text='[[silent:closing_ack]]')
        await plugin.record_reply_decision(event, response)
        self.assertEqual(response.completion_text, '《去年夏天》清唱片段')
        original_send = event.send
        await plugin.decorate_acapella(event)
        await plugin.decorate_acapella(event)
        self.assertEqual(len(event.result.chain), 1)
        self.assertIsInstance(event.result.chain[0], Record)
        self.assertEqual(base64.b64decode(event.result.chain[0].file.removeprefix('base64://')),
                         acapella.read_clip(plugin.acapella_root, '去年夏天'))
        original_send.assert_not_awaited()

    async def test_tool_stays_hidden_and_denied_for_other_contacts_and_declines(self):
        tool = next(t for t in llm_tools.func_list if t.name == acapella.TOOL)
        cases = (
            ({'sender': '31415', 'verified': True}, '唱首歌'),
            ({'group': '12345'}, '唱首歌'),
            ({}, '今天别唱歌了'),
            ({}, '文字回复就好'),
        )
        for options, text in cases:
            plugin, event = self.prepared(**options)
            event.message_str = text
            req = SimpleNamespace(func_tool=ToolSet([tool]))
            await plugin.isolate_tools(event, req)
            self.assertNotIn(acapella.TOOL, req.func_tool.names())
            self.assertTrue((await plugin.choose_acapella(event, '小半')).isError)

    async def test_sleep_after_selection_defers_without_record(self):
        plugin, event = self.prepared()
        event.message_str = '可以，小半吧'
        self.assertFalse((await plugin.choose_acapella(event, '小半')).isError)
        plugin.friends.sleeping.return_value = True
        await plugin.decorate_acapella(event)
        self.assertTrue(event.stopped)
        self.assertIsNone(event.result)
        event.send.assert_not_awaited()

    async def test_first_unnamed_invitation_plays_the_models_choice_in_same_turn(self):
        plugin, event = self.prepared()
        event.message_str = '唱首歌试试'
        plugin.context.persona_manager = SimpleNamespace(
            get_persona=AsyncMock(return_value=SimpleNamespace(system_prompt='你是小鲸鱼。')))
        plugin.context.llm_generate = AsyncMock(return_value=SimpleNamespace(completion_text='{"title":"去年夏天"}'))
        handler = plugin.acapella_request(event)
        reply = await anext(handler)
        self.assertIsInstance(reply.chain[0], Record)
        self.assertEqual(event.get_extra(acapella.PLAYBACK), '去年夏天')
        plugin.context.llm_generate.assert_awaited_once()
        self.assertFalse(plugin.acapella_history.exists())
        with self.assertRaises(StopAsyncIteration):
            await anext(handler)
        self.assertTrue(event.stopped)

    async def test_another_song_uses_voice_receipt_and_skips_the_last_clip(self):
        plugin, event = self.prepared()
        event.set_extra(acapella.PLAYBACK, '小半')
        event.set_extra('companion_voice_delivery_receipt', '501')
        event.set_extra('companion_voice_delivered_as', 'text')
        await plugin.message_sent(event)
        self.assertFalse(plugin.acapella_history.exists())
        event.set_extra('companion_voice_delivered_as', 'voice')
        await plugin.message_sent(event)
        self.assertEqual(acapella.last_played(plugin.acapella_history, event.unified_msg_origin), '小半')
        plugin.context.llm_generate = AsyncMock()
        next_event = self.prepared()[1]
        next_event.message_str = '我还想听你唱另一首歌'
        handler = plugin.acapella_request(next_event)
        result = await anext(handler)
        self.assertIsInstance(result.chain[0], Record)
        self.assertEqual(next_event.get_extra(acapella.PLAYBACK), '去年夏天')
        plugin.context.llm_generate.assert_not_awaited()
        await handler.aclose()

    async def test_short_invite_falls_back_to_one_clip_if_model_unavailable(self):
        plugin, event = self.prepared()
        event.message_str = '唱吧'
        handler = plugin.acapella_request(event)
        result = await anext(handler)
        self.assertIsInstance(result.chain[0], Record)
        self.assertIn(event.get_extra(acapella.PLAYBACK), acapella.CLIPS)
        await handler.aclose()

    def test_unnamed_invite_does_not_match_discussion_or_other_titles(self):
        for text in ('我喜欢唱歌', '你觉得她唱歌怎么样', '别唱歌', '我想听你唱《其他歌》'):
            self.assertIsNone(acapella.open_invite(text))
        for text in ('唱歌', '唱吧', '随便唱一首清唱吧'):
            self.assertEqual(acapella.open_invite(text), 'any')
        self.assertEqual(acapella.open_invite('我还想听你唱另一首歌'), 'different')

    async def test_retry_uses_same_conversations_latest_singing_intent(self):
        plugin, event = self.prepared()
        event.message_str = '现在再试一下呢'
        event.set_extra('companion_observation_id', 5)
        scope = event.unified_msg_origin
        now = time()
        rows = [
            {'id': 1, 'role': 'user', 'origin': scope, 'at': now - 522, 'text': '我还想听你唱另一首歌'},
            {'id': 2, 'role': 'assistant', 'origin': scope, 'at': now - 488, 'text': '另一首可以唱。'},
            {'id': 3, 'role': 'user', 'origin': scope, 'at': now - 468, 'text': '唱吧'},
            {'id': 4, 'role': 'assistant', 'origin': scope, 'at': now - 444, 'text': '你发完我就唱。'},
            {'id': 5, 'role': 'user', 'origin': scope, 'at': now, 'text': event.message_str},
        ]
        store = SimpleNamespace(latest_messages=Mock(return_value=rows))
        plugin.context.get_registered_star = lambda name: (
            SimpleNamespace(activated=True, star_cls=SimpleNamespace(store=store))
            if name.endswith('_memory') else None)
        acapella.remember_playback(plugin.acapella_history, scope, '小半', '501')
        handler = plugin.acapella_request(event)
        reply = await anext(handler)
        self.assertIsInstance(reply.chain[0], Record)
        self.assertEqual(event.get_extra(acapella.PLAYBACK), '去年夏天')
        store.latest_messages.assert_called_once_with(plugin.friends.owner, limit=16)
        with self.assertRaises(StopAsyncIteration):
            await anext(handler)
        self.assertTrue(event.stopped)

    def test_retry_does_not_use_stale_refused_foreign_or_interrupted_intent(self):
        scope = 'synthetic-owner-private'
        first = {'id': 1, 'role': 'user', 'origin': scope, 'at': 900, 'text': '清唱小半'}
        current = first | {'id': 3, 'at': 1000, 'text': '现在再试一下呢'}
        self.assertEqual(acapella.retry_invitation([first, current], scope, 3, 1000), ('小半', None))
        for changed in (first | {'at': 100}, first | {'origin': 'other-contact'}, first | {'at': None}):
            self.assertEqual(acapella.retry_invitation([changed, current], scope, 3, 2000), (None, None))
        for text in ('别唱了', '刚才那张图怎么打不开'):
            middle = first | {'id': 2, 'at': 950, 'text': text}
            later = first | {'id': 4, 'at': 1000, 'text': '随便唱一首清唱吧'}
            self.assertEqual(acapella.retry_invitation([first, middle, current, later], scope, 3, 1000), (None, None))

    def test_native_fixtures_have_qq_audio_format(self):
        import io
        import wave

        root = Path(os.environ['COMPANION_TEST_PLUGINS']) / 'rvc-acapella-v1'
        for title in acapella.CLIPS:
            with wave.open(io.BytesIO(acapella.read_clip(root, title))) as wav:
                self.assertEqual((wav.getnchannels(), wav.getsampwidth(), wav.getframerate()), (1, 2, 24000))
                wav.setpos(wav.getnframes() - 12000)
                self.assertEqual(wav.readframes(12000), bytes(24000))
