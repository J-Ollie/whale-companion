"""Run against the installed AstrBot SDK; never send QQ messages."""
import asyncio
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

sys.path.insert(0,os.environ.get('COMPANION_TEST_PLUGINS','/AstrBot/data/plugins'))
from astrbot_plugin_companion_memory.memory import MemoryStore, TZ, clean, parse_json
from astrbot_plugin_companion_memory.main import CompanionMemory
from astrbot.core.star.filter.command import CommandFilter
from astrbot.api.provider import ProviderRequest

NOW=datetime(2026,10,20,15,0,tzinfo=TZ).timestamp()
QQ='100000001'


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.store=MemoryStore(self.temp.name)
    def tearDown(self):
        self.store.close()
        self.temp.cleanup()
    def source(self,text='我不吃香菜',role='user',at=NOW):
        ident=self.store.observe(QQ,role,text,at=at)
        return self.store.messages(QQ,after=ident-1,limit=1)[0]

    def test_persistence_and_person_isolation(self):
        ident=self.store.remember(QQ,'咖啡不加糖')
        self.store.close()
        self.store=MemoryStore(self.temp.name)
        self.assertEqual(self.store.get_entry(QQ,ident)['text'],'咖啡不加糖')
        self.assertIsNone(self.store.get_entry('other',ident))
        self.assertNotIn('咖啡',self.store.render('other'))

    def test_only_verbatim_user_evidence_becomes_fact(self):
        user=self.source()
        bot=self.source('他养了三只猫',role='assistant')
        output={'facts':[
            {'key':'饮食.香菜','quote':'我不吃香菜','source_id':user['id']},
            {'key':'养猫','quote':'他养了三只猫','source_id':bot['id']},
            {'key':'虚构','quote':'我住在北京','source_id':user['id']},
            {'key':'假id','quote':'我不吃香菜','source_id':9999},
        ]}
        changed=self.store.apply_reflection(QQ,output,[user,bot],now=NOW)
        self.assertEqual(changed['facts'],1)
        self.assertEqual(len(self.store.entries(QQ,now=NOW)),1)

    def test_old_processed_evidence_does_not_rewrite_new_fact(self):
        old=self.source('我喜欢甜咖啡')
        recent=self.source('现在咖啡只喝无糖的',at=NOW+60)
        self.store.apply_reflection(QQ,{'facts':[{'key':'咖啡','quote':recent['text'],'source_id':recent['id']}]},[old,recent],cursor=old['id'],now=NOW+60)
        self.store.apply_reflection(QQ,{'facts':[{'key':'咖啡','quote':old['text'],'source_id':old['id']}]},[old,recent],cursor=old['id'],now=NOW+120)
        self.assertIn('无糖',self.store.entries(QQ,now=NOW+120)[0]['text'])

    def test_manual_correction_pins_against_automatic_rewrite(self):
        ident=self.store.put_entry(QQ,'fact','咖啡','加糖',[],now=NOW)
        updated=self.store.correct(QQ,ident,'不加糖')
        self.assertIsNotNone(updated)
        self.store.put_entry(QQ,'fact','咖啡','加糖',[],now=NOW+20)
        self.assertEqual(self.store.get_entry(QQ,updated)['text'],'不加糖')

    def test_out_of_order_extraction_cannot_restore_older_fact(self):
        self.store.put_entry(QQ,'fact','咖啡','新偏好',[20],now=NOW)
        self.store.put_entry(QQ,'fact','咖啡','旧偏好',[10],now=NOW+60)
        self.assertEqual(self.store.entries(QQ,now=NOW+60)[0]['text'],'新偏好')

    def test_forget_blocks_same_topic_and_same_quote_under_new_topic(self):
        ident=self.store.put_entry(QQ,'fact','咖啡','不加糖',[],now=NOW)
        self.assertTrue(self.store.forget(QQ,ident))
        self.assertIsNone(self.store.put_entry(QQ,'fact','咖啡','新推断',[],now=NOW+20))
        self.assertIsNone(self.store.put_entry(QQ,'fact','另一个key','不加糖',[],now=NOW+20))
        self.assertFalse(self.store.entries(QQ,now=NOW+20))

    def test_secret_filter_and_invalid_correction_preserves_old(self):
        ident=self.store.remember(QQ,'不加糖')
        secret='API Key: sk-'+'a'*25
        self.assertNotIn('sk-',clean(secret))
        self.assertIsNone(self.store.remember(QQ,secret))
        self.assertIsNone(self.store.correct(QQ,ident,secret))
        self.assertIsNotNone(self.store.get_entry(QQ,ident))

    def test_diary_cap_spacing_dedup_and_export_delete(self):
        ids=[]
        for index in range(3):
            ids.append(self.store.add_diary(QQ,f'今天的片段{index}','平常',[1],now=NOW+index*7201))
        self.assertTrue(all(ids))
        self.assertIsNone(self.store.add_diary(QQ,'第四条','平常',[1],now=NOW+3*7201))
        self.assertIsNone(self.store.add_diary(QQ,'今天的片段0','平常',[1],now=NOW+86400))
        exported=Path(self.temp.name)/'diary'/QQ/'日记.jsonl'
        self.assertEqual(len(exported.read_text(encoding='utf-8').splitlines()),3)
        self.store.delete_diary(QQ,ids[1])
        self.assertNotIn('今天的片段1',exported.read_text(encoding='utf-8'))
        self.assertIsNone(self.store.add_diary(QQ,'太近了','平常',[1],now=NOW+15000))

    def test_diary_and_impression_never_become_fact_sources(self):
        row=self.source()
        self.store.add_diary(QQ,'我出去喝咖啡了','开心',[row['id']],now=NOW)
        output={'facts':[{'key':'虚构经历','quote':'我出去喝咖啡了','source_id':row['id']}]}
        self.store.apply_reflection(QQ,output,[row],now=NOW)
        self.assertFalse(self.store.entries(QQ,now=NOW))

    def test_impressions_require_two_user_sources_and_expire(self):
        a=self.source('请直接点说')
        b=self.source('回复不用那么长')
        value={'impression':{'text':'似乎更喜欢简短直接的交流','source_ids':[a['id']]}}
        self.store.apply_reflection(QQ,value,[a,b],now=NOW)
        self.assertFalse(self.store.entries(QQ,'impression',now=NOW))
        value['impression']['source_ids'].append(b['id'])
        self.store.apply_reflection(QQ,value,[a,b],now=NOW)
        self.assertEqual(len(self.store.entries(QQ,'impression',now=NOW)),1)
        self.assertFalse(self.store.entries(QQ,'impression',now=NOW+31*86400))

    def test_growth_requires_distinct_days_and_cannot_change_twice_daily(self):
        a=self.source('少怼我两句吧',at=NOW-86400)
        b=self.source('认真说就好',at=NOW)
        value={'growth':{'text':'认真话题时少用无缘由的调侃','source_ids':[a['id'],b['id']]}}
        self.store.apply_reflection(QQ,value,[a,b],now=NOW)
        self.assertEqual(len(self.store.entries(QQ,'growth',now=NOW)),1)
        value['growth']['text']='瞬间换一个性格'
        self.store.apply_reflection(QQ,value,[a,b],now=NOW+3600)
        self.assertNotIn('瞬间',self.store.entries(QQ,'growth',now=NOW+3600)[0]['text'])

    def test_same_day_growth_is_rejected(self):
        a=self.source('少怼我两句吧',at=NOW-60)
        b=self.source('认真说就好',at=NOW)
        self.store.apply_reflection(QQ,{'growth':{'text':'少些调侃','source_ids':[a['id'],b['id']]}},[a,b],now=NOW)
        self.assertFalse(self.store.entries(QQ,'growth',now=NOW))

    def test_time_is_beijing_and_previous_gap_is_real(self):
        text=self.store.render(QQ,previous=NOW-3*86400,now=NOW)
        self.assertIn('2026-10-20 15:00',text)
        self.assertIn('星期二',text)
        self.assertIn('259200',text)
        self.assertIn('尚无准确记录',self.store.render(QQ,previous=None,now=NOW))

    def test_retrieval_prefers_relevant_chinese_memory(self):
        self.store.put_entry(QQ,'fact','饮食.香菜','不吃香菜',[],now=NOW)
        for index in range(15):
            self.store.put_entry(QQ,'fact',f'其他{index}','喜欢蓝色衣服',[],now=NOW+index)
        self.assertEqual(self.store.retrieve(QQ,'香菜',limit=1,now=NOW+100)[0]['text'],'不吃香菜')

    def test_duplicate_qq_event_only_records_once(self):
        self.store.observe(QQ,'user','你好',dedupe='qq:1')
        self.store.observe(QQ,'user','你好',dedupe='qq:1')
        self.assertEqual(len(self.store.messages(QQ)),1)

    def test_bad_model_json_is_not_executed(self):
        with self.assertRaises(ValueError): parse_json('不是JSON')
        with self.assertRaises(ValueError): parse_json('[]')
        self.assertEqual(parse_json('```json\n{"facts":[]}\n```'),{'facts':[]})


class PluginTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.plugin=CompanionMemory.__new__(CompanionMemory)
        self.plugin.store=MemoryStore(self.temp.name)
        self.plugin.owner=QQ
        self.plugin.umo=f'qq-companion:FriendMessage:{QQ}'
        self.plugin.config={'enabled':True,'auto_extract':True,'growth_enabled':True}
        self.plugin.processing=asyncio.Lock()
        self.plugin.context=SimpleNamespace(
            get_config=lambda **kwargs:{'provider_settings':{'enable':True}},
            llm_generate=AsyncMock(return_value=SimpleNamespace(completion_text='{"facts":[],"diary":{"write":false}}')),
        )
    async def asyncTearDown(self):
        self.plugin.store.close()
        self.temp.cleanup()

    async def test_runtime_context_is_temporary_and_does_not_rewrite_persona(self):
        event=SimpleNamespace(unified_msg_origin=self.plugin.umo,get_sender_id=lambda:QQ,get_extra=lambda key,default=None:None,message_str='你记得我吗')
        req=ProviderRequest(system_prompt='固定角色')
        await self.plugin.inject(event,req)
        self.assertEqual(req.system_prompt,'固定角色')
        self.assertEqual(len(req.extra_user_content_parts),1)
        self.assertIn('companion_context',req.extra_user_content_parts[0].text)

    async def test_other_person_cannot_read_memory(self):
        event=SimpleNamespace(unified_msg_origin='qq-companion:FriendMessage:other',get_sender_id=lambda:'other')
        req=ProviderRequest()
        await self.plugin.inject(event,req)
        self.assertEqual(req.extra_user_content_parts,[])

    async def test_failed_model_does_not_consume_pending_records(self):
        self.plugin.store.observe(QQ,'user','我不吃香菜')
        self.plugin.context.llm_generate.side_effect=RuntimeError('mock API failure')
        with self.assertRaises(RuntimeError): await self.plugin.reflect(force=True)
        self.assertEqual(self.plugin.store.get_meta('cursor',0),0)
        self.assertEqual(self.plugin.store.entries(QQ),[])

    async def test_valid_reflection_advances_cursor_and_calls_no_qq_send(self):
        ident=self.plugin.store.observe(QQ,'user','今天先测试一下')
        await self.plugin.reflect(force=True)
        self.assertEqual(self.plugin.store.get_meta('cursor'),ident)
        self.plugin.context.llm_generate.assert_awaited_once()

    async def test_greedy_commands_preserve_spaces(self):
        cf=CommandFilter('记住',handler_md=SimpleNamespace(handler=CompanionMemory.remember))
        self.assertEqual(cf.validate_and_convert_params(['我','喜欢','无糖','咖啡'],cf.handler_params)['content'],'我 喜欢 无糖 咖啡')
        cf=CommandFilter('纠正记忆',handler_md=SimpleNamespace(handler=CompanionMemory.correct))
        self.assertEqual(cf.validate_and_convert_params(['abcd1234','现在','喜欢','茶'],cf.handler_params)['content'],'现在 喜欢 茶')


if __name__=='__main__':
    unittest.main(verbosity=2)

