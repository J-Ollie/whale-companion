"""Synthetic longitudinal, rollover, forgetting, atomicity and cost acceptance tests."""
import asyncio
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock,patch
from datetime import datetime
sys.path.insert(0,os.environ.get('COMPANION_TEST_PLUGINS','/AstrBot/data/plugins'))
from astrbot_plugin_companion_memory.memory import MemoryStore,TZ
from astrbot_plugin_companion_memory.cognition import Cognition
from astrbot_plugin_companion_memory.budget import size,dump,pack,trim_history,CONTEXT_BYTES,TOOL_BYTES,REFLECTION_BYTES
from astrbot_plugin_companion_memory.reflection import prepare
from astrbot_plugin_companion_memory.main import CompanionMemory,REFLECT_SYSTEM
from astrbot.api.provider import ProviderRequest

A='12345678';B='87654321';OWNER='100000001'
NOW=datetime(2026,10,23,15,tzinfo=TZ).timestamp()


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.store=MemoryStore(self.tmp.name);self.c=Cognition(self.store)
    def tearDown(self):self.store.close();self.tmp.cleanup()
    def message(self,text,qq=A,at=NOW,role='user'):
        i=self.store.observe(qq,role,text,at=at)
        return dict(self.store.db.execute('SELECT * FROM observations WHERE id=?',(i,)).fetchone())
    def fact(self,text,key='偏好.咖啡',at=NOW):
        row=self.message(text,at=at)
        self.store.apply_reflection(A,{'facts':[{'key':key,'quote':text,'source_id':row['id']}]},[row],now=at)
        return self.store.db.execute('SELECT * FROM entries WHERE qq=? AND topic=?',(A,key)).fetchone()

    def test_plan_becomes_historical_without_claiming_completed(self):
        row=dict(self.fact('我明天要考试','计划.考试'))
        self.assertEqual(self.store.lifecycle.describe(row,NOW)['type'],'plan')
        self.assertTrue(self.store.entries(A,now=NOW))
        self.assertFalse(self.store.entries(A,now=NOW+3*86400))
        self.assertEqual(self.store.lifecycle.describe(row,NOW+3*86400)['status'],'past_due')
        self.assertIsNotNone(self.store.get_entry(A,row['id']))

    def test_one_explicit_preference_survives_many_days(self):
        row=dict(self.fact('我不吃香菜','饮食.香菜'))
        self.assertEqual(self.store.lifecycle.describe(row,NOW+400*86400)['type'],'semantic')
        self.assertTrue(self.store.entries(A,now=NOW+400*86400))

    def test_extraction_cannot_drop_negation_or_hypothetical_prefix(self):
        for text,quote in [('我不喜欢香菜','喜欢香菜'),('如果我说我养猫，那只是个假设','我养猫'),('我喜欢香菜吗？','我喜欢香菜')]:
            row=self.message(text)
            result=self.store.apply_reflection(A,{'facts':[{'key':'错误片段','quote':quote,'source_id':row['id']}]},[row],now=NOW)
            self.assertEqual(result['facts'],0)
        self.assertFalse(self.store.entries(A,now=NOW))

    def test_temporary_mood_is_not_a_permanent_trait(self):
        row=dict(self.fact('我今天心情不好','心情'))
        self.assertEqual(self.store.lifecycle.describe(row,NOW)['type'],'state')
        self.assertFalse(self.store.entries(A,now=NOW+86400))
        self.assertIsNotNone(self.store.get_entry(A,row['id']))

    def test_pinning_protects_editing_but_does_not_make_a_dated_plan_timeless(self):
        i=self.store.put_entry(A,'fact','计划.考试','我明天考试',[],now=NOW,pinned=True)
        row=self.store.get_entry(A,i)
        self.assertEqual(self.store.lifecycle.describe(row,NOW+3*86400)['status'],'past_due')
        self.assertTrue(row['pinned']);self.assertFalse(self.store.entries(A,now=NOW+3*86400))

    def test_conflict_does_not_overwrite_and_explicit_correction_resolves(self):
        old=dict(self.fact('我喝咖啡加糖'))
        self.fact('我喝咖啡不加糖',at=NOW+1)
        self.assertEqual(self.store.get_entry(A,old['id'])['text'],old['text'])
        self.assertEqual(self.store.lifecycle.describe(self.store.get_entry(A,old['id']),NOW+1)['status'],'conflicted')
        self.fact('现在喝咖啡只喝无糖的',at=NOW+2)
        self.assertIn('无糖',self.store.get_entry(A,old['id'])['text'])
        self.assertEqual(self.store.lifecycle.describe(self.store.get_entry(A,old['id']),NOW+2)['status'],'active')
        self.assertTrue(self.store.db.execute('SELECT 1 FROM entry_revisions WHERE entry_id=?',(old['id'],)).fetchone())

    def test_explicit_cancel_retires_plan(self):
        old=dict(self.fact('我明天要考试','计划.考试'))
        source=self.message('明天的考试取消了',at=NOW+30)
        self.store.apply_reflection(A,{'facts':[{'key':'计划.考试','quote':source['text'],'source_id':source['id'],'replaces_id':old['id'],'status':'cancelled'}]},[source],now=NOW+30)
        self.assertEqual(self.store.lifecycle.describe(self.store.get_entry(A,old['id']),NOW+30)['status'],'cancelled')
        self.assertFalse(self.store.entries(A,now=NOW+30))

    def test_transaction_failure_rolls_back_entries_index_and_cursor(self):
        before=self.store.db.execute('SELECT COUNT(*) FROM memory_fts').fetchone()[0]
        with self.assertRaises(RuntimeError):
            with self.store.atomic():
                self.store.remember(A,'我不吃香菜');self.store.set_meta('cursor:'+A,99)
                raise RuntimeError('synthetic interrupted consolidation')
        self.assertFalse(self.store.entries(A));self.assertEqual(self.store.get_meta('cursor:'+A,0),0)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM memory_fts').fetchone()[0],before)

    def test_full_history_index_finds_old_record_beyond_2000(self):
        source=self.message('以前推荐的书叫海底两万里',at=NOW-30*86400)
        with self.store.atomic():
            for i in range(2050):self.message('日常打招呼'+str(i),at=NOW+i)
        result=self.c.search(A,'以前那本海底两万里')
        self.assertIn('obs:'+str(source['id']),[r['id'] for r in result['matches']])
        self.assertFalse(self.c.search(B,'海底两万里')['matches'])

    def test_variant_retrieval_preserves_original_sources(self):
        source=self.message('这次推荐的小说是海底两万里')
        result=self.c.search(A,'那部潜艇冒险故事叫什么',variants=['海底两万里','推荐小说'])
        self.assertTrue(result['matches'])
        self.assertEqual(result['matches'][0]['id'],'obs:'+str(source['id']))

    def test_evidence_compaction_and_forget_do_not_create_facts_or_resurrect(self):
        ids=[]
        for text in ('上次一起讨论过海底两万里','我们下次可以继续讨论海底两万里'):
            row=self.message(text);ids.append(row['id'])
            self.c.digest(A,{'episodes':[{'topic':'海底两万里','items':[{'source_id':row['id'],'quote':row['text']}]}]},[row],0)
        self.c.consolidate(A)
        bundle=self.store.db.execute('SELECT * FROM memory_compendia WHERE qq=?',(A,)).fetchone()
        self.assertIsNotNone(bundle);self.assertFalse(self.store.entries(A,'fact'))
        entry=self.store.entries(A,'episode')[0];self.store.forget(A,entry['id']);self.c.consolidate(A)
        self.assertFalse(self.store.db.execute('SELECT 1 FROM memory_compendia WHERE qq=?',(A,)).fetchone())
        for match in self.c.search(A,'海底两万里')['matches']:
            self.assertNotIn('obs:'+str(json.loads(entry['evidence'])[0]),match['id'])

    def test_new_dialogue_and_restart_keep_preferences_and_pending_messages(self):
        self.fact('我不吃香菜','饮食.香菜')
        row=self.message('明天继续讨论海底两万里')
        self.store.set_meta('cursor:'+A,row['id']-1)
        self.store.close();self.store=MemoryStore(self.tmp.name);self.c=Cognition(self.store)
        context=self.c.render(A,'我们接着说吧')
        self.assertIn('香菜',context);self.assertIn('海底两万里',context)
        self.assertTrue(self.store.messages(A,after=self.store.get_meta('cursor:'+A)))
        self.assertNotIn('香菜',self.c.render(B,'继续聊'))

    def test_context_and_tool_outputs_have_hard_text_budgets(self):
        for i in range(30):
            self.store.put_entry(A,'fact','爱好'+str(i),'喜欢蓝色和海洋'*40,[])
        for i in range(6):self.message('超长消息'*400)
        self.assertLessEqual(size(self.c.render(A,'海洋')),CONTEXT_BYTES)
        self.assertLessEqual(size(self.c.search(A,'海洋',limit=8)),1800)

    def test_rollover_does_not_depend_on_infinite_context_or_diary(self):
        self.store.remember(A,'我一直喜欢蓝色')
        for i in range(50):self.message('普通闲聊'+str(i))
        (Path(self.tmp.name)/'private-diary.jsonl').write_text('PRIVATE_DIARY_SENTINEL',encoding='utf-8')
        context=self.c.render(A,'你记得我的偏好吗')
        self.assertIn('蓝色',context);self.assertNotIn('PRIVATE_DIARY_SENTINEL',context)
        self.assertLessEqual(size(context),CONTEXT_BYTES)


class BudgetTests(unittest.TestCase):
    def test_json_budget_is_utf8_and_well_formed(self):
        data=pack({'scope':'合成'},[('a',[{'id':str(i),'text':'鲸鱼'*20} for i in range(20)]),('b',[])],500)
        self.assertLessEqual(size(data),500);self.assertEqual(json.loads(dump(data)),data)

    def test_history_keeps_whole_tool_groups_and_does_not_mutate_archive(self):
        messages=[]
        for i in range(50):messages.extend([{'role':'user','content':'轮次'+str(i)},
            {'role':'assistant','content':None,'tool_calls':[{'id':'call'+str(i)}]},
            {'role':'tool','tool_call_id':'call'+str(i),'content':'数据'}, {'role':'assistant','content':'回答'}])
        original=dump(messages);trimmed=trim_history(messages)
        self.assertLessEqual(len(trimmed),12);self.assertEqual(trimmed[0]['role'],'user')
        self.assertEqual(dump(messages),original)
        calls={x['id'] for r in trimmed for x in r.get('tool_calls',[])}
        self.assertTrue(all(r.get('tool_call_id') in calls for r in trimmed if r['role']=='tool'))
        self.assertLessEqual(size(trimmed),7000)

    def test_single_huge_historical_turn_does_not_force_large_prompt(self):
        self.assertEqual(trim_history([{'role':'user','content':'鲸'*10000},{'role':'assistant','content':'回答'}]),[])

    def test_long_pending_message_is_processed_in_chunks_without_losing_tail(self):
        with tempfile.TemporaryDirectory() as folder:
            store=MemoryStore(folder)
            try:
                text='长消息测试'*700+'我不吃香菜';i=store.observe(A,'user',text,at=NOW)
                cursor=0;parts=[]
                for _ in range(12):
                    result=prepare(store,A,cursor,':'+A,NOW,REFLECT_SYSTEM)
                    if not result:break
                    payload,supplied,cursor,offset=result
                    self.assertLessEqual(size(REFLECT_SYSTEM)+size(payload),REFLECTION_BYTES)
                    parts.extend(r['text'] for r in supplied if r['id']==i)
                    store.set_meta('reflection_offset:'+A,offset)
                self.assertEqual(''.join(parts),text);self.assertEqual(cursor,i)
            finally:store.close()


class ToolCostTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.p=CompanionMemory.__new__(CompanionMemory)
        self.p.owner=OWNER;self.p.umo='qq-companion:FriendMessage:'+OWNER;self.p.config={'enabled':True,'auto_extract':True}
        self.p.store=MemoryStore(self.tmp.name);self.p.processing=asyncio.Lock()
        self.p.context=SimpleNamespace(get_registered_star=lambda _:None,get_config=lambda **kw:{'provider_settings':{'enable':True}},
            llm_generate=AsyncMock(return_value=SimpleNamespace(completion_text='{"queries":["推荐小说"]}')))
    def tearDown(self):self.p.store.close();self.tmp.cleanup()
    def event(self):
        extra={}
        return SimpleNamespace(get_sender_id=lambda:OWNER,unified_msg_origin=self.p.umo,message_str='继续聊',
            get_extra=lambda k,d=None:extra.get(k,d),set_extra=lambda k,v:extra.__setitem__(k,v))

    async def test_semantic_expansion_at_most_once_and_no_cross_person_data(self):
        self.p.store.observe(OWNER,'assistant','推荐小说是海底两万里')
        self.p.store.observe(B,'user','B_PRIVATE_SENTINEL')
        event=self.event()
        result=await self.p.tool_recall(event,'那部潜艇冒险故事')
        self.assertIn('海底两万里',result.content[0].text)
        for _ in range(3):await self.p.tool_recall(event,'那部冒险作品')
        self.assertEqual(self.p.context.llm_generate.await_count,1)
        self.assertNotIn('B_PRIVATE_SENTINEL',dump(self.p.context.llm_generate.call_args.kwargs))
        self.assertLessEqual(event.get_extra('companion_recall_bytes'),TOOL_BYTES)

    async def test_fresh_request_restores_same_persistent_core_without_altering_persona(self):
        self.p.store.remember(OWNER,'我喜欢蓝色')
        event=self.event();req=ProviderRequest(system_prompt='你叫岚，温和而好奇')
        await self.p.inject(event,req)
        self.assertIn('蓝色',''.join(r.text for r in req.extra_user_content_parts))
        self.assertEqual(req.system_prompt,'你叫岚，温和而好奇')
        req.contexts=[{'role':'user','content':'旧对话'*3000}]
        await self.p.bound_history(event,req)
        self.assertFalse(req.contexts);self.assertEqual(req.system_prompt,'你叫岚，温和而好奇')

    async def test_extraction_daily_cap_defers_without_advancing_cursor(self):
        self.p.store.observe(OWNER,'user','我不吃香菜')
        day=datetime.now(TZ).date().isoformat();self.p.store.set_meta('extraction_usage',{'day':day,'calls':48,'input_bytes':0})
        await self.p.reflect(force=True)
        self.p.context.llm_generate.assert_not_called();self.assertEqual(self.p.store.get_meta('cursor',0),0)

    async def test_bounded_extraction_and_pending_cursor_survive_restart(self):
        i=self.p.store.observe(OWNER,'user','我不吃香菜')
        self.p.context.llm_generate.return_value=SimpleNamespace(completion_text=dump({'facts':[{'key':'饮食.香菜','quote':'我不吃香菜','source_id':i}]}))
        await self.p.reflect(force=True)
        args=self.p.context.llm_generate.call_args.kwargs
        self.assertLessEqual(size(args['system_prompt'])+size(args['prompt']),REFLECTION_BYTES)
        self.assertEqual(args['max_tokens'],900)
        self.p.store.close();self.p.store=MemoryStore(self.tmp.name);self.p._cognition=Cognition(self.p.store)
        self.assertEqual(self.p.store.get_meta('cursor'),i)
        self.assertIn('香菜',self.p.cognition().render(OWNER))
