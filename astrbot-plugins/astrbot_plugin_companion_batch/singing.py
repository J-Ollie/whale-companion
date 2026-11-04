"""Checked score-conditioned clips; the rejected improvisation backend is removed."""
import asyncio,hashlib,io,json,re,time,uuid,wave
from .voice_lab import save
LIMIT=20
ENGINE='SoulX-Singer'
def normalized(text):return re.sub(r'[^a-z0-9\u4e00-\u9fff]','',str(text).lower())
NEW_SONG=re.compile(r'热门|比较火|最近.{0,8}(?:火|流行|新)|最新|新歌|换.{0,3}(?:首|段)|别的|其他|自己.{0,3}找|搜.{0,5}(?:首|歌)|别再|不要再')
REPLAY=re.compile(r'重播|再放|再听|刚才|上次|之前|那(?:首|段)|曲库|预制|再唱.{0,3}(?:一遍|一次)|做好|完成|进展')
FOLLOWUP=re.compile(r'做好|做完|完成|进展|结果|唱好了|制作.{0,4}好')

class Singing:
 def __init__(self,voice):
  self.voice=voice;self.root=voice.root;self.path=self.root/'singing-state.json';self.lock=asyncio.Lock()
  from .singing_jobs import SingingJobs
  from .singing_sources import SingingSources
  self.jobs=SingingJobs(self.root);self.sources=SingingSources(self)
 def read(self):return json.loads(self.path.read_text(encoding='utf-8')) if self.path.exists() else {'version':1,'jobs':[]}
 def catalog(self):
  path=self.root/'scored-singing.json'
  if not path.exists():return None
  if path.is_symlink() or path.stat().st_size>100000:raise ValueError('invalid_scored_catalog')
  data=json.loads(path.read_text(encoding='utf-8'))
  if not isinstance(data,dict):raise ValueError('invalid_scored_catalog')
  if data.get('version')!=1 or data.get('engine')!=ENGINE or data.get('control')!='score' or not re.fullmatch(r'scored-\d{8}-\d{6}',data.get('release','')):raise ValueError('unsupported_scored_catalog')
  clips=data.get('clips',[])
  if not isinstance(clips,list) or not 1<=len(clips)<=20:raise ValueError('invalid_scored_catalog')
  identifiers=set()
  for row in clips:
   if not isinstance(row,dict) or not isinstance(row.get('checks'),dict):raise ValueError('invalid_scored_clip')
   ref=row.get('reference','');checks=row.get('checks',{})
   if not isinstance(row.get('title'),str) or not 1<=len(row['title'])<=80:raise ValueError('invalid_scored_clip_title')
   for key in ('moods','aliases'):
    if not isinstance(row.get(key,[]),list) or any(not isinstance(x,str) or len(x)>60 for x in row.get(key,[])):raise ValueError('invalid_scored_clip_tags')
   if (not re.fullmatch(r'score-[a-z0-9-]{1,40}',ref) or ref in identifiers or not isinstance(row.get('lyrics'),str) or not 2<=len(row['lyrics'])<=80 or not re.fullmatch('[a-f0-9]{64}',row.get('sha256','')) or not 2<=float(row.get('seconds',0))<=LIMIT or checks.get('transcript_exact') is not True or checks.get('all_notes_within_semitone') is not True or checks.get('no_clipping') is not True):raise ValueError('unchecked_scored_clip')
   identifiers.add(ref)
  return data
 def paused(self):
  try:
   c=self.catalog()
   if not c:return True
   path=self.root/'singing-quality-hold.json'
   if not path.exists():return False
   hold=json.loads(path.read_text(encoding='utf-8'))
   return not isinstance(hold,dict) or hold.get('allow_scored_release')!=c['release']
  except (OSError,ValueError,TypeError,KeyError):return True
 def guard(self,event,allow_status=False):
  self.voice.lab.guard(event)
  if self.paused() and not allow_status:raise ValueError('singing_paused_no_checked_score_release')
  if event.get_extra('companion_voice_declined',False):raise ValueError('voice_declined')
  if not allow_status and re.search(r'(?:别|不想|不要|不用|停止|暂停).{0,4}(?:唱|歌)',getattr(event,'message_str','')):raise ValueError('singing_declined')
 def request_mode(self,event):
  text=getattr(event,'message_str','') or ''
  if FOLLOWUP.search(text) and not re.search(r'换|别的|其他|再找|重新找',text):return 'task_followup'
  if NEW_SONG.search(text):return 'new_song'
  if REPLAY.search(text):return 'replay_allowed'
  try:titles=[r['title'] for r in (self.catalog() or {}).get('clips',[])]
  except (OSError,ValueError,TypeError):titles=[]
  if any(t and t in text for t in titles):return 'replay_allowed'
  return 'new_song' if re.search(r'唱|歌',text) else 'unspecified'
 def guard_playback(self,event,row):
  if self.request_mode(event)=='task_followup' and not row.get('local_job'):raise ValueError('task_followup_cannot_use_unrelated_cached_clip')
  if self.request_mode(event)=='new_song' and row.get('local_job')!=event.get_extra('companion_singing_requested_job','not_requested'):
   raise ValueError('new_song_request_cannot_replay_cached_clip')
 def status(self,event):
  self.guard(event,allow_status=True)
  try:c=self.catalog()
  except (OSError,ValueError,TypeError):c=None
  mode=self.request_mode(event);fresh=mode=='new_song';followup=mode=='task_followup'
  return {'ready':not self.paused(),'paused':self.paused(),'request_mode':mode,'cached_playback_allowed':not fresh and not followup,'capability':'score_library_and_local_gpu_jobs','engine':ENGINE,'legacy_improvisation_disabled':True,'local_worker':self.jobs.worker_status(),'worker_status_note':'online表示连接，ready表示目前可领取任务；ready=false不等于没联网。','local_jobs':[j for j in self.jobs.recent() if not fresh or j['status'] in ('queued','running')],'score_import':['MIDI','MusicXML','MXL'],'new_songs_need_actual_score':True,'daily_selection_limit':2,'shares_existing_tts_quota':True,'maximum_seconds':LIMIT,
   'catalog':[{k:r.get(k) for k in ('reference','title','melody','lyrics','moods','seconds')} for r in c['clips']] if c and not fresh and not followup else [],
   'songs':[{'reference':r['reference'],'lyrics':r['lyrics'],'seconds':r.get('seconds'),'delivery_confirmed':bool(r.get('receipt'))} for r in self.read()['jobs'] if not fresh and (not followup or r.get('local_job')) and r.get('engine')==ENGINE and r.get('state')=='ready'][-4:],
   'note':'select是现有成品；新歌先公开搜索并read页面，再inspect真实乐谱链接，request提交中文短词和声部。电脑在线且空闲时本地GPU生成；task查结果。只有歌词不能冒充有原曲乐谱，离线或待处理不能说已唱完。'}

 async def request(self,event,reference,lyrics,kind,lyrics_reference,track,start_note,title):
  self.guard(event)
  enabled=self.root/'local-singing-enabled.json'
  if not enabled.is_file() or json.loads(enabled.read_text()).get('enabled') is not True:raise ValueError('local_singing_not_enabled')
  payload=self.sources.prepare(reference,lyrics,kind,lyrics_reference,track,start_note,title)
  raw=getattr(getattr(event,'message_obj',None),'raw_message',{})
  identity=str(raw.get('message_id')) if isinstance(raw,dict) and raw.get('message_id') is not None else event.get_extra('companion_singing_identity','')
  if not identity:identity=uuid.uuid4().hex;event.set_extra('companion_singing_identity',identity)
  identity=str(event.get_sender_id())+':'+identity
  async with self.lock:
   existing=self.jobs.find(payload,identity)
   if existing:identifier=existing
   else:
    if not await self.voice.reserve('singing',limit=2):raise ValueError('daily_singing_limit')
    if not await self.voice.reserve('tts'):raise ValueError('daily_tts_limit')
    self.guard(event);identifier=self.jobs.submit(payload,identity)
  event.set_extra('companion_singing_requested_job',identifier)
  # Most local renders finish within this turn. Offline work remains durable, not fictitiously done.
  until=time.monotonic()+45
  while time.monotonic()<until:
   row=self.jobs.get(identifier)
   if row['status'] not in ('queued','running'):break
   status=self.jobs.worker_status()
   if not status.get('online') or not self.voice.awake():break
   await asyncio.sleep(1.5)
  return await self.task(event,identifier)

 async def task(self,event,reference):
  self.guard(event);row=self.jobs.get(reference)
  if row['status']!='complete':
   return {'reference':reference,'status':row['status'],'queued':False,'local_worker':self.jobs.worker_status(),'result':row['result'],
    'note':'这是制作任务状态，不是已生成或已发送；电脑在线空闲、角色清醒才执行。完成后可task取得并播放，不承诺后台一定发来。'}
  result=row['result'];raw=(self.jobs.root/'local-songs'/(reference+'.wav')).read_bytes()
  if hashlib.sha256(raw).hexdigest()!=result['sha256']:raise ValueError('local_song_changed')
  if not result.get('audio_review'):
   if not await self.voice.reserve('review',limit=4):raise ValueError('daily_audio_review_limit')
   import subprocess
   from .voice_cloud import settings
   from .voice_review import analyze,PROMPT
   def review():
    encoded=subprocess.run(['ffmpeg','-v','error','-i','pipe:0','-ar','24000','-ac','1','-c:a','libmp3lame','-b:a','128k','-f','mp3','pipe:1'],input=raw,capture_output=True,check=True,timeout=8).stdout
    return analyze(settings(self.root),encoded,prompt=PROMPT+' transcript逐字记录全部实际唱出的中文歌词，不补写；不要因有旋律就断言好听。')
   report=await asyncio.wait_for(asyncio.to_thread(review),50);self.guard(event)
   result['audio_review']=report;self.jobs.analysis(reference,report)
  transcript=normalized(result['audio_review']['report']['transcript'])
  if transcript!=normalized(row['payload']['lyrics']):raise ValueError('quality_lyrics_transcription_mismatch')
  async with self.lock:
   state=self.read();existing=next((j for j in state['jobs'] if j.get('local_job')==reference),None)
   if existing:identifier=existing['reference']
   else:
    identifier='song-'+uuid.uuid4().hex[:16];path=self.audio_path(identifier);path.parent.mkdir(parents=True,exist_ok=True,mode=0o700);path.write_bytes(raw);path.chmod(0o600)
    state['jobs'].append({'reference':identifier,'at':time.time(),'lyrics':row['payload']['lyrics'],'title':row['payload']['title'],'seconds':result['seconds'],'engine':ENGINE,'control':'score','state':'ready','sha256':result['sha256'],'local_job':reference,'source':row['payload']['source'],'receipt':None})
    save(self.path,state)
  return {**self.playback(event,identifier),'local_job':reference,'local_generation_completed':True,'generated_at':row['updated'],'lyrics':row['payload']['lyrics'],'seconds':result['seconds'],'source':row['payload']['source']}
 def clip(self,reference):
  c=self.catalog();row=next((r for r in c['clips'] if r['reference']==reference),None) if c else None
  if not row:raise ValueError('song_not_in_scored_catalog')
  path=self.root/'scored-singing'/c['release']/(reference+'.wav')
  if path.is_symlink() or not path.is_file() or not 100<path.stat().st_size<=LIMIT*48000+44:raise ValueError('scored_clip_missing')
  raw=path.read_bytes()
  if hashlib.sha256(raw).hexdigest()!=row['sha256']:raise ValueError('scored_clip_changed')
  with wave.open(io.BytesIO(raw),'rb') as wav:
   if wav.getnchannels()!=1 or wav.getsampwidth()!=2 or wav.getframerate()!=24000 or abs(wav.getnframes()/24000-row['seconds'])>.025:raise ValueError('scored_clip_properties')
  return row,raw,c['release']
 def choose(self,reference='',query=''):
  if reference:return self.clip(reference)
  c=self.catalog()
  if not c:raise ValueError('no_scored_catalog')
  key=normalized(query)
  if not key:return self.clip(c['clips'][0]['reference'])
  matches=[r for r in c['clips'] if any(normalized(x) and normalized(x) in key for x in [r['title'],*r.get('aliases',[]),*r.get('moods',[])])]
  if not matches:raise ValueError('song_not_in_scored_catalog')
  return self.clip(matches[0]['reference'])
 async def select(self,event,reference='',query=''):
  self.guard(event)
  async with self.lock:
   row,raw,release=self.choose(reference,query);self.guard(event);self.guard_playback(event,row)
   if not await self.voice.reserve('singing',limit=2):raise ValueError('daily_singing_limit')
   if not await self.voice.reserve('tts'):raise ValueError('daily_tts_limit')
   self.guard(event);state=self.read();identifier='song-'+uuid.uuid4().hex[:16]
   path=self.audio_path(identifier);path.parent.mkdir(parents=True,exist_ok=True,mode=0o700);path.write_bytes(raw);path.chmod(0o600)
   job={'reference':identifier,'at':time.time(),'lyrics':row['lyrics'],'title':row['title'],'seconds':row['seconds'],'engine':ENGINE,'control':'score','state':'ready','sha256':row['sha256'],'clip_reference':row['reference'],'release':release,'rendered_at':row['rendered_at'],'prepared_now':False,'receipt':None}
   state['jobs'].append(job);expired=state['jobs'][:-12];state['jobs']=state['jobs'][-12:];save(self.path,state)
   for old in expired:self.audio_path(old['reference']).unlink(missing_ok=True)
   return {**self.playback(event,identifier),'title':row['title'],'lyrics':row['lyrics'],'seconds':row['seconds'],'kind':'previously_rendered_score_conditioned_clip','new_synthesis_performed':False,'arbitrary_original_song_cover':False}
 def audio_path(self,reference):
  if not isinstance(reference,str) or not re.fullmatch(r'song-[a-f0-9]{16}',reference):raise ValueError('invalid_song_reference')
  return self.root/'songs'/(reference+'.wav')
 def sample(self,reference):
  path=self.audio_path(reference);row=next((r for r in self.read()['jobs'] if r['reference']==reference and r.get('state')=='ready'),None)
  if not row or row.get('engine')!=ENGINE or row.get('control')!='score':raise ValueError('legacy_or_unknown_singing_sample')
  if row.get('local_job'):
   job=self.jobs.get(row['local_job'])
   if job['status']!='complete' or job['result']['sha256']!=row['sha256']:raise ValueError('local_song_receipt_changed')
  else:
   c=self.catalog()
   if not c or row.get('release')!=c['release']:raise ValueError('song_release_not_current')
   self.clip(row['clip_reference'])
  if not path.is_file() or path.is_symlink() or not 44<path.stat().st_size<=LIMIT*48000+44:raise ValueError('song_missing_or_changed')
  raw=path.read_bytes()
  if hashlib.sha256(raw).hexdigest()!=row['sha256']:raise ValueError('song_missing_or_changed')
  return row,raw
 def playback(self,event,reference):
  self.guard(event);row,raw=self.sample(reference);self.guard_playback(event,row);event.set_extra('companion_singing_playback',reference)
  return {'reference':reference,'queued':True,'sent':False,'next_step':'结束调用，系统发实际清唱，只发一条音频；回执前不能说已送达，也不声称刚生成新歌。'}
 def delivered(self,reference,receipt):
  state=self.read();row=next(r for r in state['jobs'] if r['reference']==reference);row['receipt']=str(receipt);row['sent_at']=time.time();save(self.path,state)
