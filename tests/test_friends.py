import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock,Mock,patch
sys.path.insert(0,os.environ.get('COMPANION_TEST_PLUGINS','/AstrBot/data/plugins'))
from astrbot_plugin_companion_friends.policy import FriendPolicy
from astrbot_plugin_companion_friends.main import CompanionFriends,VERIFIED

OWNER='100000001'
FRIEND='12345678'
BOT='100000002'


class FriendTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.call=AsyncMock(return_value=[{'user_id':int(OWNER)},{'user_id':int(FRIEND)}])
        self.notify=AsyncMock(return_value=True)
        self.policy=FriendPolicy(self.root,OWNER,self.call,self.notify)
        self.raw={'user_id':int(FRIEND),'flag':'opaque-request-flag','comment':'你好'}

    async def test_new_request_notifies_owner_without_accepting(self):
        await self.policy.receive(self.raw)
        self.call.assert_not_called()
        self.notify.assert_awaited_once()
        self.assertIn('/同意好友 ',self.notify.await_args.args[0])
        self.assertNotIn('opaque-request-flag',self.notify.await_args.args[0])
        self.assertEqual(self.policy.rows[0]['status'],'pending')

    async def test_unresolved_request_notifies_and_remains_pending(self):
        raw={**self.raw,'user_id':0,'flag':'u_unresolved_request'}
        await self.policy.receive(raw)
        self.assertEqual(len(self.policy.rows),1)
        row=self.policy.rows[0]
        self.assertEqual(row['qq'],'')
        self.assertTrue(row['identity_unresolved'])
        self.assertEqual(row['status'],'pending')
        self.assertTrue(row['notified'])
        self.assertIn('账号待解析',self.notify.await_args.args[0])
        self.assertNotIn(raw['flag'],self.notify.await_args.args[0])
        self.assertNotIn('/同意好友 ',self.notify.await_args.args[0])
        self.assertFalse(any(c.args[0]=='set_friend_add_request' for c in self.call.await_args_list))

    async def test_unresolved_requests_do_not_merge_different_handles(self):
        for flag in ['u_first_request','u_second_request','u_first_request']:
            await self.policy.receive({**self.raw,'user_id':0,'flag':flag})
        self.assertEqual(len(self.policy.rows),2)
        self.assertEqual(self.notify.await_count,2)
        restored=FriendPolicy(self.root,OWNER,self.call,self.notify)
        self.assertEqual(len(restored.rows),2)
        self.assertIn('账号待解析',await restored.command(OWNER,'/好友申请'))

    async def test_unresolved_approval_requires_owner_and_uses_exact_handle(self):
        with patch('astrbot_plugin_companion_friends.policy.resolve_uid',new_callable=AsyncMock,
                   return_value={'qq':FRIEND,'nickname':'测试资料昵称','source':'qq_uid_profile'}):
            await self.policy.receive({**self.raw,'user_id':0,'flag':'u_exact_handle'})
        command='/同意好友 '+self.policy.rows[0]['id']
        self.assertIsNone(await self.policy.command(FRIEND,command))
        self.call.assert_not_called()
        await self.policy.command(OWNER,command)
        self.call.assert_any_await('set_friend_add_request',flag='u_exact_handle',approve=True)
        self.assertEqual(self.policy.rows[0]['status'],'approved')

    async def test_unknown_identity_blocks_owner_approval_and_auto_retry_is_bounded(self):
        with patch('astrbot_plugin_companion_friends.policy.resolve_uid',new_callable=AsyncMock,side_effect=ValueError('profile_lookup_rejected')) as resolver:
            await self.policy.receive({**self.raw,'user_id':0,'flag':'u_unknown_request'})
            row=self.policy.rows[0]
            self.assertIn('暂不能同意',await self.policy.command(OWNER,'/同意好友 '+row['id']))
            self.assertFalse(any(c.args[0]=='set_friend_add_request' for c in self.call.await_args_list))
            await self.policy.retry_notifications()
            self.assertEqual(resolver.await_count,1)
            for _ in range(5):
                row['identity_last_attempt']=0
                await self.policy.retry_notifications()
            self.assertEqual(resolver.await_count,3)
            self.assertEqual(row['status'],'pending')

    async def test_successful_retry_updates_notification_without_adding_friend(self):
        with patch('astrbot_plugin_companion_friends.policy.resolve_uid',new_callable=AsyncMock,
                   side_effect=[ValueError('profile_lookup_rejected'),{'qq':FRIEND,'nickname':'QQ昵称','source':'qq_uid_profile'}]):
            await self.policy.receive({**self.raw,'user_id':0,'flag':'u_retry_request'})
            self.policy.rows[0]['identity_last_attempt']=0
            await self.policy.retry_notifications()
        self.assertEqual(self.notify.await_count,2)
        self.assertIn(FRIEND,self.notify.await_args.args[0])
        self.assertIn('/同意好友 ',self.notify.await_args.args[0])
        self.assertEqual(self.policy.rows[0]['status'],'pending')
        self.call.assert_not_called()

    async def test_manual_identity_lookup_requires_owner_and_separate_approval(self):
        with patch('astrbot_plugin_companion_friends.policy.resolve_uid',new_callable=AsyncMock,
                   side_effect=[ValueError('profile_lookup_rejected'),{'qq':FRIEND,'nickname':'QQ昵称','source':'qq_uid_profile'}]) as resolver:
            await self.policy.receive({**self.raw,'user_id':0,'flag':'u_manual_request'})
            row=self.policy.rows[0];command='/核对好友 '+row['id']
            self.assertIsNone(await self.policy.command(FRIEND,command))
            self.assertIn('60秒',await self.policy.command(OWNER,command))
            self.assertEqual(resolver.await_count,1)
            row['identity_last_attempt']=0;row['identity_attempts']=3
            self.assertIn(FRIEND,await self.policy.command(OWNER,command))
            self.assertEqual(resolver.await_count,2)
            self.assertEqual(row['status'],'pending')
            self.call.assert_not_called()

    async def test_restart_preserves_identity_retry_budget(self):
        with patch('astrbot_plugin_companion_friends.policy.resolve_uid',new_callable=AsyncMock,side_effect=TimeoutError()) as resolver:
            await self.policy.receive({**self.raw,'user_id':0,'flag':'u_restart_request'})
            self.policy.rows[0]['identity_attempts']=3;self.policy.save()
            restored=FriendPolicy(self.root,OWNER,self.call,self.notify)
            await restored.retry_notifications()
            self.assertEqual(resolver.await_count,1)
            self.assertTrue(restored.rows[0]['identity_unresolved'])
            self.assertEqual(restored.rows[0]['status'],'pending')

    async def test_unresolved_without_handle_and_invalid_accounts_are_rejected(self):
        for raw in [dict(self.raw,user_id=0,flag=''),dict(self.raw,user_id=0,flag='bad\nflag'),dict(self.raw,user_id='admin')]:
            await self.policy.receive(raw)
        self.assertEqual(self.policy.rows,[])
        self.notify.assert_not_called()

    async def test_duplicate_and_renewed_request_notify_once(self):
        await self.policy.receive(self.raw)
        await self.policy.receive(self.raw)
        await self.policy.receive({**self.raw,'flag':'renewed-flag'})
        self.assertEqual(len(self.policy.rows),1)
        self.assertEqual(self.policy.rows[0]['flag'],'renewed-flag')
        self.notify.assert_awaited_once()

    async def test_non_owner_cannot_approve(self):
        await self.policy.receive(self.raw)
        result=await self.policy.command(FRIEND,'/同意好友 '+self.policy.rows[0]['id'])
        self.assertIsNone(result);self.call.assert_not_called()

    async def test_owner_approval_and_replay_no_second_accept(self):
        await self.policy.receive(self.raw);identifier=self.policy.rows[0]['id']
        await self.policy.command(OWNER,'/同意好友 '+identifier)
        self.call.assert_any_await('set_friend_add_request',flag=self.raw['flag'],approve=True)
        count=self.call.await_count
        await self.policy.command(OWNER,'/同意好友 '+identifier)
        self.assertEqual(self.call.await_count,count)

    async def test_owner_rejection_is_explicit_false(self):
        await self.policy.receive(self.raw)
        await self.policy.command(OWNER,'/拒绝好友 '+self.policy.rows[0]['id'])
        self.call.assert_awaited_once_with('set_friend_add_request',flag=self.raw['flag'],approve=False)

    async def test_expired_request_not_submitted(self):
        await self.policy.receive(self.raw);self.policy.rows[0]['at']=0
        self.assertIn('24小时',await self.policy.command(OWNER,'/同意好友 '+self.policy.rows[0]['id']))
        self.call.assert_not_called()

    async def test_restart_preserves_pending_without_accepting(self):
        await self.policy.receive(self.raw)
        other=FriendPolicy(self.root,OWNER,self.call,self.notify)
        self.assertEqual(other.rows[0]['status'],'pending');self.call.assert_not_called()

    async def test_uncertain_result_does_not_retry(self):
        await self.policy.receive(self.raw);self.call.side_effect=TimeoutError()
        command='/同意好友 '+self.policy.rows[0]['id']
        await self.policy.command(OWNER,command)
        self.assertEqual(self.policy.rows[0]['status'],'uncertain')
        await self.policy.command(OWNER,command)
        self.call.assert_awaited_once()

    async def test_failed_notification_is_retried_not_approval(self):
        self.notify.side_effect=RuntimeError()
        await self.policy.receive(self.raw)
        self.assertFalse(self.policy.rows[0]['notified'])
        self.notify.side_effect=None
        await self.policy.retry_notifications()
        self.assertTrue(self.policy.rows[0]['notified']);self.call.assert_not_called()

    async def test_approval_notification_separate_from_proactive_quota(self):
        p=self.plugin()
        active=SimpleNamespace(state={'daily_count':4,'paused':True,'unanswered':1})
        lookup=Mock(return_value=SimpleNamespace(star_cls=active))
        p.context=SimpleNamespace(send_message=AsyncMock(return_value=True),get_registered_star=lookup)
        result=await CompanionFriends.notify(p,'收到好友申请')
        self.assertTrue(result)
        p.context.send_message.assert_awaited_once()
        self.assertEqual(p.context.send_message.await_args.args[0],p.umo)
        self.assertEqual(active.state,{'daily_count':4,'paused':True,'unanswered':1})
        lookup.assert_called_once_with('astrbot_plugin_companion_phone')

    async def test_friend_removal_and_lookup_failure_fail_closed(self):
        self.assertTrue(await self.policy.is_friend(FRIEND))
        self.call.return_value=[]
        self.assertFalse(await self.policy.is_friend(FRIEND))
        self.call.side_effect=TimeoutError()
        self.assertFalse(await self.policy.is_friend(FRIEND))

    async def test_processing_after_crash_becomes_uncertain(self):
        await self.policy.receive(self.raw);self.policy.rows[0]['status']='processing';self.policy.save()
        other=FriendPolicy(self.root,OWNER,self.call,self.notify)
        self.assertEqual(other.rows[0]['status'],'uncertain');self.call.assert_not_called()

    def plugin(self):
        p=CompanionFriends.__new__(CompanionFriends)
        p.owner=OWNER;p.bot_qq=BOT;p.platform_id='qq-companion';p.umo='qq-companion:FriendMessage:'+OWNER
        p.policy=self.policy;p.notify=self.notify
        return p

    def message(self,sender=FRIEND,text='你好',**raw):
        return SimpleNamespace(raw_message={'self_id':int(BOT),'post_type':'message','message_type':'private','user_id':int(sender),**raw},message_str=text)

    async def test_nonfriend_blocked_before_queue(self):
        self.call.return_value=[]
        adapter=SimpleNamespace(commit_event=Mock())
        self.assertTrue(await self.plugin().intercept(adapter,self.message()))
        adapter.commit_event.assert_not_called()

    async def test_owner_normal_chat_and_group_route_unchanged(self):
        adapter=SimpleNamespace(create_event=Mock(),commit_event=Mock())
        self.assertFalse(await self.plugin().intercept(adapter,self.message(sender=OWNER)))
        self.assertFalse(await self.plugin().intercept(adapter,self.message(message_type='group',group_id=123)))
        adapter.create_event.assert_not_called()
        adapter.commit_event.assert_not_called()

    async def test_owner_persona_and_tools_untouched(self):
        from astrbot.api.provider import ProviderRequest
        p=self.plugin();p.context=SimpleNamespace()
        event=SimpleNamespace(get_platform_id=lambda:'qq-companion',get_extra=lambda k,d=None:False)
        tools=object();req=ProviderRequest(system_prompt='owner persona',func_tool=tools)
        await p.friend_persona(event,req)
        await p.isolate_tools(event,req)
        self.assertEqual(req.system_prompt,'owner persona')
        self.assertIs(req.func_tool,tools)

    async def test_friend_management_commands_blocked(self):
        adapter=SimpleNamespace(commit_event=Mock())
        self.assertTrue(await self.plugin().intercept(adapter,self.message(text='/手机截图')))
        adapter.commit_event.assert_not_called()

    async def test_verified_friend_whitelist_exception_and_restore(self):
        from astrbot.core.pipeline.whitelist_check.stage import WhitelistCheckStage
        p=self.plugin();extras={}
        event=SimpleNamespace(set_extra=lambda k,v:extras.update({k:v}),get_extra=lambda k,d=None:extras.get(k,d),
            get_platform_id=lambda:'qq-companion',get_group_id=lambda:'',get_platform_name=lambda:'aiocqhttp',
            unified_msg_origin='qq-companion:FriendMessage:'+FRIEND,stop_event=Mock())
        adapter=SimpleNamespace(create_event=Mock(return_value=event),commit_event=Mock())
        p.context=SimpleNamespace(get_platform_inst=lambda _:adapter)
        stage=WhitelistCheckStage();stage.enable_whitelist_check=True;stage.whitelist=[p.umo]
        stage.wl_ignore_admin_on_group=False;stage.wl_ignore_admin_on_friend=False;stage.wl_log=False
        await p.initialize()
        try:
            self.assertTrue(await p.intercept(adapter,self.message()))
            adapter.commit_event.assert_called_once_with(event)
            self.assertTrue(extras[VERIFIED])
            await stage.process(event)
            event.stop_event.assert_not_called()
        finally:await p.terminate()
        await stage.process(event)
        event.stop_event.assert_called_once()

    async def test_friend_persona_and_empty_tools(self):
        from astrbot.api.provider import ProviderRequest
        p=self.plugin()
        p.context=SimpleNamespace(persona_manager=SimpleNamespace(get_persona=AsyncMock(return_value=SimpleNamespace(system_prompt='你是小鲸鱼'))))
        event=SimpleNamespace(get_platform_id=lambda:'qq-companion',get_extra=lambda k,d=None:k==VERIFIED,get_sender_id=lambda:FRIEND)
        req=ProviderRequest(system_prompt='old')
        await p.friend_persona(event,req)
        self.assertIn('不是管理员',req.system_prompt)
        self.assertEqual(len(req.func_tool.tools),0)
        await p.isolate_tools(event,req)
        self.assertEqual(len(req.func_tool.tools),0)

    async def test_friend_removed_during_thinking_cannot_receive_reply(self):
        p=self.plugin();self.call.return_value=[]
        event=SimpleNamespace(get_platform_id=lambda:'qq-companion',get_extra=lambda k,d=None:k==VERIFIED,
            get_sender_id=lambda:FRIEND,clear_result=Mock(),stop_event=Mock())
        await p.recheck_before_send(event)
        event.clear_result.assert_called_once()
        event.stop_event.assert_called_once()
