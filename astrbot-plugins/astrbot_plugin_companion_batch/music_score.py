"""Bounded MusicXML/MXL and standard MIDI import. No code, files or network execution."""
import bisect,io,math,re,struct,zipfile
from xml.etree import ElementTree as ET

MAX_BYTES=1024*1024

def xml_root(raw):
 if len(raw)>2*MAX_BYTES or b'<!ENTITY' in raw.upper():raise ValueError('unsafe_musicxml')
 root=ET.fromstring(raw)
 if sum(1 for _ in root.iter())>50000:raise ValueError('musicxml_too_complex')
 for element in root.iter():element.tag=element.tag.rsplit('}',1)[-1]
 return root

def musicxml(raw):
 if raw.startswith(b'PK'):
  with zipfile.ZipFile(io.BytesIO(raw)) as archive:
   files=archive.infolist()
   if len(files)>64 or sum(i.file_size for i in files)>2*MAX_BYTES or any(i.flag_bits&1 for i in files):raise ValueError('musicxml_archive_limit')
   manifest=xml_root(archive.read('META-INF/container.xml'))
   name=manifest.find('.//rootfile').get('full-path','')
   if name.startswith(('/','\\')) or '..' in name.split('/') or name not in archive.namelist():raise ValueError('musicxml_archive_path')
   raw=archive.read(name)
 root=xml_root(raw)
 if root.tag!='score-partwise':raise ValueError('only_partwise_musicxml_supported')
 names={p.get('id'):p.findtext('part-name','') for p in root.findall('./part-list/score-part')}
 tracks=[]
 for part in root.findall('part'):
  lanes={};divisions=1;tempo=100;offset=0
  for measure in part.findall('measure'):
   at=offset;end=offset;last={}
   for item in measure:
    if item.tag=='attributes':divisions=float(item.findtext('divisions',str(divisions)))
    elif item.tag=='direction':
     sound=item.find('sound')
     if sound is not None and sound.get('tempo'):tempo=float(sound.get('tempo'))
    elif item.tag=='sound' and item.get('tempo'):tempo=float(item.get('tempo'))
    elif item.tag in ('backup','forward'):
     at+=(-1 if item.tag=='backup' else 1)*float(item.findtext('duration','0'))/divisions*60/tempo
    elif item.tag=='note':
     duration=float(item.findtext('duration','0'))/divisions*60/tempo
     if item.find('grace') is not None:continue
     lane=(item.findtext('staff','1'),item.findtext('voice','1'));chord=item.find('chord') is not None
     start=last.get(lane,at) if chord else at
     pitch=item.find('pitch')
     if pitch is not None:
      note=12*(int(pitch.findtext('octave','4'))+1)+{'C':0,'D':2,'E':4,'F':5,'G':7,'A':9,'B':11}[pitch.findtext('step')]+int(float(pitch.findtext('alter','0')))
      lanes.setdefault(lane,[]).append({'start':start,'duration':duration,'pitch':note})
     if not chord:last[lane]=at;at+=duration
     end=max(end,at)
   offset=end
  for lane,notes in lanes.items():tracks.append({'name':names.get(part.get('id'),'')+' staff '+lane[0]+' voice '+lane[1],'notes':notes})
 return tracks

def variable(data,pos):
 value=0
 for _ in range(4):
  b=data[pos];pos+=1;value=(value<<7)|(b&127)
  if b<128:return value,pos
 raise ValueError('invalid_midi_integer')

def midi(raw):
 if raw[:4]!=b'MThd' or len(raw)<14:raise ValueError('invalid_midi')
 size=struct.unpack('>I',raw[4:8])[0];kind,count,division=struct.unpack('>HHH',raw[8:14])
 if kind not in (0,1) or not 1<=count<=64 or division&32768 or not division:raise ValueError('unsupported_midi_format')
 pos=8+size;tracks=[];tempos=[(0,500000)]
 for _ in range(count):
  if raw[pos:pos+4]!=b'MTrk':raise ValueError('invalid_midi_track')
  length=struct.unpack('>I',raw[pos+4:pos+8])[0];data=raw[pos+8:pos+8+length];pos+=8+length
  at=0;i=0;running=None;active={};notes=[];name=''
  while i<len(data):
   delta,i=variable(data,i);at+=delta;status=data[i]
   if status>=128:i+=1
   elif running is not None:status=running
   else:raise ValueError('invalid_midi_running_status')
   if status==255:
    meta=data[i];i+=1;n,i=variable(data,i);value=data[i:i+n];i+=n
    if meta==81 and len(value)==3:tempos.append((at,int.from_bytes(value,'big')))
    if meta==3:name=value.decode('utf-8',errors='replace')[:80]
    if meta==47:break
    continue
   if status in (240,247):n,i=variable(data,i);i+=n;running=None;continue
   if not 128<=status<240:raise ValueError('unsupported_midi_event')
   running=status;family=status>>4;channel=status&15;n=1 if family in (12,13) else 2;v=data[i:i+n];i+=n
   if len(v)!=n:raise ValueError('truncated_midi')
   if channel==9:continue
   key=(channel,v[0])
   if family==9 and v[1]>0:active[key]=at
   elif family==8 or family==9:
    start=active.pop(key,None)
    if start is not None and at>start:notes.append({'start_tick':start,'end_tick':at,'pitch':key[1]})
   if len(notes)>10000:raise ValueError('midi_too_complex')
  tracks.append({'name':name,'notes':notes})
 tempos=sorted(dict(tempos).items());ticks=[t for t,_ in tempos];seconds=[0.]
 for i in range(1,len(tempos)):seconds.append(seconds[-1]+(ticks[i]-ticks[i-1])*tempos[i-1][1]/division/1000000)
 def to_seconds(tick):
  i=bisect.bisect_right(ticks,tick)-1
  return seconds[i]+(tick-ticks[i])*tempos[i][1]/division/1000000
 for track in tracks:
  track['notes']=[{'start':to_seconds(n['start_tick']),'duration':to_seconds(n['end_tick'])-to_seconds(n['start_tick']),'pitch':n['pitch']} for n in track['notes']]
 return tracks

def parse(raw):
 if not isinstance(raw,bytes) or not 20<=len(raw)<=MAX_BYTES:raise ValueError('score_file_limit')
 try:tracks=midi(raw) if raw.startswith(b'MThd') else musicxml(raw)
 except (IndexError,KeyError,ZeroDivisionError,ET.ParseError,zipfile.BadZipFile,struct.error):raise ValueError('invalid_score_file') from None
 result=[]
 for i,track in enumerate(tracks):
  notes=sorted(track['notes'],key=lambda n:(n['start'],n['pitch']))
  if any(not math.isfinite(n['start']+n['duration']) or n['start']<-.01 or not .01<=n['duration']<=60 or not 12<=n['pitch']<=108 for n in notes):raise ValueError('invalid_score_notes')
  poly=any(a['start']+a['duration']>b['start']+.08 for a,b in zip(notes,notes[1:]))
  if notes:result.append({'track':i,'name':track['name'],'monophonic':not poly,'notes':notes})
 if not result:raise ValueError('no_notes_found')
 return result

def preview(tracks):return [{'track':t['track'],'name':t['name'],'monophonic':t['monophonic'],'note_count':len(t['notes']),'first_pitches':[n['pitch'] for n in t['notes'][:24]]} for t in tracks]

def excerpt(tracks,lyrics,track=-1,start_note=0):
 if not isinstance(lyrics,str) or not re.fullmatch(r'[\u4e00-\u9fff]{2,40}',lyrics):raise ValueError('chinese_short_lyrics_required')
 options=[t for t in tracks if t['monophonic'] and (track<0 or t['track']==track)]
 if len(options)!=1:raise ValueError('choose_one_monophonic_track_from_inspect')
 if type(start_note) is not int or not 0<=start_note<=5000:raise ValueError('invalid_start_note')
 selected=options[0]['notes'][start_note:start_note+len(lyrics)]
 if len(selected)!=len(lyrics):raise ValueError('not_enough_notes_for_lyrics')
 shift=max(-12,min(12,59-sorted(n['pitch'] for n in selected)[len(selected)//2]))
 if any(not 45<=n['pitch']+shift<=78 for n in selected):raise ValueError('melody_range_too_wide')
 scale=max(1,.28/min(n['duration'] for n in selected))
 if scale>2.2:raise ValueError('score_too_fast_for_clear_singing')
 notes=[];offset=.3;origin=selected[0]['start']
 for i,n in enumerate(selected):
  at=.3+(n['start']-origin)*scale;duration=min(n['duration'],selected[i+1]['start']-n['start']) if i+1<len(selected) else n['duration'];duration*=scale
  notes.append({'start':at,'duration':duration,'pitch':n['pitch']+shift})
 total=max(2,notes[-1]['start']+notes[-1]['duration']+.4)
 if total>20:raise ValueError('choose_shorter_lyrics_or_different_start_note')
 return {'lyrics':lyrics,'notes':notes,'seconds':total,'track':options[0]['track'],'start_note':start_note,'transpose_semitones':shift,'tempo_scale':scale,'control':'score'}
