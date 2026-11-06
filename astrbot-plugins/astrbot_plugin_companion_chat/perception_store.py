"""Private metadata for actual media-processing calls; no media or transcripts."""
from contextlib import closing
from datetime import datetime,timedelta
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import time
import uuid
from .commitments import TZ

KINDS={'image_caption':'native_caption','audio_transcript':'dashscope_stt','audio_spectrum':'mme_spectrum'}
STATES=('returned','empty','failed','cancelled')
LIMITS={'image_caption':'模型返回转述文字，不证明直接视觉、转述准确或所有细节',
        'audio_transcript':'转写返回文字片段，不证明听过音色、语气、尾字或完整音频',
        'audio_spectrum':'频谱函数返回统计摘要，不证明主观听感或心理状态'}


class PerceptionStore:
    def __init__(self,root):
        self.path=Path(root)/'perception-events.sqlite3'
        self.path.parent.mkdir(parents=True,exist_ok=True)
        with closing(self.connect()) as db,db:
            db.executescript('''CREATE TABLE IF NOT EXISTS events(
              id TEXT PRIMARY KEY,scope TEXT,message_id TEXT,material_id TEXT,
              kind TEXT,method TEXT,status TEXT,at REAL,finished REAL,
              result_hash TEXT,result_size INTEGER,error_type TEXT);
              CREATE INDEX IF NOT EXISTS perception_scope ON events(scope,at);
              CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,value TEXT);
            ''')
            db.execute('INSERT OR IGNORE INTO metadata VALUES(?,?)',('started_at',str(time.time())))
        self.path.chmod(0o600)

    def connect(self):
        db=sqlite3.connect(self.path,timeout=.05);db.row_factory=sqlite3.Row;return db

    def begin(self,scope,message_id,reference,kind,*,now=None):
        if kind not in KINDS or not isinstance(scope,str) or not scope or len(scope)>240:return None
        if not isinstance(message_id,str) or not message_id or len(message_id)>120:return None
        if not isinstance(reference,str) or not reference:return None
        now=time.time() if now is None else now
        material='material:'+hashlib.sha256((scope+'\0'+message_id+'\0'+reference).encode()).hexdigest()[:24]
        identifier='perception:'+uuid.uuid4().hex[:16]
        with closing(self.connect()) as db,db:
            db.execute('INSERT INTO events VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                (identifier,scope,message_id,material,kind,KINDS[kind],'attempted',now,None,'',0,''))
        return identifier

    def finish(self,identifier,scope,status,result='',error_type='',*,now=None):
        if status not in STATES:return False
        if not isinstance(result,str):result=''
        if status=='returned' and not result.strip():status='empty'
        digest=hashlib.sha256(result.encode()).hexdigest() if status=='returned' else ''
        error_type=error_type if re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,63}',str(error_type)) else ''
        with closing(self.connect()) as db,db:
            changed=db.execute("UPDATE events SET status=?,finished=?,result_hash=?,result_size=?,error_type=? WHERE id=? AND scope=? AND status='attempted'",
                (status,time.time() if now is None else now,digest,len(result) if status=='returned' else 0,error_type,identifier,scope)).rowcount
        return bool(changed)

    def recent(self,scope,*,now=None,max_bytes=1600,visible=None):
        now=time.time() if now is None else now
        today=datetime.fromtimestamp(now,TZ).replace(hour=0,minute=0,second=0,microsecond=0)
        since=(today-timedelta(days=6)).timestamp()
        try:
            with closing(self.connect()) as db:
                started=float(db.execute("SELECT value FROM metadata WHERE key='started_at'").fetchone()[0])
                raw=db.execute('SELECT id,message_id,material_id,kind,status,at,finished FROM events WHERE scope=? AND at>=? AND at<=? ORDER BY at DESC,rowid DESC LIMIT 5',(scope,since,now)).fetchall()
            result={'status':'ok','started_at':started,'coverage':'仅启用后、已验证私聊的已观测路径；无记录不证明未处理；不代表材料作者身份','events':[],'more':len(raw)==5}
            for row in raw:
                if visible is not None and not visible(row['message_id']):continue
                if len(result['events'])>=4:result['more']=True;break
                value=dict(row);value['coverage']=LIMITS[row['kind']]
                result['events'].append(value)
                if len(json.dumps(result,ensure_ascii=False,separators=(',',':')).encode())>max_bytes:
                    result['events'].pop();result['more']=True;break
            return result
        except (OSError,sqlite3.Error,TypeError,ValueError):
            return {'status':'unavailable','events':[],'coverage':'处理记录暂不可读，不代表没有处理'}

    def for_request(self,scope,message_id,since,*,now=None,max_bytes=1600):
        """Only this observed request, never a prior message reusing an ID."""
        now=time.time() if now is None else now
        if type(since) not in (int,float) or not 0<=since<=now:return None
        try:
            with closing(self.connect()) as db:
                raw=db.execute('SELECT id,message_id,kind,status,at,finished FROM events WHERE scope=? AND message_id=? AND at>=? AND at<=? ORDER BY at DESC,rowid DESC LIMIT 5',(scope,message_id,since,now)).fetchall()
            if not raw:return None
            result={'scope':'current_request_only','events':[],'returned_kinds':[],'more':len(raw)==5,
                'note':'returned只支持对应方法返回过文字/统计；转写不证明听过音色，转述不证明直接视觉，不证明完整处理、内容正确或对方已收到。缺记录不证明未处理。'}
            for row in raw[:4]:
                result['events'].append(dict(row))
                if len(json.dumps(result,ensure_ascii=False,separators=(',',':')).encode())>max_bytes:
                    result['events'].pop();result['more']=True;break
            result['returned_kinds']=sorted({e['kind'] for e in result['events'] if e['status']=='returned'})
            while len(json.dumps(result,ensure_ascii=False,separators=(',',':')).encode())>max_bytes and result['events']:
                result['events'].pop();result['more']=True
                result['returned_kinds']=sorted({e['kind'] for e in result['events'] if e['status']=='returned'})
            return result if result['events'] else None
        except (OSError,sqlite3.Error):return None
