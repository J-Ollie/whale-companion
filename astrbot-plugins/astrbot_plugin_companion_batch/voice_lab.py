"""Owner-scoped self-voice candidates, evidence, adoption and rollback."""
import asyncio
import base64
import hashlib
import json
from pathlib import Path
import re
import time
import uuid
from .voice_profile import DEFAULT,validate

SAMPLE='行吧，那就这么着。你先忙你的，我再看看。刚才那件事，我还有一点没想明白。等我想清楚了，再慢慢跟你说。'


def save(path,value):
 path=Path(path);path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
 temp=path.with_suffix('.tmp');temp.write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8');temp.chmod(0o600);temp.replace(path)


class VoiceLab:
 def __init__(self,voice):
  self.voice=voice;self.root=voice.root;self.path=self.root/'voice-lab.json';self.lock=asyncio.Lock()

 def guard(self,event):
  if not self.voice.permitted(event) or str(event.get_sender_id())!=str(self.voice.plugin.config.get('owner_qq','100000001')):raise ValueError('self_voice_owner_only')
  if not self.voice.awake():raise ValueError('self_voice_asleep')

 def read(self):
  return json.loads(self.path.read_text(encoding='utf-8')) if self.path.exists() else {'version':1,'current':dict(DEFAULT),'revision':'base-r12','history':[],'samples':[]}

 def status(self,event):
  self.guard(event);s=self.read()
  return {'current':validate(s['current']),'revision':s['revision'],'fixed_base_voice':True,
   'adjustable':{'rate':[.88,1.02],'pitch':[.90,1.02],'breath':['base','soft','clear'],'ending':['base','relaxed','firm'],'mood':['base','happy','serious','drowsy']},
   'samples':[{'reference':x['reference'],'kind':x['kind'],'profile':x['profile'],'matches_current':x['profile']==s['current'],'available':self.audio_path(x['reference']).is_file(),'analyzed':bool(x.get('analysis'))} for x in s['samples']],
   'audio_understanding':'on_demand_actual_audio_via_qwen3-omni-flash','hearing_limit':'尚未分析的音频不能评价；用户发来的语音目前仍仅转写。',
   'latest_analysis':next(({'reference':x['reference'],'profile':x['profile'],'matches_current':x['profile']==s['current'],**x['analysis']} for x in reversed(s['samples']) if x.get('analysis')),None),
   'next_step':'已取得的状态可直接用于决定是否微调；管理员让试听时，用现有编号play，不必反复status或让管理员再次指定编号。'}

 def audio_path(self,reference):
  if not isinstance(reference,str) or not re.fullmatch(r'voice-[a-f0-9]{16}',reference):raise ValueError('invalid_voice_reference')
  return self.root/'self-audio'/(reference+'.mp3')

 def sample(self,reference):
  self.audio_path(reference)
  row=next((x for x in self.read()['samples'] if x['reference']==reference),None)
  if not row:raise ValueError('unknown_voice_reference')
  path=self.audio_path(reference)
  if not path.is_file() or path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest()!=row['sha256']:raise ValueError('sample_missing_or_changed')
  return row,path.read_bytes()

 def store(self,data,text,profile,kind='candidate',receipt=None):
  if data.get('format')!='mp3':raise ValueError('self_voice_requires_mp3')
  raw=base64.b64decode(data['audio'],validate=True)
  if not 100<len(raw)<=1800000:raise ValueError('sample_audio_size')
  s=self.read();reference='voice-'+uuid.uuid4().hex[:16];path=self.audio_path(reference)
  path.parent.mkdir(parents=True,exist_ok=True,mode=0o700);path.write_bytes(raw);path.chmod(0o600)
  row={'reference':reference,'at':time.time(),'kind':kind,'text':text,'profile':validate(profile),'sha256':hashlib.sha256(raw).hexdigest(),'seconds':data.get('seconds'),'receipt':receipt}
  s['samples'].append(row);expired=s['samples'][:-8];s['samples']=s['samples'][-8:];save(self.path,s)
  for old in expired:self.audio_path(old['reference']).unlink(missing_ok=True)
  return reference

 async def preview(self,event,changes):
  self.guard(event)
  async with self.lock:
   profile=validate({**self.read()['current'],**changes})
   if not await self.voice.reserve('tts'):raise ValueError('daily_tts_limit')
   self.guard(event);data=await self.voice.synthesize(SAMPLE,profile=profile);self.guard(event)
   reference=self.store(data,SAMPLE,profile)
   return {'reference':reference,'profile':profile,'seconds':data.get('seconds'),'applied':False,'heard_by_user':False,'next':'analyze 分析实际音频；apply 采用；play 供管理员试听。'}

 async def analyze(self,event,reference):
  self.guard(event)
  async with self.lock:
   row,raw=self.sample(reference)
   if row.get('analysis'):return {'reference':reference,'cached':True,**row['analysis']}
   if not await self.voice.reserve('review',limit=4):raise ValueError('daily_review_limit')
   self.guard(event)
   from .voice_cloud import settings
   from .voice_review import analyze
   result=await asyncio.wait_for(asyncio.to_thread(analyze,settings(self.root),raw),50)
   self.guard(event);s=self.read();current=next(x for x in s['samples'] if x['reference']==reference)
   result['audio_sha256']=row['sha256'];current['analysis']=result;save(self.path,s)
   return {'reference':reference,**result}

 async def apply(self,event,reference,note):
  self.guard(event)
  async with self.lock:
   row,_=self.sample(reference)
   if row['kind']!='candidate' or not row.get('analysis'):raise ValueError('analyzed_candidate_required')
   s=self.read()
   if s['current']==row['profile']:return {'changed':False,'revision':s['revision']}
   if not await self.voice.reserve('adjust',limit=4):raise ValueError('daily_adjust_limit')
   self.guard(event)
   s['history'].append({'profile':s['current'],'revision':s['revision'],'at':time.time()});s['history']=s['history'][-20:]
   s.update(current=validate(row['profile']),revision=reference,note=str(note)[:180]);save(self.path,s)
   return {'changed':True,'current':s['current'],'revision':reference,'reversible':True}

 async def rollback(self,event):
  self.guard(event)
  async with self.lock:
   s=self.read()
   if not s['history']:return {'changed':False,'reason':'already_at_base_or_no_history'}
   previous=s['history'].pop();s.update(current=validate(previous['profile']),revision=previous['revision']);save(self.path,s)
   return {'changed':True,'current':s['current'],'revision':s['revision']}

 async def remember_sent(self,event,data,text):
  # Do not retain or expose other contacts' voice messages to the owner's lab.
  if str(event.get_sender_id())!=str(self.voice.plugin.config.get('owner_qq','100000001')):return
  async with self.lock:
   profile=data.get('profile') or self.read()['current']
   self.store(data,text,profile,kind='sent',receipt=event.get_extra('companion_voice_delivery_receipt'))

 def playback(self,event,reference):
  self.guard(event);row,raw=self.sample(reference)
  if event.get_extra('companion_voice_declined',False):raise ValueError('voice_playback_declined')
  event.set_extra('companion_voice_playback',reference)
  return {'reference':reference,'queued_for_reply':True,'text':row['text'],'instruction':'最终用 [[voice]] 回实际试音原文。只有收到发送回执才算已发给管理员，不另发重复文字。'}
