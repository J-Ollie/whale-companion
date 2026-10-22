"""Behavior tests, using the deployed AstrBot APIs without sending QQ messages."""
import asyncio
import sys
import os
import unittest
from types import SimpleNamespace
sys.path.insert(0, os.environ.get('COMPANION_TEST_PLUGINS','/AstrBot/data/plugins'))
from astrbot_plugin_companion_batch.main import CompanionBatch
from astrbot.api.message_components import Plain


class Event:
    def __init__(self, text, owner='100000001', platform='qq-companion'):
        self.message_str = text
        self.message_obj = SimpleNamespace(message_str=text, message=[Plain(text)])
        self.unified_msg_origin = f'{platform}:FriendMessage:{owner}'
        self.owner, self.platform = owner, platform
        self.stopped, self.suppressed = False, False
    def get_platform_id(self): return self.platform
    def get_sender_id(self): return self.owner
    def should_call_llm(self, value): self.suppressed = value
    def stop_event(self): self.stopped = True


class BatchTests(unittest.IsolatedAsyncioTestCase):
    def plugin(self, quiet=.06, maximum=.15):
        plugin = CompanionBatch(None, {'quiet_seconds': quiet, 'max_seconds': maximum})
        return plugin

    async def test_single_waits(self):
        plugin, event = self.plugin(), Event('A')
        start = asyncio.get_running_loop().time()
        await plugin.merge(event)
        self.assertGreaterEqual(asyncio.get_running_loop().time()-start, .05)
        self.assertFalse(event.suppressed)
        self.assertFalse(plugin.pending)

    async def test_trailing_wait_and_one_request(self):
        plugin, first, second = self.plugin(), Event('A'), Event('B')
        task = asyncio.create_task(plugin.merge(first))
        await asyncio.sleep(.04)
        last = asyncio.get_running_loop().time()
        await plugin.merge(second)
        await task
        self.assertGreaterEqual(asyncio.get_running_loop().time()-last, .05)
        self.assertEqual(first.message_str, 'A\nB')
        self.assertTrue(second.stopped and second.suppressed)
        self.assertEqual(len(first.message_obj.message), 3)

    async def test_continuous_messages_hit_maximum(self):
        plugin, first = self.plugin(.06, .15), Event('0')
        start = asyncio.get_running_loop().time()
        task = asyncio.create_task(plugin.merge(first))
        for value in range(1, 5):
            await asyncio.sleep(.03)
            await plugin.merge(Event(str(value)))
        await task
        elapsed = asyncio.get_running_loop().time()-start
        self.assertGreaterEqual(elapsed, .14)
        self.assertLess(elapsed, .20)
        self.assertEqual(first.message_str, '0\n1\n2\n3\n4')

    async def test_commands_and_other_users_bypass(self):
        plugin = self.plugin()
        for event in [Event('/reset'), Event('A', owner='1'), Event('A', platform='other')]:
            await plugin.merge(event)
            self.assertFalse(event.stopped)
            self.assertFalse(plugin.pending)

    async def test_unload_cancels_waiters(self):
        plugin = self.plugin(1, 2)
        task = asyncio.create_task(plugin.merge(Event('A')))
        await asyncio.sleep(.01)
        await plugin.terminate()
        self.assertTrue(task.cancelled())
        self.assertFalse(plugin.pending)


if __name__ == '__main__':
    unittest.main(verbosity=2)
