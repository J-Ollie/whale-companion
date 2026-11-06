"""Actual runtime service integration with synthetic sends and state."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock, patch

from astrbot_plugin_companion_chat.active.main import CompanionActive, eligible, apply_control, OutreachRejected
from astrbot_plugin_companion_phone.main import CompanionPhone
from astrbot_plugin_companion_chat import contact_policy as cp
from test_situated_emotion import NOW


class ContactIntegration(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.p=CompanionActive.__new__(CompanionActive)
        self.p.config={'awake_anytime':True}
        self.p.state={'last_user_at':NOW-3*3600,'unanswered':0}
        self.p.state_path=Path(self.tmp.name)/'state.json'
        self.client=NS(call_action=AsyncMock(return_value={'data':{'message_id':1}}))
        self.p.context=NS(get_platform_inst=lambda _:NS(get_client=lambda:self.client),llm_generate=AsyncMock())

    async def test_pending_commitment_and_free_outreach_share_wait_return_guard(self):
        self.assertEqual(apply_control(self.p.state,'我忙完来找你',NOW,True),'waiting_return')
        self.assertFalse(eligible(self.p.state,NOW+10*3600,self.p.config))
        self.assertFalse(eligible(self.p.state,NOW+10*3600,self.p.config,check_decision=False))
        apply_control(self.p.state,'哈哈我回来了',NOW+10*3600,True)
        self.assertTrue(self.p.contact_allowed(NOW+10*3600))

    async def test_policy_changed_after_audio_preparation_prevents_platform_call(self):
        cp.direct(self.p.state,'等我忙完',NOW)
        with self.assertRaises(OutreachRejected):await self.p.send_voice('fake-audio','测试台词')
        self.client.call_action.assert_not_awaited()

    async def test_topic_check_is_bounded_and_unrelated_messages_remain_possible(self):
        cp.direct(self.p.state,'别再提考试了',NOW)
        self.p.context.llm_generate.return_value=NS(completion_text='{"supported":false}')
        self.assertFalse(await self.p.message_allowed('考试准备如何了'))
        self.p.context.llm_generate.return_value=NS(completion_text='{"supported":true}')
        self.assertTrue(await self.p.message_allowed('分享一个海洋问题'))
        self.assertEqual(self.p.context.llm_generate.await_count,2)
        self.assertFalse(self.p.state.get('daily_count',0))

    async def test_content_shares_use_same_contact_policy_but_their_own_quota(self):
        phone=CompanionPhone.__new__(CompanionPhone)
        phone.config={'autonomous_share_enabled':True}
        phone.active_plugin=lambda:self.p
        phone.outbound=NS(share=AsyncMock(return_value={'result':'not_due'}))
        cp.direct(self.p.state,'我忙完来找你',NOW)
        result=await phone.share_interest('source','测试分享')
        self.assertEqual(result['result'],'contact_policy')
        phone.outbound.share.assert_not_awaited()
        self.p.state['contact_policy_v1']['hold']=None
        self.p.state['daily_count']=4
        await phone.share_interest('source','测试分享')
        phone.outbound.share.assert_awaited_once()

    async def test_semantic_contact_is_persisted_and_reloaded(self):
        self.p.apply_contact({'mode':'return','quote':'我好了会来找你'},'我好了会来找你','evt',NOW)
        import json
        state=json.loads(self.p.state_path.read_text())
        self.assertTrue(cp.blocked(state,NOW+86400))


if __name__=='__main__':unittest.main()
