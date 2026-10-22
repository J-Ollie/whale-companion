"""Sleep receipt buffering and wake replay; no real messages, models, or phone actions."""
import json,os,sys,tempfile,time,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock,Mock
sys.path.insert(0,os.environ.get('COMPANION_TEST_PLUGINS','/AstrBot/data/plugins'))
from astrbot_plugin_companion_friends.sleep_inbox import SleepInbox
from astrbot_plugin_companion_friends.main import CompanionFriends,VERIFIED
from astrbot.api.message_components import Plain

OWNER='100000001';FRIEND='12345678';BOT='100000002'

class SleepTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)/'companion_friends';self.root.mkdir()
        self.p=CompanionFriends.__new__(CompanionFriends)
        p=self.p;p.owner=OWNER;p.bot_qq=BOT;p.platform_id='qq-companion'
        p.sleep_inbox=SleepInbox(self.root);p.policy=SimpleNamespace(is_friend=AsyncMock(return_value=True),command=AsyncMock(return_value=None))
        p.save_chat=Mock();p.notify=AsyncMock()
        self.rhythm=SimpleNamespace(healthy=True,state={'mode':'asleep'},advance=Mock())
        self.events=[]
        def event(message):
            extras={}
            e=SimpleNamespace(message_obj=message,message_str=message.message_str,set_extra=lambda k,v:extras.__setitem__(k,v),get_extra=lambda k,d=None:extras.get(k,d),get_platform_id=lambda:'qq-companion')
            self.events.append(e);return e
        async def convert(raw):
            text=''.join(part['data']['text'] for part in raw.get('message',[]))
            return SimpleNamespace(message_str=text,message=[Plain(text)],raw_message=raw)
        self.adapter=SimpleNamespace(convert_message=AsyncMock(side_effect=convert),create_event=event,commit_event=Mock())
        p.context=SimpleNamespace(get_registered_star=lambda name:SimpleNamespace(activated=True,star_cls=SimpleNamespace(autonomy=SimpleNamespace(rhythm=self.rhythm))),get_platform_inst=lambda name:self.adapter)

    def message(self,sender,text,identifier):
        raw={'self_id':int(BOT),'post_type':'message','message_type':'private','user_id':int(sender),'message_id':identifier,
             'message':[{'type':'text','data':{'text':text}}],'sender':{'nickname':'测试者'},'time':int(time.time())}
        return SimpleNamespace(raw_message=raw,message_str=text)

    async def test_sleep_and_rest_hold_owner_friend_and_commands_without_processing(self):
        for mode in ['asleep','resting']:
            self.rhythm.state['mode']=mode
            for sender,text,i in [(OWNER,'用Via看网页',1),(FRIEND,'你好',2),(OWNER,'/手机状态',3)]:
                self.assertTrue(await self.p.intercept(self.adapter,self.message(sender,text,i)))
            await self.p.release_unread()
        self.adapter.commit_event.assert_not_called();self.adapter.convert_message.assert_not_called()
        self.p.save_chat.assert_not_called();self.p.notify.assert_not_called();self.p.policy.command.assert_not_called()
        self.assertEqual(sum(map(len,self.p.sleep_inbox.pending().values())),3)

    async def test_wake_merges_each_sender_and_rechecks_friendship_once(self):
        for sender,text,i in [(OWNER,'第一句',1),(OWNER,'第二句',2),(FRIEND,'好友私话',3)]:
            await self.p.intercept(self.adapter,self.message(sender,text,i))
        restored=SleepInbox(self.root)
        self.assertEqual(len(restored.pending()[OWNER]),2)
        self.rhythm.state['mode']='awake';await self.p.release_unread()
        self.assertEqual(self.adapter.commit_event.call_count,2)
        a,b=self.events
        self.assertEqual(a.message_str,'第一句\n第二句')
        self.assertNotIn('好友私话',a.message_str)
        self.assertTrue(b.get_extra(VERIFIED));self.assertFalse(a.get_extra(VERIFIED,False))
        self.assertEqual(a.get_extra('companion_sleep_message_count'),2)
        self.assertFalse(restored.pending())
        await self.p.release_unread();self.assertEqual(self.adapter.commit_event.call_count,2)

    async def test_removed_friend_is_not_replayed(self):
        await self.p.intercept(self.adapter,self.message(FRIEND,'你好',1))
        self.rhythm.state['mode']='awake';self.p.policy.is_friend.return_value=False
        await self.p.release_unread();self.adapter.commit_event.assert_not_called()
        self.assertFalse(self.p.sleep_inbox.pending())

    async def test_friend_command_stays_blocked_after_wake(self):
        await self.p.intercept(self.adapter,self.message(FRIEND,'/手机状态',1))
        self.rhythm.state['mode']='awake';await self.p.release_unread()
        self.adapter.commit_event.assert_not_called()
        self.assertFalse(self.p.sleep_inbox.pending())

    async def test_owner_approval_keeps_original_command_route_after_wake(self):
        await self.p.intercept(self.adapter,self.message(OWNER,'/同意好友 ABCDEF',1))
        self.p.policy.command.return_value='已处理申请'
        self.rhythm.state['mode']='awake';await self.p.release_unread()
        self.p.notify.assert_awaited_once_with('已处理申请')
        self.adapter.commit_event.assert_not_called()
        self.assertFalse(self.p.sleep_inbox.pending())

    async def test_enqueue_failure_keeps_pending(self):
        await self.p.intercept(self.adapter,self.message(OWNER,'你好',1))
        self.rhythm.state['mode']='awake';self.adapter.commit_event.side_effect=RuntimeError('queue unavailable')
        with self.assertRaises(RuntimeError):await self.p.release_unread()
        self.assertEqual(len(self.p.sleep_inbox.pending()[OWNER]),1)

    async def test_sleeping_before_reply_suppresses_and_requeues(self):
        message=self.message(OWNER,'你好',1)
        extras={}
        event=SimpleNamespace(message_obj=message,get_platform_id=lambda:'qq-companion',get_extra=lambda k,d=None:extras.get(k,d),clear_result=Mock(),stop_event=Mock())
        await self.p.recheck_before_send(event)
        event.clear_result.assert_called_once();event.stop_event.assert_called_once()
        self.assertTrue(self.p.sleep_inbox.has_pending(OWNER))

    def test_phone_reload_uses_persisted_sleep_state(self):
        folder=self.root.parent/'companion_phone';folder.mkdir()
        (folder/'rhythm.json').write_text(json.dumps({'mode':'asleep','until':time.time()+3600}))
        self.p.context.get_registered_star=lambda name:None
        self.assertTrue(self.p.sleeping())
