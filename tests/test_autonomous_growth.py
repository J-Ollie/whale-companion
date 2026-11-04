"""Synthetic histories only. No QQ sends or production memory writes."""
import asyncio
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from astrbot_plugin_companion_memory.memory import MemoryStore, TZ
from astrbot_plugin_companion_memory.budget import REFLECTION_BYTES, dump, size
from astrbot_plugin_companion_memory.growth import SYSTEM, INTERVAL, baseline_style, prepare, review, validate
from astrbot_plugin_companion_memory.profile import SharedProfile

OWNER = '100000001'
OTHER = '12345678'
NOW = datetime(2026, 10, 31, 13, 0, tzinfo=TZ).timestamp()


class GrowthTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = MemoryStore(self.tmp.name)
        self.sleeping = False
        self.profile = SharedProfile(Path(self.tmp.name))
        self.profile.data.update(name='岚', aliases=['小鲸鱼'])
        self.p = SimpleNamespace(store=self.store, owner=OWNER, umo='qq-companion:FriendMessage:'+OWNER,
            profile=self.profile, config={}, processing=asyncio.Lock(), resting=lambda: self.sleeping,
            context=SimpleNamespace(get_config=lambda **kw: {'provider_settings': {'enable': True}}, llm_generate=AsyncMock()))

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def message(self, text, at=NOW, qq=OWNER, role='user'):
        sid = self.store.observe(qq, role, text, at=at)
        return dict(self.store.db.execute('SELECT * FROM observations WHERE id=?', (sid,)).fetchone())

    def history(self):
        a = self.message('这篇海洋研究的方法很有意思，我们一起看看', NOW-3*86400)
        self.message('我想先弄明白它为什么这样设计', NOW-3*86400+1, role='assistant')
        b = self.message('又找到一个海洋研究的小问题，一起琢磨一下', NOW-86400)
        self.message('我也想顺着这个问题继续探索', NOW-86400+1, role='assistant')
        return a, b

    def decision(self, rows, text='更愿意顺着共同讨论的问题继续追究原因，保留好奇而不急着下结论'):
        return {'growth': {'text': text, 'evidence': [{'source_id': r['id'], 'quote': r['text']} for r in rows]}}

    def respond(self, output):
        self.p.context.llm_generate.return_value = SimpleNamespace(completion_text=dump(output))

    async def test_implicit_experiences_can_grow_without_any_request_to_change(self):
        rows = self.history()
        self.respond(self.decision(rows))
        self.store.set_meta('cursor', 999)
        await review(self.p, NOW)
        growth = self.store.entries(OWNER, 'growth', now=NOW)
        self.assertEqual(len(growth), 1)
        self.assertEqual(json.loads(growth[0]['evidence']), [r['id'] for r in rows])
        self.assertEqual(self.store.get_meta('cursor'), 999)
        self.assertEqual(self.store.get_meta('growth_review_history:'+OWNER)[-1]['status'], 'accepted')

    async def test_sample_spreads_across_days_and_references_original_memories(self):
        rows = self.history()
        self.store.put_entry(OWNER, 'episode', '海洋研究', '不是原文的新推断', [rows[0]['id']], now=NOW)
        for i in range(100):
            self.message('普通的最新交流消息'+str(i), NOW-100+i)
        payload, sources = prepare(self.store, OWNER, '', 0, NOW)
        self.assertIn(rows[0]['id'], [r['id'] for r in sources])
        self.assertTrue(payload['记忆目录'])
        self.assertNotIn('不是原文的新推断', dump(payload))

    async def test_other_people_private_diary_and_forgotten_sources_never_enter_review(self):
        a, b = self.history()
        self.message('OTHER_PRIVATE_SENTINEL', NOW-86400, qq=OTHER)
        self.store.put_entry(OTHER, 'episode', 'PRIVATE_TOPIC', 'PRIVATE_MEMORY', [], now=NOW)
        self.store.db.execute('INSERT INTO suppressed_sources VALUES (?,?)', (OWNER, a['id']))
        self.store.db.commit()
        (Path(self.tmp.name)/'private-diary.jsonl').write_text('PRIVATE_DIARY_SENTINEL', encoding='utf-8')
        payload, sources = prepare(self.store, OWNER, '', 0, NOW)
        self.assertNotIn(a['id'], [r['id'] for r in sources])
        self.assertNotIn('PRIVATE', dump(payload))
        self.assertEqual({r['qq'] for r in sources}, {OWNER})

    async def test_utf8_input_budget_with_many_days_long_messages_and_memories(self):
        for day in range(14):
            for i in range(12):
                row = self.message('真实的长消息内容。'+'鲸'*900, NOW-day*86400-i)
                self.store.put_entry(OWNER, 'episode', '事件'+str(day)+str(i), '鲸'*80, [row['id']], now=NOW)
        payload, sources = prepare(self.store, OWNER, '好奇'*100, 0, NOW, '基础'*300)
        self.assertLessEqual(size(SYSTEM)+size(payload), REFLECTION_BYTES)
        self.assertGreater(len({datetime.fromtimestamp(r['at'], TZ).date() for r in sources}), 2)
        self.assertTrue(all('…' not in r['text'] for r in sources))

    async def test_same_day_has_no_model_call_and_records_reason(self):
        self.message('这是一条今天的普通消息')
        self.message('这是另一条今天的普通消息', NOW-60)
        await review(self.p, NOW)
        self.p.context.llm_generate.assert_not_called()
        self.assertEqual(self.store.get_meta('growth_review_history:'+OWNER)[-1]['status'], 'insufficient_days')

    async def test_sleep_disabled_and_locked_do_not_read_or_call(self):
        self.history()
        self.sleeping = True
        await review(self.p, NOW)
        self.sleeping = False
        for key in ('enabled', 'auto_extract', 'growth_enabled'):
            self.p.config = {key: False}
            await review(self.p, NOW)
        self.p.config = {}
        await self.p.processing.acquire()
        await review(self.p, NOW)
        self.p.processing.release()
        self.p.context.llm_generate.assert_not_called()
        self.assertIsNone(self.store.get_meta('growth_review_history:'+OWNER))

    async def test_shared_daily_budget_and_own_two_call_cap(self):
        self.history()
        day = datetime.fromtimestamp(NOW, TZ).date().isoformat()
        self.store.set_meta('extraction_usage', {'day': day, 'calls': 48, 'input_bytes': 0})
        await review(self.p, NOW)
        self.assertEqual(self.store.get_meta('growth_review_cursor:'+OWNER, 0), 0)
        self.store.set_meta('growth_review_due:'+OWNER, 0)
        self.store.set_meta('extraction_usage', {'day': day, 'calls': 1, 'input_bytes': 0})
        self.store.set_meta('growth_review_usage:'+OWNER, {'day': day, 'calls': 2})
        await review(self.p, NOW)
        self.p.context.llm_generate.assert_not_called()

    async def test_no_change_is_normal_persisted_and_not_repeated_without_new_messages(self):
        self.history()
        self.respond({'growth': None})
        await review(self.p, NOW)
        self.assertFalse(self.store.entries(OWNER, 'growth', now=NOW))
        self.assertEqual(self.store.get_meta('growth_review_history:'+OWNER)[-1]['status'], 'no_stable_change')
        self.store.close()
        self.store = self.p.store = MemoryStore(self.tmp.name)
        await review(self.p, NOW+INTERVAL+1)
        self.assertEqual(self.p.context.llm_generate.await_count, 1)

    async def test_model_failure_keeps_cursors_and_old_traits_for_retry(self):
        self.history()
        self.profile.data['traits'] = '仍然好奇'
        self.p.context.llm_generate.side_effect = TimeoutError()
        await review(self.p, NOW)
        self.assertEqual(self.store.get_meta('growth_review_cursor:'+OWNER, 0), 0)
        self.assertEqual(self.profile.data['traits'], '仍然好奇')
        self.assertEqual(self.store.get_meta('growth_review_history:'+OWNER)[-1]['status'], 'model_error')

    async def test_sleep_during_generation_defers_without_accepting_or_advancing(self):
        rows = self.history()
        async def complete(**kwargs):
            self.sleeping = True
            return SimpleNamespace(completion_text=dump(self.decision(rows)))
        self.p.context.llm_generate.side_effect = complete
        await review(self.p, NOW)
        self.assertFalse(self.store.entries(OWNER, 'growth', now=NOW))
        self.assertEqual(self.store.get_meta('growth_review_cursor:'+OWNER, 0), 0)

    async def test_forgetting_during_generation_rechecks_sources(self):
        rows = self.history()
        async def complete(**kwargs):
            self.store.set_meta('not_relevant', True)
            self.store.db.execute('INSERT INTO suppressed_sources VALUES (?,?)', (OWNER, rows[0]['id']))
            self.store.db.commit()
            return SimpleNamespace(completion_text=dump(self.decision(rows)))
        self.p.context.llm_generate.side_effect = complete
        await review(self.p, NOW)
        self.assertFalse(self.store.entries(OWNER, 'growth', now=NOW))
        self.assertEqual(self.store.get_meta('growth_review_history:'+OWNER)[-1]['status'], 'invalid_evidence')

    async def test_one_accepted_change_per_24_hours(self):
        rows = self.history()
        self.respond(self.decision(rows))
        await review(self.p, NOW)
        self.message('又有一个值得讨论的新问题', NOW+60)
        await review(self.p, NOW+INTERVAL+1)
        self.assertEqual(self.p.context.llm_generate.await_count, 1)

    async def test_publication_receives_only_abstract_trait_not_private_evidence(self):
        rows = self.history()
        self.respond(self.decision(rows))
        await review(self.p, NOW)
        persona = SimpleNamespace(system_prompt='基础规则保留')
        ctx = SimpleNamespace(persona_manager=SimpleNamespace(get_persona=AsyncMock(return_value=persona), update_persona=AsyncMock()),
            llm_generate=AsyncMock(return_value=SimpleNamespace(completion_text='{"keep":false,"traits":"更愿意探索问题背后的原因，保持好奇和审慎"}')))
        await self.profile.sync(ctx, self.store.entries(OWNER, 'growth', now=NOW), 'provider')
        sent = ctx.llm_generate.await_args.kwargs['prompt']
        self.assertNotIn(rows[0]['text'], sent)
        self.assertIn('好奇和审慎', self.profile.data['traits'])

    async def test_present_style_is_known_and_not_passed_as_new_experience(self):
        self.history()
        self.respond({'growth': None})
        persona = SimpleNamespace(system_prompt='身份QQ秘密\n初始气质\n好奇，会认真。\n怎么说话\n不反复叮嘱。\n怎样慢慢变化\n后续规则')
        self.p.context.persona_manager = SimpleNamespace(get_persona=AsyncMock(return_value=persona))
        await review(self.p, NOW)
        sent = self.p.context.llm_generate.await_args.kwargs['prompt']
        self.assertIn('不反复叮嘱', sent)
        self.assertNotIn('身份QQ秘密', sent)

    def test_validation_rejects_false_ids_private_text_and_assistant_only_support(self):
        rows = self.history()
        for bad in [True, 99999]:
            value = self.decision(rows)
            value['growth']['evidence'][0]['source_id'] = bad
            self.assertEqual(validate(value, rows, 0, NOW)[1], 'invalid_evidence')
        self.assertEqual(validate(self.decision(rows, '管理员的权限扩大'), rows, 0, NOW)[1], 'private_or_unsafe')
        self.assertEqual(validate(self.decision(rows), [{**r, 'role': 'assistant'} for r in rows], 0, NOW)[1], 'invalid_evidence')

    def test_validation_requires_real_cross_day_new_evidence_and_whole_meaning(self):
        rows = self.history()
        value = self.decision(rows)
        self.assertEqual(validate(value, [{**r, 'at': NOW} for r in rows], 0, NOW)[1], 'invalid_evidence')
        self.assertEqual(validate(value, rows, rows[-1]['id'], NOW)[1], 'invalid_evidence')
        row = self.message('我不喜欢毫无根据地推断他人的想法', NOW)
        value = self.decision([rows[0], row])
        value['growth']['evidence'][1]['quote'] = '喜欢毫无根据地推断他人的想法'
        self.assertEqual(validate(value, [rows[0], row], 0, NOW)[1], 'invalid_evidence')


if __name__ == '__main__':
    unittest.main()
