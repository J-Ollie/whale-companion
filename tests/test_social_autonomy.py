"""Owner-delegated social tools and retired backend boundaries; external writes mocked."""
import asyncio
from pathlib import Path
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock,patch
from astrbot.core.agent.tool import ToolSet,FunctionTool
from astrbot_plugin_companion_phone.social_compat import SocialCompat
from astrbot_plugin_companion_phone.social_autonomy import SocialAutonomy,SocialClient
from astrbot_plugin_companion_phone import main
QzoneParseError=ValueError

class External:
    def __init__(self):
        self.controller=NS(_request=AsyncMock(return_value={'fid':'fake'}))
        self._chat_history_context=AsyncMock(return_value='private group chat')
        self._ask_llm_tool_reply=AsyncMock()
        self._llm_tool_reply_summary=lambda p:'confirmed'
    async def _publish_post_payload(self):pass

class SocialTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.awake=True;self.external=External()
        self.plugins={'astrbot_plugin_qzone_ultra':self.external,'astrbot_plugin_qqprofile':NS()}
        self.p=NS(root=self.root,config={'social_full_autonomy_enabled':True},
            executor_allowed=lambda:self.awake,books=NS(read=lambda *a:[]),visitor_inbox=NS(private_texts=lambda:[]),
            public_model=AsyncMock(return_value='{"safe":true}'),owner_event=lambda e:e.owner,
            context=NS(get_registered_star=lambda n:NS(activated=True,star_cls=self.plugins[n]) if n in self.plugins else None))
        self.offered=None
        def offer(kind,worker,*,check,**kw):self.offered=(worker,check);return True
        self.plugins['astrbot_plugin_companion_chat']=NS(ready=True,brain=NS(offer=offer))
        self.c=SocialCompat(self.p);self.p.social_compat=self.c
        self.c.bind()
        self.send=self.c.bindings[0][2]

    async def choose(self):
        offer=getattr(self,'offered',None);self.offered=None
        if offer and offer[1]():await offer[0]()

    def tool(self,name,handler=None):return FunctionTool(name=name,description='',parameters={'type':'object','properties':{}},handler=handler)
    async def call(self,path,**body):return await self.external.controller._request('POST',path,json_body=body)

    async def test_social_daily_caps_and_comment_interval_are_absent(self):
        for i in range(12):
            await self.call('/post',content='公开动态'+str(i))
            await self.call('/comment',content='公开评论'+str(i),fid=str(i))
            await self.call('/like',fid=str(i))
        self.assertEqual(self.send.await_count,36)

    async def test_delete_does_not_need_current_message_keyword(self):
        await self.call('/delete',fid='own-id')
        self.send.assert_awaited_once()

    async def test_sleep_pause_and_explicit_disable_still_block(self):
        self.awake=False
        with self.assertRaises(ValueError):await self.call('/post',content='公开动态')
        self.awake=True;self.c.pause(True)
        with self.assertRaises(ValueError):await self.call('/like',fid='a')
        self.c.pause(False);self.p.config['social_full_autonomy_enabled']=False
        with self.assertRaises(ValueError):await self.call('/delete',fid='a')
        self.send.assert_not_awaited()

    async def test_cookie_maintenance_is_independent_of_sleep(self):
        self.awake=False
        await self.call('/bind')
        self.send.assert_awaited_once()

    async def test_private_material_and_cross_platform_writes_rejected(self):
        self.p.books.read=lambda *a:[{'text':'这是一段足够长的真正私密日记内容'}]
        with self.assertRaises(ValueError):await self.call('/post',content='这是一段足够长的真正私密日记内容')
        with self.assertRaises(ValueError):await self.call('/post',content='公开动态',sync_weibo=True)
        self.send.assert_not_awaited()

    async def test_unknown_same_operation_is_not_replayed_but_other_content_is_allowed(self):
        self.send.side_effect=[TimeoutError,{'fid':'new'}]
        with self.assertRaises(TimeoutError):await self.call('/post',content='第一条')
        with self.assertRaises(ValueError):await self.call('/post',content='第一条')
        await self.call('/post',content='另一条')
        self.assertEqual(self.send.await_count,2)
        self.assertFalse(self.send.call_args.kwargs['retry_on_timeout'])

    async def test_profile_tool_delegates_to_existing_plugin_and_does_not_send_chat(self):
        async def existing(event,longnick):
            await event.bot.set_self_longnick(longNick=longnick)
            yield NS(chain=[NS(text='新插件回执')])
        source=self.tool('qqprofile_set_signature',existing)
        clone=self.c.wrap_profile(source);event=NS(owner=True,bot=NS(call_action=AsyncMock()))
        self.assertEqual(await clone.handler(event,longnick='自己选的签名'),'新插件回执')
        event.bot.call_action.assert_awaited_once_with('set_self_longnick',longNick='自己选的签名')
        self.assertIs(source.handler,existing)

    async def test_new_tools_only_reach_owner_not_other_contacts(self):
        tools=[self.tool('qzone_delete_post'),self.tool('qqprofile_set_signature',AsyncMock()),self.tool('qqprofile_switch_persona')]
        with patch('astrbot_plugin_companion_phone.social_compat.llm_tools',NS(func_list=tools)):
            req=NS(func_tool=ToolSet());self.c.filter_tools(NS(owner=False),req)
            self.assertEqual(len(req.func_tool),0)
            self.c.filter_tools(NS(owner=True),req)
            self.assertIn('qzone_delete_post',req.func_tool.names())
            self.assertIn('qqprofile_set_signature',req.func_tool.names())
            self.assertNotIn('qqprofile_switch_persona',req.func_tool.names())

    async def test_history_injection_is_removed_and_adapter_rebinds(self):
        self.assertEqual(await self.external._chat_history_context(),'')
        self.external=External();self.plugins['astrbot_plugin_qzone_ultra']=self.external
        original=self.external.controller._request;self.c.bind()
        self.assertIsNot(original,self.external.controller._request)
        self.c.close();self.assertIs(original,self.external.controller._request)

    async def test_legacy_backend_files_and_llm_entrypoints_are_removed(self):
        root=Path(main.__file__).parent
        for name in ('publisher.py','qzone_client.py','discussions.py'):self.assertFalse((root/name).exists())
        for name in ('tool_qzone_publish','tool_qq_space','qzone_auth','background_discussion_reply'):
            self.assertFalse(hasattr(main.CompanionPhone,name))

    async def test_internal_client_cannot_send_messages_or_change_account_security(self):
        client=NS(call_action=AsyncMock());wrapped=SocialClient(client,self.c.check)
        for action in ('send_private_msg','send_group_msg','set_friend_add_request','set_login_password'):
            with self.assertRaises(ValueError):await wrapped.call_action(action)
        client.call_action.assert_not_awaited()
        await wrapped.set_self_longnick(longNick='自己选择')
        client.call_action.assert_awaited_once_with('set_self_longnick',longNick='自己选择')

    async def test_background_cycle_uses_only_new_plugin_tools_without_private_context(self):
        self.p.umo='qq-companion:FriendMessage:test-owner'
        self.p.config['social_provider_id']='fake'
        platform=NS(meta=lambda:NS(id='qq-companion',name='aiocqhttp'),get_client=lambda:NS(call_action=AsyncMock()))
        self.p.context.get_platform_inst=lambda n:platform
        self.p.context.get_config=lambda **kw:{'provider_settings':{'enable':True}}
        self.p.context.persona_manager=NS(get_persona=AsyncMock(return_value=NS(system_prompt='小鲸鱼')))
        self.p.context.tool_loop_agent=AsyncMock(return_value=NS(completion_text='这次不改'))
        self.p.public_reading_context=lambda:'已公开阅读的内容'
        self.p.media=NS(recent=lambda **kw:[])
        self.c.tools=lambda base=None:ToolSet([self.tool('qzone_list_feed')])
        worker=SocialAutonomy(self.p);await worker.cycle()
        kwargs=self.p.context.tool_loop_agent.call_args.kwargs
        self.assertEqual(kwargs['contexts'],[])
        self.assertEqual(kwargs['tools'].names(),['qzone_list_feed'])
        self.assertEqual(kwargs['event'].get_sender_id(),'100000002')
        self.assertTrue(kwargs['event'].is_admin())
        self.assertFalse(self.p.owner_event(NS(owner=False)))
        self.assertEqual(worker.state['last_result'],'considered')

    async def test_schedule_is_willingness_check_not_forced_action_or_daily_quota(self):
        worker=SocialAutonomy(self.p);worker.state['next_by_category']['space']=0;worker.cycle=AsyncMock()
        self.c.tools=lambda base=None:ToolSet([self.tool('qzone_list_feed')])
        worker.tick();await self.choose()
        worker.cycle.assert_awaited_once()
        worker.tick();self.assertEqual(worker.cycle.await_count,1)
        self.awake=False;worker.state['next_by_category']['space']=0;worker.tick()
        self.assertEqual(worker.cycle.await_count,1)

    async def test_daily_turn_cannot_select_monthly_or_weekly_profile_tools(self):
        tools=[self.tool(n) for n in ('qzone_list_feed','qzone_publish_post','qqprofile_set_status',
            'qqprofile_set_signature','qqprofile_set_avatar','qqprofile_set_nickname')]
        self.c.tools=lambda base=None:ToolSet(tools)
        worker=SocialAutonomy(self.p);worker.cycle=AsyncMock()
        worker.state['next_by_category'].update(status=0,space=0)
        worker.tick();await self.choose()
        categories,chosen=worker.cycle.call_args.args
        self.assertEqual(set(categories),{'status','space'})
        self.assertEqual(set(chosen.names()),{'qzone_list_feed','qzone_publish_post','qqprofile_set_status'})

    async def test_coincident_categories_use_one_model_turn(self):
        self.c.tools=lambda base=None:ToolSet([self.tool(n) for n in ('qzone_list_feed','qqprofile_set_status',
            'qqprofile_set_signature','qqprofile_set_avatar')])
        worker=SocialAutonomy(self.p);worker.cycle=AsyncMock()
        worker.state['next_by_category']={k:0 for k in worker.DEFAULT_INTERVALS}
        worker.tick();await self.choose()
        worker.cycle.assert_awaited_once()
        self.assertEqual(set(worker.state['last_categories']),set(worker.DEFAULT_INTERVALS))

    async def test_old_daily_schedule_migrates_without_resetting_history(self):
        import json
        self.p.config['social_autonomy_interval_seconds']=86400
        anchor=10000000000
        old={'next_at':anchor+86400,'cycles':32,'last_result':'considered','old_metadata':'keep'}
        (self.root/'social-autonomy.json').write_text(json.dumps(old),encoding='utf8')
        worker=SocialAutonomy(self.p)
        self.assertEqual(worker.state['cycles'],32)
        self.assertEqual(worker.state['old_metadata'],'keep')
        self.assertEqual(worker.state['next_by_category'],{k:anchor+v for k,v in worker.DEFAULT_INTERVALS.items()})
        restarted=SocialAutonomy(self.p)
        self.assertEqual(restarted.state,worker.state)

    async def test_sleep_defers_each_category_without_spending_its_opportunity(self):
        self.c.tools=lambda base=None:ToolSet([self.tool('qqprofile_set_status')])
        worker=SocialAutonomy(self.p);worker.state['next_by_category']['status']=0
        worker.cycle=AsyncMock();self.awake=False;worker.tick()
        self.assertEqual(worker.state['next_by_category']['status'],0)
        worker.cycle.assert_not_called()
        self.awake=True;worker.tick();await self.choose()
        worker.cycle.assert_awaited_once()

if __name__=='__main__':unittest.main()
