"""Mocked provider and QQ transport: privacy, sleep, quotas and send evidence."""
import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.environ.get('COMPANION_TEST_PLUGINS', str(Path(__file__).parent)))
from astrbot_plugin_companion_breeze.client import BreezeClient, requested, preference
from astrbot_plugin_companion_breeze.main import CompanionBreeze
from astrbot_plugin_companion_friends.delivery import VoiceDelivery
from astrbot.api.message_components import Plain, Record
from astrbot.api.provider import LLMResponse
from astrbot.core.message.message_event_result import MessageEventResult, ResultContentType, MessageChain
from astrbot.core.pipeline.respond.stage import RespondStage

AUDIO = b'ID3' + b'x' * 200


class Event:
    def __init__(self, sender='100000001', verified=False, platform='qq-companion', group=''):
        self.sender, self.platform, self.group = sender, platform, group
        self.unified_msg_origin = f'{platform}:FriendMessage:{sender}'
        self.message_str = '发一句语音给我听'
        self.message_obj = SimpleNamespace(raw_message={'message_type': 'private', 'user_id': sender})
        self.extra = {'companion_friend_verified': verified}
        self.result = MessageEventResult(chain=[Plain('我在这里，今天过得怎么样？')])
        self.result.result_content_type = ResultContentType.LLM_RESULT
        self.stopped = False

    def get_platform_id(self): return self.platform
    def get_sender_id(self): return self.sender
    def get_group_id(self): return self.group
    def get_extra(self, k, d=None): return self.extra.get(k, d)
    def set_extra(self, k, v): self.extra[k] = v
    def get_result(self): return self.result
    def clear_result(self): self.result = None
    def stop_event(self): self.stopped = True
    def get_platform_name(self): return 'aiocqhttp'
    def get_sender_name(self): return 'synthetic'
    def _outline_chain(self, chain): return 'synthetic'


class ClientTests(unittest.TestCase):
    def test_latest_direction_controls_merged_batch(self):
        for text, expected in [('你好', False), ('发个语音', True), ('用语音回复', True),
                               ('不要发语音', False), ('发个语音\n不方便听，文字回复就好', False),
                               ('不用语音\n现在发一句语音', True)]:
            self.assertEqual(requested(text), expected, text)

    def test_explicit_text_is_distinct_from_no_preference(self):
        self.assertIsNone(preference('今天聊点什么'))
        self.assertEqual(preference('只发文字'), 'text')
        self.assertEqual(preference('发语音\n不用发语音，文字回复就好'), 'text')
        self.assertEqual(preference('不要语音\n现在用语音回复'), 'voice')

    def test_quota_survives_restart_and_never_resets_corrupt_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = BreezeClient(tmp)
            for _ in range(20): client.reserve()
            with self.assertRaises(RuntimeError): BreezeClient(tmp).reserve()
            path = Path(tmp) / 'state.json'
            path.write_text('{broken')
            with self.assertRaises(ValueError): client.reserve()
            self.assertEqual(path.read_text(), '{broken')

    def test_provider_contract_and_private_input_are_minimal(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = BreezeClient(tmp)
            response = SimpleNamespace(status=200, read=lambda _: AUDIO)
            context = unittest.mock.MagicMock()
            context.__enter__.return_value = response
            opener = SimpleNamespace(open=unittest.mock.Mock(return_value=context))
            with patch.object(client, 'key', return_value='synthetic-secret'), patch('urllib.request.build_opener', return_value=opener):
                self.assertEqual(client.synthesize('你好'), AUDIO)
            req = opener.open.call_args.args[0]
            self.assertEqual(json.loads(req.data), {'text': '你好', 'model_id': 'breeze-tts-2', 'language_code': 'zh'})
            self.assertIn('/voc_example_voice?', req.full_url)
            self.assertEqual(opener.open.call_count, 1)

    def test_missing_key_does_not_spend_quota(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(FileNotFoundError): BreezeClient(tmp).synthesize('你好')
            self.assertFalse((Path(tmp) / 'state.json').exists())

    def test_non_audio_response_rejected_without_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = BreezeClient(tmp)
            context = unittest.mock.MagicMock()
            context.__enter__.return_value = SimpleNamespace(status=200, read=lambda _: b'{"secret": "not audio"}')
            opener = SimpleNamespace(open=unittest.mock.Mock(return_value=context))
            with patch.object(client, 'key', return_value='synthetic'), patch('urllib.request.build_opener', return_value=opener):
                with self.assertRaises(ValueError): client.synthesize('你好')
            self.assertEqual(opener.open.call_count, 1)


class BridgeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.sleep = False
        self.tracker = VoiceDelivery()
        self.friends = SimpleNamespace(sleeping=lambda: self.sleep, voice_delivery=self.tracker,
                                       sleep_inbox=SimpleNamespace(add=unittest.mock.Mock(), mark=unittest.mock.Mock()))
        self.plugin = CompanionBreeze.__new__(CompanionBreeze)
        self.plugin.config = {'enabled': True}
        self.plugin.context = SimpleNamespace(get_registered_star=lambda _: SimpleNamespace(activated=True, star_cls=self.friends))
        self.plugin.busy = False
        self.plugin.client = SimpleNamespace(synthesize=unittest.mock.Mock(return_value=AUDIO))

    async def test_permission_sleep_and_ordinary_text_skip_provider(self):
        events = [Event(sender='99'), Event(group='12'), Event(platform='other')]
        ordinary = Event(); ordinary.message_str = '今天好吗'; events.append(ordinary)
        for event in events: await self.plugin.reply_voice(event)
        self.sleep = True; await self.plugin.reply_voice(Event())
        self.plugin.client.synthesize.assert_not_called()

    async def test_voice_identity_is_present_without_explicit_voice_request(self):
        event = Event(); event.message_str = '刚才听到的是你的声音吗'
        req = SimpleNamespace(system_prompt='现有人格与联系人边界', extra_user_content_parts=[])
        await self.plugin.voice_hint(event, req)
        self.assertTrue(req.system_prompt.startswith('现有人格与联系人边界'))
        self.assertIn('就是你当前的声音身份', req.system_prompt)
        self.assertIn('旧说法不能代表当前能力', req.system_prompt)
        self.assertIn('当前未接通按旋律演唱功能', req.system_prompt)
        self.assertIn('不赋予你听觉', req.system_prompt)
        self.assertEqual(req.extra_user_content_parts, [])
        await self.plugin.voice_hint(event, req)
        self.assertEqual(req.system_prompt.count('[companion_voice_identity]'), 1)
        self.plugin.client.synthesize.assert_not_called()

    async def test_voice_identity_same_for_owner_and_verified_friend_without_private_data(self):
        prompts = []
        for sender, verified in [('100000001', False), ('99', True)]:
            event = Event(sender=sender, verified=verified); event.message_str = '你好'
            req = SimpleNamespace(system_prompt='', extra_user_content_parts=[])
            await self.plugin.voice_hint(event, req)
            prompts.append(req.system_prompt)
        self.assertEqual(prompts[0], prompts[1])
        for sensitive in ('100000001', 'voc_', 'brz_', 'credentials'):
            self.assertNotIn(sensitive, prompts[0])

    async def test_voice_identity_not_injected_for_disabled_foreign_or_sleeping_events(self):
        for event in [Event(sender='99'), Event(group='1'), Event(platform='other')]:
            req = SimpleNamespace(system_prompt='original', extra_user_content_parts=[])
            await self.plugin.voice_hint(event, req)
            self.assertEqual(req.system_prompt, 'original')
        for disabled, asleep in [(True, False), (False, True)]:
            self.plugin.config['enabled'] = not disabled; self.sleep = asleep
            req = SimpleNamespace(system_prompt='original', extra_user_content_parts=[])
            await self.plugin.voice_hint(Event(), req)
            self.assertEqual(req.system_prompt, 'original')

    async def test_explicit_voice_request_gets_identity_and_current_output_hint(self):
        req = SimpleNamespace(system_prompt='', extra_user_content_parts=[])
        await self.plugin.voice_hint(Event(), req)
        self.assertIn('你能够用这条声线发送说话语音', req.system_prompt)
        self.assertEqual(len(req.extra_user_content_parts), 1)
        self.assertIn('自己的声线', req.extra_user_content_parts[0].text)

    async def routed(self, choice, incoming='今天聊点什么', reply='我想用自己的声音和你说句话', sender='100000001', verified=False):
        event = Event(sender=sender, verified=verified); event.message_str = incoming
        event.send = AsyncMock()
        req = SimpleNamespace(system_prompt='', extra_user_content_parts=[])
        await self.plugin.voice_hint(event, req)
        response = LLMResponse(role='assistant', result_chain=MessageChain([Plain(f'[[{choice}]]\n{reply}')]))
        await self.plugin.choose_medium(event, response)
        self.assertEqual(response.completion_text, reply)
        event.result.chain = [Plain(response.completion_text)]
        await self.plugin.reply_voice(event)
        return event

    async def test_model_can_choose_voice_for_normal_chat_without_another_model_call(self):
        event = await self.routed('voice')
        self.assertIsInstance(event.result.chain[0], Record)
        self.assertEqual(event.result.chain[0].text, '我想用自己的声音和你说句话')
        self.assertTrue(event.get_extra('companion_voice_prepared'))
        self.assertIsNone(event.get_extra('companion_voice_delivery_receipt'))

    async def test_model_can_choose_text_and_missing_final_choice_defaults_to_text(self):
        event = await self.routed('text')
        self.assertIsInstance(event.result.chain[0], Plain)
        self.plugin.client.synthesize.assert_not_called()
        event.set_extra('companion_breeze_choice', 'voice')
        response = LLMResponse(role='assistant', completion_text='这次最终回复没有媒介标记')
        await self.plugin.choose_medium(event, response)
        self.assertIsNone(event.get_extra('companion_breeze_choice'))

    async def test_explicit_preference_overrides_model_choice(self):
        text = await self.routed('voice', incoming='现在不方便听，文字回复就好')
        self.assertIsInstance(text.result.chain[0], Plain)
        self.plugin.client.synthesize.assert_not_called()
        voice = await self.routed('text', incoming='发一句语音给我听')
        self.assertIsInstance(voice.result.chain[0], Record)

    async def test_marker_cleanup_precedes_busy_or_unsuitable_text_fallback(self):
        for text in ['长' * 181, '请看 https://example.com', '```print(1)```']:
            event = await self.routed('voice', reply=text)
            self.assertEqual(event.result.get_plain_text(), text)
        self.plugin.busy = True
        event = Event(); event.message_str = '你好'; event.send = AsyncMock()
        event.set_extra('companion_breeze_route_enabled', True)
        event.result.chain = [Plain('[[voice]]\n这句话保留为文字')]
        await self.plugin.reply_voice(event)
        self.assertEqual(event.result.get_plain_text(), '这句话保留为文字')
        self.plugin.client.synthesize.assert_not_called()

    async def test_quoted_marker_in_body_does_not_select_voice(self):
        event = Event(); event.message_str = '解释一下这个例子'
        event.set_extra('companion_breeze_route_enabled', True)
        response = LLMResponse(role='assistant', completion_text='例子里写着 [[voice]]，这是引用')
        await self.plugin.choose_medium(event, response)
        self.assertIsNone(event.get_extra('companion_breeze_choice'))
        self.assertIn('[[voice]]', response.completion_text)

    async def test_autonomous_voice_choice_cannot_bypass_contact_permissions(self):
        event = Event(sender='99'); event.message_str = '你好'
        event.set_extra('companion_breeze_choice', 'voice'); event.send = AsyncMock()
        await self.plugin.reply_voice(event)
        self.plugin.client.synthesize.assert_not_called()

    async def test_autonomous_provider_failure_keeps_clean_text_without_retry(self):
        self.plugin.client.synthesize.side_effect = RuntimeError('breeze_daily_limit')
        event = await self.routed('voice')
        await self.plugin.reply_voice(event)
        self.assertEqual(event.result.get_plain_text(), '我想用自己的声音和你说句话')
        self.plugin.client.synthesize.assert_called_once()

    async def test_autonomous_voice_sleep_before_send_keeps_unread_pending(self):
        event = await self.routed('voice')
        self.sleep = True
        with self.assertRaises(RuntimeError): await event.send(event.result)
        self.assertTrue(event.stopped)
        self.assertIsNone(event.get_extra('companion_voice_delivery_receipt'))
        self.friends.sleep_inbox.add.assert_called_once()

    async def test_friend_and_owner_keep_send_evidence_separate_in_actual_pipeline(self):
        for sender, verified, segmented in [('100000001', False, False), ('99', True, True)]:
            with self.subTest(sender=sender):
                event = Event(sender=sender, verified=verified)
                event.message_str = '今天聊点什么'
                event.set_extra('companion_breeze_route_enabled', True)
                response = LLMResponse(role='assistant', completion_text='[[voice]]我在这里，今天过得怎么样？')
                await self.plugin.choose_medium(event, response)
                event.result.chain = [Plain(response.completion_text)]
                sent = []
                async def transport(chain):
                    sent.append(chain)
                    self.tracker.observe('send_private_msg', {'user_id': sender, 'message': [{'type': 'record'}]}, {'message_id': -199})
                event.send = transport
                await self.plugin.reply_voice(event)
                self.assertIsInstance(event.result.chain[0], Record)
                self.assertIsNone(event.get_extra('companion_voice_delivery_receipt'))
                stage = RespondStage.__new__(RespondStage)
                stage.platform_settings = {}; stage.enable_seg = segmented
                stage.only_llm_result = True; stage.interval_method = 'random'; stage.interval = [0, 0]
                after = AsyncMock(return_value=False)
                with patch('astrbot.core.pipeline.respond.stage.call_event_hook', after):
                    await stage.process(event)
                self.assertEqual(len(sent), 1)
                self.assertEqual(event.get_extra('companion_voice_delivery_receipt'), '-199')
                self.assertEqual(event.get_extra('companion_voice_delivered_text'), '我在这里，今天过得怎么样？')

    async def test_sleep_crossing_synthesis_requeues_without_sending(self):
        event = Event()
        event.send = AsyncMock()
        def synth(_): self.sleep = True; return AUDIO
        self.plugin.client.synthesize.side_effect = synth
        await self.plugin.reply_voice(event)
        self.assertTrue(event.stopped)
        self.friends.sleep_inbox.add.assert_called_once()
        event.send.assert_not_called()

    async def test_provider_failure_keeps_text_and_no_second_attempt(self):
        event = Event(); event.send = AsyncMock()
        self.plugin.client.synthesize.side_effect = TimeoutError()
        await self.plugin.reply_voice(event); await self.plugin.reply_voice(event)
        self.plugin.client.synthesize.assert_called_once()
        self.assertIsInstance(event.result.chain[0], Plain)
        self.assertIsNone(event.get_extra('companion_voice_delivery_receipt'))

    async def test_uncertain_send_never_retries_or_falls_back(self):
        event = Event(); transport = AsyncMock(side_effect=TimeoutError())
        event.send = transport
        await self.plugin.reply_voice(event)
        with self.assertRaises(TimeoutError): await event.send(event.result)
        transport.assert_awaited_once()
        self.assertIsNone(event.get_extra('companion_voice_delivery_receipt'))

    async def test_explicit_gateway_rejection_falls_back_with_confirmed_text(self):
        event = Event(); calls = []
        async def transport(chain):
            voice = isinstance(chain.chain[0], Record); calls.append(voice)
            self.tracker.observe('send_private_msg', {'user_id': event.sender, 'message': [{'type': 'record' if voice else 'text'}]},
                                 {'status': 'failed', 'retcode': 100} if voice else {'message_id': -201})
        event.send = transport
        await self.plugin.reply_voice(event)
        await event.send(event.result)
        self.assertEqual(calls, [True, False])
        self.assertEqual(event.get_extra('companion_voice_delivered_as'), 'text')


if __name__ == '__main__': unittest.main()
