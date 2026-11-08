from . import quota as q
"""Append observed stages and explicit short summaries, never prompts or hidden reasoning."""
import json
import math
import re
import sqlite3
import time

SCHEMA='''CREATE TABLE IF NOT EXISTS process_events(
id INTEGER PRIMARY KEY AUTOINCREMENT,scope TEXT,run_id TEXT,at REAL,kind TEXT,visibility TEXT,payload TEXT,refs TEXT);
CREATE INDEX IF NOT EXISTS process_events_run ON process_events(scope,run_id,id);'''
KINDS={'run_started','thought','memory_read','decision','validation_failed','step_started','step_finished','observation','source','run_finished','social_action'}
FIELDS={'focus','question','next_step','growth_wish','practice','attitude','activity','action','app','goal_id',
        'growth_action','next_minutes','continue','status','result','reason','error_type','calls','source_count',
        'source_id','note','page_type','public_content','tool','work_id','revision','practice_id','query','trigger'}
SECRET=re.compile(r'(?:sk-|brz_)[A-Za-z0-9_-]{8,}|(?:api[_ -]?key|access[_ -]?token|password|密码|密钥|验证码)\s*(?:[:：=]|是)\s*[^\s，。；;]{3,}',re.I)


def clean(payload):
    result={}
    for key,value in payload.items():
        if key not in FIELDS:continue
        if isinstance(value,str):
            value=SECRET.sub('[凭据已省略]',value)
            value=re.sub(r'https?://\S+','[链接已省略]',value)
            value=re.sub(r'(?:/opt/|/AstrBot/|/sdcard/|[A-Z]:\\)[^\s，。；;]*','[存储位置已省略]',value)
            result[key]=value[:400]
        elif isinstance(value,bool) or value is None:result[key]=value
        elif type(value) in (int,float) and math.isfinite(value):result[key]=value
    return result


def record(store,run_id,kind,payload,*,private=False,refs=None):
    if kind not in KINDS or not re.fullmatch(r'[a-f0-9]{32}',run_id or ''):return None
    try:
        valid_refs={k:v for k,v in (refs or {}).items() if isinstance(k,str) and len(k)<=100
                    and isinstance(v,str) and re.fullmatch(r'[a-f0-9]{64}',v)}
        with store.connect() as db:
            if not db.execute('SELECT 1 FROM runs WHERE scope=? AND id=?',(store.scope,run_id)).fetchone():return None
            count=db.execute('SELECT count(*) FROM process_events WHERE scope=? AND run_id=?',(store.scope,run_id)).fetchone()[0]
            if not q.enabled() and count>=200:return None
            return db.execute('INSERT INTO process_events(scope,run_id,at,kind,visibility,payload,refs) VALUES(?,?,?,?,?,?,?)',
                              (store.scope,run_id,time.time(),kind,'private' if private else 'public',
                               json.dumps(clean(payload),ensure_ascii=False),json.dumps(valid_refs))).lastrowid
    except (sqlite3.Error,OSError,ValueError,TypeError):
        return None  # A display log failure must never replay or fail a completed action.
