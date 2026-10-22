"""Standing QQ entry bypasses fixed phrases, not scope or sleep. No real I/O."""
import os,sys,tempfile,time,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock,Mock,patch
sys.path.insert(0,os.environ.get('COMPANION_TEST_PLUGINS','/AstrBot/data/plugins'))
from astrbot_plugin_companion_phone.main import CompanionPhone
from astrbot_plugin_companion_phone.phone import PhoneError
from test_phone import Event

class QQEntry(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        pause=patch('astrbot_plugin_companion_phone.main.asyncio.sleep',AsyncMock());pause.start();self.addCleanup(pause.stop)
        self.p=object.__new__(CompanionPhone);self.p.owner='100000001';self.p.umo='qq-companion:FriendMessage:100000001'
        self.p.config={'enabled':True,'allow_model_screen':True,'qq_comment_consent':True}
        self.p.phone=SimpleNamespace(ready=Mock(),key=AsyncMock(),open_app=AsyncMock(),swipe=AsyncMock(),
            screenshot=AsyncMock(return_value=(b'fake-image',(1080,2376),(540,1188))))
        self.p.autonomy=SimpleNamespace(rhythm=None,apps={'com.tencent.mobileqq':'QQ'},state={'day':'','used':0},save=Mock(),
            daily_limit=lambda:10,interactive_action=AsyncMock(return_value=True))
        self.e=Event();self.e.message_str='我给你争取到了一些权限，你想去试试吗'

    async def test_soft_invitation_can_open_without_manual_task(self):
        self.assertFalse(self.p.task_authorized(self.e))
        result=await self.p.tool_qq_space(self.e)
        self.assertFalse(result.isError)
        self.p.phone.open_app.assert_awaited_once_with('com.tencent.mobileqq')
        self.assertEqual(result.content[-1].type,'image')
        self.assertEqual(self.p.autonomy.state['used'],1)
        await self.p.tool_qq_space(self.e,action='view')
        self.assertEqual(self.p.autonomy.state['used'],1)

    async def test_nonowner_and_disabled_scope_never_open(self):
        for event in [Event('1'),Event(origin='qq-companion:GroupMessage:100000001')]:
            self.assertTrue((await self.p.tool_qq_space(event)).isError)
        self.p.config['qq_comment_consent']=False
        self.assertTrue((await self.p.tool_qq_space(self.e)).isError)
        self.p.phone.open_app.assert_not_called()

    async def test_sleep_and_pause_are_not_overridden(self):
        self.p.autonomy.rhythm=SimpleNamespace(advance=Mock(),healthy=True,state={'mode':'asleep'})
        self.assertTrue((await self.p.tool_qq_space(self.e)).isError)
        self.p.autonomy.rhythm=None;self.p.autonomy.state['paused']=True
        self.assertTrue((await self.p.tool_qq_space(self.e)).isError)
        self.p.phone.open_app.assert_not_called()

    async def test_autonomous_quota_and_owner_task_differ(self):
        from datetime import datetime
        from astrbot_plugin_companion_phone.autonomy import TZ
        self.p.autonomy.state.update(day=datetime.now(TZ).date().isoformat(),used=10)
        self.assertTrue((await self.p.tool_qq_space(self.e)).isError)
        self.e.message_str='去QQ空间回复评论'
        self.assertFalse((await self.p.tool_qq_space(self.e)).isError)
        self.assertEqual(self.p.autonomy.state['used'],10)

    async def test_no_open_no_act_and_ten_minute_expiry(self):
        self.assertTrue((await self.p.tool_qq_space(self.e,action='tap')).isError)
        self.e.set_extra('companion_qq_ui_until',time.time()-1)
        self.assertTrue((await self.p.tool_qq_space(self.e)).isError)
        self.p.autonomy.interactive_action.assert_not_called()

    async def test_comment_delegates_to_guarded_path_and_returns_real_screen(self):
        await self.p.tool_qq_space(self.e)
        r=await self.p.tool_qq_space(self.e,action='comment_input',text='有点意思',target_quote='屏幕上真实的评论内容')
        self.assertFalse(r.isError)
        self.assertEqual(self.p.autonomy.interactive_action.call_args.args[0],'com.tencent.mobileqq')
        self.assertEqual(self.p.autonomy.interactive_action.call_args.args[1]['action'],'comment_input')
        self.p.autonomy.interactive_action.return_value=False
        r=await self.p.tool_qq_space(self.e,action='comment_send',x=900,y=900)
        self.assertTrue(r.isError)
        self.assertIn('授权已开启',r.content[0].text)

    async def test_unavailable_result_is_not_reported_as_background_job(self):
        self.p.phone.open_app.side_effect=PhoneError('设备离线')
        r=await self.p.tool_qq_space(self.e)
        self.assertTrue(r.isError);self.assertIn('设备离线',r.content[0].text)

    async def test_stop_request_prevents_open(self):
        self.e.message_str='不要评论'
        self.assertTrue((await self.p.tool_qq_space(self.e)).isError)
        self.p.phone.open_app.assert_not_called()

    async def test_mutation_budget_is_bounded(self):
        await self.p.tool_qq_space(self.e)
        self.e.set_extra('companion_qq_ui_actions',12)
        self.assertTrue((await self.p.tool_qq_space(self.e,action='tap')).isError)
        self.assertFalse((await self.p.tool_qq_space(self.e,action='view')).isError)

    async def test_new_tool_is_added_without_changing_existing_persona_fields(self):
        self.p.context=SimpleNamespace(persona_manager=SimpleNamespace(update_persona=AsyncMock()))
        with patch('astrbot_plugin_companion_phone.main.db_helper.get_persona_by_id',AsyncMock(return_value=SimpleNamespace(tools=['old_tool']))):
            await self.p.ensure_qq_tool()
        self.p.context.persona_manager.update_persona.assert_awaited_once_with('小鲸鱼',tools=['old_tool','companion_qq_space','companion_site_visitors'])
        self.p.context.persona_manager.update_persona.reset_mock()
        for tools in (None,['old_tool','companion_qq_space','companion_site_visitors']):
            with patch('astrbot_plugin_companion_phone.main.db_helper.get_persona_by_id',AsyncMock(return_value=SimpleNamespace(tools=tools))):
                await self.p.ensure_qq_tool()
        self.p.context.persona_manager.update_persona.assert_not_called()

if __name__=='__main__':unittest.main()
