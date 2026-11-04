"""Score library selection, hold scope, quota, isolation and actual audio receipt."""
import hashlib,io,json,os,sys,time,unittest,wave
from pathlib import Path
sys.path.insert(0,os.environ.get('COMPANION_TEST_PLUGINS',str(Path(__file__).parent)))
import test_voice
from test_voice import Event,FRIEND
from astrbot_plugin_companion_batch.voice import Voice
from astrbot_plugin_companion_batch.main import CompanionBatch
from astrbot_plugin_companion_batch.voice_lab import save
from astrbot.api.provider import ProviderRequest
from astrbot.api.message_components import Record
from astrbot.core.message.message_event_result import MessageChain
RELEASE='scored-20261101-190000'

def fixture_catalog(root):
 stream=io.BytesIO()
 with wave.open(stream,'wb') as w:w.setnchannels(1);w.setsampwidth(2);w.setframerate(24000);w.writeframes(b'\0\0'*72000)
 raw=stream.getvalue();folder=root/'scored-singing'/RELEASE;folder.mkdir(parents=True,exist_ok=True)
 rows=[]
 for name,title,moods in [('calm','晚风',['安静','睡前']),('happy','好心情',['开心','轻快'])]:
  ref='score-'+name;(folder/(ref+'.wav')).write_bytes(raw)
  rows.append({'reference':ref,'title':title,'melody':'test score','lyrics':'晚风轻轻伴着你','moods':moods,'aliases':[title],'seconds':3.0,'sha256':hashlib.sha256(raw).hexdigest(),'rendered_at':time.time(),'checks':{'transcript_exact':True,'all_notes_within_semitone':True,'no_clipping':True}})
 save(root/'scored-singing.json',{'version':1,'engine':'SoulX-Singer','control':'score','release':RELEASE,'clips':rows})
 return raw

class SingingTests(unittest.IsolatedAsyncioTestCase):
 def setUp(self):
  self.h=test_voice.VoiceTests();self.h.setUp();self.addCleanup(self.h.doCleanups);self.v=self.h.v;self.s=self.v.singing;fixture_catalog(self.v.root);self.e=Event(voice=False,text='重播刚才那段，自己选。')
 async def test_known_mood_selects_real_clip_without_new_synthesis_or_sending(self):
  result=await self.s.select(self.e,query='想听轻快一点的');row=self.s.read()['jobs'][0]
  self.assertEqual(row['clip_reference'],'score-happy');self.assertTrue(result['queued']);self.assertFalse(result['sent']);self.assertFalse(result['new_synthesis_performed']);self.e.send.assert_not_called()
  state=json.loads(self.v.path.read_text());self.assertEqual(state['singing'],1);self.assertEqual(state['tts'],1)
 async def test_unlisted_song_does_not_silently_become_another_song(self):
  with self.assertRaisesRegex(ValueError,'song_not_in_scored_catalog'):await self.s.select(self.e,query='告白气球')
  self.assertFalse(self.v.path.exists());self.assertFalse(self.s.path.exists())
 async def test_hot_song_request_cannot_replay_or_select_old_clip(self):
  previous=await self.s.select(self.e,reference='score-calm')
  hot=Event(voice=False,text='那你唱一段最近比较火的歌吧')
  status=self.s.status(hot);self.assertEqual(status['request_mode'],'new_song');self.assertEqual(status['catalog'],[]);self.assertEqual(status['songs'],[])
  with self.assertRaisesRegex(ValueError,'new_song_request_cannot_replay'):self.s.playback(hot,previous['reference'])
  with self.assertRaisesRegex(ValueError,'new_song_request_cannot_replay'):await self.s.select(hot,reference='score-happy')
  self.assertIsNone(hot.get_extra('companion_singing_playback'));self.assertEqual(json.loads(self.v.path.read_text())['singing'],1)
 async def test_generic_singing_request_is_new_unless_explicitly_replaying(self):
  for text in ('唱首歌','给我唱一小段，你自己选','换首别的歌','别再唱晚风，找一首新的'):
   self.assertEqual(self.s.request_mode(Event(voice=False,text=text)),'new_song')
  for text in ('再放一遍刚才那段','唱晚风','重播听风'):
   self.assertEqual(self.s.request_mode(Event(voice=False,text=text)),'replay_allowed')
 async def test_decorating_hook_also_blocks_a_stale_injected_playback(self):
  previous=await self.s.select(self.e,reference='score-calm');hot=Event(voice=False,text='唱一首新的')
  hot.set_extra('companion_singing_playback',previous['reference']);await self.v.outgoing(hot)
  self.assertFalse(any(isinstance(p,Record) for p in hot.result.chain))
 def test_newly_requested_local_result_is_allowed_in_new_song_turn(self):
  event=Event(voice=False,text='唱首新歌');event.set_extra('companion_singing_requested_job','singjob-'+'a'*20)
  self.s.guard_playback(event,{'local_job':'singjob-'+'a'*20})
  with self.assertRaises(ValueError):self.s.guard_playback(event,{'local_job':'singjob-'+'b'*20})
 def test_followup_about_new_job_does_not_hide_completed_tasks_or_offer_old_library(self):
  event=Event(voice=False,text='新歌做好了吗');self.assertEqual(self.s.request_mode(event),'task_followup')
  status=self.s.status(event);self.assertEqual(status['catalog'],[])
  with self.assertRaises(ValueError):self.s.guard_playback(event,{'clip_reference':'score-calm'})
  self.s.guard_playback(event,{'local_job':'singjob-'+'a'*20})
 async def test_owner_sleep_and_decline_are_checked_before_quota(self):
  with self.assertRaises(ValueError):await self.s.select(Event(sender=FRIEND,verified=True))
  self.h.sleep=True
  with self.assertRaises(ValueError):await self.s.select(self.e)
  self.h.sleep=False;self.e.set_extra('companion_voice_declined',True)
  with self.assertRaises(ValueError):await self.s.select(self.e)
  self.assertFalse(self.v.path.exists())
 async def test_existing_quality_hold_only_lifted_for_exact_new_release(self):
  save(self.v.root/'singing-quality-hold.json',{'paused':True})
  self.assertTrue(self.s.status(self.e)['paused'])
  with self.assertRaises(ValueError):await self.s.select(self.e)
  save(self.v.root/'singing-quality-hold.json',{'paused':True,'allow_scored_release':RELEASE})
  self.assertTrue(self.s.status(self.e)['ready']);await self.s.select(self.e)
  save(self.v.root/'singing-quality-hold.json',{'allow_scored_release':'scored-20261031-190000'})
  with self.assertRaises(ValueError):self.s.playback(self.e,self.s.read()['jobs'][0]['reference'])
 async def test_quota_persists_after_reloading_without_widening_tts(self):
  await self.s.select(self.e);await self.s.select(Event(voice=False,text='重播'))
  fresh=Voice(self.v.plugin,self.v.root)
  with self.assertRaisesRegex(ValueError,'daily_singing_limit'):await fresh.singing.select(self.e)
  self.assertEqual(json.loads(self.v.path.read_text())['tts'],2)
 async def test_audio_hash_change_rejected_before_quota_and_before_playback(self):
  row=await self.s.select(self.e);path=self.s.audio_path(row['reference']);path.write_bytes(path.read_bytes()+b'changed')
  with self.assertRaises(ValueError):self.s.playback(self.e,row['reference'])
  path=self.v.root/'scored-singing'/RELEASE/'score-happy.wav';path.write_bytes(path.read_bytes()+b'changed')
  with self.assertRaises(ValueError):await self.s.select(self.e,reference='score-happy')
 async def test_rejected_legacy_audio_remains_in_history_but_cannot_be_played(self):
  legacy={'reference':'song-'+'a'*16,'state':'ready','lyrics':'old','seconds':3,'receipt':None};save(self.s.path,{'version':1,'jobs':[legacy]})
  with self.assertRaisesRegex(ValueError,'legacy_or_unknown'):self.s.sample(legacy['reference'])
  await self.s.select(self.e);self.assertEqual(self.s.read()['jobs'][0],legacy)
 async def test_single_record_and_true_receipt_with_actual_lyrics(self):
  result=await self.s.select(self.e);sent=[]
  async def fake_send(chain):
   sent.append(chain);self.h.friends.voice_delivery.observe('send_private_msg',{'user_id':self.e.sender,'message':[{'type':'record','data':{}}]},value={'message_id':889})
  self.e.send=fake_send;await self.v.outgoing(self.e);self.assertEqual(len(self.e.result.chain),1);self.assertIsInstance(self.e.result.chain[0],Record)
  self.assertIsNone(self.s.read()['jobs'][0]['receipt']);await self.e.send(MessageChain(self.e.result.chain))
  self.assertEqual(self.s.read()['jobs'][0]['receipt'],'889');self.assertEqual(self.e.get_extra('companion_voice_delivered_text'),'晚风轻轻伴着你');self.assertEqual(len(sent),1)
 async def test_hold_removes_singing_tool_but_keeps_self_voice(self):
  save(self.v.root/'singing-quality-hold.json',{'paused':True});p=CompanionBatch.__new__(CompanionBatch);p.voice=self.v;p.config=self.v.plugin.config;req=ProviderRequest();await p.voice_hint(self.e,req)
  self.assertNotIn('companion_sing',req.func_tool.names());self.assertIn('companion_self_voice',req.func_tool.names())
 async def test_actual_tool_caches_selection_and_does_not_reinject_after_finish(self):
  p=CompanionBatch.__new__(CompanionBatch);p.voice=self.v;p.config=self.v.plugin.config;req=ProviderRequest();await p.voice_hint(self.e,req);self.assertIn('companion_sing',req.func_tool.names())
  self.assertFalse((await p.tool_sing(self.e,'select',reference='score-calm')).isError)
  await p.tool_sing(self.e,'select',reference='score-calm');self.assertEqual(json.loads(self.v.path.read_text())['singing'],1)
  await p.voice_hint(self.e,req);self.assertNotIn('companion_sing',req.func_tool.names());self.assertNotIn('companion_self_voice',req.func_tool.names())
  self.assertTrue((await p.tool_sing(self.e,'create')).isError)
 def test_corrupt_manifest_does_not_break_normal_voice_hint(self):
  save(self.v.root/'scored-singing.json',[]);self.assertTrue(self.s.paused());self.assertFalse(self.s.status(self.e)['ready'])
 def test_unchecked_clip_never_becomes_ready(self):
  p=self.v.root/'scored-singing.json';v=json.loads(p.read_text());v['clips'][0]['checks']['transcript_exact']=False;save(p,v);self.assertTrue(self.s.paused())
 def test_paths_cannot_escape_catalog(self):
  with self.assertRaises(ValueError):self.s.choose(reference='../../private')
  with self.assertRaises(ValueError):self.s.audio_path('../private')

if __name__=='__main__':unittest.main()
