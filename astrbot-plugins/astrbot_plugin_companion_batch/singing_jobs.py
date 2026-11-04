"""Durable narrow job exchange, shared by AstrBot and a forced-command SSH gateway."""
import base64,hashlib,io,json,re,sqlite3,time,uuid,wave
from contextlib import closing
from pathlib import Path

class SingingJobs:
 def __init__(self,root):self.root=Path(root);self.path=self.root/'singing-jobs.sqlite3'
 def db(self):
  self.root.mkdir(parents=True,exist_ok=True,mode=0o700)
  db=sqlite3.connect(self.path,timeout=4);db.row_factory=sqlite3.Row
  version=db.execute('PRAGMA user_version').fetchone()[0]
  if version==0 and not db.execute("SELECT 1 FROM sqlite_master WHERE type='table'").fetchone():
   db.executescript('CREATE TABLE jobs(id TEXT PRIMARY KEY,status TEXT,payload TEXT,result TEXT,lease TEXT,created REAL,updated REAL);CREATE TABLE meta(id TEXT PRIMARY KEY,value TEXT);PRAGMA user_version=1;')
  elif version!=1:db.close();raise ValueError('unknown_singing_job_database')
  self.path.chmod(0o600);return db
 def submit(self,payload,identity):
  fingerprint=hashlib.sha256(json.dumps([identity,payload],sort_keys=True,ensure_ascii=False).encode()).hexdigest()
  with closing(self.db()) as db,db:
   existing=db.execute("SELECT id FROM jobs WHERE json_extract(payload,'$.idempotency')=?",(fingerprint,)).fetchone()
   if existing:return existing['id']
   value={**payload,'idempotency':fingerprint};identifier='singjob-'+uuid.uuid4().hex[:20];now=time.time()
   db.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?,?)',(identifier,'queued',json.dumps(value,ensure_ascii=False),None,None,now,now))
   return identifier
 def find(self,payload,identity):
  if not self.path.exists():return None
  fingerprint=hashlib.sha256(json.dumps([identity,payload],sort_keys=True,ensure_ascii=False).encode()).hexdigest()
  with closing(self.db()) as db:row=db.execute("SELECT id FROM jobs WHERE json_extract(payload,'$.idempotency')=?",(fingerprint,)).fetchone()
  return row['id'] if row else None
 def analysis(self,identifier,report):
  row=self.get(identifier)
  if row['status']!='complete':raise ValueError('job_not_complete')
  result={**row['result'],'audio_review':report}
  with closing(self.db()) as db,db:db.execute('UPDATE jobs SET result=? WHERE id=?',(json.dumps(result,ensure_ascii=False),identifier))
 def get(self,identifier):
  if not isinstance(identifier,str) or not re.fullmatch(r'singjob-[a-f0-9]{20}',identifier):raise ValueError('invalid_singing_job_reference')
  with closing(self.db()) as db:row=db.execute('SELECT * FROM jobs WHERE id=?',(identifier,)).fetchone()
  if not row:raise ValueError('unknown_singing_job')
  return {**dict(row),'payload':json.loads(row['payload']),'result':json.loads(row['result']) if row['result'] else None}
 def recent(self):
  if not self.path.exists():return []
  with closing(self.db()) as db:rows=db.execute('SELECT id,status,payload,created FROM jobs ORDER BY created DESC LIMIT 5').fetchall()
  return [{'reference':r['id'],'status':r['status'],'title':json.loads(r['payload']).get('title'),'created':r['created']} for r in rows]
 def worker_status(self):
  if not self.path.exists():return {'online':False,'ready':False}
  with closing(self.db()) as db:row=db.execute("SELECT value FROM meta WHERE id='worker'").fetchone()
  value=json.loads(row['value']) if row else {}
  return {**value,'online':time.time()-value.get('at',0)<45}
 def heartbeat(self,ready,busy=False):
  value={'at':time.time(),'ready':ready is True,'busy':busy is True,'kind':'local_soulx_gpu'}
  with closing(self.db()) as db,db:db.execute("INSERT OR REPLACE INTO meta VALUES('worker',?)",(json.dumps(value),))
  return {'ok':True}
 def claim(self,allowed):
  if not allowed:return {'job':None,'gate':'sleep_pause_or_disabled'}
  now=time.time()
  with closing(self.db()) as db:
   db.execute('BEGIN IMMEDIATE')
   db.execute("UPDATE jobs SET status='expired',updated=? WHERE status='queued' AND created<?",(now,now-86400))
   db.execute("UPDATE jobs SET status='uncertain',updated=? WHERE status='running' AND updated<?",(now,now-1800))
   row=db.execute("SELECT * FROM jobs WHERE status='queued' ORDER BY created LIMIT 1").fetchone()
   if row:
    lease=uuid.uuid4().hex;db.execute("UPDATE jobs SET status='running',lease=?,updated=? WHERE id=?",(lease,now,row['id']))
   db.commit()
  return {'job':{'reference':row['id'],'lease':lease,'payload':json.loads(row['payload'])} if row else None}
 def complete(self,identifier,lease,body):
  row=self.get(identifier)
  if row['lease']!=lease or not isinstance(lease,str) or not re.fullmatch('[a-f0-9]{32}',lease):raise ValueError('invalid_singing_lease')
  if row['status']=='complete':return {'ok':True,'already_completed':True}
  if row['status']!='running':raise ValueError('singing_job_not_running')
  if body.get('ok') is not True:
   code=str(body.get('reason','worker_failed'))[:100];status='quality_rejected' if code.startswith('quality_') else 'failed';result={'reason':code}
  else:
   encoded=body.get('wav','')
   if not isinstance(encoded,str) or len(encoded)>1300000:raise ValueError('worker_audio_limit')
   raw=base64.b64decode(encoded,validate=True);metrics=body.get('metrics',{})
   with wave.open(io.BytesIO(raw),'rb') as audio:
    seconds=audio.getnframes()/audio.getframerate()
    if audio.getnchannels()!=1 or audio.getsampwidth()!=2 or audio.getframerate()!=24000 or not 2<=seconds<=20:raise ValueError('worker_audio_properties')
   if (not isinstance(metrics,dict) or metrics.get('finite') is not True or metrics.get('clipped_fraction')!=0
    or metrics.get('notes_within_semitone')!=metrics.get('total_notes') or not 2<=metrics.get('total_notes',0)<=40):raise ValueError('worker_quality_checks_failed')
   target=self.root/'local-songs'/(identifier+'.wav');target.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
   tmp=target.with_suffix('.tmp');tmp.write_bytes(raw);tmp.chmod(0o600);tmp.replace(target)
   status='complete';result={'sha256':hashlib.sha256(raw).hexdigest(),'seconds':seconds,'metrics':metrics,'engine':'SoulX-Singer','control':'score','source':row['payload']['source']}
  with closing(self.db()) as db,db:db.execute('UPDATE jobs SET status=?,result=?,updated=? WHERE id=? AND lease=?',(status,json.dumps(result,ensure_ascii=False),time.time(),identifier,lease))
  return {'ok':True,'status':status}
