"""Public provenance, per-person continuity and per-group retrieval boundaries."""
import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock,Mock

sys.path.insert(0,os.environ.get('COMPANION_TEST_PLUGINS',str(Path(__file__).parent)))
from astrbot.api.message_components import Plain,Reply
from astrbot.core.provider.entities import ProviderRequest
from astrbot.core.agent.tool import ToolSet
from astrbot_plugin_companion_memory.memory import MemoryStore
from astrbot_plugin_companion_memory.main import CompanionMemory
from astrbot_plugin_companion_memory.shared import SharedMemory,PUBLIC,PUBLIC_KB
from astrbot_plugin_companion_memory.kb_access import install,CAPABILITY
from test_private_vault import Manager,InstalledPlugin

A='12345678';B='87654321';G='123450001';H='123450002'


class SharedTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.store=MemoryStore(Path(self.tmp.name)/'plugin_data/companion_memory');self.addCleanup(self.store.close)
        self.p=CompanionMemory.__new__(CompanionMemory);self.p.store=self.store;self.p.owner=A
        self.p.umo='qq-companion:FriendMessage:'+A;self.sleep=False
        self.p.config={'enabled':True,'shared_enabled':True,'shared_group_ids':[G,H]}
        self.p.profile=SimpleNamespace(data={'name':'','traits':''})
        self.friends=SimpleNamespace(policy=SimpleNamespace(is_friend=AsyncMock(return_value=True)))
        self.p.friends_plugin=lambda:self.friends;self.p.resting=lambda:self.sleep
        self.manager=Manager()
        self.p.context=SimpleNamespace(kb_manager=self.manager,provider_manager=SimpleNamespace(get_provider_by_id=AsyncMock(return_value=object())),
            get_registered_star=lambda n:SimpleNamespace(activated=True,star_cls=InstalledPlugin()),get_platform_inst=lambda _:None)
        key=self.store.folder/'semantic/vault-scope.key';key.parent.mkdir();key.write_bytes(b'k'*32)
        self.s=SharedMemory(self.p);self.p.shared=self.s;self.addCleanup(self.s.close)

    def event(self,group='',sender=A,text='你认识F1nger吗',mid=1,verified=True):
        extra={'companion_friend_verified':verified}
        raw={'message_type':'group' if group else 'private','user_id':int(sender),'message_id':mid,'time':time.time(),'sender':{'nickname':'合成昵称'}}
        if group:raw['group_id']=int(group)
        event=SimpleNamespace(message_str=text,message_obj=SimpleNamespace(message=[Plain(text)],raw_message=raw,message_id=str(mid)),
            unified_msg_origin='qq-companion:'+('GroupMessage:'+group if group else 'FriendMessage:'+sender),
            get_platform_id=lambda:'qq-companion',get_group_id=lambda:group,get_sender_id=lambda:sender,
            get_extra=lambda k,d=None:extra.get(k,d),set_extra=lambda k,v:extra.__setitem__(k,v),
            should_call_llm=lambda v:extra.__setitem__('suppressed',v),stop_event=lambda:extra.__setitem__('stopped',True),
            clear_result=lambda:None,plain_result=lambda t:t,get_result=lambda:None)
        return event

    async def test_public_seed_contains_known_identity_without_private_facts(self):
        self.store.observe(A,'user','私聊特有的项目密事不能公布')
        self.s.source_sync();data=await self.s.recall(self.event(group=G,sender=B),'F1nger')
        self.assertIn('F1nger',json.dumps(data,ensure_ascii=False))
        self.assertNotIn('项目密事',json.dumps(data,ensure_ascii=False))

    async def test_public_diary_requires_public_visibility_and_preserves_date(self):
        root=self.store.folder.parent/'companion_phone';root.mkdir()
        (root/'public-diary.jsonl').write_text(json.dumps({'id':'x','visibility':'public','day':'2025-01-01','text':'合成公开日记'},ensure_ascii=False)+'\n'+json.dumps({'id':'y','visibility':'private','text':'合成私密日记'},ensure_ascii=False)+'\n',encoding='utf-8')
        self.s.source_sync();texts=' '.join(r[0] for r in self.s.db.execute('SELECT text FROM records'))
        self.assertIn('合成公开日记',texts);self.assertIn('2025-01-01',texts);self.assertNotIn('合成私密日记',texts)
        self.assertEqual(self.s.db.execute("SELECT at FROM records WHERE kind='public_diary'").fetchone()[0],0)

    async def test_no_private_contact_names_are_automatically_published(self):
        self.store.observe(B,'user','我叫未公开的合成名字')
        self.s.source_sync()
        self.assertNotIn('未公开的合成名字',json.dumps(self.s.search([PUBLIC],'未公开的合成名字'),ensure_ascii=False))

    async def test_group_records_are_not_public_or_visible_in_other_group(self):
        self.s.observe_group(self.event(group=G,text='这个群的合成活动约定'))
        self.assertTrue((await self.s.recall(self.event(group=G),'合成活动'))['matches'])
        self.assertFalse(self.s.search([PUBLIC],'合成活动'))
        self.assertFalse(self.s.search(['group:'+H],'合成活动'))

    async def test_group_records_do_not_follow_person_into_private_chat(self):
        identifier=self.s.observe_group(self.event(group=G,text='本群限定的合成原话'))
        result=await self.s.recall(self.event(),'','shared:'+identifier)
        self.assertNotIn('text',result)

    async def test_same_message_id_in_two_groups_preserves_both_sources(self):
        first=self.s.observe_group(self.event(group=G,text='第一群记录',mid=4))
        second=self.s.observe_group(self.event(group=H,text='第二群记录',mid=4))
        self.assertNotEqual(first,second)
        self.assertEqual(self.s.db.execute("SELECT COUNT(*) FROM records WHERE kind='group_statement'").fetchone()[0],2)

    async def test_withdrawn_projection_preserves_hand_edits(self):
        identifier=self.s.put(PUBLIC,'manual','合成投影','projection-test',A)
        await self.s.sync(force=True)
        path=self.s.root/'vault/公共记忆'/(identifier+'.md')
        path.write_text('保留的手工批注',encoding='utf-8')
        self.s.forget(self.event(),identifier,[PUBLIC]);await self.s.sync(force=True)
        self.assertFalse(path.exists())
        edits=list((self.s.root/'vault/local-edits').rglob(identifier+'.md'))
        self.assertEqual(len(edits),1);self.assertEqual(edits[0].read_text(encoding='utf-8'),'保留的手工批注')

    async def test_different_private_windows_keep_same_person_scope(self):
        source=self.store.observe(A,'user','跨会话稳定偏好')
        self.store.put_entry(A,'fact','偏好','跨会话稳定偏好',[source])
        self.assertEqual(await self.p.recall_subject(self.event(mid=1)),A)
        self.assertEqual(await self.p.recall_subject(self.event(mid=200)),A)

    async def test_forged_group_sender_and_unapproved_group_are_rejected(self):
        e=self.event(group=G,sender=B);e.message_obj.raw_message['user_id']=int(A)
        with self.assertRaises(ValueError):await self.s.scopes(e)
        with self.assertRaises(ValueError):await self.s.scopes(self.event(group='999990001'))

    async def test_group_cannot_use_personal_memory_even_same_sender(self):
        with self.assertRaises(ValueError):await self.p.recall_subject(self.event(group=G,sender=A))

    async def test_quote_is_not_recorded_as_current_speaker_statement(self):
        e=self.event(group=G,text='帮我看这段引用')
        e.message_obj.message=[Plain('帮我看这段引用'),Reply(id='9',message_str='被引用者的私事')]
        self.s.observe_group(e)
        self.assertNotIn('被引用者的私事',json.dumps(self.s.search(['group:'+G],'被引用者的私事'),ensure_ascii=False))

    async def test_explicit_publication_command_only_owner_private(self):
        self.assertEqual([r async for r in self.p.remember_public(self.event(group=G),'不能从群命令公开')],[])
        self.assertEqual([r async for r in self.p.remember_public(self.event(sender=B),'不能由好友发布')],[])
        result=[r async for r in self.p.remember_public(self.event(),'这是明确允许公开的合成资料')]
        self.assertIn('已存为',result[0]);self.assertTrue(self.s.search([PUBLIC],'合成资料'))

    async def test_author_can_forget_own_group_statement_but_not_others(self):
        identifier=self.s.observe_group(self.event(group=G,sender=B,text='作者自己的合成记录'))
        with self.assertRaises(ValueError):self.s.forget(self.event(group=G,sender='11111222'),identifier,['group:'+G])
        self.assertTrue(self.s.forget(self.event(group=G,sender=B),identifier,['group:'+G]))
        self.assertFalse(self.s.search(['group:'+G],'合成记录'))

    async def test_public_forget_does_not_get_reseeded(self):
        self.s.source_sync();identifier=self.s.db.execute("SELECT id FROM records WHERE kind='relationship'").fetchone()[0]
        self.s.forget(self.event(),identifier,[PUBLIC]);self.s.source_sync()
        self.assertFalse(self.s.search([PUBLIC],'F1nger'))

    async def test_group_injection_has_public_identity_without_private_material(self):
        e=self.event(group=G,sender=B);req=ProviderRequest(prompt=e.message_str,func_tool=ToolSet())
        await self.p.inject_shared(e,req)
        text=' '.join(part.text for part in req.extra_user_content_parts)
        self.assertIn('F1nger',text);self.assertIn('public_and_current_group',text)
        self.assertNotIn('companion_context',text)
        self.assertTrue(e.get_extra('companion_context_round_guard'))

    async def test_sleep_defers_group_without_reading_or_model_reply(self):
        self.sleep=True;e=self.event(group=G)
        req=ProviderRequest(prompt=e.message_str)
        await self.p.inject_shared(e,req)
        self.assertTrue(e.get_extra('stopped'));self.assertEqual(req.extra_user_content_parts,[])
        self.assertEqual(self.s.db.execute("SELECT COUNT(*) FROM unread WHERE state='pending'").fetchone()[0],1)
        self.assertFalse(self.s.db.execute('SELECT 1 FROM records').fetchone())

    async def test_unread_deduplicates_and_does_not_dispatch_while_asleep(self):
        e=self.event(group=G);self.s.defer(e.message_obj.raw_message);self.s.defer(e.message_obj.raw_message)
        self.sleep=True;await self.s.drain()
        self.assertEqual(self.s.db.execute('SELECT COUNT(*) FROM unread').fetchone()[0],1)

    async def test_wake_dispatch_preserves_real_group_sender_and_marks_actual_reply(self):
        original=self.event(group=G,sender=B,text='休息期间的群原话')
        self.s.defer(original.message_obj.raw_message)
        replay=self.event(group=G,sender=B,text='休息期间的群原话')
        adapter=SimpleNamespace(convert_message=AsyncMock(return_value=original.message_obj),create_event=lambda _:replay,commit_event=Mock())
        self.p.context.get_platform_inst=lambda _:adapter
        await self.s.drain();adapter.commit_event.assert_called_once_with(replay)
        self.assertEqual(replay.get_sender_id(),B);self.assertEqual(self.s.group(replay),'group:'+G)
        replay.get_result=lambda:SimpleNamespace(is_model_result=lambda:True,get_plain_text=lambda:'收到这条群原话')
        self.s.answered(replay)
        self.assertEqual(self.s.db.execute('SELECT state FROM unread').fetchone()[0],'completed')

    async def test_uncertain_delivery_is_not_automatically_replayed(self):
        self.s.defer(self.event(group=G).message_obj.raw_message)
        with self.s.db:self.s.db.execute("UPDATE unread SET state='dispatched'")
        self.s.pipeline_finished(1)
        self.assertEqual(self.s.db.execute('SELECT state FROM unread').fetchone()[0],'uncertain')

    async def test_group_shared_tool_uses_existing_four_call_budget(self):
        self.s.source_sync();e=self.event(group=G)
        for _ in range(4):self.assertFalse((await self.p.shared_recall(e,'F1nger')).isError)
        self.assertTrue((await self.p.shared_recall(e,'F1nger')).isError)
        self.assertLessEqual(e.get_extra('companion_recall_bytes'),4200)

    async def test_reference_counted_guard_survives_one_consumer_stopping(self):
        other=install(self.manager);self.s.release()
        with self.assertRaises(ValueError):await self.manager.retrieve('x',['WhaleGroupPrivate-x'])
        token=CAPABILITY.set(frozenset(['WhaleGroupPrivate-x']))
        try:await self.manager.retrieve('x',['WhaleGroupPrivate-x'])
        finally:CAPABILITY.reset(token)
        other()

    async def test_forget_filters_stale_vector_before_physical_cleanup(self):
        identifier=self.s.put(PUBLIC,'manual','香菜的合成知识','manual-test',A)
        await self.s.sync(force=True)
        self.s.forget(self.event(),identifier,[PUBLIC])
        self.assertNotIn('shared:'+identifier,[r['id'] for r in (await self.s.recall(self.event(),'芫荽'))['matches']])
        await self.s.sync(force=True)
        self.assertFalse(self.s.db.execute('SELECT 1 FROM docs WHERE ref=?',(identifier,)).fetchone())

    async def test_group_export_and_index_remain_separate(self):
        self.s.observe_group(self.event(group=G,text='群聊合成记忆'))
        await self.s.sync(force=True)
        names=[r[0] for r in self.s.db.execute('SELECT name FROM kbs')]
        self.assertIn(PUBLIC_KB,names);self.assertTrue(any(n.startswith('WhaleGroupPrivate-') for n in names))
        self.assertTrue((self.s.root/'vault/公共记忆/00-公共记忆目录.md').exists())
        self.assertTrue(list((self.s.root/'vault/群聊记忆').rglob('*.md')))


if __name__=='__main__':unittest.main()
