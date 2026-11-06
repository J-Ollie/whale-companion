"""Actual SDK hooks with fake transports and no real model or platform actions."""
import asyncio
from pathlib import Path
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock,Mock

from astrbot.core.agent.tool import ToolSet,FunctionTool
from astrbot_plugin_companion_phone.main import CompanionPhone
from astrbot_plugin_companion_phone.social_autonomy import SocialAutonomy
from astrbot_plugin_companion_phone.thinking import Session,SESSION


class ThinkingHooks(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.p=NS(root=Path(self.tmp.name),umo='platform:FriendMessage:owner',
                  config={'thinking_enabled':True,'social_full_autonomy_enabled':True},
                  autonomy=NS(session=None,state={'last_user_at':0}),
                  books=NS(read=lambda *a:[]),media=NS(recent=lambda **kw:[]),
                  public_reading_context=lambda:'公开来源',
                  context=NS(get_config=lambda **kw:{'provider_settings':{'enable':True}},llm_generate=AsyncMock()))
        tools=ToolSet([FunctionTool(name='qzone_list_feed',description='',parameters={})])
        self.p.social_compat=NS(check=Mock(),bind=Mock(),tools=lambda:tools,activity_context=lambda:'无执行记录')
        platform=NS(meta=lambda:NS(id='platform',name='aiocqhttp'),get_client=lambda:NS(call_action=AsyncMock()))
        self.p.context.get_platform_inst=lambda name:platform
        self.p.context.persona_manager=NS(get_persona=AsyncMock(return_value=NS(system_prompt='小鲸鱼')))
        self.p.context.tool_loop_agent=AsyncMock()
        self.p.thinking=NS(enabled=lambda:True,store=NS(meta=lambda *a:False),social_reflection=AsyncMock(return_value={'focus':'想留下一点公开感想'}))

    async def test_social_tool_decision_happens_after_thought(self):
        async def run(**kw):
            self.p.thinking.social_reflection.assert_awaited_once()
            self.assertIn('想留下一点公开感想',kw['prompt'])
            self.assertEqual(kw['contexts'],[])
        self.p.context.tool_loop_agent.side_effect=run
        worker=SocialAutonomy(self.p)
        await worker.cycle(['space'])
        self.assertEqual(worker.state['last_result'],'considered')

    async def test_failed_thought_cannot_fall_through_to_social_tools(self):
        self.p.thinking.social_reflection.side_effect=ValueError('invalid reflection')
        worker=SocialAutonomy(self.p)
        await worker.cycle(['space'])
        self.p.context.tool_loop_agent.assert_not_called()
        self.assertEqual(worker.state['last_result'],'deferred')

    async def test_thinking_and_social_do_not_overlap(self):
        pending=asyncio.create_task(asyncio.Event().wait())
        self.p.autonomy.session=pending
        worker=SocialAutonomy(self.p)
        worker.state['next_by_category']['space']=0
        try:
            worker.tick()
            self.assertIsNone(worker.task)
        finally:
            pending.cancel();await asyncio.gather(pending,return_exceptions=True)

    async def test_pause_does_not_consume_social_due_time(self):
        self.p.thinking.store.meta=lambda *a:True
        worker=SocialAutonomy(self.p)
        worker.state['next_by_category']['space']=0
        worker.tick()
        self.assertEqual(worker.state['next_by_category']['space'],0)
        self.assertIsNone(worker.task)

    async def test_public_review_respects_parent_request_budget(self):
        session=Session(0,lambda:None);token=SESSION.set(session)
        try:
            with self.assertRaises(Exception):await CompanionPhone.public_model(self.p,'not sent')
        finally:SESSION.reset(token)
        self.p.context.llm_generate.assert_not_called()
        self.p.context.persona_manager.get_persona.assert_not_called()

    async def test_browsing_observation_respects_parent_request_budget(self):
        session=Session(0,lambda:None);token=SESSION.set(session)
        try:
            with self.assertRaises(Exception):await CompanionPhone.autonomy_model(self.p,'not sent',None)
        finally:SESSION.reset(token)
        self.p.context.llm_generate.assert_not_called()
        self.p.context.persona_manager.get_persona.assert_not_called()

    def test_outreach_waits_for_current_thought(self):
        self.p.thinking.run_id='active'
        self.p.autonomy.rhythm=NS(social_allowed=Mock(return_value=True))
        self.assertFalse(CompanionPhone.outreach_allowed(self.p))
        self.p.autonomy.rhythm.social_allowed.assert_not_called()


if __name__=='__main__':unittest.main()
