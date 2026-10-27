"""AstrBot integration with synthetic events only; run in the installed runtime."""
import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock,patch

sys.path.insert(0,os.environ.get('COMPANION_TEST_PLUGINS',str(Path(__file__).parent)))
from astrbot_plugin_companion_memory.memory import MemoryStore
from astrbot_plugin_companion_memory.main import CompanionMemory
from astrbot_plugin_companion_memory.budget import TOOL_BYTES,TOOL_REPLY_BYTES

OWNER='100000001';A='12345678';B='87654321'


class PagingHooksTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.p=CompanionMemory.__new__(CompanionMemory)
        self.p.owner=OWNER;self.p.umo='qq-companion:FriendMessage:'+OWNER;self.p.config={'enabled':True}
        self.p.store=MemoryStore(self.root/'plugin_data/companion_memory');self.p.processing=asyncio.Lock()
        self.friends=SimpleNamespace(sleeping=lambda:False,policy=SimpleNamespace(is_friend=AsyncMock(return_value=True)))
        self.p.context=SimpleNamespace(get_registered_star=lambda name:SimpleNamespace(activated=True,star_cls=self.friends))
    def tearDown(self):self.p.store.close();self.tmp.cleanup()
    def event(self,qq=A,verified=True):
        extra={'companion_friend_verified':verified}
        return SimpleNamespace(get_sender_id=lambda:qq,unified_msg_origin='qq-companion:FriendMessage:'+qq,
            get_extra=lambda key,default=None:extra.get(key,default),set_extra=lambda key,value:extra.__setitem__(key,value),
            plain_result=lambda text:text,stop_event=lambda:None)

    async def test_follow_up_pages_are_allowed_and_total_budget_is_preserved(self):
        i=self.p.store.observe(A,'user','鲸'*5000+'尾部')
        event=self.event();parts=[];offset=0
        for _ in range(4):
            result=await self.p.tool_read(event,'obs:'+str(i),offset)
            if result.isError:break  # Byte budget may be exhausted before the 4-call limit.
            raw=result.content[0].text;self.assertLessEqual(len(raw.encode()),TOOL_REPLY_BYTES)
            data=json.loads(raw);parts.append(data['text']);offset=data['next_offset']
        self.assertLessEqual(event.get_extra('companion_recall_bytes'),TOOL_BYTES)
        self.assertGreater(len(''.join(parts)),500)
        self.assertTrue((await self.p.tool_read(event,'obs:'+str(i),offset)).isError)

    async def test_identity_sleep_and_revocation_apply_to_every_page(self):
        i=self.p.store.observe(A,'user','private text')
        for event in (self.event(B),self.event(verified=False)):
            self.assertTrue((await self.p.tool_read(event,'obs:'+str(i))).isError)
        self.friends.sleeping=lambda:True
        self.assertTrue((await self.p.tool_read(self.event(),'obs:'+str(i))).isError)
        self.friends.sleeping=lambda:False;self.friends.policy.is_friend.return_value=False
        self.assertTrue((await self.p.tool_read(self.event(),'obs:'+str(i))).isError)

    async def test_duplicate_page_does_not_block_later_page(self):
        i=self.p.store.observe(A,'user','鲸'*2000);event=self.event()
        first=json.loads((await self.p.tool_read(event,'obs:'+str(i))).content[0].text)
        duplicate=json.loads((await self.p.tool_read(event,'obs:'+str(i))).content[0].text)
        self.assertIn('message',duplicate)
        following=json.loads((await self.p.tool_read(event,'obs:'+str(i),first['next_offset'])).content[0].text)
        self.assertEqual(following['offset'],first['next_offset'])

    async def test_full_backup_is_owner_only_and_sleep_gated(self):
        with patch('astrbot_plugin_companion_memory.portable.create_archive') as create:
            self.assertEqual([r async for r in self.p.backup_memory(self.event())],[])
            self.friends.sleeping=lambda:True
            replies=[r async for r in self.p.backup_memory(self.event(OWNER))]
            self.assertIn('睡眠',replies[0]);create.assert_not_called()
            self.friends.sleeping=lambda:False
            create.return_value=self.root/'synthetic.zip'
            with patch('astrbot_plugin_companion_memory.main.get_astrbot_data_path',return_value=str(self.root)):
                replies=[r async for r in self.p.backup_memory(self.event(OWNER))]
            create.assert_called_once();self.assertIn('已校验',replies[0])


if __name__=='__main__':unittest.main()
