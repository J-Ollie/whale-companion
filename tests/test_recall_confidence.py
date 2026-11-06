"""Topical early-stop decisions and bounded routing; no provider or platform I/O."""
import asyncio
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

sys.path.insert(0,os.environ.get('COMPANION_TEST_PLUGINS',str(Path(__file__).parent)))
from astrbot_plugin_companion_memory.memory import MemoryStore
from astrbot_plugin_companion_memory.cognition import Cognition
from astrbot_plugin_companion_memory.recall_index import weak_recall
from astrbot_plugin_companion_memory.retrieval import LayeredRecall


class ConfidenceTests(unittest.TestCase):
    def test_shared_question_glue_does_not_end_search(self):
        self.assertTrue(weak_recall('兰花每周浇水的事情不是这样，你当时怎么说的？',
            [{'text':'不是那个意思，你怎么又开始说晚饭了'}]))

    def test_actual_subjects_still_allow_early_stop(self):
        self.assertFalse(weak_recall('兰花每周浇水的安排怎么定的？',
            [{'text':'兰花每周检查土壤湿度再浇水'}]))

    def test_short_exact_keyword_keeps_low_cost_path(self):
        self.assertFalse(weak_recall('香菜',[{'text':'我不吃香菜'}]))

    def test_question_without_a_subject_is_not_a_confident_match(self):
        self.assertTrue(weak_recall('你的是不是怎么说的',[{'text':'怎么都行'}]))

    def test_disabled_flag_restores_previous_decision(self):
        q='兰花每周浇水的事情不是这样，你当时怎么说的？'
        rows=[{'text':'不是那个意思，你怎么又开始说晚饭了'}]
        self.assertFalse(weak_recall(q,rows,topical=False))


class RoutingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.store=MemoryStore(self.tmp.name)
        self.c=Cognition(self.store);self.qq='12345678'
        self.plugin=SimpleNamespace(store=self.store,cognition=lambda:self.c,resting=lambda:False,
            config={},semantic=SimpleNamespace(recall=AsyncMock(return_value=[])),expand_query=AsyncMock(return_value=[]))
        self.extra={};self.event=SimpleNamespace(get_extra=lambda key,default=None:self.extra.get(key,default),
            set_extra=lambda key,value:self.extra.__setitem__(key,value))

    def tearDown(self):self.store.close();self.tmp.cleanup()

    def fact(self,text,topic):
        source=self.store.observe(self.qq,'user',text)
        return self.store.put_entry(self.qq,'fact',topic,text,[source])

    async def test_only_glue_match_reaches_existing_semantic_path(self):
        self.fact('不是那个意思，你怎么又开始说晚饭了','随口说法')
        entry=self.fact('兰花土干了再浇水','园艺约定')
        self.plugin.semantic.recall.return_value=[entry]
        result=await LayeredRecall(self.plugin,self.qq,self.event).run('那盆开花植物需要怎么照顾，不是之前说的那样吧？')
        self.assertIn(entry,[r['id'] for r in result['matches']])
        self.assertGreaterEqual(self.plugin.semantic.recall.await_count,1)
        self.assertLessEqual(self.plugin.semantic.recall.await_count,2)

    async def test_exact_fact_does_not_call_semantic_or_archive(self):
        entry=self.fact('我不吃香菜','饮食偏好')
        result=await LayeredRecall(self.plugin,self.qq,self.event).run('香菜')
        self.assertEqual(result['matches'][0]['id'],entry)
        self.plugin.semantic.recall.assert_not_awaited()
        self.assertFalse(result['retrieval']['archive_searched'])

    async def test_semantic_failure_keeps_evidence_and_bounded_calls(self):
        self.fact('不是那个意思，你怎么又开始说晚饭了','随口说法')
        self.plugin.semantic.recall.side_effect=RuntimeError('isolated provider outage')
        result=await LayeredRecall(self.plugin,self.qq,self.event).run('那盆开花植物需要怎么照顾，不是之前说的那样吧？')
        self.assertLessEqual(self.plugin.semantic.recall.await_count,2)
        self.assertLessEqual(self.plugin.expand_query.await_count,1)
        self.assertIn('matches',result)

    async def test_switch_disables_new_decision_on_actual_router(self):
        self.fact('不是那个意思，你怎么又开始说晚饭了','随口说法')
        self.plugin.config['recall_topical_confidence']=False
        result=await LayeredRecall(self.plugin,self.qq,self.event).run('那盆开花植物需要怎么照顾，不是之前说的那样吧？')
        self.plugin.semantic.recall.assert_not_awaited()
        self.assertFalse(result['retrieval']['archive_searched'])

    async def test_archive_score_cannot_discard_a_source_checked_card(self):
        entry=self.fact('白鲸盆栽约定先看土壤再决定浇水','园艺约定')
        self.store.observe(self.qq,'user','开花植物开花植物怎样照顾开花植物')
        self.plugin.semantic.recall.return_value=[entry]
        result=await LayeredRecall(self.plugin,self.qq,self.event).run('那盆开花植物不是这样，你怎么理解每周的养护安排？')
        self.assertIn(entry,[r['id'] for r in result['matches']])
        self.assertTrue(result['retrieval']['archive_searched'])


if __name__=='__main__':unittest.main()
