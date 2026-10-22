"""Own-space reads, identity-bound replies and uncertain-send handling. No real I/O."""
import json,os,sys,tempfile,time,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock,Mock,patch
sys.path.insert(0,os.environ.get('COMPANION_TEST_PLUGINS','/AstrBot/data/plugins'))
from astrbot_plugin_companion_phone.discussions import Discussions,comment_rows
from astrbot_plugin_companion_phone.phone import PhoneError
from astrbot_plugin_companion_phone.qzone_client import QzoneClient,QzoneError,BOT_QQ


class DiscussionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.phone=SimpleNamespace(screenshot=AsyncMock(return_value=(b'real',(),())),swipe=AsyncMock(),screen=(1080,2376,time.monotonic()))
        self.browser=SimpleNamespace(journal_model=AsyncMock(),qq_comment_action=AsyncMock(return_value=True),
            interactive_action=AsyncMock(return_value=True),state={},save=Mock())
        self.guard=Mock()
        self.auth=AsyncMock(return_value=('test',BOT_QQ))
        self.d=Discussions(self.tmp.name,self.auth,self.phone,self.browser,self.guard)
        self.d.controls=AsyncMock(return_value=[])
        self.row={'tid':'post1','content':'这是测试动态的实际正文','cmtnum':1,
            'commentlist':[{'tid':'comment1','name':'读者甲','uin':'111','content':'这句很有趣','private':0,'reply_num':1,
                'list_3':[{'tid':'reply1','uin':BOT_QQ,'name':'岚','content':'之前的回复'}]}]}
        self.client=Mock();self.client.discussion_posts.return_value=([self.row],1)
        patcher=patch('astrbot_plugin_companion_phone.discussions.QzoneClient',return_value=self.client)
        # Keep the static text parser real, while replacing authenticated I/O.
        real=QzoneClient.post_text
        self.factory=patcher.start();self.factory.post_text=real;self.addCleanup(patcher.stop)
        self.d.remember([self.row])
        self.begin=AsyncMock();self.begin.reserve=Mock();self.begin.remaining=Mock(return_value=600)
        wait=patch('astrbot_plugin_companion_phone.discussions.asyncio.sleep',AsyncMock());wait.start();self.addCleanup(wait.stop)

    async def test_posts_and_comments_include_other_people_and_thread(self):
        posts=await self.d.posts_page()
        self.assertEqual(posts['posts'][0]['post_id'],'post1')
        value=await self.d.comments('post1')
        self.assertEqual(value['comments'][0]['author'],'读者甲')
        self.assertFalse(value['comments'][0]['is_self'])
        self.assertEqual(value['comments'][1]['parent_id'],'comment1')
        self.assertTrue(value['complete'])
        self.assertNotIn('author_id',value['comments'][0])

    async def test_seen_is_durable_not_just_self_comments(self):
        self.assertTrue((await self.d.comments('post1'))['comments'][0]['new'])
        d=Discussions(self.tmp.name,self.auth,self.phone,self.browser,self.guard);d.remember([self.row])
        self.assertFalse((await d.comments('post1'))['comments'][0]['new'])

    async def test_private_comments_excluded_and_partial_not_claimed_complete(self):
        self.row['commentlist'].append({'tid':'secret','uin':'555','private':1,'content':'私密内容'})
        self.row['cmtnum']=7
        result=await self.d.comments('post1')
        self.assertNotIn('secret',json.dumps(result))
        self.assertFalse(result['complete']);self.assertTrue(result['requires_phone_expansion'])

    async def test_pagination_does_not_stop_after_two(self):
        self.row['commentlist']=[{'tid':str(i),'uin':'555','content':'评论'+str(i)} for i in range(25)]
        self.row['cmtnum']=25
        first=await self.d.comments('post1');second=await self.d.comments('post1',int(first['next_cursor']))
        self.assertEqual(len(first['comments']),20);self.assertEqual(len(second['comments']),5)
        self.assertEqual(second['next_cursor'],'')

    async def test_unknown_id_and_invalid_cursor_rejected(self):
        with self.assertRaises(PhoneError):await self.d.comments('invented')
        with self.assertRaises(PhoneError):await self.d.comments('post1',-1)
        self.auth.assert_not_called()

    async def test_sleep_or_revocation_blocks_read_before_credentials(self):
        self.guard.side_effect=PhoneError('正在睡眠')
        with self.assertRaises(PhoneError):await self.d.posts_page()
        self.auth.assert_not_called()

    async def test_missing_post_on_refresh_does_not_use_stale_cache(self):
        self.client.discussion_posts.return_value=([],0)
        with self.assertRaises(PhoneError):await self.d.comments('post1')

    async def test_reply_requires_observed_target(self):
        self.browser.journal_model.return_value=json.dumps({'action':'ready','target_post_verified':True,
            'reply_target_verified':False,'quote':'这是测试动态的实际正文'})
        with self.assertRaises(PhoneError):await self.d.phone_work('post1','comment1','谢谢你的评论',self.begin)
        self.browser.qq_comment_action.assert_not_called()

    async def test_dry_run_navigates_but_never_types_or_sends(self):
        self.browser.journal_model.return_value=json.dumps({'action':'ready','target_post_verified':True,
            'reply_target_verified':True,'quote':'这是测试动态的实际正文'})
        result=await self.d.phone_work('post1','comment1','谢谢你的评论',self.begin,dry_run=True)
        self.assertEqual(result['result'],'ready_dry_run')
        self.browser.qq_comment_action.assert_not_called();self.assertEqual(self.d.state['replies'],[])

    async def test_reply_unknown_comment_does_not_open_phone(self):
        with self.assertRaises(PhoneError):await self.d.phone_work('post1','invented','谢谢',self.begin)
        self.begin.assert_not_called()

    async def test_unknown_send_is_not_repeated(self):
        self.browser.journal_model.return_value=json.dumps({'action':'ready','target_post_verified':True,
            'reply_target_verified':True,'quote':'这是测试动态的实际正文'})
        result=await self.d.phone_work('post1','comment1','谢谢你的评论',self.begin)
        self.assertEqual(result['result'],'uncertain')
        self.assertEqual(self.browser.qq_comment_action.await_count,2)
        again=await self.d.phone_work('post1','comment1','谢谢你的评论',self.begin)
        self.assertEqual(again['result'],'uncertain')
        self.assertEqual(self.browser.qq_comment_action.await_count,2)

    async def test_verify_requires_new_self_comment_under_right_parent(self):
        attempt={'request_id':'receipt','post_id':'post1','comment_id':'comment1','text':'谢谢',
            'before_ids':['reply1'],'result':'uncertain','at':time.time()}
        self.d.state['replies'].append(attempt)
        self.row['commentlist'].append({'tid':'wrong','uin':BOT_QQ,'content':'谢谢'})
        self.assertEqual((await self.d.verify('receipt'))['result'],'uncertain')
        self.row['commentlist'][0]['list_3'].append({'tid':'correct','uin':BOT_QQ,'content':'谢谢','create_time':time.time()})
        self.assertEqual((await self.d.verify('receipt'))['result'],'verified')

    async def test_verify_after_restart_rehydrates_only_recorded_post(self):
        self.d.state['replies'].append({'request_id':'saved','post_id':'post1','comment_id':'','text':'不在页面',
            'before_ids':[],'result':'uncertain','at':time.time()});self.d.save()
        d=Discussions(self.tmp.name,self.auth,self.phone,self.browser,self.guard)
        self.assertEqual((await d.verify('saved'))['result'],'uncertain')
        with self.assertRaises(PhoneError):await d.verify('invented')

    async def test_old_identical_comment_missing_from_partial_page_is_not_new_send(self):
        self.d.state['replies'].append({'request_id':'old','post_id':'post1','comment_id':'','text':'旧内容',
            'before_ids':[],'result':'uncertain','at':time.time()})
        self.row['commentlist'].append({'tid':'previously-hidden','uin':BOT_QQ,'content':'旧内容','create_time':time.time()-3600})
        self.assertEqual((await self.d.verify('old'))['result'],'uncertain')

    async def test_send_refused_is_reported_not_sent(self):
        self.browser.journal_model.return_value=json.dumps({'action':'ready','target_post_verified':True,
            'reply_target_verified':True,'quote':'这是测试动态的实际正文'})
        self.browser.qq_comment_action.side_effect=[True,False]
        r=await self.d.phone_work('post1','comment1','谢谢你的评论',self.begin)
        self.assertEqual(r['result'],'not_sent')

    async def test_expand_returns_only_current_screen_not_full_history(self):
        self.browser.journal_model.return_value=json.dumps({'action':'read','target_post_verified':True,
            'comments':[{'author':'读者甲','text':'这句很有趣','reply_to':''}]})
        r=await self.d.phone_work('post1','','',self.begin,expand=True)
        self.assertEqual(r['source'],'current_phone_screen');self.assertFalse(r['complete'])
        self.browser.qq_comment_action.assert_not_called()

    async def test_screen_comment_handles_can_be_selected_for_reply(self):
        self.browser.journal_model.side_effect=[json.dumps({'action':'read','target_post_verified':True,
            'comments':[{'author':'读者乙','text':'这是新展开出来的评论','reply_to':''}]}),
            json.dumps({'action':'ready','target_post_verified':True,'reply_target_verified':True,'quote':'这是新展开出来的评论'})]
        read=await self.d.phone_work('post1','','',self.begin,expand=True)
        cid=read['comments'][0]['comment_id']
        self.assertTrue(cid.startswith('screen:'))
        result=await self.d.phone_work('post1',cid,'谢谢你的留言',self.begin,dry_run=True)
        self.assertEqual(result['result'],'ready_dry_run')
        self.browser.qq_comment_action.assert_not_called()

    async def test_scope_revoked_during_navigation_blocks_before_input(self):
        async def revoke(prompt,image):
            self.guard.side_effect=PhoneError('已撤销')
            return json.dumps({'action':'ready','target_post_verified':True,'reply_target_verified':True,'quote':'这是测试动态的实际正文'})
        self.browser.journal_model.side_effect=revoke
        with self.assertRaises(PhoneError):await self.d.phone_work('post1','comment1','谢谢',self.begin)
        self.browser.qq_comment_action.assert_not_called()

    async def test_qzone_header_is_not_mistaken_for_home_entry(self):
        self.d.controls.return_value=[{'control_id':0,'text':'好友动态','bounds':[20,90,300,200]}]
        self.browser.journal_model.return_value=json.dumps({'action':'read','target_post_verified':True,'comments':[]})
        await self.d.phone_work('post1','','',self.begin,expand=True)
        self.browser.interactive_action.assert_not_called()

    async def test_observed_entry_is_used_once_not_repeated_forever(self):
        self.phone._shell=AsyncMock(return_value='topResumedActivity SplashActivity')
        self.d.controls.return_value=[{'control_id':0,'text':'好友动态','bounds':[20,400,300,600]}]
        self.browser.journal_model.return_value=json.dumps({'action':'read','target_post_verified':True,'comments':[]})
        await self.d.phone_work('post1','','',self.begin,expand=True)
        self.browser.interactive_action.assert_awaited_once()

    async def test_specific_reply_never_uses_generic_post_composer(self):
        self.d.controls.return_value=[{'control_id':0,'text':'说点什么吧...','bounds':[20,2100,900,2300]}]
        self.browser.journal_model.side_effect=[json.dumps({'action':'tap','control_id':0}),
            json.dumps({'action':'ready','target_post_verified':True,'reply_target_verified':True,'quote':'这是测试动态的实际正文'})]
        r=await self.d.phone_work('post1','comment1','谢谢',self.begin,dry_run=True)
        self.assertEqual(r['result'],'ready_dry_run')
        self.phone.swipe.assert_awaited_once()
        self.browser.interactive_action.assert_not_called()


class ClientPageTests(unittest.TestCase):
    def test_discussion_read_has_bounded_pagination_and_expanded_reply_count(self):
        c=QzoneClient('p_skey=fake',BOT_QQ);c.request=Mock(return_value={'msglist':[],'total':0})
        c.discussion_posts(20,10)
        payload=c.request.call_args.args[1]
        self.assertEqual(payload['pos'],20);self.assertEqual(payload['replynum'],100)
        with self.assertRaises(QzoneError):c.discussion_posts(-1)
