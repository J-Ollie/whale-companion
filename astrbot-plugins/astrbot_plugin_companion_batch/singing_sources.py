"""Fetch only a score linked by a previously read public page; persist provenance."""
import asyncio,hashlib,importlib,json,re,sqlite3,urllib.parse
from contextlib import closing
from .voice_lab import save
from .music_score import parse,preview,excerpt

class SingingSources:
 def __init__(self,singer):self.singer=singer;self.root=singer.root/'score-sources'
 def source(self,reference):
  if not isinstance(reference,str) or not re.fullmatch(r'pub:[a-f0-9]{20}',reference):raise ValueError('read_public_source_first')
  phone=self.singer.voice.meta('phone')
  if not phone:raise ValueError('public_reading_unavailable')
  path=phone.public_notebook().path
  with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True)) as db:
   db.row_factory=sqlite3.Row;row=db.execute('SELECT * FROM documents WHERE ref=?',(reference,)).fetchone()
  if not row:raise ValueError('unknown_public_source')
  return dict(row),phone
 async def inspect(self,event,reference,url):
  self.singer.guard(event);row,phone=self.source(reference)
  if not phone.owner_event(event) or not phone.autonomy.reading.available(False) or phone.phone.state['paused'] or phone.autonomy.state['paused']:raise ValueError('public_reading_paused')
  count=event.get_extra('companion_score_inspections',0)
  if count>=2:raise ValueError('score_inspection_turn_limit')
  event.set_extra('companion_score_inspections',count+1)
  if not await self.singer.voice.reserve('score_fetch',limit=4):raise ValueError('daily_score_fetch_limit')
  reading=importlib.import_module(type(phone).__module__.rsplit('.',1)[0]+'.reading')
  url=reading.public_url(url)
  def fetch():
   final,kind,raw=reading.fetch(row['url'],('text/html','application/xhtml+xml','text/plain'))
   html=raw.decode('utf-8',errors='replace');parser=reading.PageText();parser.feed(html)
   links=[]
   for href in reading.score_links(html)+parser.links:
    try:links.append(reading.public_url(urllib.parse.urljoin(final,href)))
    except ValueError:continue
   if url not in links and url not in row['body']:raise ValueError('score_url_not_linked_by_read_source')
   final,kind,raw=reading.fetch(url,('audio/midi','audio/x-midi','audio/mid','application/x-midi','application/octet-stream','application/zip','application/vnd.recordare.musicxml','application/vnd.recordare.musicxml+xml','application/xml','text/xml','text/plain'))
   tracks=parse(raw);identifier='music-'+hashlib.sha256(raw).hexdigest()[:20]
   self.root.mkdir(mode=0o700,parents=True,exist_ok=True);path=self.root/(identifier+'.bin');path.write_bytes(raw);path.chmod(0o600)
   meta={'reference':identifier,'sha256':hashlib.sha256(raw).hexdigest(),'source':{'reference':reference,'page_url':row['url'],'score_url':final,'title':row['title'],'page_hash':row['content_hash']},'tracks':preview(tracks)}
   save(self.root/(identifier+'.json'),meta);return meta
  value=await asyncio.wait_for(asyncio.to_thread(fetch),70);self.singer.guard(event);return value
 def load(self,reference):
  if not isinstance(reference,str) or not re.fullmatch(r'music-[a-f0-9]{20}',reference):raise ValueError('inspect_score_first')
  meta=json.loads((self.root/(reference+'.json')).read_text());path=self.root/(reference+'.bin')
  if path.is_symlink() or not 20<=path.stat().st_size<=1024*1024:raise ValueError('score_cache_invalid')
  raw=path.read_bytes()
  if hashlib.sha256(raw).hexdigest()!=meta['sha256']:raise ValueError('score_cache_changed')
  return meta,parse(raw)
 def prepare(self,reference,lyrics,kind,lyrics_reference,track,start_note,title):
  lyrics=re.sub(r'[\s，。！？、,.!?]','',lyrics)
  if kind not in ('original','user_provided','short_excerpt'):raise ValueError('invalid_lyric_origin')
  if kind=='short_excerpt':
   if len(lyrics)>20:raise ValueError('choose_shorter_lyrics_excerpt')
   row,_=self.source(lyrics_reference)
   if lyrics not in re.sub(r'[^\u4e00-\u9fff]','',row['body']):raise ValueError('lyrics_not_in_read_source')
  meta,tracks=self.load(reference);score=excerpt(tracks,lyrics,track,start_note)
  return {'title':str(title)[:80] or meta['source']['title'],'lyrics':lyrics,'lyric_origin':kind,'lyrics_reference':lyrics_reference if kind=='short_excerpt' else None,'source':meta['source'],'score_sha256':meta['sha256'],'score':score,'engine':'SoulX-Singer'}
