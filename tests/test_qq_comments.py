"""QQ comment consent and observed UI boundaries; no real phone or sends."""
import json,os,sys,tempfile,time,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock,Mock
sys.path.insert(0,os.environ.get('COMPANION_TEST_PLUGINS','/AstrBot/data/plugins'))
from astrbot_plugin_companion_phone.phone import Phone
from astrbot_plugin_companion_phone.autonomy import AutonomousBrowser
from astrbot_plugin_companion_phone.access import scope_text

QQ='com.tencent.mobileqq'
class QQComments(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.p=Phone(self.tmp.name)
        self.cfg={'rhythm_enabled':False,'qq_comment_consent':True,'autonomous_apps':[QQ],'autonomous_like_consent':True}
        self.a=AutonomousBrowser(self.p,self.cfg,AsyncMock(),lambda:'')
        self.p._assert_foreground=AsyncMock()
        self.activity='mResumedActivity: ActivityRecord{ com.tencent.mobileqq/com.qzone.feed.ui.QzoneActivity }'
        self.p._shell=AsyncMock(side_effect=lambda *args:self.activity if args[0]=='dumpsys' else
            '<hierarchy><node text="今天读到有趣的内容" class="android.widget.TextView" /><node text="发送" bounds="[900,1800][1000,1900]" /></hierarchy>')
        self.p.screen=(1080,2376,time.monotonic())
        self.p.screenshot=AsyncMock(return_value=(b'current-screen',(1080,2376),(540,1188)))
        self.field={'resource-id':'com.tencent.mobileqq:id/comment_input','class':'android.widget.EditText','bounds':'[0,0][100,100]','text':'回复评论'}
        self.p._input_field=AsyncMock(side_effect=lambda:dict(self.field))
        self.p.type_ascii=AsyncMock(side_effect=lambda text,**kw:self.field.update(text=text))
        self.p.target_info=AsyncMock(return_value={'text':'发送','clickable':'true'})
        self.p.tap=AsyncMock()
        self.a.journal_model=AsyncMock(return_value='{"safe":true,"is_qzone_comment":true,"public_comment_mode":true,"purchase_dialog":false}')
        self.a.publisher=SimpleNamespace(private_overlap=Mock(return_value=False))

    async def prepare(self):
        return await self.a.interactive_action(QQ,{'action':'comment_input','text':'这个想法挺有意思','target_quote':'今天读到有趣的内容'})

    async def send(self):
        return await self.a.interactive_action(QQ,{'action':'comment_send','x':900,'y':900})

    async def test_comment_input_then_observed_send_and_no_repeat(self):
        self.assertTrue(await self.prepare());self.p.tap.assert_not_called()
        self.assertTrue(await self.send());self.p.tap.assert_awaited_once()
        self.assertFalse(await self.send())
        self.assertEqual(self.a.state['qq_comment_attempts'][0]['result'],'uncertain')
        self.assertNotIn('text',self.a.state['qq_comment_attempts'][0])

    async def test_not_granted_or_other_app_is_denied(self):
        self.cfg['qq_comment_consent']=False;self.assertFalse(await self.prepare())
        self.cfg['qq_comment_consent']=True
        self.assertFalse(await self.a.interactive_action('com.coolapk.market',{'action':'comment_input','text':'你好'}))
        self.p.type_ascii.assert_not_called()

    async def test_qq_private_chat_and_search_are_denied(self):
        self.activity='mResumedActivity: ActivityRecord{ com.tencent.mobileqq/.ChatActivity }'
        self.assertFalse(await self.prepare())
        self.activity='mResumedActivity: ActivityRecord{ com.tencent.mobileqq/com.qzone.QzoneActivity }'
        self.field.update(text='搜索评论',**{'resource-id':'search_input'})
        self.assertFalse(await self.prepare())
        self.p.type_ascii.assert_not_called()

    async def test_no_draft_or_changed_field_or_changed_text_blocks_send(self):
        self.assertFalse(await self.send())
        self.assertTrue(await self.prepare())
        self.field['text']='不同内容';self.assertFalse(await self.send())
        self.field['text']='这个想法挺有意思';self.field['bounds']='[50,50][100,100]'
        self.assertFalse(await self.send());self.p.tap.assert_not_called()

    async def test_unobserved_comment_target_is_denied(self):
        self.assertFalse(await self.a.interactive_action(QQ,{'action':'comment_input','text':'你好','target_quote':'不存在的评论对象'}))
        self.assertTrue(await self.prepare())
        self.p._shell=AsyncMock(side_effect=lambda *args:self.activity if args[0]=='dumpsys' else '<hierarchy/>')
        self.assertFalse(await self.send());self.p.tap.assert_not_called()

    async def test_vivo_top_resumed_opaque_comment_field_and_visual_target(self):
        self.cfg['allow_model_screen']=True
        self.activity='topResumedActivity=ActivityRecord{ com.tencent.mobileqq/com.qzone.reborn.comment.activity.QZoneCommentPanelActivity }'
        self.field.update(text='说点什么吧...',**{'resource-id':'com.tencent.mobileqq:id/zsb'})
        self.p._shell=AsyncMock(side_effect=lambda *args:self.activity if args[0]=='dumpsys' else '<hierarchy/>')
        self.p.screenshot=AsyncMock(return_value=(b'current-screen',(1080,2376),(540,1188)))
        self.a.journal_model.side_effect=['{"is_qzone_comment":true,"target_visible":true}','{"safe":true}']
        ok=await self.a.qq_comment_action(QQ,{'action':'comment_input','text':'这个想法挺有意思','target_quote':'今天读到有趣的内容'},dry_run=True)
        self.assertTrue(ok)
        self.assertEqual(self.a.state['qq_comment_last_check']['code'],'ready_dry_run')
        self.p.type_ascii.assert_not_called();self.p.tap.assert_not_called()
        self.assertNotIn('qq_comment_draft',self.a.state)

    async def test_visual_target_failure_reports_specific_reason(self):
        self.cfg['allow_model_screen']=True
        self.p._shell=AsyncMock(side_effect=lambda *args:self.activity if args[0]=='dumpsys' else '<hierarchy/>')
        self.p.screenshot=AsyncMock(return_value=(b'current-screen',(1080,2376),(540,1188)))
        self.a.journal_model.return_value='{"is_qzone_comment":false,"target_visible":false}'
        self.assertFalse(await self.prepare())
        self.assertEqual(self.a.state['qq_comment_last_check']['code'],'target_not_visible')
        self.p.type_ascii.assert_not_called()

    async def test_keyboard_hidden_target_uses_same_page_binding_and_native_send(self):
        self.assertTrue(await self.prepare())
        self.p._shell=AsyncMock(side_effect=lambda *args:self.activity if args[0]=='dumpsys' else
            '<hierarchy><node text="发送" bounds="[900,1800][1000,1900]" /></hierarchy>')
        self.assertTrue(await self.send())
        args=self.p.tap.call_args.args
        self.assertEqual(args,(880,779)) # Real button centre, not model's 900,900.

    async def test_private_or_purchase_mode_prevents_send(self):
        self.assertTrue(await self.prepare())
        self.a.journal_model.return_value='{"is_qzone_comment":true,"public_comment_mode":false,"purchase_dialog":true}'
        self.assertFalse(await self.send());self.p.tap.assert_not_called()
        self.assertEqual(self.a.state['qq_comment_last_check']['code'],'private_or_paid_mode')

    async def test_private_controls_not_allowed_as_navigation(self):
        for label in ('私密','黄钻','开通黄钻','会员'):
            self.p.target_info.return_value={'text':label}
            self.assertFalse(await self.a.interactive_action(QQ,{'action':'tap','intent':'navigate','x':900,'y':500}))
        self.p.tap.assert_not_called()

    async def test_existing_draft_is_not_appended_or_retyped(self):
        self.assertTrue(await self.prepare())
        await self.prepare()
        self.p.type_ascii.assert_awaited_once()
        self.assertFalse(await self.a.interactive_action(QQ,{'action':'comment_input','text':'另一份稿件','target_quote':'今天读到有趣的内容'}))
        self.assertEqual(self.a.state['qq_comment_last_check']['code'],'draft_already_in_input')

    async def test_private_or_reviewer_rejected_text_does_not_input(self):
        self.a.publisher.private_overlap.return_value=True;self.assertFalse(await self.prepare())
        self.a.publisher.private_overlap.return_value=False;self.a.journal_model.return_value='{"safe":false}'
        self.assertFalse(await self.prepare());self.p.type_ascii.assert_not_called()

    async def test_pause_or_sleep_blocks_comment(self):
        self.p.state['paused']=True;self.assertFalse(await self.prepare())
        self.p.state['paused']=False
        self.a.rhythm=SimpleNamespace(advance=Mock(),healthy=True,state={'mode':'asleep'})
        self.assertFalse(await self.prepare());self.p.type_ascii.assert_not_called()

    async def test_consent_revoked_during_review_stops_input(self):
        async def revoke(*args):self.cfg['qq_comment_consent']=False;return '{"safe":true}'
        self.a.journal_model=AsyncMock(side_effect=revoke)
        self.assertFalse(await self.prepare());self.p.type_ascii.assert_not_called()

    async def test_uncertain_tap_is_reserved_before_timeout(self):
        self.assertTrue(await self.prepare());self.p.tap.side_effect=TimeoutError()
        with self.assertRaises(TimeoutError):await self.send()
        self.assertFalse(await self.send())
        self.assertEqual(len(self.a.state['qq_comment_attempts']),1)

    async def test_comment_daily_quota_and_spacing(self):
        from datetime import datetime
        from astrbot_plugin_companion_phone.autonomy import TZ
        day=datetime.now(TZ).date().isoformat()
        self.a.state['qq_comment_attempts']=[{'day':day,'at':time.time()-120,'hash':str(i),'result':'uncertain'} for i in range(10)]
        self.assertFalse(await self.prepare())
        self.a.state['qq_comment_attempts']=[{'day':day,'at':time.time(),'hash':'old','result':'uncertain'}]
        self.assertFalse(await self.prepare());self.p.type_ascii.assert_not_called()

    def test_scope_declares_qq_only(self):
        text=scope_text(self.cfg)
        self.assertIn('QQ空间已允许',text)
        self.assertIn('其他应用评论仍未开放',text)

if __name__=='__main__':unittest.main()
