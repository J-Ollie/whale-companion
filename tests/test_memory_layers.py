"""Layer routing, progressive evidence and old-topic recall with synthetic data."""
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0,os.environ.get('COMPANION_TEST_PLUGINS',str(Path(__file__).parent)))
from astrbot_plugin_companion_memory.memory import MemoryStore
from astrbot_plugin_companion_memory.cognition import Cognition
from astrbot_plugin_companion_memory.recall_index import query_tokens
from astrbot_plugin_companion_memory.budget import size,CONTEXT_BYTES,TOOL_REPLY_BYTES,LAYER_BYTES

A='12345678';B='87654321'


class LayerTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.store=MemoryStore(self.tmp.name);self.c=Cognition(self.store)
    def tearDown(self):self.store.close();self.tmp.cleanup()
    def observation(self,text,qq=A):return self.store.observe(qq,'user',text,at=time.time()-120)
    def entry(self,text,topic,kind='fact',qq=A,now=None):
        source=self.observation(text,qq)
        return self.store.put_entry(qq,kind,topic,text,[source],now=now)

    def test_automatic_fact_retrieval_has_no_two_thousand_row_cutoff(self):
        original=self.entry('我的研究方向是南极冰川','研究方向',now=time.time()-86400)
        for i in range(2005):self.store.put_entry(A,'fact','其他主题'+str(i),'这是另一个无关主题',[],now=time.time()+i)
        self.assertEqual(self.store.retrieve(A,'南极冰川')[0]['id'],original)
        self.assertIn('南极冰川',self.c.render(A,'南极冰川',include_recent=False))

    def test_channel_filter_prevents_raw_chatter_from_crowding_facts(self):
        fact=self.entry('研究项目 '+('背景材料 '*100)+' 南极远征','研究方向')
        for i in range(150):self.observation('研究项目')
        self.assertEqual(self.store.retrieve(A,'研究项目')[0]['id'],fact)
        found=self.c.search(A,'研究项目',category='facts')
        self.assertEqual(found['category'],'facts');self.assertEqual(found['matches'][0]['id'],fact)
        self.assertTrue(all(r['kind']=='fact' and r['type']=='semantic' for r in found['matches']))

    def test_suppression_and_excluded_hits_are_filtered_before_top_k(self):
        target=self.entry('研究项目 '+('背景材料 '*100),'研究方向')
        blocked=self.observation('研究项目')
        first=None
        for i in range(130):
            entry=self.store.put_entry(A,'fact','杂项'+str(i),'研究项目',[blocked])
            if first is None:first=entry
        self.store.forget(A,first)
        result=self.c.search(A,'研究项目',category='facts')
        self.assertEqual([r['id'] for r in result['matches']],[target])
        self.assertFalse(self.c.search(A,'研究项目',category='facts',exclude=[target])['matches'])

    def test_categories_are_derived_from_structured_metadata_not_user_names(self):
        fact=self.entry('我喜欢南极研究','爱好')
        plan=self.entry('我明天要讨论南极研究','计划.讨论')
        event=self.entry('我们聊过南极研究','共同讨论','episode')
        for category,identifier in (('facts',fact),('plans',plan),('events',event)):
            result=self.c.search(A,'南极研究',category=category)
            self.assertEqual([r['id'] for r in result['matches']],[identifier])
            self.assertFalse(self.c.search(B,'南极研究',category=category)['matches'])
        raw=self.c.search(A,'南极研究',category='conversation')
        self.assertTrue(raw['matches']);self.assertTrue(all(r['kind']=='conversation_quote' for r in raw['matches']))
        with self.assertRaises(ValueError):self.c.search(A,'南极研究',category=B)

    def test_noisy_recall_phrases_do_not_match_generic_chatter(self):
        target=self.observation('约好了南极旅行')
        for i in range(150):self.observation('你还记得我们之前聊过的事情吗')
        result=self.c.search(A,'你还记得我们之前聊过的南极旅行吗',category='conversation')
        self.assertEqual([r['id'] for r in result['matches']],['obs:'+str(target)])
        self.assertIn('不吃',query_tokens('我不吃香菜'))
        self.assertIn('取消',query_tokens('取消明天的考试'))

    def test_related_bundle_is_found_beyond_one_hundred_newer_bundles(self):
        for number in range(105):
            for repeat in range(2):
                self.entry('话题'+str(number)+'：我们聊过对应内容','event.'+str(number)+'.'+str(repeat),'episode')
        for repeat in range(2):self.entry('南极远征：我们讨论了冰川研究','antarctic.'+str(repeat),'episode')
        self.c.consolidate(A)
        self.store.db.execute("UPDATE memory_compendia SET updated=? WHERE topic='南极远征'",(time.time()-86400,));self.store.db.commit()
        found=self.c.related(A,'南极远征')
        self.assertEqual(found[0]['kind'],'quote_bundle');self.assertIn('冰川',found[0]['text'])
        self.assertFalse(self.c.related(B,'南极远征'))

    def test_unrelated_working_topics_and_impressions_do_not_get_injected(self):
        self.entry('我们还在讨论老游戏的队伍','旧游戏','continuity')
        self.entry('对方似乎害怕拥挤场所，但这只是暂时印象','拥挤印象','impression')
        body=self.c.render(A,'南极冰川研究',include_recent=False)
        data=json.loads(body[body.index('{'):body.rindex('}')+1])
        self.assertEqual(data['continuity'],[]);self.assertEqual(data['impressions'],[])
        handoff=self.c.render(A,'',include_recent=True)
        self.assertIn('老游戏',handoff)
        relationship=self.c.render(A,'你怎么看我',include_recent=False)
        self.assertIn('impression',relationship);self.assertIn('暂时印象',relationship)

    def test_layer_and_global_budgets_preserve_identity_and_related_events(self):
        for i in range(5):self.store.put_entry(A,'fact','偏好.'+str(i),'我喜欢讨论海洋与鲸类'+str(i),[],pinned=True)
        self.entry('南极冰川研究是近期方向','研究方向')
        self.entry('南极冰川研究：对方明确提过这个方向','共同讨论','episode')
        self.entry('下次继续讨论南极冰川研究','南极冰川','continuity')
        body=self.c.render(A,'南极冰川研究',include_recent=False)
        self.assertLessEqual(size(body),CONTEXT_BYTES)
        data=json.loads(body[body.index('{'):body.rindex('}')+1])
        self.assertTrue(data['personal_core']);self.assertTrue(data['related_episodes'])
        for name,limit in LAYER_BYTES.items():self.assertLessEqual(size({'items':data[name]}),limit)

    def test_short_index_keeps_sources_and_can_be_expanded(self):
        text='南极研究：'+('鲸'*500)+'尾部细节'
        event=self.entry(text,'事件.南极','episode')
        data=self.c.search(A,'南极研究',category='events')
        self.assertLessEqual(size(data),TOOL_REPLY_BYTES)
        self.assertEqual(data['matches'][0]['id'],event)
        source='obs:'+str(data['matches'][0]['source_ids'][0])
        page=self.c.read(A,source,query='尾部细节')
        self.assertIn('尾部细节',page['text'])

    def test_explicit_typed_directory_works_without_guessing_keywords(self):
        plan=self.entry('我明天要讨论南极研究','明天的讨论')
        self.entry('我喜欢鲸类','爱好')
        directory=self.c.search(A,'',category='plans')
        self.assertEqual([r['id'] for r in directory['matches']],[plan])
        self.assertFalse(self.c.search(B,'',category='plans')['matches'])
        for category in ('all','conversation'):
            with self.assertRaises(ValueError):self.c.search(A,'',category=category)


if __name__=='__main__':unittest.main()
