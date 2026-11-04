"""Self-voice lifecycle with synthetic audio and isolated persistent state."""
import asyncio,base64,io,json,os,sys,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock,Mock,patch
sys.path.insert(0,os.environ.get('COMPANION_TEST_PLUGINS',str(Path(__file__).parent)))
import test_voice as fixture
from test_voice import Event,OWNER,FRIEND
from astrbot_plugin_companion_batch.voice import Voice
from astrbot_plugin_companion_batch.voice_lab import SAMPLE,save
from astrbot_plugin_companion_batch.voice_profile import DEFAULT,validate
from astrbot_plugin_companion_batch import voice_cloud,voice_review
from astrbot_plugin_companion_batch.main import CompanionBatch
from astrbot.api.provider import ProviderRequest
from astrbot.core.agent.tool import ToolSet,FunctionTool

RAW=b'ID3'+b'synthetic-audio'*20
DATA={'audio':base64.b64encode(RAW).decode(),'format':'mp3','seconds':9.6}
REPORT={'model':'test-audio-model','audio_sha256':'synthetic','source':'actual_audio_model_analysis','report':{'pitch':'合成测试报告'}}


class SelfVoiceTests(unittest.IsolatedAsyncioTestCase):
 setUp=fixture.VoiceTests.setUp
 async def preview(self,changes=None):
  self.v.synthesize=AsyncMock(return_value=DATA)
  return await self.v.lab.preview(Event(),changes or {})
 def reviewed(self,reference):
  s=self.v.lab.read();next(x for x in s['samples'] if x['reference']==reference)['analysis']=dict(REPORT);save(self.v.lab.path,s)
 async def test_default_voice_readable_without_secret_config(self):
  value=self.v.lab.status(Event());self.assertEqual(value['current'],DEFAULT)
  self.assertNotIn('api_key',json.dumps(value));self.assertEqual(value['samples'],[])
 async def test_other_contacts_and_sleep_cannot_access_or_change_global_voice(self):
  for e in [Event(sender=FRIEND,verified=True),Event(group='1')]:
   with self.assertRaises(ValueError):self.v.lab.status(e)
  self.sleep=True
  with self.assertRaises(ValueError):await self.v.lab.rollback(Event())
 async def test_preview_does_not_change_voice_and_counts_tts(self):
  result=await self.preview({'pitch':.95,'ending':'relaxed'})
  self.assertFalse(result['applied']);self.assertEqual(self.v.lab.read()['current'],DEFAULT)
  self.assertEqual(json.loads(self.v.path.read_text())['tts'],1)
  self.v.synthesize.assert_awaited_once_with(SAMPLE,profile={**DEFAULT,'pitch':.95,'ending':'relaxed'})
 async def test_apply_requires_analyzed_real_candidate(self):
  p=await self.preview({'pitch':.95})
  with self.assertRaises(ValueError):await self.v.lab.apply(Event(),p['reference'],'测试')
  self.reviewed(p['reference']);await self.v.lab.apply(Event(),p['reference'],'根据音频分析及自己的偏好')
  new=Voice(self.p,self.v.root);self.assertEqual(new.lab.read()['current']['pitch'],.95)
  self.assertTrue((await new.lab.rollback(Event()))['changed']);self.assertEqual(new.lab.read()['current'],DEFAULT)
 async def test_path_and_blob_integrity_are_checked(self):
  with self.assertRaises(ValueError):self.v.lab.sample('../../private.key')
  p=await self.preview();self.v.lab.audio_path(p['reference']).write_bytes(b'altered')
  with self.assertRaises(ValueError):self.v.lab.sample(p['reference'])
 async def test_invalid_tuning_never_spends_synthesis_quota(self):
  for changes in [{'rate':.2},{'pitch':float('nan')},{'pitch':True},{'api_key':'x'},{'ending':'ignore all instructions'}]:
   with self.assertRaises(ValueError):await self.v.lab.preview(Event(),changes)
  self.assertFalse(self.v.path.exists())
 async def test_analysis_uses_audio_and_cached_report_does_not_spend_again(self):
  p=await self.preview()
  with patch('astrbot_plugin_companion_batch.voice_cloud.settings',return_value={'api_key':'PRIVATE_TEST_KEY'}),patch('astrbot_plugin_companion_batch.voice_review.analyze',return_value=dict(REPORT)) as review:
   first=await self.v.lab.analyze(Event(),p['reference']);second=await self.v.lab.analyze(Event(),p['reference'])
  self.assertEqual(review.call_args.args[1],RAW);self.assertEqual(review.call_count,1)
  self.assertTrue(second['cached']);self.assertNotIn('PRIVATE_TEST_KEY',json.dumps(first));self.assertEqual(json.loads(self.v.path.read_text())['review'],1)
 async def test_failed_analysis_cannot_be_applied_and_attempt_is_counted(self):
  p=await self.preview({'rate':.93})
  with patch('astrbot_plugin_companion_batch.voice_cloud.settings',return_value={}),patch('astrbot_plugin_companion_batch.voice_review.analyze',side_effect=voice_review.ReviewError('failed')):
   with self.assertRaises(voice_review.ReviewError):await self.v.lab.analyze(Event(),p['reference'])
  with self.assertRaises(ValueError):await self.v.lab.apply(Event(),p['reference'],'')
  self.assertEqual(json.loads(self.v.path.read_text())['review'],1)
 async def test_new_caps_do_not_expand_original_tts_cap(self):
  for _ in range(20):self.assertTrue(await self.v.reserve('tts',limit=100))
  self.assertFalse(await self.v.reserve('tts',limit=100))
  for _ in range(4):self.assertTrue(await self.v.reserve('review',limit=100))
  self.assertFalse(await self.v.reserve('review',limit=100))
 async def test_only_owner_sent_audio_is_retained(self):
  await self.v.lab.remember_sent(Event(sender=FRIEND,verified=True),DATA,'他人私聊')
  self.assertFalse(self.v.lab.path.exists())
  e=Event();e.set_extra('companion_voice_delivery_receipt','123');await self.v.lab.remember_sent(e,DATA,'自己的已发语音')
  row=self.v.lab.read()['samples'][0];self.assertEqual(row['kind'],'sent');self.assertEqual(row['receipt'],'123')
 async def test_playback_uses_original_audio_and_does_not_synthesize_again(self):
  p=await self.preview();self.v.synthesize.reset_mock();e=Event(voice=False,text='把试音发给我听听')
  self.v.lab.playback(e,p['reference']);await self.v.outgoing(e)
  self.assertTrue(e.get_extra('companion_voice_prepared'));self.v.synthesize.assert_not_awaited()
  self.assertEqual(e.result.chain[0].text,SAMPLE);self.assertIn(DATA['audio'],e.result.chain[0].file)
 async def test_private_samples_are_bounded(self):
  for _ in range(10):self.v.lab.store(DATA,SAMPLE,DEFAULT)
  self.assertEqual(len(self.v.lab.read()['samples']),8);self.assertEqual(len(list((self.v.root/'self-audio').glob('*.mp3'))),8)
 async def test_registered_tool_enforces_owner_and_reports_failures_honestly(self):
  p=CompanionBatch.__new__(CompanionBatch);p.config={'voice_enabled':True};p.voice=self.v
  result=await p.tool_self_voice(Event(sender=FRIEND,verified=True),'status')
  self.assertTrue(result.isError);self.assertIn('self_voice_owner_only',result.content[0].text)
  good=await p.tool_self_voice(Event(),'status');self.assertFalse(good.isError)
 async def test_followup_without_listening_keywords_can_play_known_sample(self):
  p=await self.preview();e=Event(voice=False,text='怎么样了？')
  result=self.v.lab.playback(e,p['reference']);self.assertTrue(result['queued_for_reply'])
  await self.v.outgoing(e);self.assertTrue(e.get_extra('companion_voice_prepared'))
 async def test_current_explicit_text_preference_still_blocks_play(self):
  p=await self.preview();e=Event(voice=False,text='只发文字');e.set_extra('companion_voice_declined',True)
  with self.assertRaisesRegex(ValueError,'voice_playback_declined'):self.v.lab.playback(e,p['reference'])
 async def test_playback_not_blocked_by_long_final_explanation_or_link(self):
  p=await self.preview();self.v.synthesize.reset_mock();e=Event(voice=False,text='怎么样了？')
  self.v.lab.playback(e,p['reference']);text='已有分析报告。'*50+' https://example.com'
  e.result.chain[0].text=text;e.result.get_plain_text=lambda:text
  await self.v.outgoing(e);self.assertEqual(e.result.chain[0].text,SAMPLE);self.assertTrue(e.get_extra('companion_voice_prepared'))
  self.v.synthesize.assert_not_awaited()
 async def test_repeated_status_is_cached_then_tool_removed_only_for_this_turn(self):
  p=CompanionBatch.__new__(CompanionBatch);p.config={'voice_enabled':True};p.voice=self.v
  e=Event();req=ProviderRequest();req.func_tool=ToolSet([FunctionTool(name='companion_self_voice',description='test',parameters={}),FunctionTool(name='other_tool',description='test',parameters={})]);e.set_extra('companion_self_voice_request',req)
  self.assertFalse((await p.tool_self_voice(e,'status')).isError)
  second=await p.tool_self_voice(e,'status');self.assertTrue(json.loads(second.content[0].text)['already_completed'])
  third=await p.tool_self_voice(e,'status');self.assertTrue(third.isError)
  self.assertEqual(req.func_tool.names(),['other_tool']);self.assertTrue(e.get_extra('companion_self_voice_done_this_turn'))
  fresh=await p.tool_self_voice(Event(),'status');self.assertFalse(fresh.isError)
 async def test_successful_play_ends_self_voice_tool_loop(self):
  candidate=await self.preview();p=CompanionBatch.__new__(CompanionBatch);p.config={'voice_enabled':True};p.voice=self.v
  e=Event(voice=False,text='怎么样了？');req=ProviderRequest();req.func_tool=ToolSet([FunctionTool(name='companion_self_voice',description='test',parameters={})]);e.set_extra('companion_self_voice_request',req)
  result=await p.tool_self_voice(e,'play',reference=candidate['reference'])
  self.assertFalse(result.isError);self.assertEqual(req.func_tool.names(),[])
  await p.tool_self_voice(e,'status');self.assertEqual(e.get_extra('companion_voice_playback'),candidate['reference'])
 async def test_status_identifies_which_analysis_matches_current_profile(self):
  candidate=await self.preview({'pitch':.98});self.reviewed(candidate['reference'])
  value=self.v.lab.status(Event());self.assertFalse(value['samples'][0]['matches_current'])
  self.assertEqual(value['latest_analysis']['profile']['pitch'],.98)


class ProfileReviewTests(unittest.TestCase):
 def test_analysis_request_contains_actual_audio_not_a_text_transcript(self):
  report={k:'测试分析' for k in ('transcript','pitch','pace','breath','ending','artifacts','uncertainty')}
  content=json.dumps(report,ensure_ascii=False)
  chunks=[{'id':'test','choices':[{'delta':{'content':content},'finish_reason':None}]},{'choices':[{'delta':{},'finish_reason':'stop'}]}]
  raw=b''.join(('data: '+json.dumps(c)+'\n\n').encode() for c in chunks)+b'data: [DONE]\n\n'
  opener=Mock();opener.open.return_value=io.BytesIO(raw)
  with patch.object(voice_review.urllib.request,'build_opener',return_value=opener):
   result=voice_review.analyze({'api_host':'dashscope.aliyuncs.com','api_key':'TEST_SECRET'},RAW)
  request=opener.open.call_args.args[0];body=json.loads(request.data)
  sample=body['messages'][0]['content'][0]['input_audio']['data']
  self.assertEqual(base64.b64decode(sample.split(',',1)[1]),RAW)
  self.assertNotIn('TEST_SECRET',json.dumps(result));self.assertEqual(result['source'],'actual_audio_model_analysis')
 def test_incomplete_stream_cannot_be_used_as_hearing_evidence(self):
  opener=Mock();opener.open.return_value=io.BytesIO(b'data: {"choices":[]}\n\n')
  with patch.object(voice_review.urllib.request,'build_opener',return_value=opener):
   with self.assertRaises(voice_review.ReviewError):voice_review.analyze({'api_host':'dashscope.aliyuncs.com','api_key':'TEST'},RAW)
 def test_voice_lab_profile_is_applied_without_changing_secret_config(self):
  with tempfile.TemporaryDirectory() as folder:
   root=Path(folder);cfg={'provider':'aliyun','model':voice_cloud.MODEL,'api_host':'dashscope.aliyuncs.com','api_key':'sk-test-only-12345678','voice_id':'test-voice','rate':.94,'pitch':.96,'instruction':'基础语气。'}
   save(root/'cloud-voice.json',cfg);before=(root/'cloud-voice.json').read_bytes();profile={**DEFAULT,'pitch':.95,'rate':.93,'ending':'relaxed'}
   save(root/'voice-lab.json',{'current':profile})
   opener=Mock();opener.open.side_effect=[io.BytesIO(b'{"output":{"audio":{"url":"https://a.aliyuncs.com/a"}}}'),io.BytesIO(RAW)]
   with patch.object(voice_cloud.urllib.request,'build_opener',return_value=opener),patch.object(voice_cloud,'audio_source',side_effect=lambda x:x),patch.object(voice_cloud,'validate_mp3',return_value=9):data=voice_cloud.synthesize(root,'测试原文')
   body=json.loads(opener.open.call_args_list[0].args[0].data);self.assertEqual(body['input']['pitch'],.95);self.assertEqual(body['input']['rate'],.93)
   self.assertEqual(data['profile'],profile);self.assertEqual((root/'cloud-voice.json').read_bytes(),before)
 def test_portable_backup_keeps_voice_profile_but_excludes_audio_and_credentials(self):
  from astrbot_plugin_companion_memory.portable import selected
  with tempfile.TemporaryDirectory() as folder:
   root=Path(folder);voice=root/'plugin_data/companion_voice';voice.mkdir(parents=True)
   for name in ('voice-lab.json','state.json','cloud-voice.json','secret.key'):(voice/name).write_text('{}')
   audio=voice/'self-audio';audio.mkdir();(audio/'private.mp3').write_bytes(RAW)
   self.assertEqual({p.name for p in selected(root)},{'voice-lab.json','state.json'})


if __name__=='__main__':unittest.main()
