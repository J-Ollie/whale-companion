"""Timing, delivery, private scope and existing role-state integration."""
import asyncio
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.environ.get('COMPANION_TEST_PLUGINS', str(Path(__file__).parent)))
from astrbot_plugin_companion_chat.batch.main import CompanionBatch, Batch
from astrbot_plugin_companion_chat.batch.pacing import quiet_window, seconds
from astrbot_plugin_companion_chat.behavior import rhythm_snapshot, reply_guidance
from astrbot.api.message_components import Plain


class Event:
    def __init__(self, text='你好', sender='owner', group='', verified=False):
        self.message_str=text
        self.message_obj=SimpleNamespace(message_str=text,message=[Plain(text)])
        self.unified_msg_origin='qq-companion:FriendMessage:'+sender
        self.sender,self.group=sender,group
        self.extra={'companion_friend_verified':verified}
        self.stopped=self.suppressed=False
    def get_platform_id(self):return 'qq-companion'
    def get_sender_id(self):return self.sender
    def get_group_id(self):return self.group
    def get_extra(self,key,default=None):return self.extra.get(key,default)
    def should_call_llm(self,value):self.suppressed=value
    def stop_event(self):self.stopped=True


def context(energy=.1, mode='awake', at=1000, healthy=True):
    rhythm=SimpleNamespace(healthy=healthy,state={'energy':energy,'mode':mode,'updated_at':at,
        'private_note':'other-contact-secret','pending':'must not be exposed'})
    meta=SimpleNamespace(activated=True,star_cls=SimpleNamespace(autonomy=SimpleNamespace(rhythm=rhythm)))
    return SimpleNamespace(get_registered_star=lambda n:meta if n.endswith('_phone') else None)


class WindowTests(unittest.TestCase):
    def test_hot_faster_than_normal_and_cold(self):
        self.assertLess(quiet_window('你好',ceiling=15,hot=True),quiet_window('你好',ceiling=15))
        self.assertLess(quiet_window('你好',ceiling=15),quiet_window('你好',ceiling=15,cold=True))
    def test_incomplete_message_waits_longer(self):
        for text in ('我想说','因为','这个，','这件事……','比如：','还有'):
            self.assertGreater(quiet_window(text,ceiling=15,hot=True),quiet_window('你好',ceiling=15,hot=True))
    def test_long_media_low_energy_allow_more_time(self):
        base=quiet_window('你好',ceiling=15,hot=True)
        self.assertGreater(quiet_window('字'*200,ceiling=15,hot=True),base)
        self.assertGreater(quiet_window('',ceiling=15,hot=True,media=True),base)
        self.assertGreater(quiet_window('你好',ceiling=15,hot=True,energy=.1),base)
    def test_respects_configured_ceiling_and_invalid_energy(self):
        self.assertEqual(quiet_window('还有',ceiling=.03),.03)
        for energy in (None,True,-1,2,float('nan'),'low'):
            self.assertEqual(quiet_window('你好',ceiling=15,energy=energy),2.4)
    def test_bad_timing_setting_is_finite(self):
        for value in ('bad',None,float('nan'),float('inf')):self.assertEqual(seconds(value,15),15)


class StateTests(unittest.TestCase):
    def test_only_energy_and_mode_are_read(self):
        self.assertEqual(rhythm_snapshot(context(),1000),{'mode':'awake','energy':.1})
    def test_stale_or_unhealthy_state_is_not_used(self):
        for ctx in (context(at=500),context(at=1001),context(healthy=False),context(energy=True),context(energy=float('nan'))):
            self.assertEqual(rhythm_snapshot(ctx,1000),{})
    def test_sleep_and_rest_do_not_produce_override_guidance(self):
        with patch('astrbot_plugin_companion_chat.behavior.time.time',return_value=1000):
            for mode in ('asleep','resting'):self.assertEqual(reply_guidance(context(mode=mode)),'')
    def test_low_energy_does_not_become_real_body_claim_or_private_leak(self):
        with patch('astrbot_plugin_companion_chat.behavior.time.time',return_value=1000):
            text=reply_guidance(context())
        self.assertIn('精力较低',text)
        self.assertIn('不虚构身体',text)
        self.assertNotIn('other-contact-secret',text)
        self.assertNotIn('must not be exposed',text)
    def test_no_phone_state_still_has_communication_guidance(self):
        self.assertIn('不预设新的性格',reply_guidance(None))


class FlowTests(unittest.IsolatedAsyncioTestCase):
    def plugin(self, **extra):
        p=CompanionBatch(None,{'owner_qq':'owner','adaptive_pacing':True,'quiet_seconds':15,
                              'max_seconds':60,'natural_closure':False}|extra)
        p.may_end_turn=AsyncMock(return_value=False)
        return p
    async def test_fixed_mode_preserves_original_wait(self):
        p=self.plugin(adaptive_pacing=False)
        self.assertEqual(p.pacing(Event(),100,15),15)
        self.assertFalse(p.cadence)
    async def test_reply_heats_only_same_contact(self):
        p=self.plugin();a,b=Event(),Event(sender='friend',verified=True)
        now=asyncio.get_running_loop().time()
        self.assertEqual(p.pacing(a,now,15),4)
        await p.replied(a)
        self.assertEqual(p.pacing(a,asyncio.get_running_loop().time(),15),1.2)
        self.assertEqual(p.pacing(b,asyncio.get_running_loop().time(),15),4)
    async def test_failed_voice_does_not_heat_conversation(self):
        p=self.plugin();e=Event();p.pacing(e,asyncio.get_running_loop().time(),15)
        e.extra['companion_voice_prepared']=True
        await p.replied(e)
        self.assertNotIn('reply',p.cadence[e.unified_msg_origin])
        e.extra['companion_voice_delivery_receipt']={'message_id':'synthetic'}
        await p.replied(e)
        self.assertIn('reply',p.cadence[e.unified_msg_origin])
    async def test_commands_groups_and_unverified_do_not_enter_merge(self):
        p=self.plugin()
        for e in (Event('/状态'),Event(group='group'),Event(sender='stranger')):await p.merge(e)
        self.assertFalse(p.pending);self.assertFalse(p.cadence)
    async def test_continuation_extends_current_deadline_and_merges(self):
        p=self.plugin(quiet_seconds=.2,max_seconds=.3);first=Event('说一下')
        with patch('astrbot_plugin_companion_chat.batch.main.quiet_window',side_effect=lambda text,**kw:.1 if text.endswith('，') else .04):
            task=asyncio.create_task(p.merge(first));await asyncio.sleep(.02)
            await p.merge(Event('因为，'));await asyncio.sleep(.04)
            self.assertFalse(task.done())
            await p.merge(Event('后面这句'));await task
        self.assertEqual(first.message_str,'说一下\n因为，\n后面这句')
        p.may_end_turn.assert_awaited_once()
    async def test_late_arrival_uses_old_deadline_not_new_message_window(self):
        p=self.plugin(quiet_seconds=.2,max_seconds=.3);e=Event('新消息')
        now=asyncio.get_running_loop().time()
        old=Batch(now-.1,now-.1,texts=['旧消息'],parts=[Plain('旧消息')],count=1,quiet=.04)
        p.pending[e.unified_msg_origin]=old
        with patch('astrbot_plugin_companion_chat.batch.main.quiet_window',return_value=.02):await p.merge(e)
        self.assertEqual(e.message_str,'新消息');self.assertFalse(e.stopped)
        self.assertEqual(old.count,1)
    async def test_continuous_messages_still_hit_maximum(self):
        p=self.plugin(quiet_seconds=.06,max_seconds=.13);e=Event('0')
        with patch('astrbot_plugin_companion_chat.batch.main.quiet_window',return_value=.06):
            start=asyncio.get_running_loop().time();task=asyncio.create_task(p.merge(e))
            for n in range(1,5):
                await asyncio.sleep(.025);await p.merge(Event(str(n)))
            await task
            elapsed=asyncio.get_running_loop().time()-start
        self.assertGreaterEqual(elapsed,.12);self.assertLess(elapsed,.22)
        self.assertEqual(e.message_str,'0\n1\n2\n3\n4')
    async def test_cadence_is_bounded_timestamp_only_and_expires(self):
        p=self.plugin()
        for n in range(520):p.pacing(Event(sender=str(n),verified=True),100+n*.01,15)
        self.assertLessEqual(len(p.cadence),512)
        self.assertTrue(all(set(r)<= {'received','reply'} and all(type(v) is float for v in r.values()) for r in p.cadence.values()))
        p.pacing(Event(),3000,15);self.assertEqual(len(p.cadence),1)
    async def test_guidance_only_in_authorized_private_request(self):
        p=self.plugin()
        for event,expected in ((Event(),3),(Event(sender='friend',verified=True),3),(Event(sender='stranger'),1),(Event(group='group'),0)):
            req=SimpleNamespace(extra_user_content_parts=[])
            await p.task_truth_hint(event,req)
            self.assertEqual(len(req.extra_user_content_parts),expected)
    async def test_terminate_clears_timing_without_saving_data(self):
        p=self.plugin(quiet_seconds=10);task=asyncio.create_task(p.merge(Event('因为，')))
        await asyncio.sleep(.005);await p.terminate()
        self.assertTrue(task.cancelled());self.assertFalse(p.cadence);self.assertFalse(p.pending)


if __name__=='__main__':unittest.main()
