"""Bounded metadata-only audit, independent of model and recall quotas."""
import json
import re
import sqlite3
import time
from contextlib import closing
from pathlib import Path

LAYERS=('history_gap','personal_core','relevant_facts','related_episodes','working_handoff','continuity','last_exchange','impressions')
STAGES={'search','read','request_attached','tool_result'}
REF=re.compile(r'(?:obs:\d+|shared:[a-f0-9]{16}|procedure:[a-f0-9]{24}|bundle:[a-f0-9]{12}|[A-Za-z0-9_-]{1,64})\Z')


def context_metadata(text):
    if not isinstance(text,str) or '<companion_context>' not in text:return None
    if '</companion_context>' not in text:return None
    block=text.split('<companion_context>',1)[1].rsplit('</companion_context>',1)[0]
    try:data=json.loads(block[block.index('{'):block.rindex('}')+1])
    except (ValueError,TypeError):return None
    if not isinstance(data,dict):return None
    refs=[];layers=[]
    for name in LAYERS:
        rows=data.get(name,[])
        if not isinstance(rows,list):continue
        ids=[r['id'] for r in rows if isinstance(r,dict) and isinstance(r.get('id'),str) and REF.fullmatch(r['id'])]
        refs.extend(ids)
        layers.append({'layer':name,'refs':ids[:64],'count':len(ids),'bytes':len(json.dumps(rows,ensure_ascii=False,separators=(',',':')).encode())})
    return {'refs':refs,'layers':layers,'bytes':len(('<companion_context>'+block+'</companion_context>').encode())}


def record(folder,qq,stage,refs=(),*,layers=(),ranges=(),bytes=0,boundary='unknown',reason=None):
    """Allowlist each persisted scalar; never persist text, queries or prompts."""
    if not re.fullmatch(r'\d{5,12}',str(qq)) or stage not in STAGES:return
    ids=list(dict.fromkeys(r for r in refs if isinstance(r,str) and REF.fullmatch(r)))
    safe_layers=[]
    for row in list(layers)[:8]:
        if not isinstance(row,dict) or row.get('layer') not in LAYERS:continue
        safe_layers.append({'layer':row['layer'],'refs':[r for r in row.get('refs',[]) if isinstance(r,str) and REF.fullmatch(r)][:64],
                            'count':row.get('count') if type(row.get('count')) is int else None,'bytes':row.get('bytes') if type(row.get('bytes')) is int else None})
    safe_ranges=[]
    for row in list(ranges)[:16]:
        if not isinstance(row,dict) or not REF.fullmatch(str(row.get('id',''))):continue
        safe_ranges.append({k:row[k] for k in ('id','offset','end','total_chars') if k=='id' or type(row.get(k)) is int and row[k]>=0})
    document={'refs':ids[:64],'count':len(ids),'truncated':len(ids)>64,'layers':safe_layers,'ranges':safe_ranges,
              'bytes':bytes if type(bytes) is int and bytes>=0 else None,
              'boundary':boundary if boundary in ('cognition_result','tool_payload','provider_request_hook','outreach_call') else 'unknown',
              'reason':reason if reason in ('no_personal_context','budget_message_only','context_unreadable') else None}
    path=Path(folder)/'usage-audit.sqlite3'
    try:
        with closing(sqlite3.connect(path,timeout=.5)) as db, db:
            db.execute('CREATE TABLE IF NOT EXISTS usage(id INTEGER PRIMARY KEY,qq TEXT,at REAL,stage TEXT,metadata TEXT)')
            db.execute('CREATE INDEX IF NOT EXISTS usage_person ON usage(qq,id)')
            db.execute('INSERT INTO usage(qq,at,stage,metadata) VALUES(?,?,?,?)',(qq,time.time(),stage,json.dumps(document,separators=(',',':'))))
            db.execute('DELETE FROM usage WHERE id NOT IN (SELECT id FROM usage ORDER BY id DESC LIMIT 2000)')
        path.chmod(0o600)
    except (OSError,sqlite3.Error):pass  # An unavailable audit must not block a reply.
