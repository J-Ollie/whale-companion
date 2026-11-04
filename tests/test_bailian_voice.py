"""Bounded network contract and original audio integrity; no paid API calls."""
import base64
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch,Mock
import urllib.error

sys.path.insert(0,os.environ.get('COMPANION_TEST_PLUGINS',str(Path(__file__).parent)))
from astrbot_plugin_companion_batch import voice_cloud as v


class CloudVoiceTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
  self.cfg={'provider':'aliyun','model':v.MODEL,'api_key':'sk-test-only-safe-key',
   'api_host':'dashscope.aliyuncs.com','voice_id':'test-voice-v3.1','rate':.94,'instruction':'自然交谈，句尾平稳。'}
  self.write()
 def write(self):
  (self.root/'cloud-voice.json').write_text(json.dumps(self.cfg),encoding='utf-8')
 def test_settings_keep_model_voice_and_rate_fixed(self):
  self.assertEqual(v.settings(self.root),self.cfg)
  for field,value in [('rate',.8),('model','other'),('api_host','localhost'),('api_key','bad\r\nkey'),('voice_id','../other')]:
   old=self.cfg[field];self.cfg[field]=value;self.write()
   with self.assertRaises(v.VoiceCloudError):v.settings(self.root)
   self.cfg[field]=old
 def test_known_http_result_is_upgraded_without_an_http_request(self):
  with patch.object(v.socket,'getaddrinfo',return_value=[(2,1,6,'',('8.8.8.8',443))]):
   self.assertEqual(v.audio_source('http://dashscope-result-bj.oss-cn-beijing.aliyuncs.com/a?sig=x'),
    'https://dashscope-result-bj.oss-cn-beijing.aliyuncs.com/a?sig=x')
 def test_untrusted_urls_and_local_dns_are_blocked(self):
  for url in ['http://other.aliyuncs.com/a','https://example.com/a','https://u:p@a.aliyuncs.com/a','https://a.aliyuncs.com:444/a','file:///tmp/a']:
   with self.assertRaises(v.VoiceCloudError):v.audio_source(url)
  with patch.object(v.socket,'getaddrinfo',return_value=[(2,1,6,'',('127.0.0.1',443))]):
   with self.assertRaises(v.VoiceCloudError):v.audio_source('https://a.aliyuncs.com/a')
 def test_no_redirects(self):self.assertIsNone(v.NoRedirect().redirect_request(None,None,None,None,None,None))
 def test_approved_lower_pitch_is_forwarded_and_other_values_rejected(self):
  self.cfg['pitch']=.96;self.write()
  response={'output':{'audio':{'url':'https://a.aliyuncs.com/a'}}}
  opener=Mock();opener.open.side_effect=[io.BytesIO(json.dumps(response).encode()),io.BytesIO(b'ID3'+b'x'*200)]
  with patch.object(v.urllib.request,'build_opener',return_value=opener),patch.object(v,'audio_source',side_effect=lambda s:s),patch.object(v,'validate_mp3',return_value=2):
   result=v.synthesize(self.root,'行吧，那就这么着。')
  self.assertEqual(json.loads(opener.open.call_args_list[0].args[0].data)['input']['pitch'],.96)
  self.assertEqual(result['pitch'],.96)
  for pitch in (.8,True,'0.96',float('nan')):
   self.cfg['pitch']=pitch;self.write()
   with self.assertRaises(v.VoiceCloudError):v.settings(self.root)
 def test_api_timeout_is_not_retried_and_exception_contains_no_secret(self):
  opener=Mock();opener.open.side_effect=urllib.error.URLError('sensitive-provider-detail')
  with patch.object(v.urllib.request,'build_opener',return_value=opener):
   with self.assertRaises(v.VoiceCloudError) as caught:v.synthesize(self.root,'行吧。')
   self.assertEqual(opener.open.call_count,1)
   self.assertEqual(str(caught.exception),'cloud_synthesis_unavailable')
 def test_exact_mp3_bytes_and_text_only_sent_to_approved_provider(self):
  raw=b'ID3'+b'original-audio'*30
  response={'output':{'audio':{'url':'https://a.aliyuncs.com/a?sig=x'}},'request_id':'test'}
  opener=Mock();opener.open.side_effect=[io.BytesIO(json.dumps(response).encode()),io.BytesIO(raw)]
  with patch.object(v.urllib.request,'build_opener',return_value=opener),patch.object(v,'audio_source',side_effect=lambda s:s),patch.object(v,'validate_mp3',return_value=2):
   result=v.synthesize(self.root,'我不是那个意思。')
  self.assertEqual(base64.b64decode(result['audio']),raw)
  api_call=opener.open.call_args_list[0].args[0];download=opener.open.call_args_list[1].args[0]
  self.assertEqual(json.loads(api_call.data)['input']['text'],'我不是那个意思。')
  self.assertEqual(json.loads(api_call.data)['input']['rate'],.94)
  self.assertEqual(json.loads(api_call.data)['input']['voice'],self.cfg['voice_id'])
  self.assertEqual(api_call.get_header('Authorization'),'Bearer '+self.cfg['api_key'])
  self.assertIsInstance(download,str)
  self.assertNotIn(self.cfg['api_key'],json.dumps(result))
 def test_oversize_download_rejected(self):
  with self.assertRaises(v.VoiceCloudError):v.validate_mp3(b'ID3'+b'x'*v.MAX_AUDIO)
 def test_corrupt_or_html_audio_rejected(self):
  with self.assertRaises(v.VoiceCloudError):v.validate_mp3(b'<html>'+b'x'*200)
 def test_unsuitable_text_never_calls_network(self):
  with patch.object(v.urllib.request,'build_opener') as net:
   for text in ['', 'x'*181,'https://example.com','```code```','<speak>hi</speak>']:
    with self.assertRaises(v.VoiceCloudError):v.synthesize(self.root,text)
   net.assert_not_called()


if __name__=='__main__':unittest.main()
