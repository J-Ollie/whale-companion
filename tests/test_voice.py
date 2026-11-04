"""No QQ messages, microphones or external recognition calls in this suite."""
import asyncio
import base64
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

sys.path.insert(0,os.environ.get('COMPANION_TEST_PLUGINS',str(Path(__file__).parent)))
from astrbot_plugin_companion_batch.voice import Voice
from astrbot_plugin_companion_batch.main import CompanionBatch
from astrbot_plugin_companion_friends.archive import ChatArchive
from astrbot_plugin_companion_friends.delivery import VoiceDelivery
from astrbot.core.message.message_event_result import MessageChain
from astrbot.api.message_components import Plain,Record

OWNER='100000001';FRIEND='12345678'
class Event:
 def __init__(self,sender=OWNER,voice=True,text='',group='',platform='qq-companion',verified=False):
  self.sender=sender;self.platform=platform;self.group=group;self.message_str=text;self.unified_msg_origin=f'{platform}:FriendMessage:{sender}'
  self.extra={'companion_friend_verified':verified};self.stopped=False
  self.send=AsyncMock()
  self.message_obj=SimpleNamespace(message_str=text,message=([Record(file='cached.amr')] if voice else [Plain(text)]),raw_message={
   'user_id':int(sender),'message_type':'private','message_id':-91,'message':[{'type':'record','data':{'file':'cached.amr'}}] if voice else []})
  self.result=SimpleNamespace(chain=[Plain('我在这里，慢慢说就好。')],is_model_result=lambda:True,get_plain_text=lambda:'我在这里，慢慢说就好。')
 def get_sender_id(self):return self.sender
 def get_platform_id(self):return self.platform
 def get_group_id(self):return self.group
 def get_extra(self,k,d=None):return self.extra.get(k,d)
 def set_extra(self,k,v):self.extra[k]=v
 def should_call_llm(self,v):pass
 def stop_event(self):self.stopped=True
 def get_result(self):return self.result

class VoiceTests(unittest.IsolatedAsyncioTestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
  self.client=SimpleNamespace(call_action=AsyncMock(return_value={'text':'明天去图书馆。'}))
  self.sleep=False;self.friends=SimpleNamespace(sleeping=lambda:self.sleep,sleep_inbox=SimpleNamespace(add=lambda r:None,mark=lambda ids,s:None),archive=ChatArchive(Path(self.tmp.name)/'friends'),voice_delivery=VoiceDelivery())
  ctx=SimpleNamespace(get_registered_star=lambda name:SimpleNamespace(activated=True,star_cls=self.friends) if name.endswith('_friends') else None,
   get_platform_inst=lambda name:SimpleNamespace(get_client=lambda:self.client))
  self.p=SimpleNamespace(context=ctx,config={'voice_enabled':True})
  self.v=Voice(self.p,Path(self.tmp.name)/'voice')
 def mock_audio(self):
  self.v.synthesize=AsyncMock(return_value={'wav':base64.b64encode(b'RIFF'+b'0'*40).decode(),'voice':'fixed'})
 async def test_actual_sender_id_is_bound_before_recognition(self):
  for e in [Event(sender=FRIEND),Event(group='1'),Event(platform='other')]:await self.v.incoming(e)
  self.client.call_action.assert_not_called();self.assertFalse(self.v.path.exists())
 async def test_owner_voice_is_transcribed_before_memory_and_requested_reply(self):
  e=Event();await self.v.incoming(e)
  self.client.call_action.assert_awaited_once_with('fetch_ptt_text',message_id=-91)
  self.assertIn('可能有识别误差',e.message_str);self.assertIn('明天去图书馆',e.message_str)
  self.assertTrue(e.get_extra('companion_voice_requested'));self.assertTrue(all(isinstance(p,Plain) for p in e.message_obj.message))
 async def test_asleep_event_requeued_without_processing(self):
  self.sleep=True;e=Event();await self.v.incoming(e)
  self.assertTrue(e.stopped);self.client.call_action.assert_not_called();self.assertFalse(self.v.path.exists())
 async def test_replay_uses_each_original_voice_id_in_order(self):
  e=Event();e.message_obj.message.append(Record(file='second.amr'))
  raw=dict(e.message_obj.raw_message,message_id=-92)
  e.set_extra('companion_sleep_voice_refs',[e.message_obj.raw_message,raw]);await self.v.incoming(e)
  self.assertEqual([c.kwargs['message_id'] for c in self.client.call_action.await_args_list],[-91,-92])
 async def test_voice_can_decline_an_audio_reply(self):
  self.client.call_action.return_value={'text':'不用发语音，文字回复就好。'}
  e=Event();await self.v.incoming(e);self.assertFalse(e.get_extra('companion_voice_requested'));self.assertTrue(e.get_extra('companion_voice_declined'))
 async def test_later_decline_cancels_earlier_batch_voice_request(self):
  p=CompanionBatch(self.p.context,{'quiet_seconds':.01,'max_seconds':.02,'natural_closure':False})
  first=Event(voice=False,text='发个语音');last=Event(voice=False,text='不方便听，文字回复就好')
  await self.v.incoming(first);await self.v.incoming(last)
  task=asyncio.create_task(p.merge(first));await asyncio.sleep(0);await p.merge(last);await task
  self.assertFalse(first.get_extra('companion_voice_requested'))
 async def test_raw_other_sender_or_missing_id_never_recognized(self):
  for raw in [dict(Event().message_obj.raw_message,user_id=int(FRIEND)),dict(Event().message_obj.raw_message,message_id=0)]:
   e=Event();e.message_obj.raw_message=raw;await self.v.incoming(e);self.assertIn('未能可靠转写',e.message_str)
  self.client.call_action.assert_not_called()
 async def test_ambiguous_multiple_record_message_is_not_guessed(self):
  e=Event();e.message_obj.message.append(Record(file='b.amr'));e.message_obj.raw_message['message'].append({'type':'record'})
  await self.v.incoming(e);self.client.call_action.assert_not_called()
 async def test_failure_has_no_fabricated_transcript(self):
  self.client.call_action.side_effect=TimeoutError();e=Event();await self.v.incoming(e)
  self.assertIn('不能猜测内容',e.message_str);self.assertNotIn('明天',e.message_str)
 async def test_bedtime_during_recognition_stops_event(self):
  async def recognize(*a,**kw):self.sleep=True;return {'text':'刚才的语音'}
  self.client.call_action.side_effect=recognize;e=Event();await self.v.incoming(e);self.assertTrue(e.stopped)
 async def test_friend_transcript_is_appended_only_to_own_original_record(self):
  a=self.friends.archive;a.record(FRIEND,'朋友','in',[{'type':'record','data':{}}],'-91')
  a.record(OWNER,'管理员','in',[{'type':'record','data':{}}],'-91')
  e=Event(sender=FRIEND,verified=True);await self.v.incoming(e)
  self.assertIn('明天去图书馆',a.context_messages(FRIEND)[0]['text']);self.assertNotIn('明天',a.context_messages(OWNER)[0]['text'])
  self.assertFalse(a.annotate_voice(FRIEND,'-91','不同转写'))
 async def test_only_explicit_text_request_or_voice_triggers_audio(self):
  for text,wanted in [('你好',False),('用语音回我吧',True),('不要发语音',False)]:
   e=Event(voice=False,text=text);await self.v.incoming(e);self.assertEqual(e.get_extra('companion_voice_requested'),wanted)
 async def test_prepared_audio_replaces_visible_text_and_is_not_send_receipt(self):
  self.mock_audio();e=Event();e.set_extra('companion_voice_requested',True);await self.v.outgoing(e)
  self.assertEqual(len(e.result.chain),1);self.assertIsInstance(e.result.chain[0],Record)
  self.assertEqual(e.result.chain[0].text,'我在这里，慢慢说就好。')
  self.assertTrue(e.get_extra('companion_voice_prepared'));self.assertIsNone(e.get_extra('companion_bot_saved'))
  await self.v.outgoing(e);self.assertEqual(len(e.result.chain),1)
 async def test_synthesis_failure_leaves_text_no_retry(self):
  self.v.synthesize=AsyncMock(side_effect=RuntimeError());e=Event();e.set_extra('companion_voice_requested',True)
  await self.v.outgoing(e);self.assertEqual(len(e.result.chain),1);self.v.synthesize.assert_awaited_once()
 async def test_cloud_mp3_only_keeps_transcript_as_metadata(self):
  self.v.synthesize=AsyncMock(return_value={'audio':base64.b64encode(b'ID3'+b'x'*150).decode(),'format':'mp3'})
  e=Event();e.set_extra('companion_voice_requested',True);await self.v.outgoing(e)
  self.assertEqual(len(e.result.chain),1);self.assertIsInstance(e.result.chain[0],Record)
  self.assertEqual(e.result.chain[0].text,'我在这里，慢慢说就好。')
 async def test_sleep_during_synthesis_drops_audio(self):
  async def synth(t):self.sleep=True;return {'wav':'a'}
  self.v.synthesize=synth;e=Event();e.set_extra('companion_voice_requested',True);await self.v.outgoing(e);self.assertEqual(len(e.result.chain),1)
 async def test_daily_limit_is_durable_and_separate(self):
  for i in range(20):self.assertTrue(await self.v.reserve('stt'))
  other=Voice(self.p,self.v.root);self.assertFalse(await other.reserve('stt'));self.assertTrue(await other.reserve('tts'))
 async def test_merged_batch_keeps_voice_request_from_later_message(self):
  p=CompanionBatch(self.p.context,{'quiet_seconds':.01,'max_seconds':.02,'natural_closure':False})
  first=Event(voice=False,text='你好');last=Event(voice=False,text='用语音回复');last.set_extra('companion_voice_requested',True)
  task=asyncio.create_task(p.merge(first));await asyncio.sleep(0);await p.merge(last);await task
  self.assertTrue(first.get_extra('companion_voice_requested'));self.assertTrue(last.stopped)

class BackupTests(unittest.TestCase):
 def test_skill_sqlite_and_voice_counts_restore_but_models_audio_and_secrets_do_not(self):
  import sqlite3
  import zipfile
  from contextlib import closing
  from astrbot_plugin_companion_memory.portable import create_archive,restore_archive
  with tempfile.TemporaryDirectory() as directory:
   root=Path(directory);data=root/'data';memory=data/'plugin_data/companion_memory';memory.mkdir(parents=True)
   with closing(sqlite3.connect(memory/'memory.sqlite3')) as db,db:db.execute('CREATE TABLE observations(id INTEGER PRIMARY KEY,text TEXT)')
   phone=data/'plugin_data/companion_phone';phone.mkdir()
   with closing(sqlite3.connect(phone/'skills.sqlite3')) as db,db:
    db.execute('CREATE TABLE evidence(id TEXT PRIMARY KEY,result TEXT)');db.execute("INSERT INTO evidence VALUES('test','focused_search_editor')")
   voice=data/'plugin_data/companion_voice';voice.mkdir();(voice/'state.json').write_text('{"day":"2026-11-01","stt":2,"tts":1}')
   for name in ('deps/private.key','model/large.onnx','audition.wav','credentials.json'):
    p=voice/name;p.parent.mkdir(exist_ok=True);p.write_text('NOT_PORTABLE')
   archive=create_archive(data,root/'archives');restore_archive(archive,root/'isolated')
   target=root/'isolated/data/plugin_data'
   with closing(sqlite3.connect(target/'companion_phone/skills.sqlite3')) as db:self.assertEqual(db.execute('SELECT result FROM evidence').fetchone()[0],'focused_search_editor')
   self.assertEqual((target/'companion_voice/state.json').read_text(),(voice/'state.json').read_text())
   self.assertEqual([p.name for p in (target/'companion_voice').iterdir()],['state.json'])

if __name__=='__main__':unittest.main()
