"""Semantic bubble choices and native send stage, with no QQ/model/voice calls."""
import asyncio
import copy
import json
import os
from pathlib import Path
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, os.environ.get('COMPANION_TEST_PLUGINS', str(Path(__file__).parent)))
from astrbot_plugin_companion_chat import bubbles
from astrbot_plugin_companion_chat.active.main import CompanionActive
from astrbot_plugin_companion_chat.main import CompanionChat
from astrbot.api.message_components import Plain, Record
from astrbot.core.message.message_event_result import MessageEventResult, ResultContentType
from astrbot.core.pipeline.respond.stage import RespondStage
from test_breeze_bridge import Event

TEXT = '嗯，我知道你在说什么\n\n不过这件事，我还有个想法'


class SegmentTests(unittest.TestCase):
    def test_selected_boundaries_recover_both_user_examples_without_rewriting(self):
        examples = [('第一张我认，第二张那口Token看着有点扎心', ['第一张我认', '第二张那口Token看着有点扎心']),
                    ('没咋，就是被第二张表情包戳了一下', ['没咋', '就是被第二张表情包戳了一下'])]
        for text, expected in examples:
            self.assertEqual(bubbles.selected_parts(text, {'breaks':[0]}), expected)

    def test_unselected_commas_and_strong_punctuation_remain(self):
        text = '没什么，我收到啦，不过还想问你一句'
        self.assertEqual(bubbles.selected_parts(text, {'breaks':[1]}), ['没什么，我收到啦','不过还想问你一句'])
        self.assertEqual(bubbles.selected_parts('我收到消息了！还想再问你一句', {'breaks':[0]}), ['我收到消息了！','还想再问你一句'])

    def test_quotes_numbers_and_malformed_selection_are_not_cut(self):
        self.assertEqual(bubbles.boundaries('她说“没咋，就是有点累了”'), [])
        self.assertEqual(bubbles.boundaries('这次一共需要1,234元费用'), [])
        text = '没什么，我收到啦，不过还想问你一句'
        for raw in ({'breaks':[True]}, {'breaks':[-1]}, {'breaks':[12]}, {'breaks':[0,0]},
                    {'breaks':[1,0]}, {'breaks':[0,1,2]}, {'breaks':'0'}, {'text':'改写后的句子'}):
            self.assertEqual(bubbles.selected_parts(text, raw), [text])

    def test_one_complete_sentence_stays_one(self):
        for text in ('嗯，那我尽量多说人话', '你认真的？我觉得挺好！', '这是同一个意思\n只是排了个版'):
            self.assertEqual(bubbles.segments(text), [text])

    def test_model_blank_paragraph_selects_bubbles_without_extra_text(self):
        self.assertEqual(bubbles.segments(TEXT), ['嗯，我知道你在说什么', '不过这件事，我还有个想法'])
        text = '收到你的意思了\n\n这点我也同意\n\n另外还有一件事'
        parts = bubbles.segments(text)
        self.assertEqual(len(parts), 3)
        self.assertEqual(''.join(text.split()), ''.join(''.join(parts).split()))

    def test_structured_long_and_excess_breaks_keep_original(self):
        for text in ('```python\nx=1\n\ny=2\n```', '我列在下面\n\n- 第一项\n- 第二项',
                     '> 这是完整引用\n\n先不拆开', 'a' * 451 + '\n\n说明',
                     '第一点是这样\n\n第二点是这样\n\n第三点是这样\n\n第四点是这样'):
            self.assertEqual(bubbles.segments(text), [text])


class SendingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.event = Event()
        self.event.result = MessageEventResult(chain=[Plain(TEXT)])
        self.event.result.result_content_type = ResultContentType.LLM_RESULT
        self.friends = SimpleNamespace(sleeping=lambda: False, policy=SimpleNamespace(is_friend=AsyncMock(return_value=True)),
                                       sleep_inbox=SimpleNamespace(add=Mock(), mark=Mock()))
        self.sent = []
        self.confirm = True

        async def send(chain):
            self.sent.append([p.text for p in chain.chain if isinstance(p, Plain)])
            if self.confirm:
                bubbles.observe_delivery('send_private_msg', {'user_id': self.event.sender}, {'message_id': str(len(self.sent))})
        self.original = self.event.send = AsyncMock(side_effect=send)
        self.audit = SimpleNamespace(mark=Mock())

    async def test_two_calls_with_pause_only_between_and_complete_receipts(self):
        self.assertTrue(bubbles.install(self.event, self.friends, self.audit))
        with patch('astrbot_plugin_companion_chat.bubbles.asyncio.sleep', new_callable=AsyncMock) as pause:
            await self.event.send(self.event.result.derive(self.event.result.chain))
        self.assertEqual(self.sent, [[p] for p in bubbles.segments(TEXT)])
        pause.assert_awaited_once()
        self.assertEqual(self.friends.policy.is_friend.await_count, 2)
        self.assertTrue(self.event.get_extra(bubbles.COMPLETE))

    async def test_semantic_choice_uses_only_reply_and_can_choose_one_bubble(self):
        text = '如果明天下雨，我就带伞过去'
        self.event.result.chain = [Plain(text)]
        context = SimpleNamespace(llm_generate=AsyncMock(return_value=SimpleNamespace(completion_text='{"breaks":[]}')))
        self.assertEqual(await bubbles.choose(self.event, context, 'model'), [text])
        payload = json.loads(context.llm_generate.await_args.kwargs['prompt'])
        self.assertEqual(set(payload), {'reply','boundaries'})
        self.assertNotIn(self.event.message_str, payload['reply'])
        context.llm_generate.return_value.completion_text='{"breaks":[0]}'
        self.event.result.chain=[Plain('没咋，就是被第二张表情包戳了一下')]
        parts=await bubbles.choose(self.event, context, 'model')
        self.assertTrue(bubbles.install(self.event,self.friends,parts=parts))
        with patch('astrbot_plugin_companion_chat.bubbles.asyncio.sleep',new_callable=AsyncMock):
            await self.event.send(self.event.result.derive(self.event.result.chain))
        self.assertEqual(self.sent,[['没咋'],['就是被第二张表情包戳了一下']])

    async def test_planner_failure_keeps_complete_original_and_no_fixed_split(self):
        text='没咋，就是被第二张表情包戳了一下'
        self.event.result.chain=[Plain(text)]
        context=SimpleNamespace(llm_generate=AsyncMock(side_effect=TimeoutError()))
        self.assertEqual(await bubbles.choose(self.event,context,'model'),[text])
        context.llm_generate.assert_awaited_once()

    async def test_media_layout_and_explicit_paragraphs_do_not_call_planner(self):
        context=SimpleNamespace(llm_generate=AsyncMock())
        self.assertEqual(await bubbles.choose(self.event,context,'model'),bubbles.segments(TEXT))
        self.event.result.chain=[Plain('```python\nx=1\n\ny=2\n```')]
        await bubbles.choose(self.event,context,'model')
        self.event.result.chain=[Record.fromBase64('aGVsbG8=')]
        self.assertIsNone(await bubbles.choose(self.event,context,'model'))
        context.llm_generate.assert_not_awaited()

    async def test_forged_planner_cannot_rewrite_body(self):
        self.assertFalse(bubbles.install(self.event,self.friends,parts=['替换第一句话','替换第二句话']))
        self.assertIs(self.event.send,self.original)

    async def test_sleep_between_bubbles_stops_suffix_and_marks_partial(self):
        bubbles.install(self.event, self.friends, self.audit)
        async def sleep(_): self.friends.sleeping = lambda: True
        with patch('astrbot_plugin_companion_chat.bubbles.asyncio.sleep', side_effect=sleep):
            with self.assertRaises(RuntimeError):
                await self.event.send(self.event.result.derive(self.event.result.chain))
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.event.result.get_plain_text(), bubbles.segments(TEXT)[0])
        self.assertFalse(self.event.get_extra(bubbles.COMPLETE))
        self.friends.sleep_inbox.add.assert_not_called()
        self.audit.mark.assert_called_once_with(self.event, 'partial_send', 'bubble_interrupted')

    async def test_deleted_friend_stops_suffix(self):
        self.friends.policy.is_friend.side_effect = [True, False]
        bubbles.install(self.event, self.friends)
        with patch('astrbot_plugin_companion_chat.bubbles.asyncio.sleep', new_callable=AsyncMock):
            with self.assertRaises(RuntimeError):
                await self.event.send(self.event.result.derive(self.event.result.chain))
        self.assertEqual(len(self.sent), 1)

    async def test_no_receipt_is_not_success_and_never_retried(self):
        self.confirm = False
        bubbles.install(self.event, self.friends)
        chain = self.event.result.derive(self.event.result.chain)
        with self.assertRaises(RuntimeError): await self.event.send(chain)
        with self.assertRaises(RuntimeError): await self.event.send(chain)
        self.assertEqual(len(self.sent), 1)
        self.assertFalse(self.event.get_extra(bubbles.COMPLETE))
        self.assertIsNone(self.event.get_result())

    async def test_record_and_commands_keep_transport(self):
        self.event.result.chain = [Record.fromBase64('aGVsbG8=')]
        self.assertFalse(bubbles.install(self.event, self.friends))
        self.assertIs(self.event.send, self.original)
        self.event.result.chain = [Plain(TEXT)]
        self.event.result.result_content_type = ResultContentType.GENERAL_RESULT
        self.assertFalse(bubbles.install(self.event, self.friends))

    async def test_native_respond_stage_keeps_core_segmentation_off_but_sends_selected_bubbles(self):
        cfg = json.loads(Path('/AstrBot/data/cmd_config.json').read_text(encoding='utf-8-sig'))
        cfg = copy.deepcopy(cfg)
        cfg['platform_settings']['segmented_reply']['enable'] = False
        stage = RespondStage()
        await stage.initialize(SimpleNamespace(astrbot_config=cfg))
        bubbles.install(self.event, self.friends)
        with patch('astrbot.core.pipeline.respond.stage.call_event_hook', new_callable=AsyncMock, return_value=False), \
             patch('astrbot_plugin_companion_chat.bubbles.asyncio.sleep', new_callable=AsyncMock):
            await stage.process(self.event)
        self.assertEqual(len(self.sent), 2)
        self.assertTrue(self.event.get_extra(bubbles.COMPLETE))

    async def test_partial_turn_does_not_complete_unread_or_followups(self):
        owner = CompanionChat.__new__(CompanionChat); owner.ready = True
        owner.active = SimpleNamespace(replied=AsyncMock())
        owner.batch = SimpleNamespace(replied=AsyncMock())
        owner.friends = SimpleNamespace(unread_answered=AsyncMock())
        owner.followups = SimpleNamespace(sent=Mock())
        self.event.set_extra(bubbles.ACTIVE, True)
        self.event.set_extra(bubbles.COMPLETE, False)
        self.event.set_extra(bubbles.SENT, 1)
        await owner.message_sent(self.event)
        owner.friends.unread_answered.assert_not_awaited()
        owner.followups.sent.assert_not_called()
        owner.active.replied.assert_awaited_once()


class OutreachDependencyTests(unittest.TestCase):
    def test_optional_thought_module_import_failure_keeps_base_verified_activity(self):
        active = CompanionActive.__new__(CompanionActive)
        phone = SimpleNamespace(recent_activity=Mock(side_effect=ModuleNotFoundError('synthetic module')),
                                autonomy=SimpleNamespace(context=Mock(return_value='verified base activity')))
        self.assertEqual(active.activity_context(phone), 'verified base activity')
        self.assertEqual(active.activity_context(None), '')

    def test_dependency_failure_does_not_invent_activity(self):
        active = CompanionActive.__new__(CompanionActive)
        phone = SimpleNamespace(recent_activity=Mock(side_effect=ImportError()),
                                autonomy=SimpleNamespace(context=Mock(side_effect=ImportError())))
        self.assertEqual(active.activity_context(phone), '')

    def test_healthy_summary_is_not_replaced(self):
        active = CompanionActive.__new__(CompanionActive)
        self.assertEqual(active.activity_context(SimpleNamespace(recent_activity=lambda:'current summary')), 'current summary')
