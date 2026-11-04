"""Contact-bound QQ voice messages. No microphone or independent persona."""
import asyncio
import base64
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from astrbot.api import logger
from astrbot.api.message_components import Plain,Record

REQUEST=re.compile(r'(?:用|发|来段|来条|说段|回段).{0,6}语音|语音.{0,4}(?:回复|回我|说|读)|读给我听|想听.{0,6}(?:你说|你的声音)')
DECLINE=re.compile(r'(?:不要|不想|别|不用|不方便|不需要).{0,6}(?:语音|声音)|文字.{0,4}(?:就好|回复)|(?:只|仅)(?:用|发|回|要).{0,3}文字|不方便听')
ROUTE=re.compile(r'^\s*\[\[(voice|text)\]\]\s*',re.I)


class Voice:
 def __init__(self,plugin,root):
  self.plugin=plugin;self.root=Path(root);self.path=self.root/'state.json';self.lock=asyncio.Lock();self.busy=False
  from .voice_lab import VoiceLab
  self.lab=VoiceLab(self)
  from .singing import Singing
  self.singing=Singing(self)

 def meta(self,name):
  lookup=getattr(self.plugin.context,'get_registered_star',None)
  m=lookup('astrbot_plugin_companion_'+name) if lookup else None
  return m.star_cls if m and m.activated else None

 def permitted(self,event):
  cfg=self.plugin.config;sender=str(event.get_sender_id());platform=cfg.get('platform_id','qq-companion')
  return bool(cfg.get('enabled',True) and cfg.get('voice_enabled',False) and event.get_platform_id()==platform
   and event.unified_msg_origin==f'{platform}:FriendMessage:{sender}'
   and not getattr(event,'get_group_id',lambda:'')()
   and (sender==str(cfg.get('owner_qq','100000001')) or event.get_extra('companion_friend_verified',False)))

 def awake(self):
  friends=self.meta('friends')
  return bool(friends is not None and not friends.sleeping())

 def defer(self,event):
  friends=self.meta('friends')
  if friends:
   ids=event.get_extra('companion_sleep_unread',[])
   if ids:friends.sleep_inbox.mark(ids,'pending')
   else:
    raw=getattr(event.message_obj,'raw_message',None)
    if isinstance(raw,dict) and raw.get('message_type')=='private':friends.sleep_inbox.add(raw)
  event.should_call_llm(True);event.stop_event()

 async def reserve(self,kind,limit=20):
  async with self.lock:
   day=datetime.now(ZoneInfo('Asia/Shanghai')).date().isoformat()
   state=json.loads(self.path.read_text()) if self.path.exists() else {}
   if state.get('day')!=day:state={'day':day,'stt':0,'tts':0}
   maximum=20 if kind in ('stt','tts') else min(4,limit)
   if state.get(kind,0)>=maximum:return False
   state[kind]=state.get(kind,0)+1
   self.root.mkdir(parents=True,exist_ok=True,mode=0o700)
   tmp=self.path.with_suffix('.tmp');tmp.write_text(json.dumps(state));tmp.chmod(0o600);tmp.replace(self.path)
   return True

 async def incoming(self,event):
  if not self.permitted(event):return
  parts=event.message_obj.message
  records=[p for p in parts if isinstance(p,Record)]
  text=event.message_str or ''
  event.set_extra('companion_voice_declined',bool(DECLINE.search(text)))
  event.set_extra('companion_voice_explicit',bool(REQUEST.search(text) and not DECLINE.search(text)))
  event.set_extra('companion_voice_requested',bool(not DECLINE.search(text) and (records or REQUEST.search(text))))
  if not records:return
  # Transport sleep inbox runs before this event; check again if a queued event crosses bedtime.
  if not self.awake():self.defer(event);return
  raw=getattr(event.message_obj,'raw_message',None)
  refs=event.get_extra('companion_sleep_voice_refs',None)
  if refs is None:
   refs=[raw] if isinstance(raw,dict) else []
  ids=[]
  for r in refs:
   if not isinstance(r,dict) or str(r.get('user_id',''))!=str(event.get_sender_id()) or r.get('message_type')!='private':continue
   segs=r.get('message',[])
   count=sum(isinstance(s,dict) and s.get('type')=='record' for s in segs) if isinstance(segs,list) else 0
   # Native QQ conversion handles exactly one voice per original message. Never guess which recording was transcribed.
   identifier=r.get('message_id')
   if count==1 and isinstance(identifier,(int,str)) and re.fullmatch(r'-?[0-9]{1,20}',str(identifier)) and int(identifier):ids.append(int(identifier))
  converted=[];index=0
  for part in parts:
   if not isinstance(part,Record):converted.append(part);continue
   transcript='';identifier=ids[index] if len(ids)==len(records) else None;index+=1
   try:
    if identifier is not None and self.awake() and await self.reserve('stt'):
     adapter=self.plugin.context.get_platform_inst(self.plugin.config.get('platform_id','qq-companion'))
     value=await asyncio.wait_for(adapter.get_client().call_action('fetch_ptt_text',message_id=identifier),24)
     if isinstance(value,dict) and value.get('retcode',0)==0:
      data=value.get('data',value)
      transcript=data.get('text','') if isinstance(data,dict) else ''
     if not isinstance(transcript,str) or not 1<=len(transcript.strip())<=4000:transcript=''
   except asyncio.CancelledError:raise
   except Exception as e:logger.warning('Voice recognition unavailable: %s',type(e).__name__)
   if transcript and self.awake():
    if DECLINE.search(transcript):
     event.set_extra('companion_voice_declined',True);event.set_extra('companion_voice_requested',False)
     event.set_extra('companion_voice_explicit',False)
    elif REQUEST.search(transcript):event.set_extra('companion_voice_explicit',True)
    converted.append(Plain('[语音转写，可能有识别误差] '+transcript.strip()))
    if str(event.get_sender_id())!=str(self.plugin.config.get('owner_qq','100000001')):
     friends=self.meta('friends')
     if friends and hasattr(friends.archive,'annotate_voice'):
      try:friends.archive.annotate_voice(str(event.get_sender_id()),str(identifier),transcript.strip())
      except Exception as e:logger.warning('Voice annotation deferred after %s',type(e).__name__)
   else:converted.append(Plain('[收到语音，但本次未能可靠转写；不能猜测内容，可以请对方改发文字]'))
  if not self.awake():self.defer(event);return
  event.message_obj.message=converted
  event.message_str=''.join(p.text for p in converted if isinstance(p,Plain))
  event.message_obj.message_str=event.message_str
  event.set_extra('companion_voice_input',True)

 async def synthesize(self,text,profile=None):
  if self.busy:raise RuntimeError('voice_busy')
  if not 1<=len(text)<=180 or re.search(r'https?://|```|<[^>]+>',text):raise ValueError('voice_text_unsuitable')
  self.busy=True;process=None
  try:
   process=await asyncio.create_subprocess_exec(sys.executable,str(Path(__file__).with_name('voice_worker.py')),
    stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
   request={'text':text,'root':str(self.root)}
   if profile is not None:request['profile']=profile
   output,errors=await asyncio.wait_for(process.communicate(json.dumps(request,ensure_ascii=False).encode()),55)
   if process.returncode or len(output)>3000000:raise RuntimeError('voice_synthesis_failed')
   data=json.loads(output)
   wave=base64.b64decode(data.get('audio') if data.get('format')=='mp3' else data.get('wav',''),validate=True)
   if data.get('format')=='mp3':
    valid=100<len(wave)<=1800000 and (wave.startswith(b'ID3') or wave[0]==255 and wave[1]&224==224)
   else:valid=44<len(wave)<=1800000 and wave.startswith(b'RIFF') and wave[8:12]==b'WAVE'
   if not valid:raise ValueError('invalid_voice')
   return data
  finally:
   if process and process.returncode is None:
    process.kill();await process.wait()
   self.busy=False

 def capture_response(self,event,response):
  if not self.permitted(event) or not event.get_extra('companion_voice_route_enabled',False) or getattr(response,'role',None)!='assistant':return
  text=response.completion_text or '';match=ROUTE.match(text)
  if match:
   response.completion_text=text[match.end():]
   event.set_extra('companion_voice_choice',match[1].lower())

 def install_delivery_guard(self,event,record,text,data=None):
  friends=self.meta('friends');tracker=getattr(friends,'voice_delivery',None)
  original=getattr(event,'send',None)
  if tracker is None or not callable(original):return False
  attempted=False
  async def send(chain):
   nonlocal attempted
   if not any(p is record for p in chain.chain):return await original(chain)
   if attempted:return
   attempted=True
   if not self.permitted(event):raise RuntimeError('voice_send_not_permitted')
   if not self.awake():self.defer(event);raise RuntimeError('voice_send_asleep')
   state={'event':event,'qq':str(event.get_sender_id()),'text':text,'mode':'voice','confirmed':False,'rejected':False}
   token=tracker.current.set(state)
   try:
    try:await original(chain)
    except Exception:
     if not state['rejected']:raise
    if not state['confirmed']:
     # Retry as text only after a definite gateway rejection; an uncertain timeout can already have sent audio.
     if not state['rejected']:raise RuntimeError('voice_send_unconfirmed')
     if not self.permitted(event):raise RuntimeError('voice_fallback_not_permitted')
     if not self.awake():self.defer(event);raise RuntimeError('voice_fallback_asleep')
     fallback='清唱语音未能发送。' if data and data.get('song_reference') else text
     state.update(mode='text',rejected=False,text=fallback)
     await original(chain.derive([Plain(fallback)]))
     if not state['confirmed']:raise RuntimeError('voice_fallback_unconfirmed')
    elif data and data.get('song_reference'):
     try:self.singing.delivered(data['song_reference'],event.get_extra('companion_voice_delivery_receipt'))
     except Exception as exc:logger.warning('Singing receipt retention skipped after %s',type(exc).__name__)
    elif data and data.get('format')=='mp3':
     try:await self.lab.remember_sent(event,data,text)
     except Exception as exc:logger.warning('Own voice sample retention skipped after %s',type(exc).__name__)
   finally:tracker.current.reset(token)
  event.send=send
  return True

 async def outgoing(self,event):
  if not self.permitted(event) or not self.awake():return
  result=event.get_result()
  if not result or not result.is_model_result() or not result.chain or not all(isinstance(p,Plain) for p in result.chain):return
  if event.get_extra('companion_voice_prepared',False):return
  text=result.get_plain_text().strip()
  match=ROUTE.match(text) if event.get_extra('companion_voice_route_enabled',False) else None
  if match:
   event.set_extra('companion_voice_choice',match[1].lower());text=text[match.end():]
   result.chain=[Plain(text)]
  if event.get_extra('companion_voice_declined',False):return
  playback=event.get_extra('companion_voice_playback',None)
  song=event.get_extra('companion_singing_playback',None)
  if song:playback=None
  choice='voice' if song or playback or event.get_extra('companion_voice_explicit',False) else event.get_extra('companion_voice_choice',None)
  if choice!='voice' and not (choice is None and event.get_extra('companion_voice_requested',False)):return
  if not getattr(self.meta('friends'),'voice_delivery',None) or not callable(getattr(event,'send',None)):return
  if not song and not playback and (not 1<=len(text)<=180 or re.search(r'https?://|```|<[^>]+>',text)):return
  try:
   if song:
    self.singing.guard(event);sample,raw=self.singing.sample(song);self.singing.guard_playback(event,sample);text=sample['lyrics']
    data={'wav':base64.b64encode(raw).decode(),'format':'wav','seconds':sample['seconds'],'song_reference':song}
   elif playback:
    self.lab.guard(event);sample,raw=self.lab.sample(playback);text=sample['text']
    data={'audio':base64.b64encode(raw).decode(),'format':'mp3','profile':sample['profile'],'seconds':sample['seconds']}
   else:
    if not await self.reserve('tts'):return
    data=await self.synthesize(text)
   if not self.awake() or event.get_result() is not result:return
   record=Record.fromBase64(data['audio'] if data.get('format')=='mp3' else data['wav'],text=text)
   if not self.install_delivery_guard(event,record,text,data=data):return
   result.chain=[record]
   event.set_extra('companion_voice_prepared',True)
   # This means prepared only. Existing after-send hooks require the actual send receipt.
  except asyncio.CancelledError:raise
  except Exception as e:logger.warning('Voice reply kept as text after %s',type(e).__name__)
