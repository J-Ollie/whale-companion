"""Synthetic process traces; no extra model requests, phone or social execution."""
import json
import sqlite3
import unittest
from unittest.mock import AsyncMock,patch
import test_thinking as fixture
import test_thinking_memory as private_fixture
from astrbot_plugin_companion_phone.process_trace import record


class ProcessTraceTests(unittest.IsolatedAsyncioTestCase):
    responses=fixture.ThinkingTests.responses
    thought=fixture.ThinkingTests.thought
    decision=fixture.ThinkingTests.decision
    def setUp(self):fixture.ThinkingTests.setUp(self)

    def events(self):
        with self.t.store.connect() as db:return [dict(r) for r in db.execute('SELECT * FROM process_events ORDER BY id')]

    async def test_ordered_thought_choice_execution_and_stop_without_extra_model_calls(self):
        self.responses(self.thought(),self.decision())
        await self.a.consider()
        events=self.events();kinds=[r['kind'] for r in events]
        self.assertEqual(kinds,['run_started','thought','decision','step_started','step_finished','run_finished'])
        self.assertEqual(self.context.llm_generate.await_count,2)
        self.assertEqual(len({r['run_id'] for r in events}),1)
        self.assertEqual(json.loads(events[-1]['payload'])['calls'],2)

    async def test_workspace_steps_and_multiple_choices_share_one_trace(self):
        self.config['self_directed_enabled']=True
        self.responses(self.thought(),self.decision(activity='workspace',workspace={'action':'list'},**{'continue':True}),self.decision())
        await self.a.consider()
        self.assertEqual([r['kind'] for r in self.events()].count('decision'),2)
        self.assertEqual(len({r['run_id'] for r in self.events()}),1)

    async def test_raw_prompts_reasoning_and_code_are_not_trace_fields(self):
        run=self.t.store.begin(1)
        record(self.t.store,run,'thought',{'focus':'短摘要','reasoning_content':'HIDDEN_MARKER','system_prompt':'SYSTEM_MARKER',
                                        'messages':'CHAT_MARKER','code':'CODE_MARKER','note':'password=secretcredential'})
        value=json.dumps(self.events(),ensure_ascii=False)
        for marker in ['HIDDEN_MARKER','SYSTEM_MARKER','CHAT_MARKER','CODE_MARKER','secretcredential']:self.assertNotIn(marker,value)

    async def test_scope_guard_and_cap_do_not_overwrite_history(self):
        run=self.t.store.begin(1)
        for _ in range(210):record(self.t.store,run,'step_started',{'activity':'read'})
        self.assertEqual(len(self.events()),200)
        with self.t.store.connect() as db:db.execute('UPDATE runs SET scope=? WHERE id=?',('other',run))
        self.assertIsNone(record(self.t.store,run,'run_finished',{'status':'completed'}))
        self.assertEqual(len(self.events()),200)

    async def test_log_failure_does_not_repeat_or_fail_activity(self):
        self.responses(self.thought(),self.decision())
        with patch('astrbot_plugin_companion_phone.process_trace.record',return_value=None):await self.a.consider()
        self.assertEqual(self.context.llm_generate.await_count,2)
        self.assertEqual(self.t.store.rows('runs')[0]['status'],'completed')

    async def test_invalid_model_cannot_inject_execution_status_into_decision(self):
        self.responses(self.thought(),self.decision(status='FORGED_SUCCESS',result='FORGED_RECEIPT'))
        await self.a.consider()
        decisions=[json.loads(r['payload']) for r in self.events() if r['kind']=='decision']
        self.assertNotIn('FORGED',json.dumps(decisions))


class PrivateTraceTests(unittest.IsolatedAsyncioTestCase):
    responses=fixture.ThinkingTests.responses
    thought=fixture.ThinkingTests.thought
    decision=fixture.ThinkingTests.decision
    ask=private_fixture.MemoryThinkingTests.ask
    private_note=private_fixture.MemoryThinkingTests.private_note
    def setUp(self):private_fixture.MemoryThinkingTests.setUp(self)

    async def test_private_summary_is_marked_and_bound_to_original_sources(self):
        self.responses(self.ask(),self.private_note(),{'activity':'idle'})
        await self.a.consider()
        with self.t.store.connect() as db:rows=db.execute('SELECT * FROM process_events ORDER BY id').fetchall()
        private=[r for r in rows if r['visibility']=='private']
        self.assertTrue(private)
        self.assertTrue(all(json.loads(r['refs']) for r in private))
        self.assertNotIn('PRIVATE_REPLY_MARKER',''.join(r['payload'] for r in rows))
        self.assertFalse(any(r['kind']=='source' for r in rows))


if __name__=='__main__':unittest.main()
