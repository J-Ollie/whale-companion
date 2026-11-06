"""Unified registration, compatibility, durable maintenance and original state."""
import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, os.environ.get('COMPANION_TEST_PLUGINS', str(Path(__file__).parent)))
from astrbot_plugin_companion_chat.main import CompanionChat
from astrbot_plugin_companion_chat.lifecycle import ComponentContext, ConsumerContext, SectionConfig, IntakeGate
from astrbot_plugin_companion_chat.migration import merge_configs
from astrbot.core.star.star import star_map
from astrbot.core.star.star_handler import star_handlers_registry
from astrbot.api.message_components import Plain
from test_breeze_bridge import Event


class ConfigTests(unittest.TestCase):
    def test_mapping_preserves_every_setting_and_independent_switch(self):
        old = {'active': {'owner_qq': '1', 'enabled': False, 'daily_limit': 4, 'persona_id': '岚'},
               'batch': {'owner_qq': '1', 'enabled': True, 'quiet_seconds': 15., 'max_seconds': 60., 'natural_closure': True},
               'friends': {'owner_qq': '1', 'bot_qq': '2'},
               'breeze': {'owner_qq': '1', 'enabled': True, 'voice_id': 'fixed'}}
        value = merge_configs(old)
        for section, source in old.items():
            view = SectionConfig(value, section)
            for key, expected in source.items():
                self.assertEqual(view.get(key), expected)
        self.assertFalse(SectionConfig(value, 'active').get('enabled'))
        self.assertTrue(SectionConfig(value, 'breeze').get('enabled'))
        value['batch_quiet_seconds'] = 8
        self.assertEqual(SectionConfig(value, 'batch').get('quiet_seconds'), 8)
        self.assertEqual(old['batch']['quiet_seconds'], 15.)

    def test_conflicting_owner_aborts(self):
        old = {s: {'owner_qq': '1'} for s in ('active', 'batch', 'friends', 'breeze')}
        old['friends']['owner_qq'] = 'other'
        with self.assertRaises(ValueError):
            merge_configs(old)

    def test_only_root_registers_star_and_handlers(self):
        entries = [m for m in star_map if 'astrbot_plugin_companion_chat' in m]
        self.assertEqual(entries, ['astrbot_plugin_companion_chat.main'])
        hooks = [h for h in star_handlers_registry if 'astrbot_plugin_companion_chat' in h.handler_module_path]
        self.assertEqual(len(hooks), 31)
        self.assertTrue(all(h.handler_module_path == 'astrbot_plugin_companion_chat.main' for h in hooks))

    def test_component_context_does_not_mutate_global_registry(self):
        friends = object()
        original = SimpleNamespace(get_registered_star=Mock(return_value='external'))
        proxy = ComponentContext(original, SimpleNamespace(friends=friends))
        self.assertIs(proxy.get_registered_star('astrbot_plugin_companion_friends').star_cls, friends)
        self.assertEqual(proxy.get_registered_star('astrbot_plugin_companion_memory'), 'external')
        original.get_registered_star.assert_called_once_with('astrbot_plugin_companion_memory')

    def test_running_consumer_resolves_new_instance_after_reload_and_falls_back(self):
        current = SimpleNamespace(ready=True, friends=object())
        enabled = True
        legacy = object()
        def lookup(name):
            return SimpleNamespace(activated=enabled, star_cls=current) if name.endswith('_chat') else legacy
        context = ConsumerContext(SimpleNamespace(get_registered_star=lookup))
        self.assertIs(context.get_registered_star('astrbot_plugin_companion_friends').star_cls, current.friends)
        current = SimpleNamespace(ready=True, friends=object())
        self.assertIs(context.get_registered_star('astrbot_plugin_companion_friends').star_cls, current.friends)
        enabled = False
        self.assertIs(context.get_registered_star('astrbot_plugin_companion_friends'), legacy)


class GateTests(unittest.IsolatedAsyncioTestCase):
    async def test_friend_request_is_durable_deduplicated_and_not_notified_while_held(self):
        with tempfile.TemporaryDirectory() as tmp, patch('astrbot_plugin_companion_chat.lifecycle.get_astrbot_data_path', return_value=tmp):
            adapter = object()
            f = SimpleNamespace(platform_id='qq-companion', bot_qq='2', policy=SimpleNamespace(receive=AsyncMock()))
            context = SimpleNamespace(get_platform_inst=lambda _: adapter)
            original = AsyncMock()
            gate = IntakeGate(context, f, original)
            raw = {'self_id': 2, 'post_type': 'request', 'request_type': 'friend', 'user_id': 12345, 'flag': 'synthetic-request'}
            await gate.handle(adapter, SimpleNamespace(raw_message=raw))
            await gate.handle(adapter, SimpleNamespace(raw_message=raw))
            await gate.drain_requests()
            f.policy.receive.assert_not_awaited()
            fresh = IntakeGate(context, f, original)
            fresh.holding = False
            await fresh.drain_requests()
            f.policy.receive.assert_awaited_once_with(raw)
            await fresh.drain_requests()
            self.assertEqual(f.policy.receive.await_count, 1)

    async def test_hold_verifies_and_persists_without_reply_or_original_dispatch(self):
        adapter = object()
        f = SimpleNamespace(platform_id='qq-companion', bot_qq='2', policy=SimpleNamespace(is_friend=AsyncMock(return_value=True)),
                            sleep_inbox=SimpleNamespace(add=Mock()))
        context = SimpleNamespace(get_platform_inst=lambda _: adapter)
        original = AsyncMock()
        gate = IntakeGate(context, f, original)
        raw = {'post_type': 'message', 'message_type': 'private', 'self_id': 2, 'user_id': 1}
        await gate.handle(adapter, SimpleNamespace(raw_message=raw))
        f.policy.is_friend.assert_awaited_once_with('1')
        f.sleep_inbox.add.assert_called_once_with(raw)
        original.assert_not_awaited()
        f.policy.is_friend.return_value = False
        await gate.handle(adapter, SimpleNamespace(raw_message=raw))
        self.assertEqual(f.sleep_inbox.add.call_count, 1)
        gate.holding = False
        await gate.handle(adapter, SimpleNamespace(raw_message=raw))
        original.assert_awaited_once()

    async def test_hold_cannot_capture_other_platform_or_account(self):
        adapter = object()
        f = SimpleNamespace(platform_id='qq-companion', bot_qq='2', policy=Mock(), sleep_inbox=Mock())
        original = AsyncMock()
        gate = IntakeGate(SimpleNamespace(get_platform_inst=lambda _: adapter), f, original)
        for source, account in ((object(), 2), (adapter, 9)):
            await gate.handle(source, SimpleNamespace(raw_message={'self_id': account, 'post_type': 'message', 'message_type': 'private'}))
        self.assertEqual(original.await_count, 2)
        f.sleep_inbox.add.assert_not_called()


class UnifiedTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for suffix in ('active', 'friends', 'breeze'):
            p = patch('astrbot_plugin_companion_chat.' + suffix + '.main.get_astrbot_data_path', return_value=self.tmp.name)
            p.start(); self.addCleanup(p.stop)
        p = patch('astrbot_plugin_companion_chat.lifecycle.get_astrbot_data_path', return_value=self.tmp.name)
        p.start(); self.addCleanup(p.stop)
        self.context = SimpleNamespace(get_registered_star=lambda _: None)
        self.config = {'owner_qq': '100000001', 'platform_id': 'qq-companion', 'bot_qq': '100000002',
                       'active_enabled': False, 'breeze_enabled': True, 'batch_quiet_seconds': .01, 'batch_max_seconds': .02}

    def plugin(self):
        p = CompanionChat(self.context, self.config)
        p.ready = True
        return p

    async def test_existing_active_count_pause_and_voice_quota_unchanged(self):
        active = self.root / 'plugin_data/companion_active'; active.mkdir(parents=True)
        state = {'paused': True, 'daily_count': 4, 'last_attempt_at': 123, 'unanswered': 1}
        (active / 'state.json').write_text(json.dumps(state))
        breeze = self.root / 'plugin_data/companion_breeze'; breeze.mkdir()
        (breeze / 'state.json').write_text('{"day":"2026-11-03","attempts":20}')
        (breeze / 'credentials.json').write_text('{"api_key":"synthetic-only"}')
        before = {p: p.read_bytes() for folder in (active, breeze) for p in folder.glob('*.json')}
        p = self.plugin()
        for k, v in state.items(): self.assertEqual(p.active.state[k], v)
        self.assertEqual(p.breeze.client.root, breeze)
        self.assertTrue(all(path.read_bytes() == raw for path, raw in before.items()))
        self.assertEqual(p.friends.sleep_inbox.path.parent, self.root / 'plugin_data/companion_friends')

    async def test_voice_uses_internal_friends_after_legacy_disappears(self):
        p = self.plugin()
        self.assertIs(p.breeze.friends(), p.friends)
        p.friends.sleeping = lambda: False
        event = Event(); event.message_str = '这是你的声音吗'
        req = SimpleNamespace(system_prompt='existing', extra_user_content_parts=[])
        await p.voice_hint(event, req)
        self.assertIn('固定声线', req.system_prompt)

    async def test_wrapper_batch_still_merges_only_same_contact(self):
        from test_batch import Event as BatchEvent
        p = self.plugin()
        first, second = BatchEvent('一'), BatchEvent('二')
        task = asyncio.create_task(p.merge(first))
        await asyncio.sleep(.002)
        await p.merge(second)
        await task
        self.assertEqual(first.message_str, '一\n二')
        self.assertTrue(second.stopped)

    async def test_sleep_unread_completion_requires_voice_receipt(self):
        p = self.plugin()
        p.friends.sleep_inbox.mark = Mock()
        event = Event(); event.set_extra('companion_sleep_unread', [3]); event.set_extra('companion_voice_prepared', True)
        await p.message_sent(event)
        p.friends.sleep_inbox.mark.assert_not_called()
        event.set_extra('companion_voice_delivery_receipt', {'message_id': 'synthetic'})
        await p.message_sent(event)
        p.friends.sleep_inbox.mark.assert_called_once_with([3], 'completed')

    async def test_drain_holds_incoming_without_sleep_override(self):
        p = self.plugin(); p.friends.draining = True; p.friends.sleeping = lambda: False
        p.friends.policy.is_friend = AsyncMock(return_value=True)
        raw = {'self_id': 100000002, 'post_type': 'message', 'message_type': 'private', 'user_id': 100000001,
               'message_id': 777, 'message': [{'type': 'text', 'data': {'text': 'hello'}}]}
        self.assertTrue(await p.friends.intercept(None, SimpleNamespace(raw_message=raw, message_str='hello')))
        self.assertEqual(len(p.friends.sleep_inbox.pending()['100000001']), 1)

    async def test_maintenance_blocks_management_notifications(self):
        p = self.plugin(); p.friends.draining = True
        p.friends.context.send_message = AsyncMock()
        with self.assertRaises(RuntimeError):
            await p.friends.notify('synthetic')
        p.friends.context.send_message.assert_not_awaited()

    async def test_active_legacy_rejected_before_state_open(self):
        self.context.get_registered_star = lambda n: SimpleNamespace(activated=True, star_cls=object()) if n.endswith('_active') else None
        with self.assertRaises(RuntimeError): CompanionChat(self.context, self.config)

    async def test_active_standalone_voice_owns_hooks_and_busy_gate(self):
        from astrbot_plugin_companion_chat.lifecycle import bind_live_consumers
        external = SimpleNamespace(context=self.context, busy=True)
        original = self.context.get_registered_star
        self.context.get_registered_star = lambda name: SimpleNamespace(activated=True, star_cls=external) if name.endswith('_breeze') else original(name)
        p = self.plugin()
        self.assertTrue(p.voice_busy())
        p.breeze.voice_hint=AsyncMock();p.breeze.choose_medium=AsyncMock();p.breeze.reply_voice=AsyncMock()
        await p.voice_hint(Event(), SimpleNamespace())
        await p.choose_medium(Event(), SimpleNamespace())
        await p.reply_voice(Event())
        p.breeze.voice_hint.assert_not_awaited();p.breeze.choose_medium.assert_not_awaited();p.breeze.reply_voice.assert_not_awaited()
        bind_live_consumers(self.context)
        self.assertIsInstance(external.context, ConsumerContext)
        # The running independent service resolves the unified friend service.
        self.context.get_registered_star = lambda name: SimpleNamespace(activated=True, star_cls=p) if name.endswith('_chat') else None
        self.assertIs(external.context.get_registered_star('astrbot_plugin_companion_friends').star_cls,p.friends)

    async def test_real_service_reload_restores_hooks_and_reuses_intake_gate(self):
        import inspect
        from aiocqhttp import CQHttp
        from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_platform_adapter import AiocqhttpAdapter
        from astrbot.core.pipeline.whitelist_check.stage import WhitelistCheckStage
        original = (AiocqhttpAdapter.handle_msg, CQHttp.call_action, WhitelistCheckStage.process)
        self.context.get_platform_inst = lambda _: None
        p = CompanionChat(self.context, self.config)
        try:
            await p.initialize()
            gate = p.gate
            self.assertTrue(p.ready)
            self.assertFalse(gate.holding)
            await p.terminate()
            self.assertTrue(gate.holding)
            self.assertIs(AiocqhttpAdapter.handle_msg, gate.handle)
            self.assertIs(CQHttp.call_action, original[1])
            self.assertIs(WhitelistCheckStage.process, original[2])
            p2 = CompanionChat(self.context, self.config)
            await p2.initialize()
            self.assertIs(p2.gate, gate)
            self.assertIs(gate.friends, p2.friends)
            self.assertIs(inspect.getclosurevars(CQHttp.call_action).nonlocals['plugin'], p2.friends)
            self.assertIs(inspect.getclosurevars(CQHttp.call_action).nonlocals['original_call'], original[1])
            await p2.terminate()
        finally:
            AiocqhttpAdapter.handle_msg, CQHttp.call_action, WhitelistCheckStage.process = original

    async def test_busy_reload_defers_without_cancelling_task_or_losing_settings(self):
        p = self.plugin()
        p.gate = SimpleNamespace(holding=False)
        original = p.active.config
        with patch('astrbot_plugin_companion_chat.main.pipeline_tasks', return_value={object()}), \
             patch('astrbot_plugin_companion_chat.main.asyncio.sleep', new_callable=AsyncMock):
            with self.assertRaises(RuntimeError):
                await p.terminate()
        self.assertIs(p.active.config, original)
        self.assertTrue(p.ready)
        self.assertFalse(p.gate.holding)
        self.assertFalse(p.friends.draining)

    async def test_staging_cannot_start_duplicate_jobs_and_abort_restores_legacy(self):
        from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_platform_adapter import AiocqhttpAdapter
        original_handle = AiocqhttpAdapter.handle_msg
        async def original(adapter, message): pass
        async def legacy_handle(adapter, message): return await original(adapter, message)
        # Match the audited legacy restoration closure.
        def restore():
            return original
        active = SimpleNamespace(config={'enabled': True}, task=None)
        legacy = SimpleNamespace(platform_id='qq-companion', bot_qq='100000002', restore=restore,
                                 release_unread=AsyncMock(), policy=Mock(), sleep_inbox=Mock())
        release = legacy.release_unread
        rows = {'astrbot_plugin_companion_friends': SimpleNamespace(activated=True, star_cls=legacy),
                'astrbot_plugin_companion_active': SimpleNamespace(activated=True, star_cls=active)}
        self.context.get_registered_star = rows.get
        self.context.get_platform_inst = lambda _: None
        AiocqhttpAdapter.handle_msg = legacy_handle
        p = CompanionChat(self.context, self.config | {'maintenance_mode': True})
        try:
            await p.initialize()
            self.assertFalse(p.ready)
            self.assertFalse(active.config['enabled'])
            self.assertTrue(p.gate.holding)
            await p.terminate()
            self.assertTrue(active.config['enabled'])
            self.assertIs(legacy.release_unread, release)
            self.assertIs(AiocqhttpAdapter.handle_msg, legacy_handle)
            self.assertFalse(p.gate.holding)
        finally:
            AiocqhttpAdapter.handle_msg = original_handle


if __name__ == '__main__':
    unittest.main()
