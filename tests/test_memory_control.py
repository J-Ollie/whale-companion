"""Durable scoped state, source lineage, learned procedures and controlled CRUD."""
import json,os,sqlite3,sys,tempfile,time,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
sys.path.insert(0,os.environ.get('COMPANION_TEST_PLUGINS',str(Path(__file__).parent)))
from astrbot.api.message_components import Plain,Reply
from astrbot_plugin_companion_memory.memory import MemoryStore
from astrbot_plugin_companion_memory.cognition import Cognition,TOOLS
from astrbot_plugin_companion_memory.control import MemoryControl,explicit_forget
from astrbot_plugin_companion_memory.budget import size,CONTEXT_BYTES,TOOL_REPLY_BYTES
from astrbot_plugin_companion_memory.retrieval import LayeredRecall
from astrbot_plugin_companion_memory.vault import Vault
from astrbot_plugin_companion_memory.portable import create_archive,restore_archive
import test_memory_completeness_hooks as hooks

A=hooks.A;B=hooks.B


class ControlTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.store=MemoryStore(self.root/'plugin_data/companion_memory');self.c=Cognition(self.store);self.m=self.c.control
    def tearDown(self):self.store.close();self.tmp.cleanup()
    def episode(self,topic,text,qq=A,at=None):
        source=self.store.observe(qq,'user',text,at=at or time.time()-120)
        entry=self.store.put_entry(qq,'episode','event.'+str(source),topic+'：'+text,[source])
        return entry,source
    def topic(self):
        first,s1=self.episode('合成海岛旅行','上周一起讨论海岛旅行',at=time.time()-2*86400)
        second,s2=self.episode('合成海岛旅行','今天决定暂缓海岛旅行',at=time.time()-120)
        self.m.consolidate(A);identifier=self.m.topics(A,'合成海岛旅行')[0]['id']
        return identifier,first,second,s1,s2

    def test_topic_links_multiple_days_and_preserves_original_quotes(self):
        identifier,first,second,s1,s2=self.topic();card=self.m.bundle(A,identifier)
        self.assertEqual(card['days'],2);self.assertEqual(set(json.loads(card['evidence'])),{s1,s2})
        self.assertIn('上周一起讨论',card['text']);self.assertIn('暂缓',card['text'])
        self.assertFalse(self.m.bundle(B,identifier));self.assertFalse(self.m.topics(B,'海岛'))

    def test_changed_leaf_invalidates_old_summary_before_rebuild(self):
        identifier,first,second,s1,s2=self.topic()
        with self.store.db:self.store.db.execute('UPDATE entries SET text=? WHERE id=?',('合成海岛旅行：已经修改原话关联',first))
        self.assertIsNone(self.m.bundle(A,identifier));self.assertFalse(self.m.topics(A,'海岛'))
        with self.assertRaises(ValueError):self.c.read(A,identifier)
        self.m.consolidate(A);self.assertIsNotNone(self.m.bundle(A,identifier))

    def test_changed_original_invalidates_summary_without_changing_entry(self):
        identifier,first,second,s1,s2=self.topic()
        with self.store.db:self.store.db.execute('UPDATE observations SET text=? WHERE id=?',('合成来源被更正',s1))
        self.assertIsNone(self.m.bundle(A,identifier))

    def test_changed_fact_evidence_blocks_card_search_core_and_vector_projection(self):
        source=self.store.observe(A,'user','我不吃合成香菜')
        ref=self.store.put_entry(A,'fact','饮食偏好','我不吃合成香菜',[source])
        self.assertTrue(self.c.search(A,'合成香菜',category='facts')['matches'])
        with self.store.db:self.store.db.execute('UPDATE observations SET text=? WHERE id=?',('这条合成原始依据已经更正',source))
        self.assertFalse(self.c.search(A,'合成香菜',category='facts',semantic_refs=[ref])['matches'])
        self.assertFalse(self.c.core(A))
        vault=Vault(self.c,self.store.folder/'semantic/vault',owner=A)
        self.assertIsNone(vault.card(A,ref))

    def test_forget_removes_topic_search_links_and_state(self):
        identifier,first,second,s1,s2=self.topic();self.store.forget(A,first)
        self.assertFalse(self.m.bundle(A,identifier));self.assertFalse(self.m.topics(A,'海岛'))
        for table in ('memory_bundle_state','memory_topics_fts'):
            self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM '+table).fetchone()[0],0)

    def test_new_refs_replace_old_edges_instead_of_accumulating(self):
        identifier,*_=self.topic()
        for i in range(9):self.episode('合成海岛旅行','又一段独立讨论 '+str(i))
        self.m.consolidate(A);row=self.m.bundle(A,identifier)
        refs=set(json.loads(row['refs']));edges={r[0] for r in self.store.db.execute('SELECT child FROM memory_links WHERE qq=? AND parent=?',(A,identifier))}
        self.assertEqual(edges,refs);self.assertLessEqual(len(edges),6)

    def test_summary_read_is_paginated_and_never_exceeds_budget(self):
        identifier,*_=self.topic();page=self.c.read(A,identifier)
        self.assertLessEqual(size(page),TOOL_REPLY_BYTES);self.assertIn('source_ids',page)

    def test_working_checkpoint_survives_reopen_and_cancellation(self):
        source=self.store.observe(A,'user','我明天准备讨论合成沙丘计划')
        entry=self.store.put_entry(A,'fact','合成沙丘','我明天准备讨论合成沙丘计划',[source])
        self.assertEqual(self.m.working(A,'继续')[0]['id'],entry)
        self.store.close();self.store=MemoryStore(self.root/'plugin_data/companion_memory');self.c=Cognition(self.store);self.m=self.c.control
        self.assertTrue(self.store.db.execute('SELECT 1 FROM memory_working WHERE qq=?',(A,)).fetchone())
        with self.store.db:self.store.db.execute("UPDATE memory_state SET status='cancelled' WHERE qq=? AND id=?",(A,entry))
        self.assertFalse(self.m.working(A,'继续'))
        snapshot=json.loads(self.store.db.execute('SELECT snapshot FROM memory_working WHERE qq=?',(A,)).fetchone()[0])
        self.assertFalse(snapshot['plans']);self.assertFalse(self.m.working(B,'继续'))

    def test_unrelated_query_does_not_receive_old_working_topic(self):
        source=self.store.observe(A,'user','还在讨论合成海岛旅行')
        self.store.put_entry(A,'continuity','海岛旅行','还在讨论合成海岛旅行',[source])
        self.assertFalse(self.m.working(A,'研究南极冰川'))
        self.assertTrue(self.m.working(A,'接着聊'))

    def test_handoff_respects_global_context_budget(self):
        for i in range(8):
            source=self.store.observe(A,'user','我明天准备讨论合成计划'+str(i))
            self.store.put_entry(A,'fact','合成计划'+str(i),'我明天准备讨论合成计划'+str(i),[source])
        body=self.c.render(A,'继续计划');self.assertLessEqual(size(body),CONTEXT_BYTES)
        data=json.loads(body[body.index('{'):body.rindex('}')+1]);self.assertIn('working_handoff',data)

    def test_reflections_are_available_but_marked_as_inference(self):
        a=self.store.observe(A,'user','合成相处材料一');b=self.store.observe(A,'user','合成相处材料二')
        ref=self.store.put_entry(A,'impression','interaction','暂时觉得对方喜欢慢慢聊',[a,b])
        result=self.c.search(A,'',category='reflections');self.assertEqual(result['matches'][0]['id'],ref)
        self.assertEqual(result['matches'][0]['source_kind'],'inference');self.assertFalse(self.c.search(B,'',category='reflections')['matches'])

    def test_explicit_forget_accepts_specific_target_and_preserves_raw_archive(self):
        source=self.store.observe(A,'user','我不吃合成香菜')
        ref=self.store.put_entry(A,'fact','饮食','我不吃合成香菜',[source])
        report=self.m.forget(A,ref,'请忘记我不吃合成香菜这条记忆','请忘记我不吃合成香菜这条记忆')
        self.assertEqual(report['status'],'recall_suppressed');self.assertTrue(self.store.db.execute('SELECT 1 FROM observations WHERE id=?',(source,)).fetchone())
        self.assertFalse(self.c.search(A,'合成香菜')['matches'])

    def test_negative_quote_historical_and_ambiguous_requests_cannot_forget(self):
        source=self.store.observe(A,'user','我不吃合成香菜');ref=self.store.put_entry(A,'fact','饮食','我不吃合成香菜',[source])
        for text in ('不要忘记我不吃合成香菜','别人说请忘记我不吃合成香菜','“请忘记我不吃合成香菜”','我之前说过请忘记我不吃合成香菜','请忘记那条记忆'):
            with self.assertRaises(ValueError,msg=text):self.m.forget(A,ref,text,text)
        with self.assertRaises(ValueError):self.m.forget(B,ref,'请忘记我不吃合成香菜','请忘记我不吃合成香菜')
        self.assertIsNotNone(self.store.get_entry(A,ref))

    def test_forget_management_message_cannot_be_extracted_as_a_new_fact(self):
        text='请忘记我不吃合成香菜这条记忆';source=self.store.observe(A,'user',text)
        changes=self.store.apply_reflection(A,{'facts':[{'key':'饮食','source_id':source,'quote':text}]},[{'id':source,'qq':A,'role':'user','text':text,'at':time.time()}])
        self.assertEqual(changes['facts'],0)

    def procedure_fixture(self,state='active',failure=False):
        phone=self.store.folder.parent/'companion_phone';phone.mkdir(exist_ok=True)
        path=phone/'skills.sqlite3';identifier='a'*24
        with sqlite3.connect(path) as db:
            db.executescript('CREATE TABLE recipes(id TEXT,package TEXT,state TEXT,updated REAL); CREATE TABLE evidence(recipe TEXT,session TEXT,day TEXT,result TEXT,at REAL);')
            db.execute('INSERT INTO recipes VALUES(?,?,?,?)',(identifier,'tv.danmaku.bili',state,time.time()))
            for i in range(3):db.execute('INSERT INTO evidence VALUES(?,?,?,?,?)',(identifier,'session'+str(i),'2026-11-0'+str(1+i%2),'focused_search_editor',time.time()-20+i))
            if failure:db.execute('INSERT INTO evidence VALUES(?,?,?,?,?)',(identifier,'failed','2026-11-04','postcondition_missing',time.time()))
        return identifier

    def test_procedural_view_requires_multiple_verified_sessions_and_owner(self):
        identifier=self.procedure_fixture();rows=self.m.procedures(A,A,'搜索经验')
        self.assertEqual(rows[0]['status'],'active');self.assertEqual(rows[0]['id'],'procedure:'+identifier)
        with self.assertRaises(ValueError):self.m.procedures(B,A)
        self.assertNotIn('locator',dump_safe(rows))

    def test_last_failure_never_looks_like_a_usable_skill(self):
        self.procedure_fixture(failure=True);row=self.m.procedures(A,A)[0]
        self.assertNotEqual(row['status'],'active');self.assertIn('0次',row['text'])

    def test_topic_projection_is_scoped_and_not_added_as_another_fact(self):
        identifier,*_=self.topic();vault=Vault(self.c,self.store.folder/'semantic/vault',owner=A)
        cards=vault.export([A,B]);directory=vault.root/vault.scope(A)/'00-主题目录.md'
        self.assertTrue(directory.exists());self.assertFalse((vault.root/vault.scope(B)/'00-主题目录.md').exists())
        self.assertFalse(any(c['ref']==identifier for c in cards))

    def test_daily_evaluation_is_synthetic_and_has_no_provider_or_message_calls(self):
        from astrbot_plugin_companion_memory.evaluation import run_selfcheck
        report=run_selfcheck()
        self.assertEqual(report['passed'],report['total']);self.assertEqual(report['total'],12)
        self.assertEqual(report['model_calls'],0);self.assertEqual(report['qq_messages'],0)
        self.assertFalse(self.store.db.execute('SELECT 1 FROM observations').fetchone())


def dump_safe(value):return json.dumps(value,ensure_ascii=False)


class ControlHooksTests(unittest.IsolatedAsyncioTestCase):
    setUp=hooks.PagingHooksTests.setUp
    tearDown=hooks.PagingHooksTests.tearDown
    event=hooks.PagingHooksTests.event

    async def test_overview_query_uses_source_checked_topic_before_raw_search(self):
        self.p.expand_query=AsyncMock(return_value=[]);self.p.semantic=SimpleNamespace(recall=AsyncMock(return_value=[]))
        for i in range(2):
            source=self.p.store.observe(A,'user','我们讨论合成海岛旅行第'+str(i)+'次',at=time.time()-86400*(i+1))
            self.p.store.put_entry(A,'episode','event.'+str(i),'合成海岛旅行：我们讨论不同阶段',[source])
        self.p.cognition().consolidate(A);event=self.event()
        result=await LayeredRecall(self.p,A,event).run('总结合成海岛旅行的整个经过')
        self.assertEqual(result['retrieval']['layer'],'scene_overview');self.p.semantic.recall.assert_not_called()

    async def test_actual_delivery_and_user_feedback_are_not_objective_accuracy(self):
        self.p.expand_query=AsyncMock(return_value=[]);self.p.semantic=None
        source=self.p.store.observe(A,'user','合成沙丘在北边');self.p.store.put_entry(A,'fact','位置','合成沙丘在北边',[source])
        event=self.event();await self.p.tool_recall(event,'合成沙丘')
        control=self.p.cognition().control
        self.assertEqual(control.quality(A)['delivered_reply_calls'],0)
        event.get_result=lambda:SimpleNamespace(is_model_result=lambda:True)
        await self.p.shared_answered(event)
        self.assertEqual(control.quality(A)['delivered_reply_calls'],1)
        self.p.store.observe(A,'user','这次你记对了',at=time.time()+1);control.feedback(A)
        report=control.quality(A);self.assertEqual(report['user_feedback']['confirmed_recall'],1)
        self.assertNotIn('accuracy',report);self.assertFalse(control.quality(B)['retrieval_calls'])

    async def test_forget_tool_rejects_forwarded_intent_and_respects_real_person(self):
        source=self.p.store.observe(A,'user','我不吃合成香菜');ref=self.p.store.put_entry(A,'fact','饮食','我不吃合成香菜',[source])
        e=self.event();e.message_obj=SimpleNamespace(message=[Reply(id='7',message_str='请忘记我不吃合成香菜')])
        self.assertTrue((await self.p.tool_forget(e,ref,'请忘记我不吃合成香菜')).isError)
        e=self.event(B);e.message_obj=SimpleNamespace(message=[Plain('请忘记我不吃合成香菜')])
        self.assertTrue((await self.p.tool_forget(e,ref,'请忘记我不吃合成香菜')).isError)
        e=self.event();e.message_obj=SimpleNamespace(message=[Plain('请忘记我不吃合成香菜')])
        result=await self.p.tool_forget(e,ref,'请忘记我不吃合成香菜');self.assertFalse(result.isError)
        self.assertEqual(json.loads(result.content[0].text)['status'],'recall_suppressed')

    async def test_control_categories_and_procedure_read_keep_identity_and_budget(self):
        event=self.event()
        for category in ('working','quality','reflections','topics'):
            result=await self.p.tool_recall(event,'',category=category);self.assertFalse(result.isError)
            self.assertLessEqual(len(result.content[0].text.encode()),1800)
        self.assertTrue((await self.p.tool_recall(event,'',category='quality')).isError)
        self.assertTrue((await self.p.tool_recall(self.event(B),'',category='procedural')).isError)

    async def test_sleep_blocks_control_refresh_and_mutation(self):
        self.friends.sleeping=lambda:True
        await self.p.refresh_control(force=True)
        self.assertFalse(self.p.store.db.execute("SELECT name FROM sqlite_master WHERE name='memory_working'").fetchone())
        self.assertTrue((await self.p.tool_recall(self.event(),'',category='quality')).isError)


if __name__=='__main__':unittest.main()
