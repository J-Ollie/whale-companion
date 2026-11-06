"""Exercise real framework compaction and history serialization, without model/QQ calls."""
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest

sys.path.insert(0, os.environ.get('COMPANION_TEST_PLUGINS', str(Path(__file__).parent)))
from astrbot.core.agent.context.config import ContextConfig
from astrbot.core.agent.context.manager import ContextManager
from astrbot.core.agent.context.truncator import ContextTruncator
from astrbot.core.agent.message import Message, dump_messages_with_checkpoints
from astrbot.core.platform.astr_message_event import current_message_event
from astrbot_plugin_companion_chat.context_guard import MARKER, install


def msg(role, text):
    return Message(role=role, content=text)


class RoundGuardTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.original = ContextManager.process
        self.restore = install()
        self.addCleanup(self.restore)
        self.event = SimpleNamespace(get_extra=lambda k, default=None: True if k == MARKER else default)
        self.token = current_message_event.set(self.event)
        self.addCleanup(current_message_event.reset, self.token)

    def sample(self):
        return [msg('system', '合成角色资料'), msg('user', '旧问题：准备好了吗'),
                msg('assistant', '旧回复：准备好了'), msg('user', '新问题：请画全身照')]

    def manager(self, limit=16384, max_turns=12):
        return ContextManager(ContextConfig(max_context_tokens=limit, enforce_max_turns=max_turns, truncate_turns=2))

    async def test_reproduces_unmarked_framework_fault(self):
        token = current_message_event.set(None)
        try:
            messages = self.sample()
            result = await self.manager().process(messages, trusted_token_usage=15008)
            self.assertEqual(result, messages[:2])
            self.assertNotIn(messages[-1], result)
        finally:
            current_message_event.reset(token)

    async def test_prior_total_usage_does_not_delete_new_question(self):
        messages = self.sample()
        self.assertEqual(await self.manager().process(messages, trusted_token_usage=15008), messages)

    async def test_real_token_pressure_keeps_latest_even_when_oversized(self):
        messages = self.sample()
        messages[-1] = msg('user', '本轮独有问题'*1000)
        result = await self.manager(limit=100).process(messages, trusted_token_usage=15008)
        self.assertEqual(result, [messages[0], messages[-1]])

    async def test_soft_pressure_preserves_adjacent_topic_before_image_acknowledgement(self):
        image = Message.model_validate({'role':'user','content':[
            {'type':'text','text':'[图片]'},
            {'type':'image_url','image_url':{'url':'https://example.invalid/ack.png'}}]})
        messages = [msg('system', '固定资料'*4000), msg('user', '我准备去考试了'), msg('assistant', '去吧'),
                    msg('user', '想好名字了吗'), msg('assistant', '还没定下来'),
                    msg('user', '记得这件事，我等着呢'), msg('assistant', '记得'), image]
        manager = self.manager()
        limit = int(manager.token_counter.count_tokens(messages) / .90)
        manager.config.max_context_tokens = limit
        result = await manager.process(messages)
        self.assertEqual(result, messages)
        self.assertLessEqual(manager.token_counter.count_tokens(result), limit)
        saved = dump_messages_with_checkpoints(result[1:])
        self.assertIn('想好名字', str(saved))
        self.assertIs(result[-1], image)

    async def test_adjacency_floor_does_not_exceed_hard_limit(self):
        messages = [msg('system','小段说明'), msg('user','旧话'*1000), msg('assistant','旧答'*1000),
                    msg('user','这一轮要保留')]
        manager = self.manager(limit=100)
        result = await manager.process(messages)
        self.assertEqual(result, [messages[0], messages[-1]])

    async def test_hard_pressure_drops_only_the_rounds_needed_and_keeps_tool_pairs(self):
        current = [msg('user','本轮问题'),
                   Message(role='assistant',tool_calls=[{'id':'t','type':'function','function':{'name':'test','arguments':'{}'}}]),
                   Message(role='tool',tool_call_id='t',content='真实工具结果')]
        system = msg('system','必要说明')
        adjacent = [msg('user','紧邻的话'), msg('assistant','紧邻的答复')]
        messages = [system, msg('user','旧资料'*3000), msg('assistant','旧回复')]+adjacent+current
        manager = self.manager()
        manager.config.max_context_tokens = manager.token_counter.count_tokens([system]+adjacent+current)+1
        result = await manager.process(messages)
        self.assertEqual(result, [system]+adjacent+current)
        self.assertLessEqual(manager.token_counter.count_tokens(result), manager.config.max_context_tokens)

    async def test_drop_more_rounds_than_available_keeps_current(self):
        messages = self.sample()
        self.assertEqual(ContextTruncator().truncate_by_dropping_oldest_turns(messages, 20), [messages[0], messages[-1]])

    async def test_incomplete_current_turn_counts_as_a_round(self):
        messages = self.sample()
        result = await self.manager(max_turns=1).process(messages)
        self.assertEqual(result, [messages[0], messages[-1]])

    async def test_tool_chain_stays_with_its_user(self):
        messages = self.sample()+[
            Message(role='assistant', tool_calls=[{'id':'synthetic','type':'function','function':{'name':'synthetic_tool','arguments':'{}'}}]),
            Message(role='tool', tool_call_id='synthetic', content='合成结果'),
        ]
        result = ContextTruncator().truncate_by_dropping_oldest_turns(messages, 2)
        self.assertEqual(result, [messages[0]]+messages[3:])

    async def test_halving_keeps_latest_complete_round(self):
        messages = self.sample()+[msg('assistant', '第二轮回复'),msg('user','第三轮问题')]
        result = ContextTruncator().truncate_by_halving(messages)
        self.assertEqual(result, [messages[0]]+messages[3:])

    async def test_serialized_history_accumulates_new_user_turn(self):
        messages = self.sample()
        compacted = await self.manager().process(messages, trusted_token_usage=15008)
        saved = dump_messages_with_checkpoints(compacted[1:]+[msg('assistant','新回复：生成全身照')])
        self.assertEqual([m['role'] for m in saved], ['user','assistant','user','assistant'])
        self.assertEqual(saved[-2]['content'], '新问题：请画全身照')

    async def test_other_requests_keep_original_behavior_and_marker_is_exact(self):
        for value in (False, 'true', 1, None):
            event = SimpleNamespace(get_extra=lambda k, default=None: value)
            token=current_message_event.set(event)
            try:
                messages=self.sample()
                # bool 1 must also be rejected by the identity check.
                result=await self.manager().process(messages, trusted_token_usage=15008)
                self.assertEqual(result, messages[:2])
            finally:
                current_message_event.reset(token)

    async def test_restore_is_owned_and_idempotent(self):
        self.restore()
        self.assertIs(ContextManager.process, self.original)
        self.restore()
        self.assertIs(ContextManager.process, self.original)

    async def test_no_input_mutation_or_new_model_calls(self):
        messages=self.sample()
        before=[m.model_dump() for m in messages]
        await self.manager().process(messages, trusted_token_usage=15008)
        self.assertEqual([m.model_dump() for m in messages], before)


if __name__ == '__main__':
    unittest.main()
