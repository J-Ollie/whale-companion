"""Ex plugin integration, real SDK events, only mocked OneBot actions."""
import importlib
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock,Mock,patch
from astrbot.core.agent.tool import ToolSet
from astrbot_plugin_companion_phone.social_autonomy import SocialAutonomy
from test_social_autonomy import SocialTests


class ProfileExTests(SocialTests):
    async def test_ex_discovery_without_qzone_and_category_routing(self):
        self.plugins.clear();self.plugins['astrbot_plugin_qqprofile_ex']=NS(set_avatar=AsyncMock())
        native=[self.tool(n,AsyncMock()) for n in ('change_my_signature','change_my_status','change_my_nickname','qqprofile_set_signature')]
        with patch('astrbot_plugin_companion_phone.social_compat.llm_tools',NS(func_list=native)):
            self.assertTrue(self.c.available())
            self.assertEqual(set(self.c.tools().names()),{'change_my_signature','change_my_status','change_my_nickname','qqprofile_set_avatar'})
            w=SocialAutonomy(self.p)
            self.assertEqual(w.tools_for(['status']).names(),['change_my_status'])
            self.assertEqual(w.tools_for(['signature']).names(),['change_my_signature'])
            self.assertEqual(set(w.tools_for(['identity']).names()),{'change_my_nickname','qqprofile_set_avatar'})

    async def test_native_global_tools_stripped_for_other_contacts(self):
        self.plugins['astrbot_plugin_qqprofile_ex']=NS()
        native=[self.tool(n,AsyncMock()) for n in ('change_my_signature','change_my_status','change_my_nickname')]
        with patch('astrbot_plugin_companion_phone.social_compat.llm_tools',NS(func_list=native)):
            req=NS(func_tool=ToolSet(native+[self.tool('unrelated')]))
            self.c.filter_tools(NS(owner=False),req)
            self.assertEqual(req.func_tool.names(),['unrelated'])
            self.c.filter_tools(NS(owner=True),req)
            self.assertEqual(len(req.func_tool.names()),4)
            self.assertIsNot(req.func_tool.get_tool('change_my_signature').handler,native[0].handler)

    async def test_handler_rejects_other_contact_even_if_obtained(self):
        fn=AsyncMock();t=self.c.wrap_profile(self.tool('change_my_status',fn))
        with self.assertRaises(ValueError):await t.handler(NS(owner=False),status='在线')
        fn.assert_not_awaited()

    def internal_event(self):
        client=NS(call_action=AsyncMock(return_value={}))
        platform=NS(meta=lambda:NS(id='qq-companion',name='aiocqhttp'),get_client=lambda:client)
        return SocialAutonomy(self.p).event(platform),client

    async def test_actual_upstream_handlers_receive_native_event_and_acknowledgements(self):
        # Load installed upstream unchanged; do not replace its handler with a fake.
        module=importlib.import_module('data.plugins.astrbot_plugin_qqprofile_ex.main')
        plugin=object.__new__(module.QQProfileExPlugin)
        plugin.conf={k:0 for k in ('signature_cooldown_minutes','status_cooldown_minutes','nickname_cooldown_minutes')}
        for key in ('curr_signature','curr_status','curr_nickname'):setattr(plugin,key,None)
        for key in ('_last_signature_change','_last_status_change','_last_nickname_change'):setattr(plugin,key,0)
        for name,method,args,action in (
            ('change_my_signature','auto_set_signature',{'signature':'公开的感想'},'set_self_longnick'),
            ('change_my_status','auto_set_status',{'status':'在线'},'set_online_status'),
            ('change_my_nickname','auto_set_nickname',{'nickname':'小鲸鱼'},'set_qq_profile')):
            event,client=self.internal_event()
            value=await self.c.wrap_profile(self.tool(name,getattr(plugin,method))).handler(event,**args)
            self.assertIn('成功',value);self.assertEqual(client.call_action.call_args.args[0],action)
            self.assertEqual(self.c.state['actions'][-1]['result'],'api_acknowledged')
            self.assertIn(next(iter(args.values())),self.c.state['actions'][-1]['public_note'])

    async def test_signature_private_content_blocked_before_api(self):
        self.p.books.read=lambda *a:[{'text':'这是一段足够长的真正私密日记内容'}]
        fn=AsyncMock();event,client=self.internal_event()
        with self.assertRaises(ValueError):await self.c.wrap_profile(self.tool('change_my_signature',fn)).handler(event,signature='这是一段足够长的真正私密日记内容')
        fn.assert_not_awaited();client.call_action.assert_not_awaited()

    async def test_noop_is_not_success_and_unknown_write_not_retried(self):
        self.p.report_own_outcome=Mock()
        event,client=self.internal_event()
        async def noop(event,status):return '当前平台不支持'
        await self.c.wrap_profile(self.tool('change_my_status',noop)).handler(event,status='不支持')
        self.assertEqual(self.c.state['actions'][-1]['result'],'not_executed')
        self.p.report_own_outcome.assert_not_called()
        async def write(event,status):return await event.bot.set_online_status(status=10)
        client.call_action.side_effect=TimeoutError
        tool=self.c.wrap_profile(self.tool('change_my_status',write))
        with self.assertRaises(TimeoutError):await tool.handler(event,status='在线')
        with self.assertRaises(ValueError):await tool.handler(event,status='在线')
        self.assertEqual(client.call_action.await_count,1)
        self.assertEqual(self.c.state['actions'][-1]['result'],'uncertain')
        self.p.report_own_outcome.assert_called_once_with('social','operate','social_unconfirmed',error_type='TimeoutError')

    async def test_receipt_and_public_content_reach_existing_brain_memory(self):
        event,client=self.internal_event()
        coordinator=type('Coordinator',(),{'__module__':'astrbot_plugin_companion_chat.brain'})()
        coordinator.remember=Mock()
        async def write(event,signature):await event.bot.set_self_longnick(longNick=signature);return '已提交'
        with patch('astrbot_plugin_companion_phone.compat.brain',return_value=coordinator):
            await self.c.wrap_profile(self.tool('change_my_signature',write)).handler(event,signature='今天想试试新的表达')
        args,kwargs=coordinator.remember.call_args
        self.assertEqual(args[1:3],('change_my_signature','api_acknowledged'))
        self.assertEqual(kwargs['scope'],'public')
        self.assertEqual(kwargs['note'],'今天想试试新的表达')

    async def test_sleep_during_public_review_prevents_write(self):
        event,client=self.internal_event()
        async def review(text):self.awake=False
        self.c.review=review
        fn=AsyncMock()
        with self.assertRaises(ValueError):await self.c.wrap_profile(self.tool('change_my_signature',fn)).handler(event,signature='新的公开想法')
        fn.assert_not_awaited();client.call_action.assert_not_awaited()

    async def test_avatar_delegates_to_manual_upstream_after_real_review(self):
        module=importlib.import_module('data.plugins.astrbot_plugin_qqprofile_ex.main')
        plugin=object.__new__(module.QQProfileExPlugin)
        plugin.get_curr_persona_id=AsyncMock(return_value=None)
        image=self.root/'public.png';image.write_bytes(b'image fixture')
        self.p.media=NS(asset=lambda *a,**kw:(None,image),preview_bytes=lambda *a:b'preview')
        event,client=self.internal_event()
        await self.c.wrap_profile(self.tool('qqprofile_set_avatar',plugin.set_avatar)).handler(event,image_id='media:test')
        self.assertEqual(client.call_action.call_args.args[0],'set_qq_avatar')
        self.assertTrue(client.call_action.call_args.kwargs['file'].startswith('base64://'))
        self.assertEqual(self.c.state['actions'][-1]['result'],'api_acknowledged')


# Avoid counting inherited fixture tests twice in this focused suite.
for _name in list(vars(SocialTests)):
    if _name.startswith('test_'):setattr(ProfileExTests,_name,None)
del SocialTests
