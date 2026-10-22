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
        self.model.return_value='{"write":false}'
        await self.b.tick(self.model,now=self.at('23:30'))
        await self.b.tick(self.model,now=self.at('23:59')+120)
        self.assertEqual(self.model.await_count,2);self.assertFalse(self.b.read('public'))

    async def test_old_entries_preserved(self):
        self.b.write('public','旧随手记','phone:one',[self.source])
        await self.b.tick(self.model,now=self.at('23:30'))
        self.assertEqual(len(self.b.read('public')),2)
        self.assertEqual(self.b.read('public')[0]['text'],'旧随手记')

    async def test_no_invented_source_accepted(self):
        self.model.return_value='{"write":true,"text":"随便编一篇","source_ids":["fake"]}'
        await self.b.tick(self.model,now=self.at('23:30'))
        self.assertFalse(self.b.read('public'));self.assertFalse(self.b.read('private'))

    async def test_next_day_catches_unfinished_yesterday(self):
        await self.b.tick(self.model,now=self.at('23:59')+600)
        self.assertEqual(self.b.read('public')[0]['day'],self.day)

    async def test_empty_day_does_not_call_model(self):
        self.b.daily['sources']={}
        await self.b.tick(self.model,now=self.at('23:30'))
        self.model.assert_not_called()
