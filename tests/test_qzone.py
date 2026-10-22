"""Publication boundaries and journal separation. No external I/O."""
import asyncio
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock,Mock,patch
sys.path.insert(0,os.environ.get('COMPANION_TEST_PLUGINS','/AstrBot/data/plugins'))
from astrbot_plugin_companion_phone.books import Books
from astrbot_plugin_companion_phone.publisher import Publisher
from astrbot_plugin_companion_phone.qzone_client import BOT_QQ,QzoneClient,QzoneError,parse
from astrbot_plugin_companion_phone import quiet
from astrbot_plugin_companion_phone.public_images import PublicHTTPS,public_bytes


class QzoneTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name); self.books=Books(self.root)
        self.auth=AsyncMock(return_value=('p_skey=test; uin=o'+BOT_QQ,BOT_QQ))
        self.model=AsyncMock(return_value=json.dumps({'publish':True,'text':'想了一会儿，还是蓝色顺眼。','image_query':''}))
        self.p=Publisher(self.root,{'qzone_enabled':True},self.auth,self.model,self.books,lambda:True)
        self.source={'id':'phone:real','app':'酷安','views':1,'note':'技术话题','public_content':True}

    def test_private_book_never_in_public_read(self):
        self.assertTrue(self.books.write('private','只写在这里的想法',self.source['id'],[self.source]))
        self.assertEqual(self.books.read('public'),[])
        self.assertEqual(len(self.books.read('private')),1)

    def test_no_public_diary_from_qq_or_non_observation(self):
        for source in ({**self.source,'app':'QQ'},{**self.source,'views':0}):
            self.assertFalse(self.books.write('public','今天想点什么',source['id'],[source]))

    def test_public_diary_real_evidence_and_duplicate(self):
        self.assertTrue(self.books.write('public','这个思路还挺有意思',self.source['id'],[self.source]))
        self.assertFalse(self.books.write('public','不同文案',self.source['id'],[self.source]))

    def test_wrong_account_rejected(self):
        for cookie,qq in [('p_skey=test','123'),('p_skey=test; uin=o123',BOT_QQ),('skey=x',BOT_QQ)]:
            with self.assertRaises(QzoneError): QzoneClient(cookie,qq)

    def test_fs_html_callback_with_extra_braces(self):
        raw=b'<script>function deliver(){frameElement.callback({"code":0,"t1_tid":"real-id"});}</script>'
        self.assertEqual(parse(raw)['t1_tid'],'real-id')

    def test_max_five_and_corrupt_state_fail_closed(self):
        self.p.state['attempts']=[{'day':__import__('datetime').datetime.now(__import__('zoneinfo').ZoneInfo('Asia/Shanghai')).date().isoformat(),
             'at':time.time()-8000,'result':'verified'}]*5
        self.assertFalse(self.p.permitted())
        self.p.path.write_text('{broken')
        other=Publisher(self.root,{'qzone_enabled':True},self.auth,self.model,self.books,lambda:True)
        self.assertFalse(other.permitted())

    def test_four_posts_and_recent_verified_post_allow_another(self):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        day = datetime.now(ZoneInfo('Asia/Shanghai')).date().isoformat()
        self.p.state['attempts'] = [{'day':day,'at':time.time(),'result':'verified'} for _ in range(4)]
        self.assertTrue(self.p.permitted())
        self.assertEqual(self.p.status()['daily_limit'],5)
        self.p.state['attempts'].append({'day':day,'at':time.time(),'result':'verified'})
        self.assertFalse(self.p.permitted())

    def test_uncertain_survives_restart_and_blocks_new_publication(self):
        self.p.state['attempts']=[{'day':'old','at':1,'result':'uncertain','text':'可能已发出','hash':'x'}]
        self.p.save()
        other=Publisher(self.root,{'qzone_enabled':True},self.auth,self.model,self.books,lambda:True)
        self.assertFalse(other.permitted())

    async def test_private_not_in_public_generator_prompt(self):
        self.books.write('private','PRIVATE_SENTINEL',self.source['id'],[self.source])
        self.model.return_value='{"publish":false}'
        await self.p.consider()
        self.assertNotIn('PRIVATE_SENTINEL',self.model.call_args.args[0])
        self.auth.assert_not_called()

    async def test_spontaneous_consideration_has_persistent_cooldown(self):
        self.model.return_value='{"publish":false}'
        first=await self.p.consider(spontaneous=True)
        self.assertEqual(first['result'],'chose_not_to_post')
        second=await self.p.consider(spontaneous=True)
        self.assertEqual(second['result'],'consider_cooldown')
        other=Publisher(self.root,{'qzone_enabled':True},self.auth,self.model,self.books,lambda:True)
        self.assertEqual((await other.consider(spontaneous=True))['result'],'consider_cooldown')
        self.assertEqual(self.model.await_count,1)

    async def test_explicit_request_can_consider_without_resetting_publish_count(self):
        self.model.return_value='{"publish":false}'
        await self.p.consider(spontaneous=True)
        self.assertEqual((await self.p.consider())['result'],'chose_not_to_post')
        self.assertEqual(self.p.used(),0)
        self.assertEqual(self.model.await_count,2)

    async def test_pause_during_model_prevents_send(self):
        async def model(*args):
            self.p.state['paused']=True
            return '{"publish":true,"text":"今天蓝色很顺眼"}'
        self.p.model=model
        result=await self.p.consider()
        self.assertEqual(result['result'],'cancelled_before_publish')
        self.auth.assert_not_called()

    async def test_real_send_reserves_before_io_and_verifies(self):
        client=Mock()
        text='想了一会儿，还是蓝色顺眼。'
        client.recent.side_effect=[[],[{'tid':'real-id','text':text,'visible':1}]]
        def publish(*args):
            saved=json.loads(self.p.path.read_text())
            self.assertEqual(saved['attempts'][-1]['result'],'reserved')
            return 'real-id'
        client.publish.side_effect=publish
        with patch('astrbot_plugin_companion_phone.publisher.QzoneClient',return_value=client),patch('astrbot_plugin_companion_phone.publisher.asyncio.sleep',AsyncMock()):
            result=await self.p.consider()
        self.assertEqual(result['result'],'verified')
        self.assertEqual(result['visible_readback'],1)
        self.assertEqual(self.p.used(),1)
        self.assertTrue(self.p.permitted())
        self.assertEqual(result['published_text'],text)
        self.assertEqual(result['authored_text'],text)
        self.assertEqual(result['authored_by'],'background_agent')
        self.assertTrue((self.root/'qzone-publications.jsonl').exists())

    async def test_authored_text_is_sent_verbatim_and_receipt_contains_it(self):
        text='折腾了很久，终于能自己说句话了\n我在，别急着关掉'
        self.model.return_value=json.dumps({'safe':True,'text':'审查器企图替换的文案'})
        client=Mock();client.publish.return_value='authored-id'
        client.recent.side_effect=[[],[{'tid':'authored-id','text':text,'visible':1}]]
        with patch('astrbot_plugin_companion_phone.publisher.QzoneClient',return_value=client),patch('astrbot_plugin_companion_phone.publisher.asyncio.sleep',AsyncMock()):
            report=await self.p.consider(draft={'publish':True,'text':text,'image_source':'none'})
        client.publish.assert_called_once_with(text,None)
        self.assertEqual(report['published_text'],text)
        self.assertEqual(report['authored_text'],text)
        self.assertEqual(report['authored_by'],'chat_agent')
        self.assertEqual(self.model.await_count,1) # privacy review only, no second writer
        saved=json.loads((self.root/'qzone-publications.jsonl').read_text())
        self.assertEqual(saved['published_text'],text)

    async def test_rejected_draft_is_not_replaced_or_published(self):
        self.model.return_value='{"safe":false,"text":"替代稿"}'
        report=await self.p.consider(draft={'publish':True,'text':'这是不适合公开的一句话','image_source':'none'})
        self.assertEqual(report['result'],'draft_rejected')
        self.auth.assert_not_called()
        self.assertEqual(self.p.used(),0)

    async def test_private_diary_overlap_rejected_locally(self):
        secret='这一句只应保留在私密日记里面不要公开'
        self.books.write('private',secret,self.source['id'],[self.source])
        report=await self.p.consider(draft={'publish':True,'text':secret,'image_source':'none'})
        self.assertEqual(report['result'],'draft_rejected')
        self.model.assert_not_called()
        self.auth.assert_not_called()

    async def test_selected_image_failure_does_not_publish_text_substitute(self):
        self.model.side_effect=['{"safe":true}','{"suitable":false}']
        self.p.media=SimpleNamespace(recent=lambda **k:[],asset=Mock(),preview_bytes=lambda _:b'fake')
        report=await self.p.consider(draft={'publish':True,'text':'这句话想配刚才选好的那张图','image_source':'library','image_id':'media:chosen'})
        self.assertEqual(report['result'],'image_unavailable')
        self.auth.assert_not_called()
        self.assertEqual(self.p.used(),0)

    async def test_timeout_not_retried(self):
        client=Mock(); client.recent.return_value=[]; client.publish.side_effect=TimeoutError()
        with patch('astrbot_plugin_companion_phone.publisher.QzoneClient',return_value=client):
            result=await self.p.consider()
        self.assertEqual(result['result'],'uncertain')
        client.publish.assert_called_once()
        self.assertFalse(self.p.permitted())
        self.assertIsNone(result['published_text'])
        self.assertEqual(result['attempted_text'],'想了一会儿，还是蓝色顺眼。')

    async def test_reconcile_confirms_old_post_without_resending(self):
        self.p.state['attempts']=[{'at':time.time(),'day':'2026-10-20','hash':'hash','text':'已发出去的内容','result':'uncertain'}]
        client=Mock(); client.recent.return_value=[{'tid':'server-id','text':'已发出去的内容','visible':1}]
        with patch('astrbot_plugin_companion_phone.publisher.QzoneClient',return_value=client):
            await self.p.reconcile()
        self.assertEqual(self.p.state['attempts'][0]['result'],'verified')
        self.assertEqual(self.p.state['last_result'],'verified')
        client.publish.assert_not_called()
        self.p.archive_verified(self.p.state['attempts'][0])
        self.assertEqual(len((self.root/'qzone-publications.jsonl').read_text().splitlines()),1)

    def test_rich_text_readback_preserves_newlines_and_links(self):
        text='我叫岚\n访客入口 https://203.0.113.10/\n可以看看公开日记'
        row={'content':text.replace('\n',''),'conlist':[{'type':2,'con':'我叫岚\n访客入口 '},
            {'type':1,'url':'https://203.0.113.10/'},{'type':2,'con':'\n可以看看公开日记'}],
            'tid':'rich-id','ugc_right':1,'pictotal':1,'has_more_con':0}
        client=QzoneClient('p_skey=fake; uin=o'+BOT_QQ,BOT_QQ);client.request=Mock(return_value={'msglist':[row]})
        result=client.recent()[0]
        self.assertEqual(result['text'],text);self.assertEqual(result['image_count'],1)
        self.assertEqual(client.request.call_args.args[1]['not_trunc_con'],1)
        attempt={'text':text,'tid':'rich-id','image':{'origin':'album'}}
        self.assertTrue(self.p.confirm_readback(attempt,[result]))
        receipt=self.p.receipt(attempt)
        self.assertEqual(receipt['published_text'],text)
        self.assertFalse(receipt['audience_visibility_confirmed'])
        self.assertEqual(receipt['verification_scope'],'own_account')

    def test_unknown_or_truncated_rich_text_is_not_silently_accepted(self):
        for row in ({'content':'fallback','conlist':[{'type':99,'con':'missing'}]},
                    {'content':'fallback','conlist':[{'type':2,'con':'partial'}],'has_more_con':1}):
            self.assertEqual(QzoneClient.post_text(row),'fallback')

    def test_readback_requires_same_tid_and_actual_text(self):
        row={'text':'原文\n下一行','tid':'expected'}
        self.assertFalse(self.p.confirm_readback(row,[{'tid':'other','text':row['text']}]))
        self.assertFalse(self.p.confirm_readback(row,[{'tid':'expected','text':'原文下一行'}]))
        self.assertEqual(row['readback_status'],'content_mismatch')
        self.assertNotEqual(row.get('result'),'verified')

    def test_readback_missing_selected_image_is_not_verified(self):
        row={'text':'原文','tid':'expected','image':{'origin':'album'}}
        self.assertFalse(self.p.confirm_readback(row,[{'tid':'expected','text':'原文','image_count':0}]))
        self.assertEqual(row['readback_status'],'image_missing')

    def test_server_collapsed_blank_lines_reports_actual_text(self):
        original='我叫岚\n\n访客入口 https://203.0.113.10/\n\n公开日记'
        actual=original.replace('\n\n','\n')
        row={'text':original,'tid':'expected'}
        self.assertTrue(self.p.confirm_readback(row,[{'tid':'expected','text':actual,'visible':1}]))
        receipt=self.p.receipt(row)
        self.assertEqual(receipt['published_text'],actual)
        self.assertEqual(receipt['attempted_text'],original)
        self.assertTrue(receipt['formatting_adjusted'])
        for different in (actual.replace('公开日记','私密日记'),actual.replace('岚','小鲸鱼'),actual.replace(' ','')):
            self.assertFalse(self.p.confirm_readback({'text':original,'tid':'expected'},[{'tid':'expected','text':different}]))

    def test_owner_reported_missing_is_not_overridden_by_own_readback(self):
        row={'day':'test','result':'verified','text':'原文','tid':'expected','audience_status':'owner_reported_missing','audience_observed_at':123}
        self.assertTrue(self.p.confirm_readback(row,[{'tid':'expected','text':'原文','visible':1}]))
        receipt=self.p.receipt(row)
        self.assertEqual(receipt['audience_status'],'owner_reported_missing')
        self.assertFalse(receipt['audience_visibility_confirmed'])
        self.assertIn('公开可见性未达成',receipt['verification_note'])
        self.p.state['attempts']=[row]
        self.assertEqual(self.p.status()['latest_audience_status'],'owner_reported_missing')

    def test_official_platform_notice_overrides_own_public_rights(self):
        row={'text':'原文','tid':'restricted','audience_status':'platform_restricted',
             'platform_notice':'内容涉嫌违规无法分享'}
        self.assertTrue(self.p.confirm_readback(row,[{'tid':'restricted','text':'原文','visible':1}]))
        receipt=self.p.receipt(row)
        self.assertFalse(receipt['audience_visibility_confirmed'])
        self.assertIn('平台内容限制已确认',receipt['verification_note'])
        self.assertEqual(receipt['platform_notice'],'内容涉嫌违规无法分享')

    async def test_delayed_readback_recovers_without_republishing(self):
        row={'at':time.time(),'day':'today','text':'等一会儿才出现','tid':'late-id','result':'uncertain','hash':'x'}
        self.p.state['attempts']=[row]
        client=Mock();client.recent.side_effect=[[],[{'tid':'late-id','text':row['text'],'visible':1}]]
        with patch('astrbot_plugin_companion_phone.publisher.QzoneClient',return_value=client):
            await self.p.reconcile_pending()
            self.assertEqual(row['result'],'uncertain')
            await self.p.reconcile_pending()
            self.assertEqual(client.recent.call_count,1)
            self.p.state['last_reconcile_at']-=61
            await self.p.reconcile_pending()
        self.assertEqual(row['result'],'verified')
        client.publish.assert_not_called()
        self.assertEqual(self.p.used(),0) # fixture day differs; no new reservation

    async def test_pending_check_does_not_race_active_publication(self):
        self.p.state['attempts']=[{'at':time.time(),'result':'uncertain'}]
        async with self.p.lock:await self.p.reconcile_pending()
        self.auth.assert_not_called()

    def test_private_details_rejected_in_draft(self):
        for text in ('主人QQ是100000001','私密日记内容','https://example.com','验证码123456'):
            self.assertFalse(self.p.safe_text(text))

    def test_owner_public_nickname_and_relationship_are_allowed(self):
        for text in ('F1nger是我的管理员，也是帮忙搭建我的朋友',
                     '有个朋友叫F1nger，帮忙搭建了这里', '我的管理员网名叫F1nger'):
            self.assertTrue(self.p.safe_text(text),text)
        for text in ('F1nger的QQ是100000001', '管理员验证码123456', 'F1nger的密码在这里'):
            self.assertFalse(self.p.safe_text(text),text)

    async def test_owner_nickname_does_not_bypass_privacy_review(self):
        self.model.return_value='{"safe":false}'
        r=await self.p.consider(draft={'publish':True,'text':'F1nger在私聊里偷偷告诉我他的秘密', 'image_source':'none'})
        self.assertEqual(r['result'],'draft_rejected')
        self.auth.assert_not_called()
        self.assertIn('真实姓名',self.model.call_args.args[0])

    def test_public_visitor_url_and_chat_capability_are_allowed(self):
        text='我是岚，可以和好友私聊啦。公开档案和公开日记在 https://203.0.113.10/ ，访客就能看'
        self.assertTrue(self.p.safe_text(text))
        for url in ('https://203.0.113.10/api/memories','https://203.0.113.10/?token=abc',
                    'https://203.0.113.10.evil.example/','https://203.0.113.10@evil.example/',
                    'https://203.0.113.10/#secret','http://203.0.113.10/',
                    'https://203.0.113.10:6185/','203.0.113.10/api/memories',
                    '203.0.113.10:6185','203.0.113.10?token=abc','203.0.113.10.evil.example'):
            self.assertFalse(self.p.safe_text('去这里 '+url),url)

    def test_explicitly_public_address_can_be_plain_text_or_omitted(self):
        for text in ('公开档案在203.0.113.10，有空可以看看','公开入口 203.0.113.10/',
                     '我叫岚，想聊可以来找我，这次只介绍自己'):
            self.assertTrue(self.p.safe_text(text),text)
        for text in ('地址 192.168.1.1','地址 203.0.113.10#secret','地址 user@203.0.113.10'):
            self.assertFalse(self.p.safe_text(text),text)

    async def test_rejected_authored_draft_does_not_spend_cooldown(self):
        bad={'publish':True,'text':'看看 https://example.com/','image_source':'none'}
        r=await self.p.consider(spontaneous=True,draft=bad)
        self.assertEqual(r['result'],'draft_rejected')
        self.assertTrue(r['reason']);self.assertFalse(r['sent'])
        self.assertEqual(self.p.used(),0)
        self.assertEqual(self.p.state.get('last_spontaneous_consider_at',0),0)
        self.model.return_value='{"safe":false}'
        r=await self.p.consider(spontaneous=True,draft={'publish':True,'text':'这是一份改稿','image_source':'none'})
        self.assertEqual(r['result'],'draft_rejected')
        self.assertEqual(self.p.state.get('last_spontaneous_consider_at',0),0)

    async def test_approved_url_is_published_verbatim_after_review(self):
        text='我叫岚，可以和好友私聊。我的公开档案在 https://203.0.113.10/'
        self.model.return_value='{"safe":true}'
        client=Mock();client.publish.return_value='visitor-url-id'
        client.recent.side_effect=[[],[{'tid':'visitor-url-id','text':text,'visible':1}]]
        with patch('astrbot_plugin_companion_phone.publisher.QzoneClient',return_value=client),patch('astrbot_plugin_companion_phone.publisher.asyncio.sleep',AsyncMock()):
            r=await self.p.consider(spontaneous=True,draft={'publish':True,'text':text,'image_source':'none'})
        self.assertEqual(r['result'],'verified')
        client.publish.assert_called_once_with(text,None)
        self.assertGreater(self.p.state['last_spontaneous_consider_at'],0)

    def test_image_dns_rejects_private_and_mixed_answers(self):
        private=(2,1,6,'',('127.0.0.1',443))
        public=(2,1,6,'',('1.1.1.1',443))
        for answers in ([private],[public,private]):
            with patch('socket.getaddrinfo',return_value=answers),patch('socket.socket') as sock:
                with self.assertRaises(ValueError): PublicHTTPS('example.com').connect()
                sock.assert_not_called()

    def test_image_url_rejects_credentials_and_non_https(self):
        for url in ('http://example.com/p.jpg','https://a:b@example.com/p.jpg','https://example.com:22/p.jpg'):
            with self.assertRaises(ValueError): public_bytes(url)

    def test_uncertain_reconcile_never_blindly_resets(self):
        self.assertFalse(self.p.safe_text(''))
        self.assertFalse(self.p.safe_text('x'*181))

    async def test_both_books_have_distinct_model_contexts(self):
        self.books.write('private','PRIVATE_SENTINEL',self.source['id'],[self.source])
        callback=AsyncMock(return_value='{"write":false}')
        from datetime import datetime
        from zoneinfo import ZoneInfo
        now=__import__('time').time()
        self.books.collect([{**self.source,'timestamp':now}])
        await self.books.finish_day(callback,datetime.fromtimestamp(now,ZoneInfo('Asia/Shanghai')).date().isoformat(),now)
        self.assertNotIn('PRIVATE_SENTINEL',callback.call_args_list[0].args[0])
        self.assertIn('PRIVATE_SENTINEL',callback.call_args_list[1].args[0])


class QuietTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_pending_phone_preamble_dropped(self):
        from astrbot.core.agent.runners.tool_loop_agent_runner import ToolLoopAgentRunner
        async def original(runner):
            yield SimpleNamespace(type='llm_result',text='先看看状态')
            yield SimpleNamespace(type='tool_call',text='tool')
        plugin=SimpleNamespace(owner_event=lambda e:True,task_authorized=lambda e:True)
        with patch.object(ToolLoopAgentRunner,'step',original):
            restore=quiet.install(plugin)
            runner=SimpleNamespace(run_context=SimpleNamespace(context=SimpleNamespace(event=object())),done=lambda:False)
            result=[r async for r in ToolLoopAgentRunner.step(runner)]
            self.assertEqual([r.type for r in result],['tool_call'])
            restore()

    async def test_final_answer_kept(self):
        from astrbot.core.agent.runners.tool_loop_agent_runner import ToolLoopAgentRunner
        async def original(runner): yield SimpleNamespace(type='llm_result',text='完成了')
        plugin=SimpleNamespace(owner_event=lambda e:True,task_authorized=lambda e:True)
        with patch.object(ToolLoopAgentRunner,'step',original):
            restore=quiet.install(plugin)
            runner=SimpleNamespace(run_context=SimpleNamespace(context=SimpleNamespace(event=object())),done=lambda:True)
            result=[r async for r in ToolLoopAgentRunner.step(runner)]
            self.assertEqual(result[0].text,'完成了'); restore()


if __name__=='__main__': unittest.main()
