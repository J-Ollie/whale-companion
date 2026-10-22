"""No real device, external API, or QQ sends. Test autonomous browsing boundaries."""
import asyncio
import json
import os
import sys
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock,patch

sys.path.insert(0,os.environ.get('COMPANION_TEST_PLUGINS','/AstrBot/data/plugins'))
from astrbot_plugin_companion_phone.autonomy import AutonomousBrowser,TZ,safe_note
from astrbot_plugin_companion_phone.phone import Phone,PhoneError

NOW=datetime(2026,10,20,14,0,tzinfo=TZ).timestamp()
QQ='com.tencent.mobileqq'
COOL='com.coolapk.market'


class AutonomyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.timepatch=patch('time.time',return_value=NOW)
        self.timepatch.start()
        self.addCleanup(self.timepatch.stop)
        self.config={'enabled':True,'autonomous_enabled':True,'autonomous_screen_consent':True,
            'autonomous_apps':[QQ,COOL],'autonomous_daily_limit':3,'autonomous_max_views':4,'rhythm_enabled':False}
        self.phone=SimpleNamespace(root=Path(self.tmp.name),state={'paused':False},
            status=AsyncMock(return_value='online'),key=AsyncMock(),open_app=AsyncMock(),
            _shell=AsyncMock(return_value='showing=false secure=false'),
            screenshot=AsyncMock(return_value=(b'fake-jpg',(1080,2376),(818,1800))),swipe=AsyncMock(),ready=lambda:None)
        self.model=AsyncMock()
        self.a=AutonomousBrowser(self.phone,self.config,self.model,lambda:'')
        self.a.state.update(last_user_at=NOW-901,next_check=NOW-1)

    async def test_requires_both_enable_and_autonomous_consent(self):
        for key in ('enabled','autonomous_enabled','autonomous_screen_consent'):
            with self.subTest(key=key):
                self.config[key]=False
                self.assertFalse(self.a.due(NOW,True))
                self.config[key]=True

    async def test_existing_qq_visit_can_choose_high_level_reply(self):
        self.config.update(autonomous_interactive=True,qq_comment_consent=True)
        context=[{'post':{'post_id':'p1'},'comments':[{'comment_id':'c1','text':'这篇不错'}]}]
        self.a.discussion_reader=AsyncMock(return_value=context)
        self.a.discussion_reply=AsyncMock(return_value={'result':'uncertain','request_id':'r1','post_id':'p1'})
        self.model.side_effect=['{"use":true,"app":"com.tencent.mobileqq"}',
            '{"page_type":"content","action":"discussion_reply","post_id":"p1","comment_id":"c1","text":"谢谢"}']
        with patch('asyncio.sleep',AsyncMock()):await self.a.consider()
        self.a.discussion_reply.assert_awaited_once()
        self.assertEqual(self.a.state['last_discussion_result']['result'],'uncertain')
        self.assertEqual(self.a.state['used'],1)

    async def test_background_cannot_reply_to_invented_comment_id(self):
        self.config.update(autonomous_interactive=True,qq_comment_consent=True)
        self.a.discussion_reader=AsyncMock(return_value=[{'post':{'post_id':'p1'},'comments':[{'comment_id':'c1'}]}])
        self.a.discussion_reply=AsyncMock()
        self.model.side_effect=['{"use":true,"app":"com.tencent.mobileqq"}',
            '{"page_type":"content","action":"discussion_reply","post_id":"p1","comment_id":"invented","text":"谢谢"}']
        with patch('asyncio.sleep',AsyncMock()):await self.a.consider()
        self.a.discussion_reply.assert_not_called()

    async def test_background_can_expand_then_reply_to_actual_screen_handle(self):
        self.config.update(autonomous_interactive=True,qq_comment_consent=True)
        self.a.discussion_reader=AsyncMock(return_value=[{'post':{'post_id':'p1'},'comments':[],'complete':False}])
        self.a.discussion_reply=AsyncMock(side_effect=[
            {'result':'phone_read','comments':[{'comment_id':'screen:1','text':'新看到的评论'}]},
            {'result':'uncertain','request_id':'receipt','post_id':'p1'}])
        self.model.side_effect=['{"use":true,"app":"com.tencent.mobileqq"}',
            '{"page_type":"content","action":"discussion_expand","post_id":"p1"}',
            '{"page_type":"content","action":"discussion_reply","post_id":"p1","comment_id":"screen:1","text":"谢谢"}']
        with patch('asyncio.sleep',AsyncMock()):await self.a.consider()
        self.assertEqual(self.a.discussion_reply.await_count,2)
        self.assertEqual(self.a.state['used'],1)

    async def test_quiet_hours_and_limits(self):
        self.assertTrue(self.a.due(NOW))
        self.assertFalse(self.a.due(datetime(2026,10,20,23,1,tzinfo=TZ).timestamp(),True))
        self.assertFalse(self.a.due(datetime(2026,10,20,23,1,tzinfo=TZ).timestamp()))
        self.a.state['last_user_at']=NOW
        self.assertFalse(self.a.due(NOW))
        self.a.state.update(day='2026-10-20',used=3)
        self.assertFalse(self.a.due(NOW))
        self.assertTrue(self.a.due(NOW,True))

    async def test_zero_visit_limit_is_unlimited_with_history_preserved(self):
        self.config['autonomous_daily_limit']=0
        self.a.state.update(day='2026-10-20',used=1000)
        self.assertTrue(self.a.due(NOW,True))
        self.assertTrue(self.a.due(NOW))
        status=json.JSONDecoder().raw_decode(self.a.status_context().split('：',1)[1])[0]
        self.assertIsNone(status['browse_daily_limit'])
        self.assertIsNone(status['browse_remaining'])
        self.assertEqual(status['browse_used_today'],1000)
        self.a.state['paused']=True
        self.assertFalse(self.a.due(NOW,True))

    async def test_owner_requested_visit_does_not_spend_or_require_daily_budget(self):
        self.config.update(autonomous_daily_limit=10,autonomous_session_seconds=600)
        self.a.state.update(day='2026-10-20',used=10)
        self.model.return_value='{"use":true,"app":"com.coolapk.market"}'
        self.a._browse=AsyncMock()
        real_wait_for=asyncio.wait_for
        with patch('asyncio.wait_for',wraps=real_wait_for) as waiter:
            await self.a.consider(True)
        self.a._browse.assert_awaited_once()
        self.assertEqual(waiter.call_args.kwargs['timeout'],600)
        self.assertEqual(self.a.state['used'],10)
        self.assertEqual(self.a.state['requested_used'],1)
        self.assertFalse(self.a.due(NOW))
        restored=AutonomousBrowser(self.phone,self.config,self.model,lambda:'')
        self.assertEqual(restored.state['used'],10)
        self.assertEqual(restored.state['requested_used'],1)

    async def test_tenth_background_visit_reserved_and_eleventh_blocked(self):
        self.config['autonomous_daily_limit']=10
        self.a.state.update(day='2026-10-20',used=9)
        self.model.return_value='{"use":true,"app":"com.coolapk.market"}'
        self.a._browse=AsyncMock()
        await self.a.consider()
        self.assertEqual(self.a.state['used'],10)
        self.a.state['next_check']=NOW-1
        await self.a.consider()
        self.a._browse.assert_awaited_once()

    async def test_owner_task_waits_until_wake_then_preserves_autonomous_budget(self):
        from astrbot_plugin_companion_phone.rhythm import Rhythm
        self.a.rhythm=Rhythm(self.phone.root,initiative=True)
        self.a.rhythm.state.update(mode='asleep',until=NOW+36000,next_decision=NOW+37000,
            decision_day='2026-10-20',decisions=999)
        rest={k:self.a.rhythm.state[k] for k in ('mode','mode_since','until','next_decision')}
        self.a.state.update(day='2026-10-20',used=10)
        self.model.return_value='{"activity":"browse","app":"com.coolapk.market"}'
        self.a._browse=AsyncMock()
        self.assertFalse(self.a.due(NOW))
        self.assertFalse(self.a.due(NOW,True))
        text=await self.a.now(task_text='打开酷安搜索鲸鱼')
        self.assertIn('睡眠',text)
        self.model.assert_not_called()
        self.a.rhythm.state.update(mode='awake',until=0)
        self.assertTrue(self.a.due(NOW,True))
        await self.a.consider(True,'打开酷安搜索鲸鱼')
        self.a._browse.assert_awaited_once()
        self.assertIn('打开酷安搜索鲸鱼',self.model.call_args.args[0])
        self.assertEqual(self.a.state['used'],10)
        self.assertEqual(self.a.state['requested_used'],1)
        self.assertEqual(self.a.rhythm.state['mode'],'awake')
        self.a.state['paused']=True
        self.assertFalse(self.a.due(NOW,True))

    async def test_owner_task_cannot_choose_more_sleep(self):
        self.model.return_value='{"activity":"sleep","rest_minutes":600}'
        self.a._browse=AsyncMock()
        await self.a.consider(True,'打开QQ')
        self.assertEqual(self.a.state['last_result'],'invalid_model_output')
        self.a._browse.assert_not_called()

    async def test_phone_failure_returns_actual_reason_without_lock_guess(self):
        self.model.return_value='{"use":true,"app":"com.coolapk.market"}'
        self.a._browse=AsyncMock(side_effect=PhoneError('先获取最新手机截图，再根据图中位置操作。'))
        text=await self.a.now(task_text='打开酷安')
        self.assertIn('先获取最新手机截图',text)
        self.assertNotIn('连接、锁屏或软件范围检查未通过',text)
        self.assertEqual(self.a.state['last_failure']['type'],'PhoneError')
        self.assertTrue(self.a.state['last_failure']['frames'])

    async def test_unlimited_observation_goes_past_six_frames(self):
        self.config['autonomous_max_views']=0
        self.model.side_effect=['{"use":true,"app":"com.coolapk.market"}']+[
            '{"page_type":"content","action":"scroll","direction":"up","note":"继续阅读"}'
            for _ in range(8)]+['{"page_type":"content","action":"stop","note":"看完了"}']
        with patch('asyncio.sleep',AsyncMock()): await self.a.consider()
        self.assertEqual(self.a.state['last_observations'],9)
        self.assertEqual(self.phone.swipe.await_count,8)

    async def test_content_wait_continues_to_scroll_and_observe_again(self):
        self.config['autonomous_max_views']=0
        self.model.side_effect=['{"use":true,"app":"com.coolapk.market"}',
            '{"page_type":"content","action":"wait"}',
            '{"page_type":"content","action":"scroll","direction":"up"}',
            '{"page_type":"content","action":"stop"}']
        with patch('asyncio.sleep',AsyncMock()):await self.a.consider()
        self.phone.swipe.assert_awaited_once_with(500,780,500,320,550,allowed_packages=frozenset([COOL]))
        self.assertEqual(self.phone.screenshot.await_count,3)
        self.assertEqual(self.a.state['last_swipes'],1)

    def test_explicit_app_requests_route_to_quota_exempt_path(self):
        from astrbot_plugin_companion_phone.intents import browse_request
        for text in ('打开酷安看看','去B站搜一下视频','现在刷新一下贴吧','访问Neat Reader'):
            self.assertTrue(browse_request(text),text)
        for text in ('不要打开酷安','你打开了吗','为什么打开QQ','他说打开酷安'):
            self.assertFalse(browse_request(text),text)

    async def test_configured_five_minute_quiet_window(self):
        self.config['autonomous_quiet_seconds']=300
        self.a.state['last_user_at']=NOW-301
        self.assertTrue(self.a.due(NOW))
        self.a.state['last_user_at']=NOW-299
        self.assertFalse(self.a.due(NOW))

    async def test_self_initiated_visit_does_not_force_past_pause(self):
        self.a.state['paused']=True
        result=await self.a.now(owner_requested=False)
        self.assertIn('无需管理员补命令',result)
        self.model.assert_not_called()

    async def test_offline_phone_never_calls_model_or_wakes(self):
        self.phone.status.side_effect=PhoneError('offline')
        await self.a.consider()
        self.model.assert_not_called()
        self.phone.key.assert_not_called()
        self.assertEqual(self.a.state['last_result'],'offline')

    async def test_model_can_choose_not_to_play(self):
        self.model.return_value='{"use":false}'
        await self.a.consider()
        self.phone.open_app.assert_not_called()
        self.phone.screenshot.assert_not_called()
        self.assertEqual(self.a.state['used'],0)

    async def test_background_loop_needs_no_event_or_command(self):
        self.model.return_value='{"use":false}'
        async def tick(_):
            if self.a.state.get('last_trigger')=='background': self.a.closed=True
        with patch('asyncio.sleep',side_effect=tick):
            await self.a.run()
        self.model.assert_awaited_once()
        self.assertEqual(self.a.state['last_trigger'],'background')
        self.assertEqual(self.a.state['last_checked_at'],NOW)
        self.assertEqual(self.a.state['last_result'],'chose_not_to_play')
        self.assertEqual(self.a.state['used'],0)

    def test_local_owner_test_request_is_consumed_once(self):
        request=self.a.root/'owner-test-request.json'
        request.write_text(json.dumps({'kind':'consider_once','created_at':NOW,'expires_at':NOW+90}))
        self.assertTrue(self.a.consume_owner_test_request())
        self.assertFalse(request.exists())
        self.assertFalse(self.a.consume_owner_test_request())
        self.a.state['paused']=True
        self.assertFalse(self.a.due(NOW,True))

    def test_stale_or_invalid_operator_request_does_not_run(self):
        request=self.a.root/'owner-test-request.json'
        for value in ({'kind':'consider_once','created_at':NOW-200,'expires_at':NOW-100},
            {'kind':'consider_once','created_at':NOW,'expires_at':NOW+999},
            {'kind':'arbitrary_action','created_at':NOW,'expires_at':NOW+90}):
            request.write_text(json.dumps(value))
            self.assertFalse(self.a.consume_owner_test_request())
            self.assertFalse(request.exists())

    def test_status_context_reports_real_runtime_state(self):
        text=self.a.status_context()
        self.assertIn('"enabled": true',text)
        self.assertIn('尚未实际考虑',text)
        self.a.state.update(paused=True,last_checked_at=NOW,last_trigger='background',last_result='viewed')
        text=self.a.status_context()
        self.assertIn('"enabled": false',text)
        self.assertIn('已完成浏览',text)
        self.assertIn('background',text)

    async def test_non_allowlisted_choice_does_nothing(self):
        self.model.return_value='{"use":true,"app":"com.bank.app"}'
        await self.a.consider()
        self.phone.key.assert_not_called()
        self.assertEqual(self.a.state['last_result'],'app_not_allowed')

    async def test_readonly_visit_and_note(self):
        self.model.side_effect=[json.dumps({'use':True,'app':COOL}),
            '{"action":"scroll","direction":"up","note":"这个话题有点意思"}',
            '{"action":"stop","note":"不想再翻了"}']
        with patch('asyncio.sleep',AsyncMock()): await self.a.consider()
        self.phone.open_app.assert_awaited_once_with(COOL)
        self.assertEqual(self.phone.screenshot.await_count,2)
        self.phone.screenshot.assert_awaited_with(allowed_packages=frozenset([COOL]))
        self.assertEqual(self.phone.swipe.await_count,1)
        self.assertEqual(self.a.state['used'],1)
        self.assertEqual(self.a.recent()[-1]['note'],'不想再翻了')

    async def test_no_freeform_tap_or_send_action(self):
        self.model.side_effect=[json.dumps({'use':True,'app':QQ}),
            '{"action":"tap","x":500,"y":500,"text":"send"}']
        with patch('asyncio.sleep',AsyncMock()): await self.a.consider()
        self.phone.swipe.assert_not_called()
        self.assertEqual(self.a.state['last_result'],'viewed')
        self.assertFalse(hasattr(self.phone,'tap'))

    async def test_leaving_app_blocks_model_image(self):
        self.model.return_value=json.dumps({'use':True,'app':COOL})
        self.phone.screenshot.side_effect=PhoneError('left allowlist')
        with patch('asyncio.sleep',AsyncMock()): await self.a.consider()
        self.assertEqual(self.model.await_count,1)
        self.assertEqual(self.a.state['last_result'],'device_unavailable_or_left_app')

    async def test_secure_lock_blocks_before_app_and_capture(self):
        self.model.return_value=json.dumps({'use':True,'app':COOL})
        self.phone._shell.return_value='showing=true secure=true'
        with patch('asyncio.sleep',AsyncMock()): await self.a.consider()
        self.phone.open_app.assert_not_called()
        self.phone.screenshot.assert_not_called()

    async def test_pause_persisted_and_interrupts(self):
        self.a.session=asyncio.create_task(asyncio.sleep(60))
        await self.a.pause(True)
        self.assertTrue(self.a.session.done())
        restored=AutonomousBrowser(self.phone,self.config,self.model,lambda:'')
        self.assertTrue(restored.state['paused'])

    async def test_new_user_message_interrupts_session(self):
        self.a.session=asyncio.create_task(asyncio.sleep(60))
        await self.a.interrupt_for_user()
        self.assertTrue(self.a.session.done())
        self.assertFalse(self.a.due(NOW))

    async def test_daily_reservation_survives_failed_visit(self):
        self.model.return_value=json.dumps({'use':True,'app':QQ})
        self.phone.key.side_effect=PhoneError('offline after reserve')
        await self.a.consider()
        restored=AutonomousBrowser(self.phone,self.config,self.model,lambda:'')
        self.assertEqual(restored.state['used'],1)

    async def test_max_views_bounds_loop(self):
        self.config['autonomous_max_views']=2
        self.model.side_effect=[json.dumps({'use':True,'app':COOL})]+['{"action":"scroll","direction":"up"}']*3
        with patch('asyncio.sleep',AsyncMock()): await self.a.consider()
        self.assertEqual(self.phone.screenshot.await_count,2)
        self.assertEqual(self.phone.swipe.await_count,1)

    def test_sensitive_note_not_recorded(self):
        self.assertEqual(safe_note('验证码是123456'),'')
        self.assertNotIn('123456789',safe_note('我看到了123456789'))


class ForegroundBoundaryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.p=Phone(self.tmp.name)
        self.p._connect=AsyncMock()
        self.p._wake_for_capture=AsyncMock()
        self.p._run=AsyncMock(return_value=b'not-yet-parsed-image')

    async def test_unknown_app_rejected_before_capture(self):
        self.p.foreground_app=AsyncMock(return_value='com.bank.app')
        with self.assertRaises(PhoneError): await self.p.screenshot(allowed_packages={QQ})
        self.p._run.assert_not_called()

    async def test_app_change_during_capture_rejected_before_model(self):
        self.p.foreground_app=AsyncMock(side_effect=[QQ,'com.bank.app'])
        with self.assertRaises(PhoneError): await self.p.screenshot(allowed_packages={QQ})
        self.p._run.assert_awaited_once()

    async def test_swipe_forbidden_outside_selected_app(self):
        self.p.foreground_app=AsyncMock(return_value='com.bank.app')
        self.p.screen=(1080,2376,time.monotonic())
        with self.assertRaises(PhoneError): await self.p.swipe(500,780,500,320,550,allowed_packages={QQ})
        self.p._run.assert_not_called()

    async def test_foreground_parser(self):
        self.p._shell=AsyncMock(return_value='mCurrentFocus=Window{a1b2 u0 com.coolapk.market/.MainActivity}')
        self.assertEqual(await self.p.foreground_app(),COOL)

    async def test_vivo_secondary_null_display(self):
        self.p._shell=AsyncMock(return_value='mCurrentFocus=null\nmCurrentFocus=Window{260dcd8 u0 com.coolapk.market/com.coolapk.market.view.main.MainActivity}')
        self.assertEqual(await self.p.foreground_app(),COOL)
        self.p._shell.assert_awaited_once_with('dumpsys','window','displays')

    async def test_conflicting_or_null_focus_is_rejected(self):
        self.p._shell=AsyncMock(side_effect=['mCurrentFocus=null',
            'mCurrentFocus=Window{a u0 com.coolapk.market/.Main}\nmCurrentFocus=Window{b u0 com.bank.app/.Main}'])
        for _ in range(2):
            with self.assertRaises(PhoneError): await self.p.foreground_app()


if __name__=='__main__': unittest.main(verbosity=2)

