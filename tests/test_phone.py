"""Behavioral checks for phone boundaries; no device actions or QQ sends."""
import asyncio
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

sys.path.insert(0,os.environ.get('COMPANION_TEST_PLUGINS','/AstrBot/data/plugins'))
from astrbot_plugin_companion_phone.phone import Phone,PhoneError,PhoneOffline,PhonePortChanged,PHONE_IP,TOOL_NAMES
from astrbot_plugin_companion_phone.main import CompanionPhone
from astrbot_plugin_companion_phone.intents import qzone_request,browse_request


class Event:
    def __init__(self,owner='100000001',origin=None):
        self.owner=owner
        self.unified_msg_origin=origin or f'qq-companion:FriendMessage:{owner}'
        self.extra={}
        self.message_str='请检查备用机屏幕，并按要求操作手机'
    def get_sender_id(self): return self.owner
    def get_extra(self,k,d=None): return self.extra.get(k,d)
    def set_extra(self,k,v): self.extra[k]=v


class PhoneTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.phone=Phone(self.tmp.name)
        self.phone._shell=AsyncMock(return_value='')
        self.phone._connect=AsyncMock()

    async def test_private_target_only(self):
        for serial in ('127.0.0.1:22','example.com:1',PHONE_IP+':0',PHONE_IP+':99999',PHONE_IP+':1;id'):
            with self.assertRaises(PhoneError):
                await self.phone.connect_to(serial)
        self.phone._connect.assert_not_called()

    async def test_failed_port_change_rolls_back(self):
        self.phone._connect.side_effect=PhoneError('offline')
        with self.assertRaises(PhoneError):
            await self.phone.connect_to(PHONE_IP+':12345')
        self.assertTrue(self.phone.state['serial'].endswith(':38605'))

    async def test_pause_persists_and_blocks_actions(self):
        self.phone.pause(True)
        restarted=Phone(self.tmp.name)
        self.assertTrue(restarted.state['paused'])
        with self.assertRaises(PhoneError): await self.phone.key('HOME')
        with self.assertRaises(PhoneError): await self.phone.screenshot()
        self.phone._shell.assert_not_called()

    async def test_tap_requires_fresh_observation_and_invalidates(self):
        with self.assertRaises(PhoneError): await self.phone.tap(500,500)
        self.phone.screen=(1080,2376,time.monotonic()-31)
        with self.assertRaises(PhoneError): await self.phone.tap(500,500)
        self.phone.screen=(1080,2376,time.monotonic())
        await self.phone.tap(500,500)
        self.phone._shell.assert_awaited_once_with('input','tap',540,1188)
        self.assertIsNone(self.phone.screen)
        with self.assertRaises(PhoneError): await self.phone.tap(500,500)

    async def test_invalid_inputs_never_reach_adb(self):
        for value in (-1,1001,1.5,True,'5'):
            self.phone.screen=(1080,2376,time.monotonic())
            with self.assertRaises(PhoneError): await self.phone.tap(value,100)
        for text in ('','a\nb','a\x00b','x'*201):
            with self.assertRaises(PhoneError): await self.phone.type_ascii(text)
        for package in ('com.android.settings;reboot','foo','com.a/evil'):
            with self.assertRaises(PhoneError): await self.phone.open_app(package)
        with self.assertRaises(PhoneError): await self.phone.key('POWER')
        self.phone._shell.assert_not_called()

    async def test_rate_limit(self):
        self.phone.actions.extend([time.monotonic()-5]*20)
        with self.assertRaises(PhoneError): await self.phone.key('HOME')
        self.phone._shell.assert_not_called()

    async def test_swipe_batch_needs_only_one_observation_but_invalidates_taps(self):
        from unittest.mock import patch
        self.phone.screen=(1080,2376,time.monotonic())
        self.phone._assert_foreground=AsyncMock()
        with patch('astrbot_plugin_companion_phone.phone.asyncio.sleep',new_callable=AsyncMock),patch.object(self.phone,'_reserve_action') as reserve:
            message=await self.phone.swipe(500,780,500,320,550,allowed_packages={'com.coolapk.market'},repeat=5)
        self.assertEqual(self.phone._shell.await_count,5)
        self.assertEqual(reserve.call_count,5)
        self.assertIn('5',message)
        self.assertIsNone(self.phone.screen)
        with self.assertRaises(PhoneError):await self.phone.tap(500,500)

    async def test_swipe_batch_stops_on_pause_or_foreground_change(self):
        from unittest.mock import patch
        self.phone.screen=(1080,2376,time.monotonic())
        self.phone._assert_foreground=AsyncMock(side_effect=[None,None,PhoneError('left app')])
        with patch('astrbot_plugin_companion_phone.phone.asyncio.sleep',new_callable=AsyncMock),patch.object(self.phone,'_reserve_action'):
            with self.assertRaises(PhoneError):await self.phone.swipe(500,780,500,320,550,allowed_packages={'com.coolapk.market'},repeat=5)
        self.assertEqual(self.phone._shell.await_count,1)

    async def test_swipe_batch_rejects_invalid_repetition_before_adb(self):
        for repeat in (True,0,6,1.5,'3'):
            with self.assertRaises(PhoneError):await self.phone.swipe(500,780,500,320,550,repeat=repeat)
        self.phone._shell.assert_not_called()

    async def test_chinese_is_base64_not_shell_and_restores_ime(self):
        import base64
        self.phone.screen=(1080,2376,time.monotonic())
        original='com.baidu.input_vivo/.ImeVivoService'
        self.phone._input_field=AsyncMock(side_effect=[{'bounds':'[0,0][100,100]','text':''},{'text':'你好；$(id)'}])
        self.phone._shell.side_effect=[original,'ok','com.android.adbkeyboard/.AdbIME','tap ok','Broadcast completed','ok']
        await self.phone.type_ascii('你好；$(id)')
        args=self.phone._shell.await_args_list[4].args
        self.assertEqual(args[:6],('am','broadcast','-a','ADB_INPUT_B64','-p','com.android.adbkeyboard'))
        self.assertEqual(base64.b64decode(args[-1]).decode(),'你好；$(id)')
        self.assertEqual(self.phone._shell.await_args_list[-1].args,('ime','set',original))

    async def test_ime_restored_when_broadcast_fails(self):
        self.phone.screen=(1080,2376,time.monotonic())
        original='com.baidu.input_vivo/.ImeVivoService'
        self.phone._input_field=AsyncMock(return_value={'bounds':'[0,0][100,100]','text':''})
        self.phone._shell.side_effect=[original,'ok','com.android.adbkeyboard/.AdbIME','tap ok',PhoneError('offline'),'ok']
        with self.assertRaises(PhoneError): await self.phone.type_ascii('中文')
        self.assertEqual(self.phone._shell.await_args_list[-1].args,('ime','set',original))

    async def test_subprocess_timeout_kills_child(self):
        proc=SimpleNamespace(returncode=None,kill=lambda:None,communicate=AsyncMock(side_effect=[asyncio.TimeoutError(),(b'',b'')]))
        from unittest.mock import patch,Mock
        proc.kill=Mock()
        with patch('asyncio.create_subprocess_exec',AsyncMock(return_value=proc)):
            with self.assertRaises(PhoneError): await self.phone._run(['connect',PHONE_IP+':38605'])
        proc.kill.assert_called_once()

    async def test_stale_offline_transport_reconnected_once(self):
        self.phone._connect=Phone._connect.__get__(self.phone,Phone)
        self.phone._run=AsyncMock(side_effect=[
            b'already connected to '+self.phone.state['serial'].encode(),PhoneOffline('offline'),
            b'disconnected',b'connected to '+self.phone.state['serial'].encode(),b'device\n'])
        await self.phone._connect()
        self.assertEqual(self.phone._run.await_count,5)
        self.assertEqual(self.phone._run.await_args_list[2].args[0],['disconnect',self.phone.state['serial']])

    async def test_failed_action_is_not_replayed(self):
        self.phone.screen=(1080,2376,time.monotonic())
        self.phone._shell.side_effect=PhoneOffline('offline')
        with self.assertRaises(PhoneOffline): await self.phone.tap(500,500)
        self.phone._shell.assert_awaited_once()
        self.phone._connect.assert_not_called()
        self.assertIsNone(self.phone.screen)

    async def test_refused_port_recovers_paired_model_and_persists(self):
        self.phone._connect=Phone._connect.__get__(self.phone,Phone)
        self.phone._connect_once=AsyncMock(side_effect=[PhonePortChanged('refused'),None])
        self.phone._discover_ports=AsyncMock(return_value=[41965])
        self.phone._shell.return_value='V2046A'
        await self.phone._connect()
        self.assertEqual(self.phone.state['serial'],PHONE_IP+':41965')
        self.assertEqual(Phone(self.tmp.name).state['serial'],PHONE_IP+':41965')

    async def test_port_discovery_rejects_wrong_model(self):
        self.phone._discover_ports=AsyncMock(return_value=[41965])
        self.phone._connect_once=AsyncMock()
        self.phone._shell.return_value='different-device'
        with self.assertRaises(PhoneError): await self.phone._recover_port()
        self.assertTrue(self.phone.state['serial'].endswith(':38605'))

    async def test_port_discovery_is_throttled(self):
        self.phone.last_port_discovery=time.monotonic()
        self.phone._discover_ports=AsyncMock()
        with self.assertRaises(PhoneError): await self.phone._recover_port()
        self.phone._discover_ports.assert_not_called()

    async def test_capture_wakes_sleeping_screen_without_unlock(self):
        self.phone._screen_power=AsyncMock(side_effect=['Asleep','Awake'])
        self.phone._shell.side_effect=['','showing=false secure=false']
        from unittest.mock import patch
        with patch('asyncio.sleep',AsyncMock()):
            await self.phone._wake_for_capture()
        self.assertEqual(self.phone._shell.await_args_list[0].args,('input','keyevent',224))
        self.assertEqual(self.phone._shell.await_count,2)

    async def test_capture_stops_at_secure_lock(self):
        self.phone._screen_power=AsyncMock(side_effect=['Asleep','Awake'])
        self.phone._shell.side_effect=['','showing=true secure=true']
        from unittest.mock import patch
        with patch('asyncio.sleep',AsyncMock()):
            with self.assertRaisesRegex(PhoneError,'安全锁屏'):
                await self.phone._wake_for_capture()
        self.assertEqual(self.phone._shell.await_count,2)

    async def test_awake_capture_does_not_send_wake_key(self):
        self.phone._screen_power=AsyncMock(return_value='Awake')
        await self.phone._wake_for_capture()
        self.phone._shell.assert_not_called()

    def plugin(self):
        p=object.__new__(CompanionPhone)
        p.config={'enabled':True,'allow_model_screen':True}
        p.owner='100000001'
        p.umo='qq-companion:FriendMessage:100000001'
        p.phone=self.phone
        return p

    async def test_non_owner_and_group_blocked_before_call(self):
        p=self.plugin()
        fn=AsyncMock(return_value='must not execute')
        for e in (Event('1'),Event(origin='qq-companion:GroupMessage:100000001'),None):
            r=await p.invoke(e,fn,True)
            self.assertTrue(r.isError)
        fn.assert_not_called()

    async def test_per_event_operation_budget(self):
        p=self.plugin()
        e=Event()
        fn=AsyncMock(return_value='ok')
        for _ in range(12): self.assertFalse((await p.invoke(e,fn,True)).isError)
        self.assertTrue((await p.invoke(e,fn,True)).isError)
        self.assertEqual(fn.await_count,12)

    async def test_screen_recognition_requires_explicit_setting(self):
        p=self.plugin()
        p.config['allow_model_screen']=False
        fn=AsyncMock(return_value='must not execute')
        self.assertTrue((await p.invoke(Event(),fn,True)).isError)
        fn.assert_not_called()
        self.phone.screenshot=AsyncMock()
        r=await p.tool_screenshot(Event(),False)
        self.assertTrue(r.isError)
        self.phone.screenshot.assert_not_called()

    async def test_ordinary_chat_has_no_phone_authorization(self):
        p=self.plugin()
        e=Event()
        e.message_str='今天心情不错'
        fn=AsyncMock(return_value='must not execute')
        self.assertTrue((await p.invoke(e,fn)).isError)
        fn.assert_not_called()

    async def test_only_completed_manual_actions_get_activity_receipts(self):
        from astrbot_plugin_companion_phone.activity import ActivityLedger
        p=self.plugin();p.activity=ActivityLedger(self.phone.root)
        e=Event();fn=AsyncMock(return_value='done')
        self.assertFalse((await p.invoke(e,fn,True,activity='phone_open')).isError)
        import sqlite3
        with sqlite3.connect(p.activity.path) as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM receipts').fetchone()[0],1)
        fn.side_effect=PhoneError('not executed')
        self.assertTrue((await p.invoke(e,fn,True,activity='phone_input')).isError)
        with sqlite3.connect(p.activity.path) as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM receipts').fetchone()[0],1)

    def test_diary_generation_waits_until_awake(self):
        p=self.plugin()
        rhythm=SimpleNamespace(healthy=True,advance=lambda:None,state={'mode':'asleep'})
        p.autonomy=SimpleNamespace(rhythm=rhythm)
        self.assertFalse(p.diary_ready())
        rhythm.state['mode']='resting';self.assertFalse(p.diary_ready())
        rhythm.state['mode']='awake';self.assertTrue(p.diary_ready())
        rhythm.healthy=False;self.assertFalse(p.diary_ready())

    async def test_status_works_while_paused(self):
        p=self.plugin()
        self.phone.pause(True)
        self.phone._shell.side_effect=['V2046A','13','mWakefulness=Awake']
        r=await p.invoke(Event(),self.phone.status)
        self.assertFalse(r.isError)
        self.assertIn('已暂停',r.content[0].text)
        self.assertTrue(self.phone.state['paused'])

    async def test_autonomy_respects_global_model_disable(self):
        p=self.plugin()
        p.context=SimpleNamespace(get_config=lambda **kw:{'provider_settings':{'enable':False}},
            persona_manager=SimpleNamespace(get_persona=AsyncMock()))
        with self.assertRaises(PhoneError): await p.autonomy_model('check',None)
        p.context.persona_manager.get_persona.assert_not_called()

    async def test_autonomy_respects_removed_owner_whitelist(self):
        p=self.plugin()
        p.context=SimpleNamespace(get_config=lambda **kw:{'provider_settings':{'enable':True},
            'platform_settings':{'enable_id_white_list':True,'id_whitelist':[]}},
            persona_manager=SimpleNamespace(get_persona=AsyncMock()))
        with self.assertRaises(PhoneError): await p.autonomy_model('check',None)
        p.context.persona_manager.get_persona.assert_not_called()

    def test_tool_set_has_no_shell_or_file_access(self):
        self.assertEqual(len(TOOL_NAMES),12)
        self.assertIn('companion_qq_space',TOOL_NAMES)
        self.assertFalse(any('shell' in n or 'file' in n for n in TOOL_NAMES))

    async def test_natural_autonomous_request_uses_bounded_browser(self):
        p=self.plugin()
        p.autonomy=SimpleNamespace(now=AsyncMock(return_value='已完成浏览'))
        e=Event()
        e.message_str='现在你试试自主去玩玩手机'
        self.assertFalse(p.task_authorized(e))
        r=await p.tool_autonomous_browse(e)
        self.assertFalse(r.isError)
        p.autonomy.now.assert_awaited_once()

    async def test_autonomous_entry_rejects_ordinary_chat_and_negation(self):
        p=self.plugin()
        p.autonomy=SimpleNamespace(now=AsyncMock())
        e=Event()
        for text in ('今天心情如何','不要自己去玩手机','我需要在没有命令时它也可以主动玩手机'):
            e.message_str=text
            self.assertTrue((await p.tool_autonomous_browse(e)).isError)
        p.autonomy.now.assert_not_called()

    async def test_autonomous_entry_is_owner_only(self):
        p=self.plugin()
        p.autonomy=SimpleNamespace(now=AsyncMock())
        e=Event(owner='1')
        e.message_str='现在自己去玩手机'
        self.assertTrue((await p.tool_autonomous_browse(e)).isError)
        p.autonomy.now.assert_not_called()

    async def test_space_delegation_routes_without_phone_task(self):
        p=self.plugin()
        p.publisher=SimpleNamespace(consider=AsyncMock(return_value={'result':'chose_not_to_post'}))
        e=Event(); e.message_str='还在吗？要不要去尝试发一次动态，发什么全权在你，包括配图'
        self.assertFalse(p.task_authorized(e))
        self.assertTrue(qzone_request(e.message_str))
        r=await p.tool_qzone_publish(e,text='今天想留一句自己的话')
        self.assertFalse(r.isError)
        self.assertIn('chose_not_to_post',r.content[0].text)
        p.publisher.consider.assert_awaited_once()

    async def test_space_tool_once_per_turn_and_owner_only(self):
        p=self.plugin(); p.publisher=SimpleNamespace(consider=AsyncMock(return_value={'result':'verified'}))
        other=Event(owner='1'); other.message_str='去发一条空间动态'
        self.assertTrue((await p.tool_qzone_publish(other)).isError)
        p.publisher.consider.assert_not_called()
        e=Event(); e.message_str='去发一条空间动态'
        self.assertFalse((await p.tool_qzone_publish(e,text='今天想留一句自己的话')).isError)
        self.assertTrue((await p.tool_qzone_publish(e,text='另一句自己的话')).isError)
        p.publisher.consider.assert_awaited_once()

    async def test_space_rejected_draft_can_be_corrected_but_not_repeated(self):
        p=self.plugin();p.publisher=SimpleNamespace(consider=AsyncMock(side_effect=[
            {'result':'draft_rejected','reason':'正文太长'}, {'result':'verified'}]))
        e=Event();e.message_str='发一条空间动态'
        await p.tool_qzone_publish(e,text='第一份稿件')
        await p.tool_qzone_publish(e,text='第一份稿件')
        self.assertEqual(p.publisher.consider.await_count,1)
        await p.tool_qzone_publish(e,text='精简后的稿件')
        self.assertEqual(p.publisher.consider.await_count,2)
        self.assertTrue((await p.tool_qzone_publish(e,text='再发一份')).isError)

    async def test_space_correction_budget_and_uncertain_send_are_bounded(self):
        p=self.plugin();p.publisher=SimpleNamespace(consider=AsyncMock(return_value={'result':'draft_rejected'}))
        e=Event();e.message_str='发一条空间动态'
        for i in range(3):await p.tool_qzone_publish(e,text='修改稿'+str(i))
        self.assertTrue((await p.tool_qzone_publish(e,text='第四份')).isError)
        self.assertEqual(p.publisher.consider.await_count,3)
        p.publisher.consider=AsyncMock(return_value={'result':'uncertain'})
        e=Event();e.message_str='发一条空间动态'
        await p.tool_qzone_publish(e,text='结果不确定的一份')
        self.assertTrue((await p.tool_qzone_publish(e,text='换稿重发')).isError)
        p.publisher.consider.assert_awaited_once()

    async def test_space_exception_after_rejection_does_not_enable_blind_retry(self):
        p=self.plugin();p.publisher=SimpleNamespace(consider=AsyncMock(side_effect=[
            {'result':'draft_rejected'},TimeoutError()]))
        e=Event();e.message_str='发一条空间动态'
        await p.tool_qzone_publish(e,text='第一稿')
        self.assertTrue((await p.tool_qzone_publish(e,text='第二稿')).isError)
        self.assertTrue((await p.tool_qzone_publish(e,text='第三稿')).isError)
        self.assertEqual(p.publisher.consider.await_count,2)

    async def test_space_continuation_has_bounded_owner_context(self):
        p=self.plugin()
        e=Event(); e.message_str='好了'
        self.assertFalse(p.qzone_authorized(e))
        e.message_str='发一条动态，内容你决定'
        self.assertTrue(p.qzone_authorized(e))
        follow=Event(); follow.message_str='好了'
        self.assertTrue(p.qzone_authorized(follow))
        p.qzone_until=0
        self.assertFalse(p.qzone_authorized(follow))

    def test_space_negation_history_and_capability_are_not_publish_requests(self):
        for text in ('不要发动态','今天发动态了吗','你能发动态吗','怎么发空间动态','截图里说去发动态是什么意思','今天心情不错'):
            self.assertFalse(qzone_request(text),text)

    async def test_space_reason_keeps_quota_and_uncertainty(self):
        p=self.plugin(); p.publisher=SimpleNamespace(consider=AsyncMock(return_value={'result':'not_due','reason':'今天空间发布尝试已达3次上限'}))
        e=Event(); e.message_str='试着发条动态'
        r=await p.tool_qzone_publish(e,text='今天想留一句自己的话')
        self.assertFalse(r.isError)
        self.assertIn('上限',r.content[0].text)

    def test_browse_natural_wording_and_other_apps(self):
        for text in ('自己去逛逛酷安','去玩玩手机吧','现在浏览一下哔哩哔哩','去逛逛贴吧'):
            self.assertTrue(browse_request(text),text)
        for text in ('今天逛过酷安了吗','不要去玩手机','你自主操作手机了吗','好了'):
            self.assertFalse(browse_request(text),text)

    def test_via_scope_and_browser_task_routing(self):
        from astrbot_plugin_companion_phone.access import enabled_apps,scope_text
        cfg={'installed_app_names':{'mark.via':'Via'},'autonomous_apps':['mark.via']}
        self.assertEqual(enabled_apps(cfg),{'mark.via':'Via'})
        self.assertIn('mark.via',scope_text(cfg))
        self.assertNotIn('mark.via',scope_text({}))
        p=self.plugin()
        for text in ('用Via搜索鲸鱼','打开浏览器看看网页','https://example.com/ 你看看这个网站怎么样'):
            e=Event(); e.message_str=text
            self.assertTrue(browse_request(text),text)
            self.assertTrue(p.task_authorized(e),text)
        for text in ('不要打开Via','今天浏览网页了吗'):
            self.assertFalse(browse_request(text),text)

    def test_phone_readiness_and_contextual_retry_are_manual_tasks(self):
        p=self.plugin()
        e=Event();e.message_str='你现在能控制手机了吗'
        self.assertTrue(p.task_authorized(e))
        for text in ('试试看吧','再试试','那就试一下','你再试一次'):
            followup=Event();followup.message_str=text
            self.assertTrue(p.task_authorized(followup),text)
        explicit=Event();explicit.message_str='你走手动任务试试吧，修好了明天我不在你就也能玩手机了'
        self.assertTrue(p.task_authorized(explicit))
        autonomous=Event();autonomous.message_str='你试试自主去玩玩手机'
        self.assertFalse(p.task_authorized(autonomous))

    def test_retry_does_not_create_a_phone_task_without_recent_context(self):
        for until in (0,time.monotonic()-1):
            p=self.plugin();p.task_until=until
            e=Event();e.message_str='试试看吧'
            self.assertFalse(p.task_authorized(e))

    async def test_retry_task_keeps_owner_sleep_and_screen_consent_checks(self):
        p=self.plugin();p.task_until=time.monotonic()+60
        e=Event();e.message_str='试试看吧'
        other=Event(owner='12345');other.message_str=e.message_str
        with self.assertRaises(PhoneError):p.guard(other)
        p.autonomy=SimpleNamespace(rhythm=SimpleNamespace(advance=lambda:None,state={'mode':'asleep'}))
        with self.assertRaises(PhoneError):p.guard(e)
        p.autonomy.rhythm.state['mode']='awake'
        p.config['allow_model_screen']=False
        with self.assertRaises(PhoneError):p.guard(e,action=True)

    async def test_standing_initiative_needs_no_publish_phrase(self):
        p=self.plugin(); p.config.update(initiative_enabled=True,qzone_enabled=True)
        p.publisher=SimpleNamespace(consider=AsyncMock(return_value={'result':'chose_not_to_post'}))
        e=Event(); e.message_str='我就想看看你想发什么嘛，你没有什么想和大家说的吗'
        self.assertTrue(p.qzone_authorized(e))
        r=await p.tool_qzone_publish(e,text='今天想留一句自己的话')
        self.assertFalse(r.isError)
        p.publisher.consider.assert_awaited_once_with(spontaneous=True,draft={'publish':True,'text':'今天想留一句自己的话','image_source':'none','image_id':'','image_query':''})

    async def test_space_requires_own_draft_not_another_generator(self):
        p=self.plugin();p.config.update(initiative_enabled=True,qzone_enabled=True)
        p.publisher=SimpleNamespace(consider=AsyncMock())
        e=Event();e.message_str='你自己决定'
        self.assertTrue((await p.tool_qzone_publish(e)).isError)
        p.publisher.consider.assert_not_called()

    async def test_space_image_must_be_previewed_and_exact_id_is_forwarded(self):
        p=self.plugin();p.config.update(initiative_enabled=True,qzone_enabled=True)
        p.publisher=SimpleNamespace(consider=AsyncMock(return_value={'result':'verified'}))
        e=Event();e.message_str='你自己决定'
        self.assertTrue((await p.tool_qzone_publish(e,text='今天想留一句自己的话',image_id='media:chosen')).isError)
        e.set_extra('companion_media_previewed',['media:chosen'])
        self.assertFalse((await p.tool_qzone_publish(e,text='今天想留一句自己的话',image_id='media:chosen')).isError)
        self.assertEqual(p.publisher.consider.call_args.kwargs['draft']['image_id'],'media:chosen')

    async def test_standing_initiative_still_rejects_other_owner_and_stop(self):
        p=self.plugin(); p.config.update(initiative_enabled=True,qzone_enabled=True)
        p.publisher=SimpleNamespace(consider=AsyncMock())
        for e in (Event(owner='1'),Event()):
            e.message_str='先别发动态'
            self.assertTrue((await p.tool_qzone_publish(e)).isError)
        p.publisher.consider.assert_not_called()

    async def test_standing_browse_uses_schedule_not_forced_task(self):
        p=self.plugin(); p.config['initiative_enabled']=True
        p.autonomy=SimpleNamespace(now=AsyncMock(return_value='等待合适时机，无需命令'))
        e=Event(); e.message_str='你今天有什么想做的'
        self.assertFalse((await p.tool_autonomous_browse(e)).isError)
        p.autonomy.now.assert_awaited_once_with(owner_requested=False)

    def test_not_wanting_to_force_post_is_not_stop_command(self):
        from astrbot_plugin_companion_phone.intents import publishing_declined
        for text in ('你不想发也行','不用问我发什么','要不要发条动态','我想看你自己决定'):
            self.assertFalse(publishing_declined(text),text)
        for text in ('先别发了','不要发动态','暂停空间'):
            self.assertTrue(publishing_declined(text),text)


if __name__=='__main__': unittest.main(verbosity=2)

