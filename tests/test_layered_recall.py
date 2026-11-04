"""Layer routing, source safety and a large archive that cannot crowd out cards."""
import json,os,sys,time,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock,patch
sys.path.insert(0,os.environ.get('COMPANION_TEST_PLUGINS',str(Path(__file__).parent)))
import test_private_vault as vault_fixture
import test_memory_completeness_hooks as hook_fixture
from astrbot_plugin_companion_memory.retrieval import LayeredRecall
from astrbot_plugin_companion_memory.vault import fingerprint
from astrbot_plugin_companion_memory.cognition import Cognition

A=hook_fixture.A;B=hook_fixture.B


class RouterTests(unittest.IsolatedAsyncioTestCase):
    setUp=hook_fixture.PagingHooksTests.setUp
    tearDown=hook_fixture.PagingHooksTests.tearDown
    event=hook_fixture.PagingHooksTests.event

    def prepare(self):
        self.p.semantic=SimpleNamespace(recall=AsyncMock(return_value=[]))
        self.p.expand_query=AsyncMock(return_value=[])

    async def test_card_hit_never_searches_archive_or_calls_embedding(self):
        self.prepare();source=self.p.store.observe(A,'user','我不吃香菜')
        entry=self.p.store.put_entry(A,'fact','饮食偏好','我不吃香菜',[source])
        for i in range(600):self.p.store.observe(A,'assistant','香菜相关的合成闲聊 '+str(i))
        c=self.p.cognition()
        with patch.object(self.p.store.index,'search',wraps=self.p.store.index.search) as indexed:
            result=await LayeredRecall(self.p,A,self.event()).run('香菜')
            self.assertTrue(all('observation' not in call.kwargs.get('kinds',()) for call in indexed.call_args_list))
        self.assertEqual(result['matches'][0]['id'],entry)
        self.assertFalse(result['retrieval']['archive_searched']);self.p.semantic.recall.assert_not_called()
        self.p.expand_query.assert_not_called()

    async def test_missing_card_falls_back_to_same_person_original(self):
        self.prepare();source=self.p.store.observe(A,'user','合成彗星桥的见面口令是蓝羽')
        self.p.store.observe(B,'user','合成彗星桥另一个人的私事')
        result=await LayeredRecall(self.p,A,self.event()).run('合成彗星桥')
        self.assertEqual(result['matches'][0]['id'],'obs:'+str(source))
        self.assertEqual(result['retrieval']['layer'],'conversation_archive')
        self.assertNotIn('另一个人的私事',json.dumps(result,ensure_ascii=False))

    async def test_explicit_original_query_skips_card_search(self):
        self.prepare();self.p.store.observe(A,'user','旧合成灯塔的原话')
        result=await LayeredRecall(self.p,A,self.event()).run('合成灯塔原话')
        self.assertTrue(result['retrieval']['archive_searched'])
        stages=json.loads(self.p.store.db.execute('SELECT stages FROM recall_routes').fetchone()[0])
        self.assertTrue(all(r['layer']=='archive' for r in stages))

    async def test_categories_never_widen_to_raw_facts(self):
        self.prepare();self.p.store.observe(A,'user','鲸尾合成研究')
        result=await LayeredRecall(self.p,A,self.event()).run('鲸尾合成研究',category='facts')
        self.assertFalse(result['matches']);self.assertFalse(result['retrieval']['archive_searched'])

    async def test_weak_semantic_card_is_retained_while_archive_is_checked(self):
        self.prepare();source=self.p.store.observe(A,'user','我不吃芫荽')
        entry=self.p.store.put_entry(A,'fact','饮食','我不吃芫荽',[source])
        self.p.semantic.recall.return_value=[entry]
        result=await LayeredRecall(self.p,A,self.event()).run('料理里别加那种绿叶调料')
        self.assertEqual(result['matches'][0]['id'],entry)
        self.assertEqual(self.p.semantic.recall.call_args_list[0].kwargs['layer'],'memory')
        self.assertTrue(result['retrieval']['archive_searched'])

    async def test_semantic_failure_keeps_lexical_archive_fallback(self):
        self.prepare();self.p.semantic.recall.side_effect=RuntimeError('synthetic outage')
        source=self.p.store.observe(A,'user','合成海岬那天的讨论')
        result=await LayeredRecall(self.p,A,self.event()).run('合成海岬')
        self.assertEqual(result['matches'][0]['id'],'obs:'+str(source))

    async def test_forgotten_sources_cannot_return_through_either_layer(self):
        self.prepare();source=self.p.store.observe(A,'user','我喜欢合成星云茶')
        entry=self.p.store.put_entry(A,'fact','偏好','我喜欢合成星云茶',[source])
        self.p.store.forget(A,entry);self.p.semantic.recall.return_value=[entry,'obs:'+str(source)]
        result=await LayeredRecall(self.p,A,self.event()).run('合成星云茶')
        self.assertFalse(result['matches'])

    async def test_date_filter_and_cancelled_state_remain_effective(self):
        self.prepare();source=self.p.store.observe(A,'user','2020-01-01 讨论合成雾岛计划',at=1577808000)
        entry=self.p.store.put_entry(A,'fact','合成雾岛','2020-01-01 讨论合成雾岛计划',[source])
        with self.p.store.db:self.p.store.db.execute("UPDATE memory_state SET status='cancelled' WHERE qq=? AND id=?",(A,entry))
        result=await LayeredRecall(self.p,A,self.event()).run('合成雾岛',category='plans')
        self.assertFalse(result['matches'])
        result=await LayeredRecall(self.p,A,self.event()).run('合成雾岛','2020-01-01','2020-01-01',category='plans')
        self.assertEqual(result['matches'][0]['id'],entry)

    async def test_sleep_transition_after_vector_stops_archive_read(self):
        self.prepare()
        async def sleep(*a,**kw):self.friends.sleeping=lambda:True;return []
        self.p.semantic.recall.side_effect=sleep
        with self.assertRaises(ValueError):await LayeredRecall(self.p,A,self.event()).run('完全不同的合成题目')
        self.p.expand_query.assert_not_called()

    async def test_stale_working_card_cannot_bypass_review_via_semantic_hit(self):
        self.prepare();source=self.p.store.observe(A,'user','合成的短时未完话题')
        entry=self.p.store.put_entry(A,'continuity','话题','合成的短时未完话题',[source])
        with self.p.store.db:self.p.store.db.execute('UPDATE memory_state SET review_after=? WHERE qq=? AND id=?',(time.time()-1,A,entry))
        result=self.p.cognition().search(A,'未完话题',layer='memory',semantic_refs=[entry])
        self.assertFalse(result['matches'])

    async def test_audit_contains_only_routes_counts_and_duration(self):
        self.prepare();self.p.store.observe(A,'user','别将这个合成问题正文放进路由审计')
        await LayeredRecall(self.p,A,self.event()).run('合成问题')
        row=self.p.store.db.execute('SELECT * FROM recall_routes').fetchone()
        self.assertNotIn('合成问题',str(tuple(row)));self.assertGreaterEqual(row['elapsed_ms'],0)


class SplitIndexTests(vault_fixture.VaultTests):
    # Inherit the source/forget/revoke tests so both tiers get the same boundary audit.
    async def test_two_semantic_layers_use_disjoint_native_kbs(self):
        await self.s.sync(force=True)
        shallow=await self.s.recall(vault_fixture.A,'合成问题',layer='memory')
        deep=await self.s.recall(vault_fixture.A,'合成问题',layer='archive')
        self.assertIn(self.entry,shallow);self.assertNotIn('obs:'+str(self.source),shallow)
        self.assertIn('obs:'+str(self.source),deep);self.assertNotIn(self.entry,deep)

    async def test_legacy_mixed_index_migrates_only_derived_documents(self):
        # Simulate the v1.9.1 layout, preserving real IDs/fingerprints for raw text.
        for card in self.s.vault.cards(vault_fixture.A):
            engine=await self.s.engine(vault_fixture.A,card['bucket'])
            identifier=await engine._upload_document('legacy.md',self.s.vault.render(card,index=True))
            with self.s.db:self.s.db.execute('INSERT INTO docs VALUES(?,?,?,?,?)',(vault_fixture.A,card['ref'],card['bucket'],identifier,fingerprint(card)))
        before=dict(self.s.db.execute('SELECT * FROM docs WHERE ref=?',('obs:'+str(self.source),)).fetchone())
        await self.s.sync(force=True)
        self.assertEqual(dict(self.s.db.execute('SELECT * FROM docs WHERE ref=?',('obs:'+str(self.source),)).fetchone()),before)
        self.assertEqual(self.s.db.execute('SELECT bucket FROM docs WHERE ref=?',(self.entry,)).fetchone()[0],'shallow-active')


if __name__=='__main__':unittest.main()
