"""Exploration receipts and routing: synthetic state, no real accounts or sends."""
import copy
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, os.environ.get('COMPANION_TEST_PLUGINS', str(Path(__file__).parent)))
from astrbot_plugin_companion_active.goals import GoalLedger, WEEK
from astrbot_plugin_companion_phone.exploration import Exploration

NOW = 1792476000.0
OWNER = 'test:FriendMessage:owner'
MESSAGE = '我看到电池衰减有两种解释，还想找些资料核对'


def candidate(identifier='a', **changes):
    row = {'id': 'explore:'+identifier*12, 'revision': identifier*64, 'question': '电池为什么会衰减',
        'progress': '读到两种不同解释，还没有核实', 'next_step': '找第二份资料', 'status': 'open',
        'source': {'id': 'phone:'+identifier, 'timestamp': NOW-60, 'app': '酷安',
                   'note': '电池原理讨论', 'views': 1, 'public_content': True}}
    row.update(changes)
    return row


class GoalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ledger = GoalLedger(self.tmp.name, OWNER)
        self.goal = candidate()

    def send(self, goal=None, complete=True, parts=None):
        goal = goal or self.goal
        token = self.ledger.reserve(goal, [goal], NOW)
        self.assertTrue(token)
        self.ledger.finish(goal['id'], token, [MESSAGE] if parts is None else parts, complete)

    def quote(self, text=MESSAGE, at=NOW):
        return {'text': text, 'at': at}

    def test_same_progress_blocked_after_restart_and_punctuation_change(self):
        self.send()
        restored = GoalLedger(self.tmp.name, OWNER)
        self.assertEqual(restored.offers([self.goal], NOW+86400), {})
        edited = candidate(revision='b'*64, progress=self.goal['progress']+'。',
            source={**self.goal['source'], 'id': 'phone:new', 'timestamp': NOW+10})
        self.assertEqual(restored.offers([edited], NOW+20), {})

    def test_fresh_source_and_changed_progress_are_both_required(self):
        self.send()
        changed = candidate(revision='b'*64, progress='第二份资料提出了另一种机制')
        self.assertEqual(self.ledger.offers([changed], NOW), {})
        changed['source'] = {**changed['source'], 'id': 'phone:new', 'timestamp': NOW+10}
        self.assertIn(changed['id'], self.ledger.offers([changed], NOW+20))

    def test_reservation_survives_crash_without_resending(self):
        self.assertTrue(self.ledger.reserve(self.goal, [self.goal], NOW))
        restored = GoalLedger(self.tmp.name, OWNER)
        self.assertEqual(restored.rows[self.goal['id']]['delivery'], 'reserved')
        self.assertEqual(restored.offers([self.goal], NOW+86400), {})

    def test_uncertain_and_partial_delivery_are_not_retried(self):
        for parts in ([], [MESSAGE]):
            with self.subTest(parts=parts), tempfile.TemporaryDirectory() as root:
                ledger = GoalLedger(root, OWNER)
                token = ledger.reserve(self.goal, [self.goal], NOW)
                ledger.finish(self.goal['id'], token, parts, False)
                self.assertEqual(ledger.offers([self.goal], NOW+86400), {})
                self.assertEqual(ledger.rows[self.goal['id']]['delivery'], 'partial' if parts else 'uncertain')

    def test_invalid_revoked_or_superseded_candidate_cannot_be_reserved(self):
        for items in ([], [candidate(revision='b'*64)], [candidate(status='paused')]):
            self.assertIsNone(self.ledger.reserve(self.goal, items, NOW))
        self.assertFalse(self.ledger.path.exists())

    def test_stale_future_private_and_malformed_sources_are_not_offered(self):
        for source in ({'timestamp': NOW-WEEK-1}, {'timestamp': NOW+1}, {'timestamp': float('nan')},
                       {'public_content': False}, {'views': True}, {'app': 'QQ'}, {'id': 'diary:fake'}):
            self.assertEqual(self.ledger.offers([candidate(source={**self.goal['source'], **source})], NOW), {})

    def test_unrelated_positive_reply_cannot_confirm_two_goals(self):
        self.send()
        self.send(candidate('b', question='鲸类怎样学习叫声'))
        before = self.ledger.path.read_bytes()
        self.assertFalse(self.ledger.feedback([], '好呀，午饭我吃过了', 'lunch', NOW+10))
        self.assertEqual(self.ledger.path.read_bytes(), before)
        self.assertTrue(all(row['feedback'] is None for row in self.ledger.rows.values()))

    def test_explicit_quote_records_one_response_without_truth_or_sentiment(self):
        self.send()
        other = candidate('b', question='鲸类怎样学习叫声')
        token = self.ledger.reserve(other, [other], NOW)
        self.ledger.finish(other['id'], token, ['鲸类学习叫声的问题还需要更多资料'], True)
        self.assertTrue(self.ledger.feedback([self.quote()], '好呀', 'reply', NOW+10))
        row = self.ledger.rows[self.goal['id']]
        self.assertEqual(row['feedback']['kind'], 'received')
        self.assertNotIn('confidence', row)
        self.assertIsNone(self.ledger.rows[other['id']]['feedback'])
        self.assertFalse(self.ledger.feedback([self.quote()], '好呀', 'reply', NOW+20))

    def test_ambiguous_wrong_time_short_and_unquoted_replies_do_not_bind(self):
        self.send()
        for quotes in ([], [self.quote(at=NOW-1000)], [self.quote(text='好呀')], [self.quote(at=0)]):
            self.assertFalse(self.ledger.feedback(quotes, '不要再聊了', 'one', NOW+10))
        self.send(candidate('b'))
        self.assertFalse(self.ledger.feedback([self.quote()], '好呀', 'two', NOW+10))

    def test_explicit_decline_only_stops_sharing_its_goal_even_after_new_evidence(self):
        self.send()
        self.assertTrue(self.ledger.feedback([self.quote()], '这个话题先别聊了', 'stop', NOW+10))
        newer = candidate(revision='b'*64, progress='查到了新的解释',
            source={**self.goal['source'], 'id': 'phone:new', 'timestamp': NOW+20})
        self.assertEqual(self.ledger.offers([newer], NOW+30), {})
        self.assertTrue(self.ledger.rows[self.goal['id']]['blocked'])
        self.assertEqual(self.goal['status'], 'open')

    def test_general_busy_or_negative_content_is_not_topic_rejection(self):
        self.send()
        self.ledger.feedback([self.quote()], '不要把高温的影响忽略了', 'detail', NOW+10)
        self.assertFalse(self.ledger.rows[self.goal['id']]['blocked'])

    def test_explicit_resume_allows_new_progress_without_resending_old_material(self):
        self.send()
        self.ledger.feedback([self.quote()], '这个话题先别聊了', 'stop', NOW+10)
        self.ledger.feedback([self.quote()], '好呀', 'unrelated-positive', NOW+20)
        self.assertTrue(self.ledger.rows[self.goal['id']]['blocked'])
        self.ledger.feedback([self.quote()], '这个话题可以继续聊了', 'resume', NOW+30)
        self.assertFalse(self.ledger.rows[self.goal['id']]['blocked'])
        self.assertEqual(self.ledger.offers([self.goal], NOW+40), {})
        newer = candidate(revision='b'*64, progress='找到另一份资料，仍有疑问',
            source={**self.goal['source'], 'id': 'phone:new', 'timestamp': NOW+40})
        self.assertIn(newer['id'], self.ledger.offers([newer], NOW+50))

    def test_corrupt_and_foreign_owner_receipts_are_preserved(self):
        self.send()
        original = self.ledger.path.read_bytes()
        other = GoalLedger(self.tmp.name, 'other-owner')
        self.assertFalse(other.healthy)
        self.assertEqual(other.offers([candidate('b')], NOW), {})
        self.assertEqual(other.path.read_bytes(), original)
        other.path.write_text('{bad', encoding='utf-8')
        bad = GoalLedger(self.tmp.name, OWNER)
        self.assertFalse(bad.healthy)
        self.assertIsNone(bad.reserve(candidate('b'), [candidate('b')], NOW))
        self.assertEqual(bad.path.read_text(), '{bad')

    def test_context_is_bounded_and_does_not_modify_sources(self):
        rows = [candidate(x) for x in 'abcdef']
        before = copy.deepcopy(rows)
        result = self.ledger.context(self.ledger.offers(rows, NOW), limit=1400)
        self.assertLessEqual(len(json.dumps(result, ensure_ascii=False).encode()), 1400)
        self.assertLessEqual(len(result), 3)
        self.assertEqual(rows, before)

    def test_exploration_export_is_read_only_and_requires_all_apps_still_allowed(self):
        book = Exploration(self.tmp.name)
        with patch('time.time', return_value=NOW):
            self.assertTrue(book.observe(self.goal['source'], {'question': self.goal['question'], 'progress': '看到两种解释'}))
        before = book.path.read_bytes()
        rows = book.candidates(['酷安'])
        self.assertEqual(len(rows), 1)
        self.assertIn(rows[0]['id'], self.ledger.offers(rows, NOW))
        rows[0]['source']['note'] = '不能改原始笔记'
        self.assertEqual(book.path.read_bytes(), before)
        self.assertEqual(book.candidates(['QQ']), [])
        book.defer(book.rows[0]['id'], 'paused')
        self.assertEqual(book.candidates(['酷安']), [])

    def test_new_frame_or_next_step_alone_does_not_change_revision(self):
        book = Exploration(self.tmp.name)
        with patch('time.time', return_value=NOW):
            book.observe(self.goal['source'], {'question': self.goal['question'], 'progress': '尚未核实'})
            first = book.candidates(['酷安'])[0]
            source = {**self.goal['source'], 'id': 'phone:second', 'timestamp': NOW}
            book.observe(source, {'id': first['id'], 'progress': '尚未核实', 'next_step': '换一个搜索词'}, selected=first['id'])
            self.assertEqual(book.candidates(['酷安'])[0]['revision'], first['revision'])


class OutreachFlowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        from test_active import FlowTests, NOW as ACTIVE_NOW
        self.harness = FlowTests()
        self.plugin = self.harness.plugin()
        self.addCleanup(self.harness.temporary.cleanup)
        self.now = ACTIVE_NOW
        self.goal = candidate(source={**candidate()['source'], 'timestamp': self.now-60})
        self.phone = SimpleNamespace(outreach_allowed=Mock(return_value=True),
            exploration_goals=Mock(return_value=[self.goal]), recent_activity=Mock(return_value='真实浏览'), outreach_sent=Mock())
        self.plugin.context.get_registered_star = lambda name: SimpleNamespace(activated=True, star_cls=self.phone) if name == 'astrbot_plugin_companion_phone' else None
        self.plugin.context.llm_generate.return_value.completion_text = json.dumps(
            {'send': True, 'message': MESSAGE, 'exploration_id': self.goal['id']}, ensure_ascii=False)
        self.clock = patch('astrbot_plugin_companion_active.main.time.time', return_value=self.now)
        self.clock.start()
        self.addCleanup(self.clock.stop)

    async def test_goal_send_has_receipt_and_no_additional_model_call(self):
        await self.plugin.consider()
        self.plugin.context.llm_generate.assert_awaited_once()
        self.plugin.context.send_message.assert_awaited_once()
        ledger = GoalLedger(self.plugin.state_path.parent, self.plugin.umo)
        self.assertEqual(ledger.rows[self.goal['id']]['delivery'], 'sent')
        self.assertIn(self.goal['id'], self.plugin.context.llm_generate.await_args.kwargs['prompt'])

    async def test_unknown_or_previously_sent_goal_cannot_send(self):
        self.plugin.context.llm_generate.return_value.completion_text = json.dumps({'send': True, 'message': MESSAGE, 'exploration_id': 'explore:ffffffffffff'})
        await self.plugin.consider()
        self.plugin.context.send_message.assert_not_called()

    async def test_progress_changed_or_permission_revoked_during_model_aborts(self):
        async def model(**kwargs):
            self.phone.exploration_goals.return_value = []
            return SimpleNamespace(completion_text=json.dumps({'send': True, 'message': MESSAGE, 'exploration_id': self.goal['id']}))
        self.plugin.context.llm_generate.side_effect = model
        await self.plugin.consider()
        self.plugin.context.send_message.assert_not_called()
        self.assertFalse(self.plugin.goal_ledger().path.exists())

    async def test_network_failure_retains_uncertain_receipt(self):
        self.plugin.context.send_message.side_effect = RuntimeError('simulated unavailable')
        with self.assertRaises(RuntimeError):
            await self.plugin.consider()
        row = self.plugin.goal_ledger().rows[self.goal['id']]
        self.assertEqual(row['delivery'], 'uncertain')
        self.assertEqual(self.plugin.state['unanswered'], 1)

    async def test_sleep_and_unanswered_gate_model_and_receipts(self):
        self.phone.outreach_allowed.return_value = False
        await self.plugin.consider()
        self.phone.outreach_allowed.return_value = True
        self.plugin.state['unanswered'] = 1
        await self.plugin.consider()
        self.plugin.context.llm_generate.assert_not_called()
        self.assertFalse(self.plugin.goal_ledger().path.exists())

    async def test_delivered_goal_not_repeated_after_ordinary_user_response(self):
        await self.plugin.consider()
        self.plugin.state.update(unanswered=0, last_user_at=1, last_sent_at=1, last_bot_at=1, last_attempt_at=1, last_decision_at=1)
        await self.plugin.consider()
        self.plugin.context.send_message.assert_awaited_once()

    def reply_event(self, sender='bot', body=MESSAGE, at=None, owner=True):
        from astrbot.api.message_components import Reply, Plain
        return SimpleNamespace(unified_msg_origin=self.plugin.umo if owner else 'another:FriendMessage:stranger',
            get_sender_id=lambda: str(self.plugin.config.get('owner_qq', '100000001')),
            get_self_id=lambda: 'bot', message_obj=SimpleNamespace(message_id='owner-reply', message=[
                Reply(id='quote-1', sender_id=sender, time=int(self.now if at is None else at), message_str=body),
                Plain('这个话题先别聊了')]))

    async def test_quote_feedback_hook_binds_owner_only_and_does_not_call_model(self):
        await self.plugin.consider()
        self.plugin.context.llm_generate.reset_mock()
        req = SimpleNamespace(extra_user_content_parts=[])
        await self.plugin.exploration_feedback(self.reply_event(), req)
        self.assertTrue(self.plugin.goal_ledger().rows[self.goal['id']]['blocked'])
        self.assertTrue(req.extra_user_content_parts)
        self.plugin.context.llm_generate.assert_not_called()

    async def test_wrong_author_foreign_contact_wrong_time_and_sleep_do_not_read_feedback(self):
        await self.plugin.consider()
        req = SimpleNamespace(extra_user_content_parts=[])
        for event in (self.reply_event(sender='other'), self.reply_event(owner=False), self.reply_event(at=self.now-1000)):
            await self.plugin.exploration_feedback(event, req)
        self.assertIsNone(self.plugin.goal_ledger().rows[self.goal['id']]['feedback'])
        self.phone.outreach_allowed.return_value = False
        await self.plugin.exploration_feedback(self.reply_event(), req)
        self.assertIsNone(self.plugin.goal_ledger().rows[self.goal['id']]['feedback'])


if __name__ == '__main__':
    unittest.main()
