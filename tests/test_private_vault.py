"""Private projections, candidate isolation, revocation and unchanged recall gates."""
import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock
import uuid

sys.path.insert(0,os.environ.get('COMPANION_TEST_PLUGINS',str(Path(__file__).parent)))
from astrbot_plugin_companion_memory.memory import MemoryStore
from astrbot_plugin_companion_memory.cognition import Cognition
from astrbot_plugin_companion_memory.semantic import SemanticMemory,PREFIX
from astrbot_plugin_companion_memory.vault import fingerprint
from astrbot_plugin_companion_memory.main import CompanionMemory
import test_memory_completeness_hooks as fixture

A='12345678';B='87654321'


class StagingManager:
    def __init__(self,data_dir,**kwargs):self.root=data_dir


class SyncEngine:
    def __init__(self,staging,context,kb_id,kb_name,**kwargs):
        self.context=context;self.kb_id=kb_id;self.kb_name=kb_name
    async def _upload_document(self,name,content):
        identifier=uuid.uuid4().hex
        self.context.kb_manager.documents[identifier]={'content':content,'name':name,'kb_id':self.kb_id,'kb_name':self.kb_name}
        return identifier
    async def _delete_document(self,identifier):
        self.context.kb_manager.documents.pop(identifier,None)
        return True


class InstalledPlugin:pass


class Manager:
    def __init__(self):self.kbs={};self.documents={};self.requests=0;self.extra_hits=[]
    async def get_kb_by_name(self,name):return self.kbs.get(name)
    async def get_kb(self,identifier):return next((v for v in self.kbs.values() if v.kb.kb_id==identifier),None)
    async def create_kb(self,kb_name,embedding_provider_id,**kwargs):
        helper=SimpleNamespace(kb=SimpleNamespace(kb_id=uuid.uuid4().hex,kb_name=kb_name,embedding_provider_id=embedding_provider_id))
        self.kbs[kb_name]=helper;return helper
    async def retrieve(self,query,kb_names,**kwargs):
        self.requests+=1
        return {'results':[{'doc_id':k,**v} for k,v in self.documents.items() if v['kb_name'] in kb_names]+self.extra_hits}


class VaultTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.store=MemoryStore(Path(self.tmp.name)/'memory');self.addCleanup(self.store.close)
        self.c=Cognition(self.store);self.manager=Manager();self.sleep=False
        self.friends=SimpleNamespace(policy=SimpleNamespace(is_friend=AsyncMock(return_value=True)))
        self.config={'semantic_enabled':True}
        context=SimpleNamespace(kb_manager=self.manager,provider_manager=SimpleNamespace(get_provider_by_id=AsyncMock(return_value=object())),
            get_registered_star=lambda n:SimpleNamespace(activated=True,star_cls=InstalledPlugin()))
        self.p=SimpleNamespace(config=self.config,owner=A,store=self.store,cognition=lambda:self.c,resting=lambda:self.sleep,
            context=context,friends_plugin=lambda:self.friends)
        self.s=SemanticMemory(self.p);self.addCleanup(self.s.close)
        self.source=self.store.observe(A,'user','我不吃芫荽',origin='qq-companion:FriendMessage:'+A)
        self.entry=self.store.put_entry(A,'fact','饮食','我不吃芫荽',[self.source])
        self.other=self.store.observe(B,'user','另一位联系人自己的私密原话',origin='qq-companion:FriendMessage:'+B)

    async def test_export_preserves_full_text_and_typed_sources(self):
        text='长原话'+('鲸'*10000)+'结尾'
        source=self.store.observe(A,'user',text)
        cards=self.s.vault.export([A])
        card=next(c for c in cards if c['ref']=='obs:'+str(source))
        body=(self.s.vault.root/self.s.vault.relative(card)).read_text()
        self.assertIn(text,body);self.assertIn('source_ids:',body);self.assertIn('来源原文编号',body)
        self.assertLess(len(self.s.vault.render(card,index=True)),len(body))

    async def test_export_filters_secrets_and_never_exports_diaries(self):
        self.store.observe(A,'user','密码：synthetic_secret_only',origin='test')
        cards=self.s.vault.export([A]);all_text=''.join(self.s.vault.render(c) for c in cards)
        self.assertNotIn('synthetic_secret_only',all_text)
        self.assertNotIn('diary',set(c['kind'] for c in cards))

    async def test_contact_scopes_are_distinct_and_stable(self):
        self.assertNotEqual(self.s.vault.scope(A),self.s.vault.scope(B))
        from astrbot_plugin_companion_memory.vault import Vault
        self.assertEqual(self.s.vault.scope(A),Vault(self.c,self.s.vault.root).scope(A))

    async def test_chinese_category_navigation_is_readable_and_not_indexed(self):
        cards=self.s.vault.export([A,B])
        overview=(self.s.vault.root/'00-记忆总览.md').read_text()
        self.assertIn('管理员',overview);self.assertNotIn(A,overview);self.assertNotIn(B,overview)
        directory=(self.s.vault.root/self.s.vault.scope(A)/'00-分类目录.md').read_text()
        for label in ('稳定事实与偏好','计划与约定','共同事件','话题续接','用户原话','助手原话'):
            self.assertIn(label,directory)
        self.assertTrue(all(c['ref']!='00-记忆总览' for c in cards))
        manifest=json.loads((self.s.vault.root/'manifest.json').read_text())
        self.assertEqual(manifest['notes'],len(cards));self.assertEqual(manifest['navigation'],3)

    async def test_native_document_name_shows_type_and_keeps_reference(self):
        card=self.s.vault.card(A,self.entry)
        title=self.s.vault.document_name(card)
        self.assertTrue(title.startswith('稳定事实与偏好__'))
        self.assertIn(self.entry,title)

    async def test_label_changes_do_not_change_source_fingerprint(self):
        card=self.s.vault.card(A,self.entry);before=fingerprint(card)
        self.s.vault.render(card);self.s.vault.document_name(card);self.s.vault.navigation([card],[A])
        self.assertEqual(fingerprint(card),before)

    async def test_manual_edits_are_preserved_before_projection_refresh(self):
        cards=self.s.vault.export([A]);path=self.s.vault.root/self.s.vault.relative(cards[0])
        path.write_text('手工批注需要保留',encoding='utf-8')
        self.s.vault.export([A])
        edits=list((self.s.vault.root/'local-edits').rglob('*.md'))
        self.assertTrue(any(p.read_text()=='手工批注需要保留' for p in edits))

    async def test_unmanaged_file_survives_export(self):
        note=self.s.vault.root/'my-note.md';note.write_text('自建笔记')
        self.s.vault.export([A]);self.assertEqual(note.read_text(),'自建笔记')

    async def test_sync_creates_separate_private_kbs_and_uses_plugin_engine(self):
        await self.s.sync(force=True)
        scopes=list(self.s.db.execute('select * from scopes'))
        self.assertEqual(len(scopes),3);self.assertEqual(len({r['kb_id'] for r in scopes}),3)
        self.assertTrue(all(r['kb_name'].startswith(PREFIX) for r in scopes))
        self.assertEqual(self.s.last['pending'],0)
        self.assertTrue(all(isinstance(e,SyncEngine) for e in self.s.engines.values()))

    async def test_generic_knowledge_retrieval_cannot_read_private_memory(self):
        await self.s.sync(force=True)
        name=next(iter(self.manager.kbs))
        with self.assertRaises(ValueError):await self.manager.retrieve('test',[name])
        await self.manager.retrieve('test',['public-notes'])

    async def test_semantic_queries_only_current_subject(self):
        await self.s.sync(force=True)
        refs=await self.s.recall(A,'香菜要不要加')
        self.assertIn(self.entry,refs)
        self.assertNotIn('obs:'+str(self.other),refs)

    async def test_malicious_other_kb_candidate_is_rejected(self):
        await self.s.sync(force=True)
        other_doc=self.s.db.execute('select doc_id from docs where qq=?',(B,)).fetchone()[0]
        self.manager.extra_hits=[{'kb_id':'foreign','doc_id':other_doc,'content':'伪造结果'}]
        refs=await self.s.recall(A,'问题');self.assertNotIn('obs:'+str(self.other),refs)

    async def test_forged_reference_in_document_body_is_not_trusted(self):
        await self.s.sync(force=True)
        for doc in self.manager.documents.values():doc['content']='memory_ref: obs:'+str(self.other)
        self.assertNotIn('obs:'+str(self.other),await self.s.recall(A,'问题'))

    async def test_forget_is_immediately_effective_before_index_cleanup(self):
        await self.s.sync(force=True)
        self.assertTrue(self.store.forget(A,self.entry))
        refs=await self.s.recall(A,'香菜')
        self.assertNotIn(self.entry,refs);self.assertNotIn('obs:'+str(self.source),refs)
        self.assertIsNotNone(self.store.db.execute('select 1 from observations where id=?',(self.source,)).fetchone())

    async def test_forget_removes_vectors_and_generated_note(self):
        await self.s.sync(force=True)
        self.store.forget(A,self.entry);await self.s.sync(force=True)
        self.assertFalse(self.s.db.execute('select 1 from docs where ref=?',(self.entry,)).fetchone())
        self.assertFalse(any(self.entry in str(p) for p in self.s.vault.root.rglob('*.md')))

    async def test_failed_vector_delete_stays_in_pending_and_cannot_be_used(self):
        await self.s.sync(force=True)
        for engine in self.s.engines.values():engine._delete_document=AsyncMock(return_value=False)
        self.store.forget(A,self.entry);await self.s.sync(force=True)
        self.assertGreater(self.s.last['pending'],0)
        self.assertNotIn(self.entry,await self.s.recall(A,'香菜'))

    async def test_source_edit_invalidates_stale_vector(self):
        await self.s.sync(force=True)
        with self.store.db:self.store.db.execute('update entries set text=? where id=?',('我现在吃芫荽',self.entry))
        self.assertNotIn(self.entry,await self.s.recall(A,'香菜'))

    async def test_completed_plan_migrates_to_history_bucket(self):
        source=self.store.observe(A,'user','我明天讨论海洋调查')
        entry=self.store.put_entry(A,'fact','计划','我明天讨论海洋调查',[source])
        await self.s.sync(force=True)
        with self.store.db:self.store.db.execute("update memory_state set status='completed' where qq=? and id=?",(A,entry))
        self.assertNotIn(entry,await self.s.recall(A,'调查'))
        await self.s.sync(force=True)
        self.assertEqual(self.s.db.execute('select bucket from docs where ref=?',(entry,)).fetchone()[0],'shallow-history')
        self.assertIn(entry,await self.s.recall(A,'以前的调查',historical=True))

    async def test_revoked_contact_notes_and_vectors_are_removed(self):
        await self.s.sync(force=True)
        self.friends.policy.is_friend.return_value=False
        await self.s.sync(force=True)
        self.assertFalse(self.s.db.execute('select 1 from docs where qq=?',(B,)).fetchone())
        self.assertFalse(list((self.s.vault.root/self.s.vault.scope(B)).rglob('*.md')))

    async def test_sleep_prevents_export_embedding_and_retrieval(self):
        self.sleep=True;await self.s.sync(force=True)
        self.assertFalse((self.s.vault.root/'manifest.json').exists())
        self.assertEqual(await self.s.recall(A,'问题'),[]);self.assertEqual(self.manager.requests,0)

    async def test_sleep_transition_during_upload_does_not_commit_index_mapping(self):
        engine=await self.s.engine(A,'shallow-active')
        original=engine._upload_document
        async def sleeping_upload(*args):
            doc=await original(*args);self.sleep=True;return doc
        engine._upload_document=sleeping_upload
        await self.s.sync(force=True)
        self.assertFalse(self.s.db.execute('select 1 from docs').fetchone())
        self.assertFalse(self.manager.documents)

    async def test_semantic_query_budget_persists(self):
        await self.s.sync(force=True);self.config['semantic_daily_queries']=1
        await self.s.recall(A,'问题');calls=self.manager.requests
        self.assertEqual(await self.s.recall(A,'另一个问题'),[]);self.assertEqual(self.manager.requests,calls)

    async def test_zero_index_budget_preserves_notes_and_reports_pending(self):
        self.config['semantic_daily_index_docs']=0
        await self.s.sync(force=True)
        self.assertGreater(self.s.last['notes'],0);self.assertGreater(self.s.last['pending'],0)
        self.assertFalse(self.manager.documents)

    async def test_category_date_and_contact_filters_apply_to_semantic_refs(self):
        plan_source=self.store.observe(A,'user','我明天讨论海底调查')
        plan=self.store.put_entry(A,'fact','计划','我明天讨论海底调查',[plan_source])
        refs=[self.entry,plan,'obs:'+str(self.other)]
        plans=self.c.search(A,'明日行程',category='plans',semantic_refs=refs)
        self.assertEqual([r['id'] for r in plans['matches']],[plan])
        facts=self.c.search(A,'饮食习惯',category='facts',semantic_refs=refs)
        self.assertEqual([r['id'] for r in facts['matches']],[self.entry])
        dated=self.c.search(A,'饮食',start_day='2000-01-01',end_day='2000-01-01',semantic_refs=refs)
        self.assertFalse(dated['matches'])

    async def test_restore_does_not_overwrite_another_component(self):
        replacement=AsyncMock();self.manager.retrieve=replacement;self.s.restore()
        self.assertIs(self.manager.retrieve,replacement)

    async def test_interrupted_unmapped_upload_is_physically_reconciled(self):
        await self.s.sync(force=True)
        engine=next(iter(self.s.engines.values()))
        orphan=await engine._upload_document('interrupted.md','未提交映射的合成内容')
        for current in self.s.engines.values():
            async def listing(offset=0,limit=500,search=None,kb=current.kb_id):
                return [SimpleNamespace(doc_id=k) for k,v in self.manager.documents.items() if v['kb_id']==kb][offset:offset+limit]
            current._kb_helper=SimpleNamespace(list_documents=listing)
        await self.s.sync(force=True)
        self.assertNotIn(orphan,self.manager.documents)


class ExistingGateTests(unittest.IsolatedAsyncioTestCase):
    setUp=fixture.PagingHooksTests.setUp
    tearDown=fixture.PagingHooksTests.tearDown
    event=fixture.PagingHooksTests.event

    async def test_semantic_path_obeys_sender_revocation_and_sleep_before_call(self):
        self.p.semantic=SimpleNamespace(recall=AsyncMock(return_value=[]))
        self.friends.policy.is_friend.return_value=False
        self.assertTrue((await self.p.tool_recall(self.event(),'问题')).isError)
        self.p.semantic.recall.assert_not_called()
        self.friends.policy.is_friend.return_value=True;self.friends.sleeping=lambda:True
        self.assertTrue((await self.p.tool_recall(self.event(),'问题')).isError)
        self.p.semantic.recall.assert_not_called()

    async def test_same_tool_budget_and_source_revalidation_for_semantic_results(self):
        source=self.p.store.observe(fixture.A,'user','我不吃芫荽')
        entry=self.p.store.put_entry(fixture.A,'fact','饮食','我不吃芫荽',[source])
        self.p.semantic=SimpleNamespace(recall=AsyncMock(return_value=[entry]))
        event=self.event()
        result=await self.p.tool_recall(event,'香菜要不要加',category='facts')
        data=json.loads(result.content[0].text)
        self.assertEqual(data['matches'][0]['id'],entry)
        self.assertEqual(event.get_extra('companion_recall_calls'),1)
        self.assertLessEqual(event.get_extra('companion_recall_bytes'),4200)
        self.assertLessEqual(len(result.content[0].text.encode()),1800)


if __name__=='__main__':unittest.main()
