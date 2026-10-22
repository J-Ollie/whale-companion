"""Bounded, read-only access for the authenticated administration page."""
import json
import re
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo

TZ=ZoneInfo('Asia/Shanghai')


class Library:
    def __init__(self, root):
        self.data=Path(root)/'data/astrbot/plugin_data'

    def connection(self,path):
        db=sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True,timeout=3)
        db.row_factory=sqlite3.Row
        return db

    def memories(self,qq='',kind='',term='',state='active',q='',before=0):
        """Explicit administrator query of structured memory, never conversation evidence."""
        if qq and not re.fullmatch(r'\d{5,12}',qq):raise ValueError('QQ号格式不正确')
        if kind not in ('','fact','impression','growth','episode','continuity'):raise ValueError('未知记忆分类')
        if term not in ('','long','temporary'):raise ValueError('未知保存期限')
        if state not in ('active','all','expired'):raise ValueError('未知记忆状态')
        if len(q)>100:raise ValueError('搜索词请限制在100个字符内')
        if not 0<=before<=1000000000:raise ValueError('分页参数无效')
        qq=qq or '100000001'
        path=self.data/'companion_memory/memory.sqlite3'
        empty={'contacts':[],'qq':qq,'entries':[],'total':0,'offset':before,'page_size':30,
               'has_more':False,'available':False,
               'notice':'展示已整理保存的记忆，不等同于完整聊天记录。长期记忆指未设置到期时间的条目；相处印象和成长倾向也可按分类查看。'}
        if not path.exists():return empty
        now=time.time();where=['qq=?'];args=[qq]
        if kind:where.append('kind=?');args.append(kind)
        if term:where.append('expires IS NULL' if term=='long' else 'expires IS NOT NULL')
        if state=='active':where.append('(expires IS NULL OR expires>?)');args.append(now)
        elif state=='expired':where.append('expires IS NOT NULL AND expires<=?');args.append(now)
        if q:
            literal=q.replace('\\','\\\\').replace('%','\\%').replace('_','\\_')
            where.append("(topic LIKE ? ESCAPE '\\' OR text LIKE ? ESCAPE '\\')");args.extend(['%'+literal+'%']*2)
        condition=' AND '.join(where)
        with closing(self.connection(path)) as db:
            db.execute('PRAGMA query_only=ON')
            contacts=[{'qq':r['qq'],'count':r['count']} for r in db.execute('SELECT qq,COUNT(*) count FROM entries GROUP BY qq ORDER BY qq LIMIT 500')]
            total=db.execute('SELECT COUNT(*) FROM entries WHERE '+condition,args).fetchone()[0]
            rows=[dict(r) for r in db.execute('SELECT id,kind,topic,text,created,updated,expires,pinned,evidence FROM entries WHERE '+condition+' ORDER BY pinned DESC,updated DESC,id LIMIT 30 OFFSET ?',args+[before])]
            audit=[]
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='recall_audit'").fetchone():
                for item in db.execute('SELECT at,operation,refs FROM recall_audit WHERE qq=? ORDER BY id DESC LIMIT 5',(qq,)):
                    try:refs=json.loads(item['refs'])
                    except (ValueError,TypeError):refs=[]
                    audit.append({'at':item['at'],'operation':item['operation'],'matched_count':len(refs)})
        secret=re.compile(r'(?i)sk-[a-z0-9_-]{16,}|AKIA[A-Z0-9]{16}|-----BEGIN [^-]*PRIVATE KEY-----[\s\S]*|(?:api[_ -]?key|access[_ -]?token|password|密码|密钥|令牌)\s*(?:[:：=]|是)\s*[^\s，。；;]{4,}')
        for row in rows:
            row['topic']=secret.sub('[凭据已省略]',str(row['topic']))[:200]
            row['text']=secret.sub('[凭据已省略]',str(row['text']))[:4000]
            try:
                evidence=json.loads(row.pop('evidence'));row['evidence_count']=len(evidence) if isinstance(evidence,list) else 0
            except (ValueError,TypeError):row.pop('evidence',None);row['evidence_count']=0
            row['expired']=row['expires'] is not None and row['expires']<=now
        return {**empty,'available':True,'contacts':contacts,'entries':rows,'total':total,'has_more':before+len(rows)<total,'recall_audit':audit}

    def memory_evidence(self,qq,identifier):
        if not re.fullmatch(r'\d{5,12}',qq or '') or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}',identifier or ''):
            raise ValueError('联系人或记忆编号无效')
        path=self.data/'companion_memory/memory.sqlite3'
        if not path.exists():raise ValueError('尚无记忆库')
        with closing(self.connection(path)) as db:
            row=db.execute('SELECT id,kind,evidence FROM entries WHERE qq=? AND id=?',(qq,identifier)).fetchone()
            if not row:raise ValueError('没有找到此联系人的记忆')
            ids=json.loads(row['evidence'])
            sources=[]
            columns={item['name'] for item in db.execute('PRAGMA table_info(observations)')}
            if not {'id','qq','role','text','at'}.issubset(columns):return {'id':identifier,'sources':[],'notice':'旧记录未保存可读取的原文依据。'}
            for source_id in ids[:6] if isinstance(ids,list) else []:
                if not isinstance(source_id,int):continue
                if db.execute("SELECT 1 FROM sqlite_master WHERE name='suppressed_sources'").fetchone():
                    if db.execute('SELECT 1 FROM suppressed_sources WHERE qq=? AND observation_id=?',(qq,source_id)).fetchone():continue
                source=db.execute('SELECT role,text,at FROM observations WHERE qq=? AND id=?',(qq,source_id)).fetchone()
                if source:
                    text=re.sub(r'(?i)sk-[a-z0-9_-]{16,}|AKIA[A-Z0-9]{16}|-----BEGIN [^-]*PRIVATE KEY-----[\s\S]*|(?:api[_ -]?key|access[_ -]?token|password|密码|密钥|令牌)\s*(?:[:：=]|是)\s*[^\s，。；;]{4,}','[凭据已省略]',source['text'])
                    sources.append({'role':source['role'],'text':text[:1200],'at':source['at']})
        return {'id':identifier,'sources':sources,'notice':'仅管理员主动查阅；对话原文是证据，不代表其中的事实已核实。'}

    def chats(self,qq='',before=0):
        path=self.data/'companion_friends/chats.sqlite3'
        empty={'contacts':[],'messages':[],'has_more':False,'next_before':None,
               'notice':'从启用记录功能后保存，不代表完整QQ历史；图片超限或缓存失败时显示占位。'}
        if not path.exists():return empty
        if qq and not re.fullmatch(r'\d{5,12}',qq):raise ValueError('QQ号格式不正确')
        with self.connection(path) as db:
            contacts=[dict(r) for r in db.execute('''SELECT qq,
                COALESCE((SELECT name FROM chats n WHERE n.qq=c.qq AND name!='' ORDER BY id DESC LIMIT 1),'') name,
                MAX(at) last_at,COUNT(*) count FROM chats c GROUP BY qq ORDER BY last_at DESC LIMIT 500''')]
            if not qq:return {**empty,'contacts':contacts}
            messages=[dict(r) for r in db.execute('SELECT id,qq,direction,at,segments FROM chats WHERE qq=? AND (?=0 OR id<?) ORDER BY id DESC LIMIT 51',(qq,before,before))]
        more=len(messages)>50
        messages=messages[:50]
        for row in messages:
            # Never send source URLs/base64 strings or local filesystem paths to the browser.
            row['segments']=[{k:v for k,v in p.items() if k in ('type','text','label','media')} for p in json.loads(row['segments'])]
        return {**empty,'contacts':contacts,'messages':list(reversed(messages)),'has_more':more,
                'next_before':messages[-1]['id'] if messages else None}

    def diaries(self,book,before=0):
        if book not in ('public','private','legacy'):raise ValueError('未知日记本')
        items=[]
        if book in ('public','legacy'):
            path=self.data/'companion_memory/memory.sqlite3'
            if path.exists():
                with self.connection(path) as db:
                    items=[dict(r) for r in db.execute('SELECT id,at,text,mood FROM diary WHERE qq=? ORDER BY at DESC LIMIT 3000',('100000001',))]
        paths=[]
        if book!='legacy':paths.append((self.data/'companion_phone'/f'{book}-diary.jsonl',False))
        if book in ('public','legacy'):paths.append((self.data/'companion_phone/life-diary.jsonl',True))
        for path,historical in paths:
            if not path.exists():continue
            with path.open(encoding='utf-8') as stream:
                for line in stream:
                    try:
                        row=json.loads(line)
                        if not isinstance(row,dict):continue
                        if (historical and row.get('visibility') in (None,'public')) or (not historical and row.get('visibility')==book):items.append(row)
                    except (ValueError,TypeError):continue
        normalized=[]
        for row in items:
            try:
                day=row.get('day')
                if not isinstance(day,str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}',day):
                    day=datetime.fromtimestamp(row.get('at',0),TZ).date().isoformat()
                normalized.append({**row,'day':day})
            except (ValueError,TypeError,OverflowError,OSError):continue
        normalized.sort(key=lambda r:(r['day'],r.get('at',0)),reverse=True)
        page=normalized[before:before+51]
        return {'book':book,'entries':[{k:v for k,v in r.items() if k in
                ('id','at','updated_at','day','text','mood','kind')} for r in page[:50]],
                'has_more':len(page)>50,'next_before':before+50,
                'schedule':'北京时间23:30起整理当天经历；同一天后续新增素材会更新同一篇，跨日或重启后可补写。没素材或不想写可留空。',
                'notice':'日记包含以前的日记与随手感想，按日期归档；私密日记单独读取，不混入日记，也不会因查看而发布。'}

    def media(self,name):
        if not re.fullmatch(r'[0-9a-f]{64}\.(png|jpg|gif|webp)',name):raise ValueError('未知图片')
        path=self.data/'companion_friends/media'/name
        if not path.exists():raise ValueError('图片未缓存')
        mime={'png':'image/png','jpg':'image/jpeg','gif':'image/gif','webp':'image/webp'}[name.rsplit('.',1)[-1]]
        return path.read_bytes(),mime
