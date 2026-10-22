"""Exercise outreach limits, pause controls, and concurrency without QQ sends."""
import asyncio
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

sys.path.insert(0, os.environ.get('COMPANION_TEST_PLUGINS','/AstrBot/data/plugins'))
from astrbot_plugin_companion_active.main import CompanionActive, TZ, apply_control, eligible, parse_decision, send_outreach

NOW = datetime(2026, 10, 20, 15, 0, tzinfo=TZ).timestamp()
CONFIG = {'enabled': True, 'daily_limit': 4, 'min_interval_hours': 2, 'start_hour': 9, 'end_hour': 23}


class LimitTests(unittest.TestCase):
    def state(self):
        return {'last_user_at': NOW - 8000, 'last_bot_at': NOW - 8000, 'last_sent_at': NOW - 8000}

    def test_window_interval_daily_and_unanswered(self):
        state = self.state()
        self.assertTrue(eligible(state, NOW, CONFIG))
        for patch in [
            {'last_user_at': NOW-7199}, {'last_bot_at': NOW-7199}, {'paused': True},
            {'snooze_until': NOW+1}, {'unanswered': 1},
            {'day': '2026-10-20', 'daily_count': 4}, {'last_decision_at': NOW-10},
        ]:
            self.assertFalse(eligible(state | patch, NOW, CONFIG), patch)
        self.assertTrue(eligible(state | {'day': '2026-10-19', 'daily_count': 4}, NOW, CONFIG))
        for hour in [0, 8, 23]:
            point = datetime(2026, 10, 20, hour, tzinfo=TZ).timestamp()
            self.assertFalse(eligible({'last_user_at': point-10000}, point, CONFIG))

    def test_pause_busy_sleep_return(self):
        state = self.state()
        self.assertEqual(apply_control(state, '暂停主动聊天', NOW), 'paused')
        self.assertTrue(state['paused'])
        self.assertEqual(apply_control(state, '恢复主动', NOW), 'resumed')
        self.assertFalse(state['paused'])
        self.assertEqual(apply_control(state, '我现在忙，别打扰我', NOW), 'busy')
        self.assertEqual(state['snooze_until'], NOW+14400)
        self.assertEqual(apply_control(state, '忙完了，我回来了', NOW), 'returned')
        self.assertEqual(state['snooze_until'], 0)
        self.assertEqual(apply_control(state, '我要睡觉了，晚安', NOW), 'sleep')
        self.assertEqual(datetime.fromtimestamp(state['snooze_until'], TZ).isoformat(), '2026-10-21T09:00:00+08:00')

    def test_strict_decision_format(self):
        self.assertEqual(parse_decision('{"send":true,"message":"在干嘛"}'), '在干嘛')
        for raw in ['{"send":false,"message":"在干嘛"}', '{"send":"true","message":"在干嘛"}', '{"send":true,"message":""}', json.dumps({'send':True,'message':'鲸'*181})]:
            self.assertIsNone(parse_decision(raw))
        with self.assertRaises(ValueError):
            parse_decision('随便聊两句')


class FlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_own_sleep_blocks_outreach_before_model(self):
        plugin=self.plugin()
        phone=SimpleNamespace(outreach_allowed=Mock(return_value=False))
        plugin.context.get_registered_star=lambda name:SimpleNamespace(activated=True,star_cls=phone) if name=='astrbot_plugin_companion_phone' else None
        from unittest.mock import patch
        with patch('astrbot_plugin_companion_active.main.time.time',return_value=NOW):
            await plugin.consider()
        plugin.context.llm_generate.assert_not_called()
        plugin.context.send_message.assert_not_called()
        self.temporary.cleanup()

    async def test_real_phone_experience_is_available_to_outreach(self):
        plugin=self.plugin()
        phone=SimpleNamespace(outreach_allowed=Mock(return_value=True),
            recent_activity=Mock(return_value='真实浏览：酷安刷机帖子'),outreach_sent=Mock())
        plugin.context.get_registered_star=lambda name:SimpleNamespace(activated=True,star_cls=phone) if name=='astrbot_plugin_companion_phone' else None
        from unittest.mock import patch
        with patch('astrbot_plugin_companion_active.main.time.time',return_value=NOW):
            await plugin.consider()
        self.assertIn('酷安刷机帖子',plugin.context.llm_generate.await_args.kwargs['prompt'])
        phone.outreach_sent.assert_called_once()
        self.temporary.cleanup()

    async def test_sleep_started_during_decision_prevents_sending(self):
        plugin=self.plugin()
        phone=SimpleNamespace(outreach_allowed=Mock(side_effect=[True,False]),
            recent_activity=Mock(return_value='真实作息'),outreach_sent=Mock())
        plugin.context.get_registered_star=lambda name:SimpleNamespace(activated=True,star_cls=phone) if name=='astrbot_plugin_companion_phone' else None
        from unittest.mock import patch
        with patch('astrbot_plugin_companion_active.main.time.time',return_value=NOW):
            await plugin.consider()
        plugin.context.send_message.assert_not_called()
        self.temporary.cleanup()

    async def test_outreach_shares_persisted_memory(self):
        plugin=self.plugin()
        memory=SimpleNamespace(background_context=Mock(return_value='<companion_context>shared memory</companion_context>'),record_bot=Mock())
        plugin.context.get_registered_star=lambda name:SimpleNamespace(activated=True,star_cls=memory)
        from unittest.mock import patch
        with patch('astrbot_plugin_companion_active.main.time.time',return_value=NOW):
            await plugin.consider()
        self.assertIn('shared memory',plugin.context.llm_generate.await_args.kwargs['prompt'])
        memory.record_bot.assert_called_once_with('在干嘛')
        self.temporary.cleanup()

    async def test_segmented_send_has_real_gap_between_calls(self):
        from unittest.mock import patch
        context = SimpleNamespace(send_message=AsyncMock(return_value=True))
        sent_parts, order = [], []
        async def send(umo, chain):
            order.append(('send', chain.chain[0].text))
            return True
        async def sleep(delay):
            self.assertGreaterEqual(delay, 1.5)
            self.assertLessEqual(delay, 3.5)
            order.append(('wait', delay))
        context.send_message.side_effect = send
        with patch('astrbot_plugin_companion_active.main.asyncio.sleep', side_effect=sleep):
            await send_outreach(context, 'test', '来了。你呢？', {'enable':True, 'regex':r'.*?[。！？!?]+|.+$', 'interval':'1.5,3.5'}, sent_parts)
        self.assertEqual(sent_parts, ['来了', '你呢？'])
        self.assertEqual([item[0] for item in order], ['send', 'wait', 'send'])

    def plugin(self):
        self.temporary = tempfile.TemporaryDirectory()
        state_path = Path(self.temporary.name) / 'state.json'
        plugin = CompanionActive.__new__(CompanionActive)
        plugin.config = CONFIG
        plugin.state = {'last_user_at': 1, 'unanswered': 0}
        plugin.state_path = state_path
        manager = SimpleNamespace(
            get_curr_conversation_id=AsyncMock(return_value='test-cid'),
            get_conversation=AsyncMock(return_value=SimpleNamespace(history='[{"role":"user","content":"你好"}]')),
            update_conversation=AsyncMock(),
        )
        context = SimpleNamespace(
            get_config=lambda **kwargs: {'provider_settings': {'enable':True}, 'platform_settings':{'enable_id_white_list': True,'id_whitelist':[plugin.umo]}},
            conversation_manager=manager,
            persona_manager=SimpleNamespace(get_persona=AsyncMock(return_value=SimpleNamespace(system_prompt='小鲸鱼'))),
            llm_generate=AsyncMock(return_value=SimpleNamespace(completion_text='{"send":true,"message":"在干嘛"}')),
            send_message=AsyncMock(return_value=True),
        )
        plugin.context = context
        return plugin

    async def test_success_records_and_blocks_repeat(self):
        plugin = self.plugin()
        # Fixed local daytime avoids tests depending on server wall time.
        from unittest.mock import patch
        with patch('astrbot_plugin_companion_active.main.time.time', return_value=NOW):
            await plugin.consider()
            await plugin.consider()
        plugin.context.send_message.assert_awaited_once()
        self.assertEqual(plugin.context.send_message.await_args.args[0], plugin.umo)
        self.assertEqual(plugin.state['daily_count'], 1)
        self.assertEqual(plugin.state['unanswered'], 1)
        history = plugin.context.conversation_manager.update_conversation.await_args.kwargs['history']
        self.assertEqual(history[-1], {'role':'assistant','content':'在干嘛'})
        self.temporary.cleanup()

    async def test_new_user_message_during_decision_aborts(self):
        plugin = self.plugin()
        async def model(**kwargs):
            plugin.state['last_user_at'] = NOW
            return SimpleNamespace(completion_text='{"send":true,"message":"在干嘛"}')
        plugin.context.llm_generate.side_effect = model
        from unittest.mock import patch
        with patch('astrbot_plugin_companion_active.main.time.time', return_value=NOW):
            await plugin.consider()
        plugin.context.send_message.assert_not_awaited()
        self.temporary.cleanup()

    async def test_failed_send_does_not_repeat_on_restart(self):
        plugin = self.plugin()
        plugin.context.send_message.side_effect = RuntimeError('mock disconnected')
        from unittest.mock import patch
        with patch('astrbot_plugin_companion_active.main.time.time', return_value=NOW):
            with self.assertRaises(RuntimeError):
                await plugin.consider()
            persisted = json.loads(plugin.state_path.read_text())
            self.assertEqual(persisted['daily_count'], 1)
            self.assertEqual(persisted['unanswered'], 1)
            await plugin.consider()
        plugin.context.send_message.assert_awaited_once()
        self.temporary.cleanup()

    async def test_removed_whitelist_never_calls_model(self):
        plugin = self.plugin()
        plugin.context.get_config = lambda **kwargs: {'provider_settings':{'enable':True}, 'platform_settings':{'enable_id_white_list':True,'id_whitelist':[]}}
        from unittest.mock import patch
        with patch('astrbot_plugin_companion_active.main.time.time', return_value=NOW):
            await plugin.consider()
        plugin.context.llm_generate.assert_not_awaited()
        plugin.context.send_message.assert_not_awaited()
        self.temporary.cleanup()


if __name__ == '__main__':
    unittest.main(verbosity=2)

