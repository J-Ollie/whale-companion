import json
import os
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo
sys.path.insert(0,os.environ.get('COMPANION_TEST_PLUGINS','/AstrBot/data/plugins'))
from astrbot_plugin_companion_phone.books import Books

TZ=ZoneInfo('Asia/Shanghai')


class DailyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.b=Books(self.root)
        self.day=datetime.now(TZ).date().isoformat()
        self.at=lambda hm:datetime.fromisoformat(self.day+'T'+hm).replace(tzinfo=TZ).timestamp()
        self.source={'id':'phone:one','app':'酷安','views':2,'public_content':True,'timestamp':self.at('15:00'),'note':'看到一个新思路'}
        self.b.collect([self.source])
        self.model=AsyncMock(return_value=json.dumps({'write':True,'text':'这个思路在脑子里绕了一天','source_ids':['phone:one']}))

    async def test_daytime_no_model_or_diary(self):
        await self.b.tick(self.model,now=self.at('20:00'))
        self.model.assert_not_called();self.assertFalse(self.b.read('public'))

    async def test_nightly_and_restart_no_duplicate(self):
        await self.b.tick(self.model,now=self.at('23:30'))
        self.assertEqual(len(self.b.read('public')),1);self.assertEqual(len(self.b.read('private')),1)
        b=Books(self.root)
        await b.tick(self.model,now=self.at('23:59'))
        self.assertEqual(self.model.await_count,2)

    async def test_new_material_updates_same_daily_entry(self):
        await self.b.tick(self.model,now=self.at('23:30'))
        original=self.b.read('public')[0]
        self.b.collect([{**self.source,'id':'phone:two','timestamp':self.at('23:40'),'note':'后来又看到新解释'}])
        self.model.return_value=json.dumps({'write':True,'text':'两个想法连起来了','source_ids':['phone:one','phone:two']})
        await self.b.tick(self.model,now=self.at('23:59')+120)
        rows=self.b.read('public');self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['id'],original['id']);self.assertEqual(rows[0]['at'],original['at'])
        self.assertEqual(rows[0]['text'],'两个想法连起来了')

    async def test_private_sources_never_in_public_request(self):
        self.b.collect([{**self.source,'id':'chat:one','app':'私聊','public_content':False,'note':'PRIVATE_SENTINEL'}])
        await self.b.tick(self.model,now=self.at('23:30'))
        self.assertNotIn('PRIVATE_SENTINEL',self.model.await_args_list[0].args[0])
        self.assertIn('PRIVATE_SENTINEL',self.model.await_args_list[1].args[0])

    async def test_no_forced_content_when_declined(self):
        self.model.return_value='{"write":false,"reason":"今天想把念头先留着，暂时不写成一篇"}'
        await self.b.tick(self.model,now=self.at('23:30'))
        await self.b.tick(self.model,now=self.at('23:59')+120)
        self.assertEqual(self.model.await_count,2)
        row=self.b.read('public')[0];self.assertEqual(row['kind'],'no_entry')
        self.assertEqual(row['reason_status'],'declined');self.assertIn('暂时不写',row['text'])

    async def test_old_entries_preserved(self):
        self.b.write('public','旧随手记','phone:one',[self.source])
        await self.b.tick(self.model,now=self.at('23:30'))
        self.assertEqual(len(self.b.read('public')),2)
        self.assertEqual(self.b.read('public')[0]['text'],'旧随手记')

    async def test_no_invented_source_accepted(self):
        self.model.return_value='{"write":true,"text":"随便编一篇","source_ids":["fake"]}'
        await self.b.tick(self.model,now=self.at('23:30'))
        self.assertEqual(self.b.read('public')[0]['reason_status'],'retry_pending')
        self.assertNotIn('public:'+self.day,self.b.daily['processed'])

    async def test_next_day_catches_unfinished_yesterday(self):
        await self.b.tick(self.model,now=self.at('23:59')+600)
        self.assertEqual(self.b.read('public')[0]['day'],self.day)

    async def test_empty_day_can_write_own_reflection_without_source_ids(self):
        self.b.daily['sources']={}
        self.model.return_value='{"write":true,"text":"今天没有特别想展开的事，留几句话给慢慢过去的一天。","source_ids":[]}'
        await self.b.tick(self.model,now=self.at('23:30'))
        self.assertEqual(self.model.await_count,2)
        self.assertEqual(self.b.read('public')[0]['kind'],'daily')
        self.assertEqual(self.b.read('private')[0]['source_ids'],[])
        await self.b.tick(self.model,now=self.at('23:59')+120)
        self.assertEqual(self.model.await_count,2)

    async def test_public_reflection_without_sending_private_material(self):
        self.b.daily['sources']={}
        self.b.collect([{**self.source,'app':'QQ','public_content':False,'note':'PRIVATE_SENTINEL'}])
        await self.b.tick(self.model,now=self.at('23:30'))
        self.model.return_value='{"write":true,"text":"今天也留一点安静给自己。"}'
        # The first attempt used a real source ID whose text remains private.
        self.assertEqual(self.model.await_count,2)
        entry=self.b.read('public')[0]
        self.assertEqual(entry['kind'],'no_entry')  # Private ID cannot be cited publicly.
        self.assertNotIn('PRIVATE_SENTINEL',json.dumps(entry,ensure_ascii=False))
        self.assertNotIn('PRIVATE_SENTINEL',self.model.await_args_list[0].args[0])
        self.assertIn('PRIVATE_SENTINEL',self.model.await_args_list[1].args[0])
        await self.b.tick(self.model,now=self.at('23:59')+120)
        self.assertEqual(self.b.read('public')[0]['kind'],'daily')

    async def test_skip_replaced_by_diary_when_new_public_evidence_arrives(self):
        self.model.return_value='{"write":false,"reason":"暂时没想好"}'
        await self.b.tick(self.model,now=self.at('23:30'))
        self.b.collect([{**self.source,'id':'phone:two'}])
        self.model.return_value='{"write":true,"text":"后来想清楚一点了","source_ids":["phone:two"]}'
        await self.b.tick(self.model,now=self.at('23:59')+120)
        entries=self.b.read('public');self.assertEqual(len(entries),1)
        self.assertEqual(entries[0]['kind'],'daily')

    async def test_legacy_decline_does_not_invent_a_reason(self):
        self.model.return_value='{"write":false}'
        await self.b.tick(self.model,now=self.at('23:30'))
        self.b.daily['decisions']={}
        (self.root/'public-diary.jsonl').unlink()
        await self.b.tick(self.model,now=self.at('23:59')+120)
        entry=self.b.read('public')[0]
        self.assertEqual(entry['reason_status'],'historical_unknown')
        self.assertIn('无法还原',entry['text'])

    async def test_generation_failure_is_retryable_not_a_choice(self):
        self.model.side_effect=TimeoutError()
        await self.b.tick(self.model,now=self.at('23:30'))
        self.assertEqual(self.b.read('public')[0]['reason_status'],'retry_pending')
        self.assertNotIn('public:'+self.day,self.b.daily['processed'])
        self.model.side_effect=None
        await self.b.tick(self.model,now=self.at('23:59')+120)
        self.assertEqual(self.b.read('public')[0]['kind'],'daily')

    async def test_operator_backfill_is_labeled_and_does_not_call_model_or_change_private(self):
        self.b.daily['sources']={}
        now=self.at('23:59')+600
        result=await self.b.explain_missing_public(self.day,now=now)
        self.assertEqual(result['result'],'recorded')
        entry=self.b.read('public')[0];self.assertTrue(entry['retrospective'])
        self.assertIn('无法还原',entry['text']);self.assertEqual(self.b.read('private'),[])
        self.assertEqual((await self.b.explain_missing_public('../bad',now=now))['result'],'invalid_day')

    async def test_operator_does_not_replace_existing_diary_or_invent_lack_of_evidence(self):
        now=self.at('23:59')+600
        self.assertEqual((await self.b.explain_missing_public(self.day,now=now))['result'],'public_evidence_exists')
        self.b.write_daily('public',self.day,{'text':'原来的日记','source_ids':['phone:one']},[self.source],now)
        self.assertEqual((await self.b.explain_missing_public(self.day,now=now))['result'],'already_written')
        self.assertEqual(self.b.read('public')[0]['text'],'原来的日记')

    async def test_yesterday_missing_note_replaced_by_labeled_reflection_only(self):
        now=self.at('23:59')+600
        self.b.daily['sources']={}
        await self.b.explain_missing_public(self.day,now=now)
        self.b.write_daily('private',self.day,{'text':'只想留给自己的小小犹豫。'},[],now)
        private=(self.root/'private-diary.jsonl').read_bytes()
        self.model.return_value='{"write":true,"text":"平淡的一天，也想给它留个小记号。"}'
        result=await self.b.rewrite_missing_public(self.model,self.day,now=now)
        self.assertEqual(result['result'],'written');self.assertTrue(result['retrospective'])
        self.assertEqual(len(self.b.read('public')),1)
        self.assertEqual((self.root/'private-diary.jsonl').read_bytes(),private)
        self.assertNotIn('只想留给',self.model.await_args.args[0])
        self.assertEqual((await self.b.rewrite_missing_public(self.model,self.day,now=now))['result'],'already_written')
        self.assertEqual((await self.b.rewrite_missing_public(self.model,'../bad',now=now))['result'],'invalid_day')

    async def test_policy_migration_preserves_finished_diaries(self):
        self.b.write_daily('public',self.day,{'text':'原来的日记'},[],self.at('23:31'))
        self.b.write_daily('private',self.day,{'text':'不公开的小念头'},[],self.at('23:31'))
        before={v:(self.root/(v+'-diary.jsonl')).read_bytes() for v in ('public','private')}
        await self.b.tick(self.model,now=self.at('23:59')+600)
        self.model.assert_not_called()
        for visibility in before:self.assertEqual((self.root/(visibility+'-diary.jsonl')).read_bytes(),before[visibility])

    async def test_public_day_outline_never_copies_unknown_app_or_private_diary(self):
        self.b.collect([{**self.source,'id':'chat:secret','app':'私聊','note':'PRIVATE_SENTINEL','public_content':False},
                        {**self.source,'id':'bad:app','app':'PRIVATE_APP_SENTINEL','note':'PRIVATE_PAGE','public_content':False}])
        (self.root/'private-diary.jsonl').write_text(json.dumps({'text':'PRIVATE_DIARY','day':'2000-01-01','visibility':'private'})+'\n',encoding='utf-8')
        await self.b.tick(self.model,now=self.at('23:30'))
        public=self.model.await_args_list[0].args[0]
        for secret in ('PRIVATE_SENTINEL','PRIVATE_APP_SENTINEL','PRIVATE_PAGE','PRIVATE_DIARY'):self.assertNotIn(secret,public)
        private=self.model.await_args_list[1].args[0]
        self.assertIn('PRIVATE_SENTINEL',private)
        self.assertIn('私密空间',private);self.assertIn('没有素材编号也能写',public)
