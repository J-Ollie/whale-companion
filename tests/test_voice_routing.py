"""Real SDK, synthetic recipients/receipts only. No model, QQ or paid TTS calls."""
import asyncio
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock,Mock

sys.path.insert(0,os.environ.get('COMPANION_TEST_PLUGINS',str(Path(__file__).parent)))
import test_voice as fixture
from test_voice import Event,OWNER,FRIEND
from astrbot.api.message_components import Plain,Record
from astrbot.api.provider import LLMResponse,ProviderRequest
from astrbot.core.message.message_event_result import MessageChain
from astrbot_plugin_companion_batch.main import CompanionBatch
from astrbot_plugin_companion_memory.main import CompanionMemory
from astrbot_plugin_companion_friends.main import CompanionFriends


class ActionFailed(Exception):retcode=100


class RoutingTests(unittest.IsolatedAsyncioTestCase):
 setUp=fixture.VoiceTests.setUp
 mock_audio=fixture.VoiceTests.mock_audio
 async def ready(self,choice='voice',**extras):
  self.mock_audio();e=Event(voice=False,text='今天的天气真舒服。')
  e.set_extra('companion_voice_choice',choice)
  for k,v in extras.items():e.set_extra(k,v)
  await self.v.outgoing(e);return e

 def ack(self,e,chain):
  self.friends.voice_delivery.observe('send_private_msg',{'user_id':e.sender,'message':[
   {'type':'record' if isinstance(p,Record) else 'text','data':{}} for p in chain.chain]},value={'message_id':123})

 async def test_model_can_choose_voice_for_a_text_input(self):
  e=await self.ready();self.assertEqual(len(e.result.chain),1);self.assertIsInstance(e.result.chain[0],Record)

 async def test_model_text_choice_is_respected_for_voice_input(self):
  e=await self.ready('text',companion_voice_requested=True)
  self.assertIsInstance(e.result.chain[0],Plain);self.v.synthesize.assert_not_awaited()

 async def test_explicit_voice_overrides_model_text_choice(self):
  e=await self.ready('text',companion_voice_explicit=True)
  self.assertIsInstance(e.result.chain[0],Record)

 async def test_explicit_text_overrides_model_voice_choice(self):
  e=await self.ready(companion_voice_declined=True)
  self.assertIsInstance(e.result.chain[0],Plain);self.v.synthesize.assert_not_awaited()

 async def test_unmarked_normal_reply_stays_text(self):
  self.mock_audio();e=Event(voice=False,text='你好');await self.v.outgoing(e)
  self.assertIsInstance(e.result.chain[0],Plain);self.v.synthesize.assert_not_awaited()

 async def test_control_prefix_removed_before_saved_completion(self):
  e=Event();e.set_extra('companion_voice_route_enabled',True)
  response=LLMResponse(role='assistant',result_chain=MessageChain([Plain('[[voice]]\n我在这里。')]))
  self.v.capture_response(e,response)
  self.assertEqual(response.completion_text,'我在这里。');self.assertEqual(e.get_extra('companion_voice_choice'),'voice')

 async def test_quoted_route_in_body_is_not_a_selector(self):
  e=Event();e.set_extra('companion_voice_route_enabled',True)
  response=LLMResponse(role='assistant',completion_text='这个例子写的是 [[voice]]。')
  self.v.capture_response(e,response);self.assertIsNone(e.get_extra('companion_voice_choice'))

 async def test_long_or_reference_content_stays_complete_text(self):
  for text in ['鲸'*181,'请看 https://example.com','```print(1)```']:
   self.mock_audio();e=Event();e.set_extra('companion_voice_choice','voice')
   e.result.chain=[Plain(text)];e.result.get_plain_text=lambda:text
   await self.v.outgoing(e);self.assertEqual(e.result.chain[0].text,text);self.v.synthesize.assert_not_awaited()

 async def test_model_voice_choice_never_expands_contact_access(self):
  self.mock_audio();e=Event(sender=FRIEND,verified=False);e.set_extra('companion_voice_choice','voice')
  await self.v.outgoing(e);self.v.synthesize.assert_not_awaited()

 async def test_quota_exhaustion_stays_text(self):
  self.v.reserve=AsyncMock(return_value=False);e=await self.ready()
  self.assertIsInstance(e.result.chain[0],Plain);self.v.synthesize.assert_not_awaited()

 async def test_only_one_voice_send_and_receipt_bound_transcript(self):
  self.mock_audio();e=Event();original=AsyncMock(side_effect=lambda chain:self.ack(e,chain));e.send=original
  e.set_extra('companion_voice_choice','voice');await self.v.outgoing(e)
  chain=MessageChain(e.result.chain);await e.send(chain);await e.send(chain)
  original.assert_awaited_once();self.assertEqual(e.get_extra('companion_voice_delivery_receipt'),'123')
  self.assertEqual(e.get_extra('companion_voice_delivered_text'),'我在这里，慢慢说就好。')
  self.assertTrue(all(isinstance(p,Record) for p in original.await_args.args[0].chain))

 async def test_definite_rejection_falls_back_once_without_altering_result_chain(self):
  self.mock_audio();e=Event();calls=[]
  async def send(chain):
   calls.append(chain)
   if isinstance(chain.chain[0],Record):
    self.friends.voice_delivery.observe('send_private_msg',{'user_id':OWNER,'message':[{'type':'record'}]},error=ActionFailed())
    raise ActionFailed()
   self.ack(e,chain)
  e.send=send;e.set_extra('companion_voice_choice','voice');await self.v.outgoing(e)
  await e.send(MessageChain(e.result.chain))
  self.assertEqual(len(calls),2);self.assertIsInstance(calls[1].chain[0],Plain)
  self.assertIsInstance(e.result.chain[0],Record);self.assertEqual(e.get_extra('companion_voice_delivered_as'),'text')

 async def test_uncertain_timeout_does_not_resend_as_text(self):
  self.mock_audio();e=Event();original=AsyncMock(side_effect=TimeoutError());e.send=original
  e.set_extra('companion_voice_choice','voice');await self.v.outgoing(e)
  with self.assertRaises(TimeoutError):await e.send(MessageChain(e.result.chain))
  original.assert_awaited_once();self.assertIsNone(e.get_extra('companion_voice_delivery_receipt'))

 async def test_sleep_between_preparation_and_send_requeues(self):
  self.mock_audio();e=Event();original=e.send;e.set_extra('companion_voice_choice','voice');await self.v.outgoing(e)
  self.sleep=True
  with self.assertRaises(RuntimeError):await e.send(MessageChain(e.result.chain))
  original.assert_not_awaited();self.assertTrue(e.stopped)

 async def test_voice_receipt_cannot_be_borrowed_from_another_recipient(self):
  e=Event();tracker=self.friends.voice_delivery
  state={'event':e,'qq':OWNER,'text':'私聊语音','mode':'voice','confirmed':False,'rejected':False}
  token=tracker.current.set(state)
  try:
   tracker.observe('send_private_msg',{'user_id':FRIEND,'message':[{'type':'record'}]},value={'message_id':321})
   self.assertFalse(state['confirmed']);self.assertIsNone(e.get_extra('companion_voice_delivery_receipt'))
  finally:tracker.current.reset(token)

 async def test_voice_archive_has_own_receipt_and_original_text_without_audio_payload(self):
  e=Event(sender=FRIEND,verified=True);tracker=self.friends.voice_delivery
  state={'event':e,'qq':FRIEND,'text':'这句话只有朋友能看到。','mode':'voice','confirmed':False,'rejected':False}
  token=tracker.current.set(state)
  try:
   parts=[{'type':'record','data':{'file':'base64://SECRET_AUDIO'}}]
   tracker.observe('send_private_msg',{'user_id':FRIEND,'message':parts},value={'message_id':321})
   archive=self.friends.archive;archive.record(FRIEND,'','out',tracker.archived(FRIEND,parts),'321')
   self.assertIn(state['text'],archive.context_messages(FRIEND)[0]['text'])
   self.assertEqual(archive.context_messages(OWNER),[])
   self.assertNotIn('voice_transcript',str(parts))
  finally:tracker.current.reset(token)

 async def test_memory_observes_voice_only_after_receipt_even_with_empty_result_chain(self):
  e=Event();e.result.chain=[];e.result.get_plain_text=lambda:'';e.set_extra('companion_voice_prepared',True)
  memory=CompanionMemory.__new__(CompanionMemory);memory.allowed=lambda e:True;memory.record_bot=Mock()
  await memory.observe_bot(e);memory.record_bot.assert_not_called()
  e.set_extra('companion_voice_delivery_receipt','123');e.set_extra('companion_voice_delivered_text','真正发出的语音原文。')
  await memory.observe_bot(e);await memory.observe_bot(e)
  memory.record_bot.assert_called_once_with('真正发出的语音原文。')

 async def test_unread_not_completed_when_voice_was_not_delivered(self):
  e=Event();e.set_extra('companion_sleep_unread',['synthetic']);e.set_extra('companion_voice_prepared',True)
  p=CompanionFriends.__new__(CompanionFriends);p.platform_id='qq-companion';p.sleep_inbox=SimpleNamespace(mark=Mock())
  await p.unread_answered(e);p.sleep_inbox.mark.assert_not_called()

 async def test_later_text_preference_survives_merge_for_autonomous_routing(self):
  p=CompanionBatch(self.p.context,{'quiet_seconds':.01,'max_seconds':.02,'natural_closure':False})
  first=Event(voice=False,text='你好');last=Event(voice=False,text='只发文字')
  await self.v.incoming(first);await self.v.incoming(last)
  task=asyncio.create_task(p.merge(first));await asyncio.sleep(0);await p.merge(last);await task
  self.assertTrue(first.get_extra('companion_voice_declined'));self.assertFalse(first.get_extra('companion_voice_explicit'))

 async def test_hint_is_request_local_and_does_not_call_another_model(self):
  p=CompanionBatch(self.p.context,{'voice_enabled':True});p.voice=self.v;e=Event(voice=False,text='你好')
  req=ProviderRequest();await p.voice_hint(e,req)
  self.assertTrue(e.get_extra('companion_voice_route_enabled'));self.assertIn('[[voice]]',req.extra_user_content_parts[0].text)


if __name__=='__main__':unittest.main()
