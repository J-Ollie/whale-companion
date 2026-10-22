import base64
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock,Mock,patch
sys.path.insert(0,os.environ.get('COMPANION_TEST_PLUGINS','/AstrBot/data/plugins'))
from astrbot_plugin_companion_friends.archive import ChatArchive
from astrbot_plugin_companion_friends.main import CompanionFriends


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.a=ChatArchive(Path(self.tmp.name))

    def test_dedup_and_actual_directions(self):
        first=self.a.record('12345678','好友','in',[{'type':'text','data':{'text':'你好'}}],11)
        self.assertIsNone(self.a.record('12345678','好友','in','重复',11))
        self.a.record('12345678','','out','在',12)
        with self.a.connect() as db:
            rows=db.execute('SELECT direction,segments FROM chats ORDER BY id').fetchall()
        self.assertEqual([r[0] for r in rows],['in','out'])
        self.assertEqual(json.loads(rows[0][1])[0]['text'],'你好')

    def test_safe_local_image_cache_and_remove_source(self):
        data=b'GIF89a'+b'0'*32
        i=self.a.record('12345678','','out',[{'type':'image','data':{'file':'base64://'+base64.b64encode(data).decode()}}],22)
        self.a.cache_images(i)
        with self.a.connect() as db:parts=json.loads(db.execute('SELECT segments FROM chats').fetchone()[0])
        self.assertNotIn('source',parts[0]);self.assertTrue((self.a.media/parts[0]['media']).exists())

    def test_context_query_never_crosses_people_or_exports_image_sources(self):
        self.a.record('12345678','甲','in','甲的消息',31)
        self.a.record('87654321','乙','out','OTHER_PERSON_PRIVATE_SENTINEL',32)
        self.a.record('12345678','','out',[{'type':'image','data':{'file':'SECRET_MEDIA_SOURCE'}}],33)
        rows=self.a.context_messages('12345678')
        self.assertEqual(len(rows),2)
        self.assertNotIn('OTHER_PERSON_PRIVATE_SENTINEL',json.dumps(rows))
        self.assertNotIn('SECRET_MEDIA_SOURCE',json.dumps(rows))
        self.assertEqual(self.a.context_messages('bad-id'),[])

    def test_external_host_and_filesystem_outside_data_rejected(self):
        for source in ('http://127.0.0.1/x','https://example.com/x','https://qpic.cn.evil.com/x','file:///etc/passwd','/AstrBot/data/../../etc/passwd'):
            with self.assertRaises((ValueError,OSError)):self.a.image_bytes(source)


class OutgoingTests(unittest.IsolatedAsyncioTestCase):
    async def test_capture_only_after_send_receipt_and_restore(self):
        from aiocqhttp import CQHttp
        from types import SimpleNamespace
        bot=Mock()
        adapter=SimpleNamespace(get_client=lambda:bot)
        p=CompanionFriends.__new__(CompanionFriends)
        p.owner='100000001';p.platform_id='qq-companion';p.bot_qq='100000002'
        p.context=SimpleNamespace(get_platform_inst=lambda _:adapter)
        p.save_chat=Mock();p.policy=SimpleNamespace(retry_notifications=AsyncMock())
        original=AsyncMock(return_value={'message_id':55})
        with patch.object(CQHttp,'call_action',original):
            await p.initialize()
            try:
                await CQHttp.call_action(bot,'send_private_msg',user_id=12345678,message='回复')
                p.save_chat.assert_called_once_with('12345678','','out','回复',55)
                p.save_chat.reset_mock()
                await CQHttp.call_action(bot,'send_private_msg',user_id=100000001,message='管理员消息')
                p.save_chat.assert_not_called()
                original.side_effect=TimeoutError()
                with self.assertRaises(TimeoutError):await CQHttp.call_action(bot,'send_private_msg',user_id=12345678,message='未确认')
                p.save_chat.assert_not_called()
            finally:await p.terminate()
            self.assertIs(CQHttp.call_action,original)
