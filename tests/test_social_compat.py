"""Isolated compatibility tests: no QQ, models, cookies or real profile writes."""
import asyncio
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace as NS
import tempfile
import unittest
from unittest.mock import AsyncMock

from astrbot.core.agent.tool import ToolSet, FunctionTool
from astrbot_plugin_companion_phone.publisher import Publisher, TZ
from astrbot_plugin_companion_phone.social_compat import SocialCompat, _reserved
QzoneParseError=ValueError


class ExternalPlugin:
    def __init__(self):
        self.controller=NS(_request=AsyncMock(return_value={}))
        self._chat_history_context=AsyncMock(return_value='private group history')
        self._ask_llm_tool_reply=AsyncMock()
        self._llm_tool_reply_summary=lambda p:'confirmed'
    async def _publish_post_payload(self):pass


class CompatibilityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.config={'qzone_enabled':True,'qq_comment_consent':True,'autonomous_like_consent':True}
        self.awake=True
        self.publisher=Publisher(Path(self.tmp.name),self.config,AsyncMock(),AsyncMock(),NS(read=lambda *a:[]),lambda:True)
        self.publisher.reconcile=AsyncMock()
        self.browser=NS(state={},save=lambda:None)
        self.phone=NS(config=self.config,publisher=self.publisher,autonomy=self.browser,
            executor_allowed=lambda:self.awake,public_model=AsyncMock(return_value='{"safe":true}'),
            context=NS(get_registered_star=lambda n:None),owner_event=lambda e:True)
        self.compat=SocialCompat(self.phone)
        self.send=AsyncMock(return_value={'fid':'fake-post'})

    async def call(self,path='/post',**body):
        return await self.compat.request(self.send,'POST',path,json_body=body)

    async def test_sleep_rejects_reads_and_writes_but_not_cookie_maintenance(self):
        self.awake=False
        for path in ('/post','/comment','/reply','/like','/delete','/feeds','/detail'):
            with self.assertRaises(ValueError):await self.call(path,content='公开想法')
        self.send.assert_not_awaited()
        await self.call('/bind')
        self.send.assert_awaited_once()

    async def test_existing_post_quota_and_pause_apply_to_external(self):
        day=datetime.now(TZ).date().isoformat()
        self.publisher.state['attempts']=[{'day':day,'result':'verified'} for _ in range(5)]
        with self.assertRaises(ValueError):await self.call(content='公开想法')
        self.publisher.state.update(attempts=[],paused=True)
        with self.assertRaises(ValueError):await self.call(content='公开想法')
        self.send.assert_not_awaited()

    async def test_unknown_publish_is_reserved_once_and_blocks_retry(self):
        with self.assertRaisesRegex(ValueError,'尚未回读确认'):await self.call(content='公开想法')
        self.assertEqual(self.publisher.used(),1)
        with self.assertRaises(ValueError):await self.call(content='另一条想法')
        self.send.assert_awaited_once()
        self.assertFalse(self.send.call_args.kwargs['retry_on_timeout'])
        self.assertEqual(self.publisher.state['attempts'][0]['result'],'uncertain')

    async def test_post_timeout_keeps_quota(self):
        self.send.side_effect=TimeoutError
        with self.assertRaises(TimeoutError):await self.call(content='公开想法')
        self.assertEqual(self.publisher.used(),1)
        self.assertEqual(self.publisher.state['attempts'][0]['result'],'uncertain')

    async def test_privacy_and_cross_network_publish_are_rejected(self):
        self.publisher.private_overlap=lambda text:True
        with self.assertRaises(ValueError):await self.call(content='私密素材')
        self.publisher.private_overlap=lambda text:False
        with self.assertRaises(ValueError):await self.call(content='公开想法',sync_weibo=True)
        self.phone.public_model.return_value='{"safe":false}'
        with self.assertRaises(ValueError):await self.call(content='公开想法')
        self.send.assert_not_awaited()

    async def test_original_autonomous_reservation_does_not_count_twice(self):
        row={'day':datetime.now(TZ).date().isoformat(),'at':0,'text':'原稿','result':'reserved'}
        self.publisher.state['attempts']=[row]
        token=_reserved.set(row)
        try:await self.call(content='原稿')
        finally:_reserved.reset(token)
        self.assertEqual(self.publisher.used(),1)
        self.phone.public_model.assert_not_awaited()
        self.assertEqual(row['tid'],'fake-post')

    async def test_comment_uses_original_ledger_and_sixty_second_gap(self):
        await self.call('/comment',content='公开评论',hostuin=1,fid='p')
        with self.assertRaisesRegex(ValueError,'60秒'):await self.call('/reply',content='公开回复',fid='p')
        self.assertEqual(len(self.browser.state['qq_comment_attempts']),1)
        self.send.assert_awaited_once()

    async def test_comment_and_like_grants_and_independent_quotas(self):
        day=datetime.now(TZ).date().isoformat()
        self.config['qq_comment_consent']=False
        with self.assertRaises(ValueError):await self.call('/comment',content='公开评论')
        self.browser.state.update(likes_day=day,likes_used=9)
        await self.call('/like',fid='a')
        with self.assertRaises(ValueError):await self.call('/like',fid='b')
        self.assertEqual(self.browser.state['likes_used'],10)

    async def test_concurrent_posts_cannot_bypass_pending_reservation(self):
        result=await asyncio.gather(self.call(content='第一条'),self.call(content='第二条'),return_exceptions=True)
        self.assertTrue(all(isinstance(r,ValueError) for r in result))
        self.assertEqual(self.send.await_count,1)

    async def test_tool_set_is_local_and_legacy_is_only_hidden_for_owner(self):
        self.compat.bind=lambda:True
        original=ToolSet([FunctionTool(name=n,description='',parameters={'type':'object','properties':{}},handler=None) for n in
            ('companion_qzone_publish','companion_qq_space','qzone_delete_post','companion_memory_recall')])
        req=NS(func_tool=original)
        self.compat.filter_tools(NS(message_str='看看空间'),req)
        self.assertNotIn('companion_qzone_publish',req.func_tool.names())
        self.assertNotIn('qzone_delete_post',req.func_tool.names())
        self.assertIn('companion_memory_recall',req.func_tool.names())
        self.assertEqual(len(original),4)
        req=NS(func_tool=original)
        self.compat.filter_tools(NS(message_str='删除指定说说'),req)
        self.assertIn('qzone_delete_post',req.func_tool.names())

    async def test_bind_is_idempotent_and_restores_third_party_methods(self):
        plugin=ExternalPlugin()
        self.phone.context.get_registered_star=lambda n:NS(activated=True,star_cls=plugin)
        original=plugin.controller._request
        self.compat.bind();wrapped=plugin.controller._request
        self.compat.bind()
        self.assertIs(plugin.controller._request,wrapped)
        self.assertEqual(await plugin._chat_history_context(),'')
        self.awake=False
        with self.assertRaises(ValueError):await plugin.controller._request('POST','/post',json_body={'content':'原稿'})
        original.assert_not_awaited()
        self.compat.close()
        self.assertIs(plugin.controller._request,original)

    async def test_external_reload_is_rebound_without_source_changes(self):
        plugins=[ExternalPlugin()]
        self.phone.context.get_registered_star=lambda n:NS(activated=True,star_cls=plugins[0])
        self.compat.bind()
        plugins[0]=ExternalPlugin();original=plugins[0].controller._request
        self.compat.bind()
        self.assertIsNot(plugins[0].controller._request,original)
        reply=await plugins[0]._ask_llm_tool_reply(None,{'ok':False,'public_reason':'睡眠中'},'fallback')
        self.assertEqual(reply,'睡眠中')


if __name__=='__main__':unittest.main()
