"""Source-bound, cross-day exploration; all model/device operations are simulated."""
import asyncio
import copy
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.environ.get('COMPANION_TEST_PLUGINS', str(Path(__file__).parent)))
from astrbot_plugin_companion_phone.exploration import Exploration
import test_rhythm as flows

NOW = flows.NOW


def event(identifier='phone:one', at=NOW, **extra):
    return {'id': identifier, 'timestamp': at, 'app': '酷安', 'note': '读到一段电池原理的讨论',
            'views': 1, 'public_content': True, **extra}


def update(**extra):
    return {'question': '电池为什么会衰减', 'why': '想弄清讨论中的分歧',
            'progress': '看到两种解释，还需要核对', 'next_step': '继续找循环次数的解释', **extra}


class ExplorationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.clock = patch('time.time', return_value=NOW)
        self.clock.start()
        self.addCleanup(self.clock.stop)
        self.book = Exploration(self.tmp.name)

    def seed(self):
        self.assertTrue(self.book.observe(event(), update()))
        return self.book.rows[0]['id']

    def test_cross_day_restart_and_resolution_keep_sources(self):
        identifier = self.seed()
        later = NOW+86400
        with patch('time.time', return_value=later):
            restored = Exploration(self.tmp.name)
            self.assertTrue(restored.healthy)
            self.assertEqual(restored.select(identifier, '酷安')['next_step'], '继续找循环次数的解释')
            self.assertTrue(restored.observe(event('phone:two', later),
                {'id': identifier, 'progress': '看到新的解释，暂时保留判断', 'status': 'resolved'}, selected=identifier))
            final = Exploration(self.tmp.name)
            self.assertEqual(final.rows[0]['status'], 'resolved')
            self.assertEqual([s['id'] for s in final.rows[0]['sources']], ['phone:one', 'phone:two'])
            self.assertIsNone(final.select(identifier, '酷安'))

    def test_private_unknown_apps_and_empty_content_cannot_create(self):
        for extra in ({'public_content': False}, {'app': 'QQ'}, {'app': '相册'}, {'app': 'Via'},
                      {'views': 0}, {'views': True}, {'timestamp': float('nan')},
                      {'timestamp': NOW+3600}, {'id': 'diary:fake'}, {'note': ''}, {'app': []}):
            with self.subTest(extra=extra):
                self.assertFalse(self.book.observe(event(**extra), update()))
        self.assertEqual(self.book.rows, [])
        self.assertFalse(self.book.path.exists())

    def test_duplicate_observation_does_not_invent_new_progress(self):
        identifier = self.seed()
        before = self.book.path.read_bytes()
        self.assertFalse(self.book.observe(event(), {'id': identifier, 'progress': '我又学会了'}, selected=identifier))
        self.assertEqual(self.book.path.read_bytes(), before)

    def test_unknown_id_and_wrong_app_cannot_update_question(self):
        identifier = self.seed()
        before = copy.deepcopy(self.book.rows)
        self.assertFalse(self.book.observe(event('phone:two'), update(id='explore:fake')))
        self.assertFalse(self.book.observe(event('phone:two', app='哔哩哔哩'), update(id=identifier), selected=identifier))
        self.assertEqual(self.book.rows, before)

    def test_pause_needs_no_new_fact_and_resume_needs_observation(self):
        identifier = self.seed()
        self.assertFalse(self.book.defer(identifier, 'resolved'))
        self.assertTrue(self.book.defer(identifier, 'paused'))
        self.assertEqual(self.book.rows[0]['progress'], '看到两种解释，还需要核对')
        self.assertIsNotNone(self.book.select(identifier, '酷安'))
        self.assertTrue(self.book.observe(event('phone:two'), {'id': identifier}, selected=identifier))
        self.assertEqual(self.book.rows[0]['status'], 'open')

    def test_bounds_keep_existing_open_questions(self):
        for n in range(5):
            self.assertTrue(self.book.observe(event('phone:'+str(n)), update(question='问题'+str(n))))
        before = self.book.path.read_bytes()
        self.assertFalse(self.book.observe(event('phone:six'), update(question='另一个问题')))
        self.assertEqual(self.book.path.read_bytes(), before)

    def test_bad_state_is_preserved_and_only_notebook_is_disabled(self):
        self.book.path.write_text('{broken', encoding='utf-8')
        broken = Exploration(self.tmp.name)
        self.assertFalse(broken.healthy)
        self.assertFalse(broken.observe(event(), update()))
        self.assertEqual(self.book.path.read_text(), '{broken')

    def test_malformed_schema_is_not_silently_overwritten(self):
        self.seed()
        data = json.loads(self.book.path.read_text())
        data['rows'][0]['sources'][0]['app'] = 'QQ'
        self.book.path.write_text(json.dumps(data))
        before = self.book.path.read_bytes()
        broken = Exploration(self.tmp.name)
        self.assertFalse(broken.healthy)
        self.assertFalse(broken.defer(data['rows'][0]['id'], 'paused'))
        self.assertEqual(self.book.path.read_bytes(), before)

    def test_legacy_copy_requires_actual_public_source_and_preserves_pending(self):
        pending = [{'topic': '继续研究电池', 'source': 'phone:one'},
                   {'topic': '私聊里的事', 'source': 'phone:private'},
                   {'topic': '没有原始依据', 'source': 'phone:missing'}]
        before = copy.deepcopy(pending)
        self.book.seed_legacy(pending, [event(), event('phone:private', public_content=False)])
        self.book.seed_legacy(pending, [event()])
        self.assertEqual(len(self.book.rows), 1)
        self.assertEqual(pending, before)

    def test_context_filters_revoked_apps_and_is_bounded_valid_json(self):
        self.seed()
        self.assertNotIn('电池为什么', self.book.context(['QQ']))
        context = self.book.context(['酷安'])
        payload = context.split('：', 1)[1]
        self.assertLessEqual(len(payload.encode()), 2400)
        self.assertEqual(json.loads(payload)[0]['question'], '电池为什么会衰减')
        self.assertIn('不代表独立核实', context)

    def test_many_sources_keep_first_and_latest(self):
        identifier = self.seed()
        for n in range(1, 10):
            self.book.observe(event('phone:'+str(n)), {'id': identifier}, selected=identifier)
        self.assertEqual(len(self.book.rows[0]['sources']), 6)
        self.assertEqual(self.book.rows[0]['sources'][0]['id'], 'phone:one')
        self.assertEqual(self.book.rows[0]['sources'][-1]['id'], 'phone:9')

    def test_old_observation_cannot_overwrite_newer_progress(self):
        identifier = self.seed()
        self.assertFalse(self.book.observe(event('phone:old', NOW-1), {'id': identifier}, selected=identifier))
        self.assertEqual(len(self.book.rows[0]['sources']), 1)


class ExplorationFlowTests(unittest.IsolatedAsyncioTestCase):
    setUp = flows.RhythmFlowTests.setUp

    def seed(self):
        self.a.exploration.observe(event(), update())
        return self.a.exploration.rows[0]['id']

    async def test_new_question_from_background_content_uses_existing_two_calls(self):
        self.model.side_effect = [json.dumps({'activity': 'browse', 'app': flows.COOL}),
            json.dumps({'page_type': 'content', 'action': 'stop', 'note': '电池原理的讨论',
                        'public_content': True, 'exploration': update()}, ensure_ascii=False)]
        with patch('asyncio.sleep', AsyncMock()):
            await self.a.consider()
        self.assertEqual(len(self.a.exploration.rows), 1)
        self.assertEqual(self.model.await_count, 2)
        self.assertEqual(self.a.state['used'], 1)

    async def test_selected_question_reaches_screen_prompt_and_retains_goal(self):
        identifier = self.seed()
        self.model.side_effect = [json.dumps({'activity': 'browse', 'app': flows.COOL, 'exploration_id': identifier}),
            json.dumps({'page_type': 'content', 'action': 'stop', 'note': '看到了后续解释', 'public_content': True,
                        'exploration': {'id': identifier, 'progress': '发现后续解释', 'status': 'resolved'}}, ensure_ascii=False)]
        with patch('asyncio.sleep', AsyncMock()):
            await self.a.consider()
        self.assertIn('电池为什么会衰减', self.model.await_args_list[1].args[0])
        self.assertEqual(self.a.exploration.rows[0]['status'], 'resolved')
        self.assertEqual(len(self.a.exploration.rows[0]['sources']), 2)

    async def test_loading_cannot_turn_model_output_into_progress(self):
        self.model.side_effect = [json.dumps({'activity': 'browse', 'app': flows.COOL}),
            json.dumps({'page_type': 'loading', 'action': 'stop', 'note': '加载中', 'public_content': True,
                        'exploration': update(status='resolved')})]
        with patch('asyncio.sleep', AsyncMock()):
            await self.a.consider()
        self.assertEqual(self.a.exploration.rows, [])

    async def test_idle_can_shelve_but_not_resolve_without_reading(self):
        identifier = self.seed()
        self.model.return_value = json.dumps({'activity': 'idle', 'exploration_id': identifier, 'exploration_status': 'resolved'})
        await self.a.consider()
        self.assertEqual(self.a.exploration.rows[0]['status'], 'open')
        self.a.rhythm.state['next_decision'] = NOW-1
        self.model.return_value = json.dumps({'activity': 'idle', 'exploration_id': identifier, 'exploration_status': 'paused'})
        await self.a.consider()
        self.assertEqual(self.a.exploration.rows[0]['status'], 'paused')
        self.phone.open_app.assert_not_called()
        self.assertEqual(self.a.state['used'], 0)

    async def test_unknown_selection_does_not_start_device_task(self):
        self.model.return_value = json.dumps({'activity': 'browse', 'app': flows.COOL, 'exploration_id': 'explore:fake'})
        await self.a.consider()
        self.phone.open_app.assert_not_called()
        self.assertEqual(self.a.state['used'], 0)

    async def test_owner_task_does_not_become_self_chosen_exploration(self):
        self.model.side_effect = [json.dumps({'activity': 'browse', 'app': flows.COOL}),
            json.dumps({'page_type': 'content', 'action': 'stop', 'note': '电池原理', 'public_content': True, 'exploration': update()})]
        with patch('asyncio.sleep', AsyncMock()):
            await self.a.consider(True, '查看一个帖子')
        self.assertEqual(self.a.exploration.rows, [])
        self.assertEqual(self.a.state['used'], 0)
        self.assertEqual(self.a.state['requested_used'], 1)

    async def test_paused_or_asleep_do_not_review_or_update_exploration(self):
        self.seed()
        before = self.a.exploration.path.read_bytes()
        self.a.state['paused'] = True
        await self.a.consider()
        self.a.state['paused'] = False
        self.a.rhythm.state.update(mode='asleep', until=NOW+3600)
        await self.a.consider(True)
        self.model.assert_not_called()
        self.assertEqual(self.a.exploration.path.read_bytes(), before)

    async def test_interruption_during_decision_cannot_shelve_question(self):
        identifier = self.seed()
        async def interrupted(*args):
            self.a.state['last_user_at'] = NOW
            return json.dumps({'activity': 'idle', 'exploration_id': identifier, 'exploration_status': 'abandoned'})
        self.model.side_effect = interrupted
        with self.assertRaises(asyncio.CancelledError):
            await self.a.consider()
        self.assertEqual(self.a.exploration.rows[0]['status'], 'open')


if __name__ == '__main__':
    unittest.main(verbosity=2)
