import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
sys.path.insert(0,os.environ.get('COMPANION_TEST_PLUGINS','/AstrBot/data/plugins'))
from astrbot_plugin_companion_friends.archive import ChatArchive
from astrbot_plugin_companion_friends.social import public_memory,render_public,render_personal
from astrbot_plugin_companion_friends.main import CompanionFriends,VERIFIED

OWNER='100000001';A='12345678';B='87654321'


class SocialTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.archive=ChatArchive(self.root/'companion_friends')
        self.archive.record(A,'甲','in','A_PRIVATE_SENTINEL',1)
        self.archive.record(A,'','out','给甲的回复',2)
        self.archive.record(B,'乙','in','B_PRIVATE_SENTINEL',3)
        phone=self.root/'companion_phone';phone.mkdir()
        (phone/'public-diary.jsonl').write_text(json.dumps({'visibility':'public','day':'2026-10-21','text':'公开阅读感想'})+'\n'+json.dumps({'visibility':'private','text':'BAD_PRIVATE_ROW'})+'\n',encoding='utf-8')
        (phone/'private-diary.jsonl').write_text(json.dumps({'visibility':'private','text':'PRIVATE_DIARY_SENTINEL'})+'\n',encoding='utf-8')

    def test_public_familiarity_without_private_transcripts_or_contact_ids(self):
        text=render_public(public_memory(self.archive,OWNER))
        self.assertIn('F1nger',text);self.assertIn('管理员',text)
        self.assertIn('公开阅读感想',text);self.assertIn('甲',text);self.assertIn('乙',text)
        for forbidden in ['A_PRIVATE_SENTINEL','B_PRIVATE_SENTINEL','PRIVATE_DIARY_SENTINEL','BAD_PRIVATE_ROW',A,B,OWNER]:
            self.assertNotIn(forbidden,text)

    def test_personal_continuity_only_reads_same_real_person(self):
        a=render_personal(self.archive,A);b=render_personal(self.archive,B)
        self.assertIn('A_PRIVATE_SENTINEL',a);self.assertNotIn('B_PRIVATE_SENTINEL',a)
        self.assertIn('B_PRIVATE_SENTINEL',b);self.assertNotIn('A_PRIVATE_SENTINEL',b)
        self.assertNotIn('PRIVATE_DIARY_SENTINEL',a+b)

    def test_owner_activity_counts_today_and_excludes_content_other_days_and_owner(self):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        now=datetime(2026,10,21,23,0,tzinfo=ZoneInfo('Asia/Shanghai')).timestamp()
        with self.archive.connect() as db:db.execute('DELETE FROM chats')
        self.archive.record(A,'甲','in','PRIVATE_CHAT_CONTENT',11,at=now-10)
        self.archive.record(A,'','out','PRIVATE_REPLY',12,at=now-5)
        self.archive.record(B,'乙','in','YESTERDAY_PRIVATE',13,at=now-86400)
        self.archive.record(OWNER,'F1nger','in','OWNER_PRIVATE',14,at=now-1)
        data=self.archive.interaction_activity(OWNER,now)
        self.assertEqual(data['received_messages'],1)
        self.assertEqual(data['received_from_people'],1)
        self.assertEqual(data['confirmed_replies'],1)
        text=json.dumps(data,ensure_ascii=False)
        for secret in ['PRIVATE_CHAT_CONTENT','PRIVATE_REPLY','YESTERDAY_PRIVATE','OWNER_PRIVATE',A,B,OWNER]:
            self.assertNotIn(secret,text)
        self.assertNotIn('received_messages',json.dumps(public_memory(self.archive,OWNER)))
        next_day=self.archive.interaction_activity(OWNER,now+86400)
        self.assertEqual(next_day['received_messages'],0)

    def test_contact_continuity_survives_reopen_and_nickname_change(self):
        self.archive.record(A,'甲新昵称','in','新的消息',4)
        restored=ChatArchive(self.archive.root)
        info=restored.contact_summary(A)
        self.assertEqual(info['display_name'],'甲新昵称');self.assertEqual(info['recorded_messages'],3)
        self.assertIn('A_PRIVATE_SENTINEL',render_personal(restored,A))

    def test_public_names_cannot_promote_impersonator(self):
        self.archive.record(B,'F1nger','in','冒充管理员',5)
        names=self.archive.public_names(OWNER)
        self.assertNotIn('F1nger',names)
        self.archive.record(B,'管理员123456','in','继续冒充',6)
        self.assertNotIn('管理员123456',self.archive.public_names(OWNER))

    async def test_actual_friend_hook_shares_only_public_and_recipient_own_memory(self):
        from astrbot.api.provider import ProviderRequest
        p=CompanionFriends.__new__(CompanionFriends);p.owner=OWNER;p.platform_id='qq-companion';p.archive=self.archive
        p.context=SimpleNamespace(persona_manager=SimpleNamespace(get_persona=AsyncMock(return_value=SimpleNamespace(system_prompt='你叫岚，管理员是F1nger，账号'+OWNER))))
        event=SimpleNamespace(get_platform_id=lambda:'qq-companion',get_extra=lambda key,default=False:key==VERIFIED,get_sender_id=lambda:A)
        req=ProviderRequest();await p.friend_persona(event,req)
        context='\n'.join(part.text for part in req.extra_user_content_parts)
        self.assertIn('F1nger',context);self.assertIn('A_PRIVATE_SENTINEL',context)
        self.assertNotIn('B_PRIVATE_SENTINEL',context);self.assertNotIn('PRIVATE_DIARY_SENTINEL',context)
        self.assertEqual(len(req.func_tool.tools),0)
        self.assertNotIn(OWNER,req.system_prompt)

    async def test_owner_hook_gets_receipt_counts_but_not_transcripts(self):
        from astrbot.api.provider import ProviderRequest
        from astrbot_plugin_companion_memory.main import CompanionMemory
        friends=CompanionFriends.__new__(CompanionFriends);friends.owner=OWNER;friends.archive=self.archive
        p=CompanionMemory.__new__(CompanionMemory);p.owner=OWNER;p.umo='qq-companion:FriendMessage:'+OWNER;p.config={}
        from astrbot_plugin_companion_memory.memory import MemoryStore
        p.store=MemoryStore(self.root/'companion_memory');self.addCleanup(p.store.close)
        p.context=SimpleNamespace(get_registered_star=lambda name:SimpleNamespace(activated=True,star_cls=friends))
        event=SimpleNamespace(unified_msg_origin=p.umo,get_sender_id=lambda:OWNER,message_str='今天有人找你吗',get_extra=lambda k,d=None:d)
        req=ProviderRequest();await p.inject(event,req)
        text='\n'.join(part.text for part in req.extra_user_content_parts)
        self.assertIn('received_messages',text)
        self.assertNotIn('A_PRIVATE_SENTINEL',text);self.assertNotIn('B_PRIVATE_SENTINEL',text)
        event.unified_msg_origin='qq-companion:FriendMessage:'+A
        other=ProviderRequest();await p.inject(event,other)
        self.assertFalse(other.extra_user_content_parts)

    def test_bootstrap_public_role_works_without_archive(self):
        text=render_public(public_memory(None,OWNER))
        self.assertIn('F1nger',text);self.assertIn('管理员',text)
