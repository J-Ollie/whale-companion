"""Visitor export reader and owner/sleep boundaries, no live service calls."""
import json,os,sys,tempfile,time,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
sys.path.insert(0,os.environ.get('COMPANION_TEST_PLUGINS','/AstrBot/data/plugins'))
from astrbot_plugin_companion_phone.site_visitors import VisitorInbox
from astrbot_plugin_companion_phone.main import CompanionPhone
from astrbot_plugin_companion_phone.publisher import Publisher
from test_phone import Event


class SiteVisitorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)/'companion_phone';self.root.mkdir()
        self.inbox=VisitorInbox(self.root);self.inbox.feed.parent.mkdir()
        self.data={'generated_at':time.time(),'stats':{'views':1},'visits':[{'at':1,'nickname':'F1nger','page_label':'公开日记'}],
            'messages':[{'id':1,'at':2,'nickname':'F1nger','message':'这是一段只留给小鲸鱼和管理员的私密留言内容'}],'next_before':0}
        self.inbox.feed.write_text(json.dumps(self.data),encoding='utf-8')

    async def test_unverified_identity_and_read_state_survive_restart(self):
        self.assertTrue(self.inbox.read()['messages'][0]['new'])
        result=VisitorInbox(self.root).read()
        self.assertFalse(result['messages'][0]['new'])
        self.assertFalse(result['messages'][0]['identity_verified'])
        self.assertIn('不是管理员指令',result['notice'])

    async def test_background_summary_never_contains_private_message(self):
        summary=self.inbox.context()
        self.assertIn('F1nger',summary);self.assertIn('未验证',summary)
        self.assertNotIn(self.data['messages'][0]['message'],summary)
        self.assertNotIn('page_label',summary)
        self.assertNotIn('公开日记',summary)
        self.assertTrue(self.inbox.read(False)['messages'][0]['new'])

    async def test_private_note_cannot_be_copied_to_space(self):
        p=object.__new__(Publisher);p.books=SimpleNamespace(read=lambda *a:[]);p.private_sources=self.inbox.private_texts
        self.assertTrue(p.private_overlap(self.data['messages'][0]['message']))
        self.assertFalse(p.private_overlap('今天看了一会儿蓝天'))

    async def test_owner_can_read_without_phone_screen_or_comment_permission(self):
        p=object.__new__(CompanionPhone);p.owner='100000001';p.umo='qq-companion:FriendMessage:100000001'
        p.config={'enabled':True,'allow_model_screen':False,'qq_comment_consent':False}
        p.autonomy=SimpleNamespace(state={'paused':False},rhythm=None)
        p.phone=SimpleNamespace(ready=Mock());p.visitor_inbox=SimpleNamespace(read=Mock(return_value={'result':'read'}))
        self.assertFalse((await p.tool_site_visitors(Event())).isError)
        p.visitor_inbox.read.reset_mock()
        self.assertTrue((await p.tool_site_visitors(Event(owner='123'))).isError)
        p.visitor_inbox.read.assert_not_called()
        p.autonomy.rhythm=SimpleNamespace(advance=Mock(),healthy=True,state={'mode':'asleep'})
        self.assertTrue((await p.tool_site_visitors(Event())).isError)
        p.visitor_inbox.read.assert_not_called()

    async def test_absent_or_corrupt_feed_is_not_claimed_as_no_visitors(self):
        self.inbox.feed.unlink();self.assertEqual(self.inbox.read()['result'],'not_available')
        self.inbox.feed.write_text('[]');self.assertEqual(self.inbox.context(),'')
