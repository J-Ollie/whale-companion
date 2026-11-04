"""Approved Bailian voice: fixed identity and delivery, original provider MP3."""
import base64
import ipaddress
import json
from pathlib import Path
import re
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
try:from .voice_profile import validate as validate_profile,instruction as profile_instruction
except ImportError:from voice_profile import validate as validate_profile,instruction as profile_instruction

MAX_AUDIO=1800000
MODEL='qwen-audio-3.1-tts-plus'


class VoiceCloudError(Exception):pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
 def redirect_request(self,*args,**kwargs):return None


def settings(root):
 cfg=json.loads((Path(root)/'cloud-voice.json').read_text(encoding='utf-8'))
 if cfg.get('provider')!='aliyun' or cfg.get('model')!=MODEL:raise VoiceCloudError('unsupported_provider_model')
 host=cfg.get('api_host','')
 if host!='dashscope.aliyuncs.com' and not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9-]{0,62}\.cn-beijing\.maas\.aliyuncs\.com',host):
  raise VoiceCloudError('unapproved_api_host')
 if not re.fullmatch(r'[\x21-\x7e]{16,512}',cfg.get('api_key','')):raise VoiceCloudError('invalid_key')
 if not re.fullmatch(r'[A-Za-z0-9._-]{1,240}',cfg.get('voice_id','')):raise VoiceCloudError('invalid_voice_id')
 if cfg.get('rate')!=0.94 or type(cfg['rate']) is bool:raise VoiceCloudError('unapproved_rate')
 if cfg.get('pitch',1.0) not in (1.0,0.96) or type(cfg.get('pitch')) is bool:raise VoiceCloudError('unapproved_pitch')
 if not isinstance(cfg.get('instruction'),str) or not 1<=len(cfg['instruction'])<=500:raise VoiceCloudError('invalid_instruction')
 return cfg


def audio_source(url):
 if not isinstance(url,str):raise VoiceCloudError('missing_audio_source')
 p=urllib.parse.urlsplit(url);host=p.hostname or ''
 if p.scheme=='http' and host=='dashscope-result-bj.oss-cn-beijing.aliyuncs.com' and p.port is None and not p.username and not p.password:
  p=p._replace(scheme='https',fragment='')
 if p.scheme!='https' or p.username or p.password or p.port not in (None,443) or not host.endswith('.aliyuncs.com'):
  raise VoiceCloudError('unapproved_audio_source')
 addresses=socket.getaddrinfo(host,443,type=socket.SOCK_STREAM)
 if not addresses or any(not ipaddress.ip_address(r[4][0]).is_global for r in addresses):raise VoiceCloudError('nonpublic_audio_source')
 return urllib.parse.urlunsplit(p)


def validate_mp3(raw):
 if not 100<len(raw)<=MAX_AUDIO:raise VoiceCloudError('invalid_audio_size')
 if not (raw.startswith(b'ID3') or raw[0]==255 and raw[1]&224==224):raise VoiceCloudError('invalid_mp3_header')
 with tempfile.TemporaryDirectory(prefix='companion-voice-') as folder:
  path=Path(folder)/'reply.mp3';path.write_bytes(raw);path.chmod(0o600)
  result=subprocess.run(['ffprobe','-v','error','-show_entries','format=duration:stream=codec_name,sample_rate,channels',
   '-of','json',str(path)],capture_output=True,check=True,timeout=5)
  data=json.loads(result.stdout);streams=data.get('streams',[])
  duration=float(data.get('format',{}).get('duration',0))
  if not 0<duration<=35 or len(streams)!=1 or streams[0].get('codec_name')!='mp3' or streams[0].get('sample_rate')!='24000' or streams[0].get('channels')!=1:
   raise VoiceCloudError('invalid_audio_properties')
  subprocess.run(['ffmpeg','-v','error','-xerror','-i',str(path),'-f','null','-'],capture_output=True,check=True,timeout=5)
 return round(duration,3)


def synthesize(root,text,profile=None):
 if not isinstance(text,str) or not 1<=len(text)<=180 or re.search(r'https?://|```|<[^>]+>',text):raise VoiceCloudError('invalid_text')
 cfg=settings(root);started=time.monotonic()
 if profile is None and (Path(root)/'voice-lab.json').exists():profile=json.loads((Path(root)/'voice-lab.json').read_text(encoding='utf-8'))['current']
 if profile is not None:
  profile=validate_profile(profile)
  cfg={**cfg,'rate':profile['rate'],'pitch':profile['pitch'],'instruction':profile_instruction(cfg['instruction'],profile)}
 else:profile={'rate':cfg['rate'],'pitch':cfg.get('pitch',1.0),'breath':'base','ending':'base','mood':'base'}
 body={'model':cfg['model'],'input':{'text':text,'voice':cfg['voice_id'],'format':'mp3','sample_rate':24000,
  'instruction':cfg['instruction'],'rate':cfg['rate']}}
 if 'pitch' in cfg:body['input']['pitch']=cfg['pitch']
 request=urllib.request.Request('https://'+cfg['api_host']+'/api/v1/services/audio/tts/SpeechSynthesizer',
  data=json.dumps(body,ensure_ascii=False).encode(),headers={'Content-Type':'application/json','Authorization':'Bearer '+cfg['api_key']},method='POST')
 try:
  # There is exactly one synthesis attempt. A timeout never triggers a retry.
  with urllib.request.build_opener(NoRedirect).open(request,timeout=25) as response:reply=response.read(1024*1024+1)
  if len(reply)>1024*1024:raise VoiceCloudError('api_reply_too_large')
  data=json.loads(reply)
  source=audio_source(data.get('output',{}).get('audio',{}).get('url'))
  # Fresh opener and URL-only request: API authorization is never sent to OSS.
  with urllib.request.build_opener(NoRedirect).open(source,timeout=15) as response:raw=response.read(MAX_AUDIO+1)
  seconds=validate_mp3(raw)
 except (urllib.error.URLError,OSError,ValueError,subprocess.SubprocessError):
  raise VoiceCloudError('cloud_synthesis_unavailable') from None
 return {'audio':base64.b64encode(raw).decode(),'format':'mp3','seconds':seconds,
  'elapsed':round(time.monotonic()-started,3),'voice':'bailian-approved-young-adult-r12' if cfg.get('pitch')==.96 else 'bailian-approved-young-adult-r11',
  'model':cfg['model'],'speed':cfg['rate'],'pitch':cfg.get('pitch',1.0),'profile':profile,'request_id':data.get('request_id')}
