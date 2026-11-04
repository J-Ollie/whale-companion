import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

sys.path.insert(0,os.environ.get('COMPANION_TEST_PLUGINS',str(Path(__file__).parent)))
from astrbot_plugin_companion_chat.reply_state import ReplyState,KEY
from astrbot_plugin_companion_chat.deliberation import require_visible_output
from astrbot_plugin_companion_chat.batch.main import CompanionBatch
from astrbot_plugin_companion_chat.main import CompanionChat
from astrbot.core.platform.astr_message_event import current_message_event
from astrbot.api.provider import LLMResponse
from test_chat_pacing import Event


class E(Event):
    def is_stopped(self):return self.stopped
    def set_extra(self,key,value):self.extra[key]=value


class StateTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.audit=ReplyState(self.tmp.name);self.addCleanup(lambda:self.audit.db.close())

    def row(self,e):return self.audit.latest(e.unified_msg_origin)[-1]

    def begin(self,text='private content',sender='owner'):
        e=E(text,sender=sender);self.audit.begin(e);return e

    async def test_no_receipt_is_not_reinterpreted_as_silence(self):
        e=self.begin();self.audit.mark(e,'generated')
        t=asyncio.create_task(asyncio.sleep(0));await t;self.audit.finished(e,t)
        self.assertEqual(self.row(e)['state'],'no_confirmed_delivery')
        self.assertEqual(self.row(e)['reason'],'generated')

    async def test_explicit_silence_is_preserved_and_has_no_visible_text(self):
        for marker in ['[[silent:closing_ack]]','[[text]][[silent:pause]]']:
            e=self.begin();response=LLMResponse(role='assistant',completion_text=marker)
            require_visible_output(response)
            self.audit.response(e,response)
            self.assertEqual(response.completion_text,'');self.assertTrue(e.stopped)
            t=asyncio.create_task(asyncio.sleep(0));await t;self.audit.finished(e,t)
            self.assertEqual(self.row(e)['state'],'silent')

    async def test_quote_invalid_marker_and_tools_do_not_trigger_silence(self):
        for body in ['他说[[silent:pause]]','[[silent:unknown]]','[[silent:pause]]额外正文']:
            e=self.begin();r=LLMResponse(role='assistant',completion_text=body);self.audit.response(e,r)
            self.assertFalse(e.stopped);self.assertEqual(self.row(e)['state'],'generated')
        e=self.begin();r=LLMResponse(role='tool',completion_text='[[silent:pause]]',tools_call_name=['read'])
        self.audit.response(e,r);self.assertFalse(e.stopped)

    async def test_empty_generation_is_separate_from_intentional_silence(self):
        e=self.begin();self.audit.response(e,LLMResponse(role='assistant',reasoning_content='private reasoning'))
        self.assertEqual(self.row(e)['state'],'empty_output')

    async def test_natural_closure_and_batch_merges_have_explicit_metadata(self):
        p=CompanionBatch(None,{'owner_qq':'owner','quiet_seconds':.02,'max_seconds':.1})
        p.reply_state=self.audit;p.may_end_turn=AsyncMock(return_value=True)
        a,b=self.begin('好的'),self.begin('嗯')
        task=asyncio.create_task(p.merge(a));await asyncio.sleep(.003);await p.merge(b);await task
        rows=self.audit.latest(a.unified_msg_origin)
        self.assertEqual(rows[0]['state'],'silent');self.assertEqual(rows[0]['reason'],'closing_ack')
        self.assertEqual(rows[1]['state'],'merged');self.assertEqual(rows[1]['merged_into'],a.get_extra(KEY))

    async def test_real_task_completion_marks_unanswered_event(self):
        e=E('private content');gate=asyncio.Event()
        task=asyncio.create_task(gate.wait());self.audit.begin(e,task)
        gate.set();await task;await asyncio.sleep(0)
        self.assertEqual(self.row(e)['state'],'no_confirmed_delivery')

    async def test_delivery_requires_matching_sender_prepared_event_and_real_receipt(self):
        e=self.begin();token=current_message_event.set(e);self.addCleanup(current_message_event.reset,token)
        params={'user_id':'owner','message':[]}
        self.audit.delivery('send_private_msg',params,value={'message_id':'early'})
        self.assertEqual(self.row(e)['state'],'received')
        e.set_extra('companion_reply_state_prepared',True)
        self.audit.delivery('send_private_msg',{'user_id':'other'},value={'message_id':'wrong'})
        self.assertEqual(self.row(e)['state'],'received')
        self.audit.delivery('send_private_msg',params,attempted=True)
        self.assertEqual(self.row(e)['state'],'sending')
        self.audit.delivery('send_private_msg',params,value={'status':'ok','retcode':0,'data':{'message_id':'receipt-1'}})
        self.assertEqual(self.row(e)['state'],'submitted')
        self.assertEqual(json.loads(self.row(e)['receipts']),['receipt-1'])

    async def test_partial_failure_and_unknown_send_are_not_success(self):
        e=self.begin();e.set_extra('companion_reply_state_prepared',True)
        token=current_message_event.set(e);self.addCleanup(current_message_event.reset,token)
        params={'user_id':'owner'}
        self.audit.delivery('send_private_msg',params,error=TimeoutError())
        self.assertEqual(self.row(e)['state'],'send_uncertain')
        self.audit.delivery('send_private_msg',params,value={'message_id':'one'})
        self.audit.delivery('send_private_msg',params,error=TimeoutError())
        self.assertEqual(self.row(e)['state'],'partial_send')

    async def test_finished_event_cannot_be_reused_by_background_task(self):
        e=self.begin();e.set_extra('companion_reply_state_prepared',True)
        t=asyncio.create_task(asyncio.sleep(0));await t;self.audit.finished(e,t)
        token=current_message_event.set(e);self.addCleanup(current_message_event.reset,token)
        self.audit.delivery('send_private_msg',{'user_id':'owner'},value={'message_id':'late'})
        self.assertEqual(self.row(e)['state'],'no_confirmed_delivery')

    async def test_scope_isolation_no_raw_text_or_reasoning_and_no_cross_scope_merge(self):
        a=self.begin('SENSITIVE_TEXT');b=self.begin('OTHER_SECRET',sender='friend')
        self.audit.merge(b,a)
        self.assertEqual(self.row(b)['state'],'received')
        self.audit.response(a,LLMResponse(role='assistant',completion_text='REPLY_SECRET',reasoning_content='THINK_SECRET'))
        rows=str(self.audit.latest(a.unified_msg_origin))
        for text in ['SENSITIVE_TEXT','OTHER_SECRET','REPLY_SECRET','THINK_SECRET']:self.assertNotIn(text,rows)
        self.assertEqual(len(self.audit.latest(a.unified_msg_origin)),1)

    async def test_restart_preserves_silence_but_marks_pending_as_interrupted(self):
        a=self.begin();self.audit.mark(a,'silent','pause');b=self.begin()
        self.audit.close();self.audit=ReplyState(self.tmp.name)
        self.assertEqual(self.audit.latest(a.unified_msg_origin)[0]['state'],'silent')
        self.assertEqual(self.row(b)['state'],'interrupted')

    async def test_query_command_does_not_expose_owner_state_to_friend_or_group(self):
        p=CompanionChat.__new__(CompanionChat);p.ready=True;p.reply_state=self.audit
        p.friends=SimpleNamespace(owner='owner',umo='qq-companion:FriendMessage:owner')
        self.begin()
        for e in [E(sender='friend',verified=True),E(sender='owner',group='group')]:
            self.assertEqual([x async for x in p.reply_status(e)],[])

    async def test_current_contact_hint_distinguishes_silence_from_unknown(self):
        a=self.begin();self.audit.mark(a,'silent','pause')
        b=self.begin();self.audit.mark(b,'no_confirmed_delivery','generated')
        other=self.begin(sender='other');self.audit.mark(other,'send_rejected','OTHER_SECRET')
        current=self.begin()
        text=self.audit.context_hint(current)
        self.assertIn('"state": "silent"',text)
        self.assertIn('"state": "no_confirmed_delivery"',text)
        self.assertNotIn('OTHER_SECRET',text)
        self.assertNotIn('"state": "received"',text)

    async def test_error_notification_receipt_does_not_erase_model_failure(self):
        e=self.begin();self.audit.response(e,LLMResponse(role='err',completion_text='error'))
        self.audit.mark(e,'prepared');e.set_extra('companion_reply_state_prepared',True)
        token=current_message_event.set(e);self.addCleanup(current_message_event.reset,token)
        self.audit.delivery('send_private_msg',{'user_id':'owner'},attempted=True)
        self.audit.delivery('send_private_msg',{'user_id':'owner'},value={'message_id':'notification'})
        self.assertEqual(self.row(e)['state'],'submitted')
        self.assertEqual(self.row(e)['reason'],'model_error')

    async def test_cancel_after_one_segment_receipt_is_partial(self):
        e=self.begin();e.set_extra('companion_reply_state_prepared',True)
        token=current_message_event.set(e);self.addCleanup(current_message_event.reset,token)
        self.audit.delivery('send_private_msg',{'user_id':'owner'},value={'message_id':'first'})
        task=asyncio.create_task(asyncio.sleep(10));task.cancel()
        with self.assertRaises(asyncio.CancelledError):await task
        self.audit.finished(e,task)
        self.assertEqual(self.row(e)['state'],'partial_send')

if __name__=='__main__':unittest.main()
