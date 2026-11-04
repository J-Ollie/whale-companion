"""AstrBot runtime hook integration using isolated synthetic contacts."""
import asyncio
import json
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from astrbot_plugin_companion_memory.main import CompanionMemory
from astrbot_plugin_companion_memory.memory import MemoryStore
from astrbot_plugin_companion_memory.profile import SharedProfile
from astrbot_plugin_companion_memory.growth import review


class HookTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = MemoryStore(self.tmp.name)
        self.sleeping = False
        self.p = CompanionMemory.__new__(CompanionMemory)
        self.p.store = self.store
        self.p.config = {}
        self.p.owner = '100000001'
        self.p.umo = 'qq-companion:FriendMessage:'+self.p.owner
        self.p.resting = lambda: self.sleeping
        self.p.friends_plugin = lambda: SimpleNamespace(policy=SimpleNamespace(is_friend=AsyncMock(return_value=True)))
        self.p.context = SimpleNamespace(get_config=lambda **kw: {'provider_settings': {'enable': True}},
            llm_generate=AsyncMock(return_value=SimpleNamespace(completion_text='{"delta":0,"emotions":[],"resolve":[],"quote":""}')))

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def event(self, qq=None, verified=False, group=False):
        qq = qq or self.p.owner
        extra = {'companion_friend_verified': verified}
        return SimpleNamespace(get_sender_id=lambda: qq, message_str='今天找到一个很有趣的问题',
            message_obj=SimpleNamespace(message_id='synthetic-1'),
            unified_msg_origin=f'qq-companion:{"GroupMessage" if group else "FriendMessage"}:{qq}',
            get_extra=lambda k, d=None: extra.get(k, d), set_extra=lambda k, v: extra.__setitem__(k, v))

    async def test_owner_and_verified_friend_receive_request_local_state(self):
        for event in (self.event(), self.event('12345678', True)):
            req = SimpleNamespace(extra_user_content_parts=[])
            await self.p.inject_affect(event, req)
            self.assertEqual(len(req.extra_user_content_parts), 1)
            self.assertIn('<companion_expressive_state>', req.extra_user_content_parts[0].text)

    async def test_unverified_group_and_removed_friend_receive_nothing(self):
        for event in (self.event('12345678'), self.event('12345678', True, True)):
            req = SimpleNamespace(extra_user_content_parts=[])
            await self.p.inject_affect(event, req)
            self.assertFalse(req.extra_user_content_parts)
        self.p.friends_plugin = lambda: SimpleNamespace(policy=SimpleNamespace(is_friend=AsyncMock(return_value=False)))
        await self.p.inject_affect(self.event('12345678', True), SimpleNamespace(extra_user_content_parts=[]))
        self.p.context.llm_generate.assert_not_awaited()

    async def test_tool_round_or_request_retry_does_not_duplicate_state_or_call(self):
        event = self.event()
        req = SimpleNamespace(extra_user_content_parts=[])
        await self.p.inject_affect(event, req)
        await self.p.inject_affect(event, req)
        self.assertEqual(len(req.extra_user_content_parts), 1)
        self.assertEqual(self.p.context.llm_generate.await_count, 1)

    async def test_disabled_and_sleeping_hooks_do_not_read_or_call(self):
        self.sleeping = True
        req = SimpleNamespace(extra_user_content_parts=[])
        await self.p.inject_affect(self.event(), req)
        self.sleeping = False
        self.p.config['affect_enabled'] = False
        await self.p.inject_affect(self.event(), req)
        self.p.context.llm_generate.assert_not_awaited()
        self.assertFalse(req.extra_user_content_parts)

    async def test_only_validated_cross_day_growth_can_move_shared_axis(self):
        now = 1793674800
        self.p.profile = SharedProfile(self.store.folder)
        self.p.processing = asyncio.Lock()
        a = self.store.observe(self.p.owner, 'user', '这个问题我们再一起研究看看', at=now-2*86400)
        b = self.store.observe(self.p.owner, 'user', '今天又有一个问题我们一起研究', at=now-86400)
        self.p.context.llm_generate.return_value = SimpleNamespace(completion_text=json.dumps({'growth': {
            'text': '更愿意顺着共同探索的问题继续理解原因', 'axis': 'curiosity', 'delta': 1,
            'evidence': [{'source_id': a, 'quote': '这个问题我们再一起研究看看'},
                         {'source_id': b, 'quote': '今天又有一个问题我们一起研究'}]}}))
        await review(self.p, now)
        self.assertEqual(self.store.get_meta('character_axes_v1')['values']['curiosity'], 1)
        self.assertEqual(self.p.context.llm_generate.await_count, 1)

    async def test_invalid_growth_never_moves_axis(self):
        now = 1793674800
        self.p.profile = SharedProfile(self.store.folder)
        self.p.processing = asyncio.Lock()
        self.store.observe(self.p.owner, 'user', '这个问题我们再一起研究看看', at=now-2*86400)
        self.store.observe(self.p.owner, 'user', '今天又有一个问题我们一起研究', at=now-86400)
        self.p.context.llm_generate.return_value = SimpleNamespace(completion_text=json.dumps({'growth': {
            'text': '更愿意继续理解原因', 'axis': 'curiosity', 'delta': 1,
            'evidence': [{'source_id': 999, 'quote': '假的原话'}, {'source_id': 1000, 'quote': '假的原话'}]}}))
        await review(self.p, now)
        self.assertIsNone(self.store.get_meta('character_axes_v1'))


if __name__ == '__main__':
    unittest.main()
