"""Synthetic state tests. No real chats, keys, or production data."""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from astrbot_plugin_companion_memory.memory import MemoryStore
from astrbot_plugin_companion_memory.affect import (
    Affect, AXES, EMOTIONS, SYSTEM, fresh, evolve, transition, affect_summary,
    validate_decision, advance_character, character_summary)

NOW = 1793674800.0


def decision(delta=0, emotions=(), resolve=(), intensity=2):
    return {'delta': delta, 'intensity': intensity, 'emotions': list(emotions), 'resolve': list(resolve)}


class StateTests(unittest.TestCase):
    def test_seven_grades_and_nineteen_emotions(self):
        self.assertEqual(len(EMOTIONS), 19)
        for i, label in [(-3, '非常生气'), (0, '平静'), (3, '十分开心')]:
            s = fresh(NOW); s['mood'] = i
            self.assertEqual(affect_summary(s, NOW)['心情'], label)

    def test_repeated_insults_accumulate_within_minutes(self):
        s = fresh(NOW)
        values = []
        for offset in (0, 15, 30, 45, 60):
            s = transition(s, decision(-1, ['hurt']), NOW+offset)
            values.append(s['mood'])
        self.assertTrue(all(b < a for a, b in zip(values, values[1:])))
        self.assertEqual(s['mood'], -3)
        self.assertGreater(s['emotions'][0]['strength'], 2)

    def test_one_repair_cannot_jump_from_anger_to_happiness(self):
        s = fresh(NOW); s['mood'] = -0.2
        s = transition(s, decision(1, ['touched'], intensity=3), NOW)
        self.assertEqual(s['mood'], 0)
        s = transition(s, decision(1, ['happy']), NOW+15)
        self.assertGreater(s['mood'], 0)

    def test_eventually_decays_without_new_messages(self):
        s = transition(fresh(NOW), decision(-1, ['hurt']), NOW)
        self.assertEqual(evolve(s, NOW+86400)['mood'], 0)
        self.assertEqual(evolve(s, NOW+86400)['emotions'], [])

    def test_only_a_new_grounded_trigger_can_reinforce_and_extend(self):
        s = transition(fresh(NOW), decision(0, ['proud']), NOW)
        before = s['emotions'][0]['strength']
        idle = evolve(s, NOW+900)
        self.assertLess(idle['emotions'][0]['strength'], before)
        self.assertEqual(idle['emotions'][0]['until'], NOW+25*60)
        strengthened = transition(s, decision(0, ['proud']), NOW+900)
        self.assertGreater(strengthened['emotions'][0]['strength'], idle['emotions'][0]['strength'])
        self.assertEqual(strengthened['emotions'][0]['until'], NOW+900+25*60)
        self.assertEqual(s['emotions'][0]['until'], NOW+25*60)
        self.assertFalse(evolve(s, NOW+25*60)['emotions'])

    def test_apology_softens_but_does_not_delete_all_state(self):
        s = transition(fresh(NOW), decision(-1, ['hurt']), NOW)
        s = transition(s, decision(1, ['touched'], ['hurt']), NOW+60)
        self.assertEqual(s['mood'], 0)
        hurt = next(e for e in s['emotions'] if e['kind'] == 'hurt')
        self.assertFalse(hurt['waiting'])
        self.assertLess(hurt['strength'], 0.4)
        self.assertEqual(hurt['until'], NOW+3660)
        self.assertIn('感动', affect_summary(s, NOW+60)['余绪'])

    def test_three_emotions_max_but_new_strong_one_displaces_weak_residue(self):
        s = transition(fresh(NOW), decision(0, ['proud', 'curious', 'surprised'], intensity=1), NOW)
        s = transition(s, decision(-1, ['hurt'], intensity=3), NOW+20)
        self.assertEqual(len(s['emotions']), 3)
        self.assertIn('hurt', [e['kind'] for e in s['emotions']])

    def test_strong_unresolved_hurt_survives_three_hours_but_not_forever(self):
        s = transition(fresh(NOW), decision(-1, ['hurt'], intensity=3), NOW)
        self.assertTrue(evolve(s, NOW+6*3600)['emotions'])
        self.assertEqual(s['emotions'][0]['until'], NOW+12*3600)
        self.assertFalse(evolve(s, NOW+12*3600)['emotions'])

    def test_intensity_changes_strength_and_horizon(self):
        weak = transition(fresh(NOW), decision(-1, ['hurt'], intensity=1), NOW)
        strong = transition(fresh(NOW), decision(-1, ['hurt'], intensity=3), NOW)
        self.assertLess(abs(weak['mood']), abs(strong['mood']))
        self.assertLess(weak['emotions'][0]['strength'], strong['emotions'][0]['strength'])
        self.assertLess(weak['emotions'][0]['until'], strong['emotions'][0]['until'])

    def test_ordinary_good_news_can_coexist_with_unrepaired_hurt(self):
        s = transition(fresh(NOW), decision(-1, ['hurt']), NOW)
        s = transition(s, decision(1, ['happy']), NOW+20)
        self.assertEqual({e['kind'] for e in s['emotions']}, {'hurt', 'happy'})
        self.assertTrue(next(e for e in s['emotions'] if e['kind'] == 'hurt')['waiting'])

    def test_insult_apology_and_quiet_recovery_form_continuous_sequence(self):
        s = fresh(NOW)
        for t in (0, 30, 60):
            s = transition(s, decision(-1, ['hurt']), NOW+t)
        angry = s['mood']
        old_strength = s['emotions'][0]['strength']
        s = transition(s, decision(1, ['relieved'], ['hurt']), NOW+90)
        self.assertGreater(s['mood'], angry)
        self.assertLess(s['mood'], 0)
        self.assertLess(next(e for e in s['emotions'] if e['kind']=='hurt')['strength'], old_strength)
        calm = evolve(s, NOW+12*3600)
        self.assertEqual(calm['mood'], 0)
        self.assertFalse(calm['emotions'])

    def test_upgrade_keeps_old_expiry_and_ignores_old_freeze(self):
        old = {'version': 1, 'mood': -1, 'updated': NOW, 'hold_until': NOW+1800,
               'emotions': [{'kind': 'hurt', 'since': NOW, 'until': NOW+10800, 'waiting': True}],
               'seen': ['old-id'], 'last_check': NOW, 'audit': []}
        upgraded = evolve(old, NOW+30)
        self.assertEqual(upgraded['version'], 2)
        self.assertEqual(upgraded['emotions'][0]['until'], old['emotions'][0]['until'])
        self.assertEqual(upgraded['seen'], ['old-id'])
        self.assertLess(transition(old, decision(-1), NOW+30)['mood'], -1)
        self.assertFalse(evolve(old, NOW+10800)['emotions'])
        self.assertEqual(old['version'], 1)

    def test_snapshot_does_not_depend_on_frequency_of_decay_reads(self):
        s = transition(fresh(NOW), decision(-1, ['hurt'], intensity=3), NOW)
        direct = evolve(s, NOW+3600)
        incremental = s
        for t in range(60, 3601, 60):
            incremental = evolve(incremental, NOW+t)
        self.assertAlmostEqual(direct['mood'], incremental['mood'])
        self.assertAlmostEqual(direct['emotions'][0]['strength'], incremental['emotions'][0]['strength'])

    def test_malformed_and_unquoted_decisions_are_rejected(self):
        for value in ({'delta': True}, {'delta': 7}, {'delta': -1, 'emotions': [], 'resolve': [], 'quote': '伪造'},
                      {'delta': 0, 'emotions': ['unknown'], 'resolve': [], 'quote': '原文'}):
            with self.assertRaises(ValueError):
                validate_decision(value, '原文')
        self.assertEqual(validate_decision({'delta': 0, 'emotions': [], 'resolve': [], 'quote': ''}, '普通聊天')['delta'], 0)
        for intensity in (True, 0, 4, '3'):
            with self.assertRaises(ValueError):
                validate_decision({'delta': 0, 'intensity': intensity, 'emotions': [], 'resolve': []}, '普通聊天')
        with self.assertRaises(ValueError):
            validate_decision({'delta': 0, 'emotions': ['hurt'], 'resolve': ['hurt'], 'quote': '原文'}, '原文')

    def test_state_render_contains_no_raw_reason_or_fixed_line(self):
        self.assertNotIn('本鱼', SYSTEM)
        self.assertNotIn('quote', json.dumps(fresh(NOW)))


class PersistentTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = MemoryStore(self.tmp.name)
        self.affect = Affect(self.store)
        self.sleeping = False
        self.model = AsyncMock(return_value=SimpleNamespace(completion_text=json.dumps({
            'delta': -1, 'emotions': ['hurt'], 'resolve': [], 'quote': '你怎么这样说'})))
        self.plugin = SimpleNamespace(config={}, resting=lambda: self.sleeping, context=SimpleNamespace(
            llm_generate=self.model, get_config=lambda **kw: {'provider_settings': {'enable': True}}))

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def event(self, qq='12345678', mid='msg1', text='你怎么这样说'):
        return SimpleNamespace(get_sender_id=lambda: qq, message_str=text,
            message_obj=SimpleNamespace(message_id=mid), unified_msg_origin='qq-companion:FriendMessage:'+qq)

    async def test_persists_across_reload_and_never_leaks_other_contact(self):
        self.store.observe('87654321', 'user', 'OTHER_PRIVATE_SENTINEL')
        await self.affect.prepare(self.plugin, self.event(), NOW)
        restarted = Affect(self.store)
        self.assertEqual(restarted.snapshot('12345678', NOW)['余绪'], ['委屈'])
        self.assertEqual(restarted.snapshot('87654321', NOW)['余绪'], ['委屈'])
        self.assertNotIn('OTHER_PRIVATE', self.model.call_args.kwargs['prompt'])
        self.assertIn('委屈', restarted.render('87654321', NOW))
        self.assertNotIn('OTHER_PRIVATE', restarted.render('87654321', NOW))

    async def test_new_event_object_same_message_no_double_count(self):
        await self.affect.prepare(self.plugin, self.event(), NOW)
        await self.affect.prepare(self.plugin, self.event(), NOW+60)
        self.assertEqual(self.model.await_count, 1)

    async def test_same_batch_concurrent_calls_are_deduplicated(self):
        await asyncio.gather(*(self.affect.prepare(self.plugin, self.event(), NOW) for _ in range(5)))
        self.assertEqual(self.model.await_count, 1)

    async def test_distinct_batches_within_twenty_seconds_are_not_dropped(self):
        await self.affect.prepare(self.plugin, self.event(), NOW)
        await self.affect.prepare(self.plugin, self.event(mid='msg2'), NOW+15)
        self.assertEqual(self.model.await_count, 2)
        self.assertLess(self.store.get_meta('affect_global_v3')['mood'], -1)

    async def test_global_daily_budget_does_not_use_memory_extraction_budget(self):
        self.plugin.config['affect_daily_calls'] = 1
        self.store.set_meta('extraction_usage', {'calls': 21})
        await self.affect.prepare(self.plugin, self.event(), NOW)
        await self.affect.prepare(self.plugin, self.event('87654321'), NOW+60)
        self.assertEqual(self.model.await_count, 1)
        self.assertEqual(self.store.get_meta('extraction_usage'), {'calls': 21})

    async def test_sleep_before_or_during_model_call_cannot_update_affect(self):
        self.sleeping = True
        self.assertEqual(await self.affect.prepare(self.plugin, self.event(), NOW), '')
        self.model.assert_not_awaited()
        self.sleeping = False
        async def falling_asleep(**kw):
            self.sleeping = True
            return SimpleNamespace(completion_text='{}')
        self.model.side_effect = falling_asleep
        self.assertEqual(await self.affect.prepare(self.plugin, self.event(), NOW), '')
        self.assertEqual(self.affect.snapshot('12345678', NOW)['余绪'], [])

    async def test_errors_continue_with_previous_state_and_no_false_emotion(self):
        self.model.side_effect = TimeoutError()
        text = await self.affect.prepare(self.plugin, self.event(), NOW)
        self.assertIn('平静', text)
        self.assertEqual(self.affect.snapshot('12345678', NOW)['余绪'], [])
        self.assertEqual(self.store.get_meta('affect_global_v3')['audit'][-1]['status'], 'model_error')

    async def test_invalid_model_quote_never_changes_state(self):
        self.model.return_value.completion_text = json.dumps({'delta': -1, 'emotions': ['hurt'], 'resolve': [], 'quote': '没说过'})
        await self.affect.prepare(self.plugin, self.event(), NOW)
        self.assertEqual(self.affect.snapshot('12345678', NOW)['余绪'], [])

    async def test_commands_nontext_and_missing_id_never_call_model(self):
        for text in ('', '/情绪状态'):
            await self.affect.prepare(self.plugin, self.event(text=text), NOW)
        await self.affect.prepare(self.plugin, self.event(mid=''), NOW)
        self.model.assert_not_awaited()

    async def test_input_and_output_bounded(self):
        await self.affect.prepare(self.plugin, self.event(text='你怎么这样说'+'鲸'*10000), NOW)
        kw = self.model.call_args.kwargs
        self.assertLessEqual(len((kw['system_prompt']+kw['prompt']).encode()), 6000)
        self.assertEqual(kw['max_tokens'], 260)

    def test_character_rate_caps_total_movement_in_both_directions(self):
        for i in range(5):
            out = {'growth': {'axis': 'patience', 'delta': 1 if i % 2 else -1}}
            advance_character(self.store, out, {'text': str(i), 'source_ids': [i, i+1]}, NOW+i)
        state = self.store.get_meta('character_axes_v1')
        self.assertEqual(len(state['history']), 3)
        out = {'growth': {'axis': 'curiosity', 'delta': 1}}
        self.assertTrue(advance_character(self.store, out, {'text': 'later', 'source_ids': [9, 10]}, NOW+7300))
        self.assertFalse(advance_character(self.store, out, {'text': 'extra', 'source_ids': [11, 12]}, NOW+7400))
        self.assertEqual(len(self.store.get_meta('character_axes_v1')['history']), 4)

    def test_character_no_axis_no_change_and_no_private_text_in_shared_render(self):
        self.assertFalse(advance_character(self.store, {'growth': {}}, {'source_ids': [1, 2]}, NOW))
        candidate = {'text': 'PRIVATE_SENTINEL', 'source_ids': [1, 2]}
        self.assertTrue(advance_character(self.store, {'growth': {'axis': 'curiosity', 'delta': 1}}, candidate, NOW))
        self.assertNotIn('PRIVATE_SENTINEL', json.dumps(self.store.get_meta('character_axes_v1')))
        self.assertTrue(character_summary(self.store))
        self.assertFalse(advance_character(self.store, {'growth': {'axis': 'curiosity', 'delta': 1}}, candidate, NOW+86400))


if __name__ == '__main__':
    unittest.main()
