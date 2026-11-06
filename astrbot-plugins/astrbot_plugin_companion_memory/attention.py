"""Rehearsal changes accessibility, never truth, hard validity or explicit forgetting."""
import hashlib
import json
import math
import time
from datetime import datetime
from .memory import TZ

HALF_LIFE=14*86400
MAX_STRENGTH=6.0


def exists(store):
    return bool(store.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='memory_attention'").fetchone())


def source(store,qq,ref):
    if ref.startswith('obs:') and ref[4:].isdigit():
        row=store.db.execute('''SELECT id,role,text,at FROM observations o WHERE qq=? AND id=? AND NOT EXISTS
            (SELECT 1 FROM suppressed_sources s WHERE s.qq=o.qq AND s.observation_id=o.id)''',(qq,int(ref[4:]))).fetchone()
        return dict(row) if row else None
    row=store.get_entry(qq,ref)
    if not row or not store.lifecycle.provenance_valid(row):return None
    blocked={r[0] for r in store.db.execute('SELECT observation_id FROM suppressed_sources WHERE qq=?',(qq,))}
    return row if not set(json.loads(row['evidence']))&blocked else None


def signature(row):
    return hashlib.sha256(json.dumps(row,sort_keys=True,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()


def current(store,qq,ref,now=None):
    if not exists(store):return None
    now=time.time() if now is None else now
    row=source(store,qq,ref)
    if not row:return None
    saved=store.db.execute('SELECT * FROM memory_attention WHERE qq=? AND ref=?',(qq,ref)).fetchone()
    if not saved or saved['signature']!=signature(row):return None
    result=dict(saved)
    result['strength']=saved['strength']*math.exp(-math.log(2)*max(0,now-saved['updated'])/HALF_LIFE)
    return result


def boost(store,qq,ref,now=None):
    row=current(store,qq,ref,now)
    return .25*row['strength']/MAX_STRENGTH if row else 0.0


def review_after(store,row,base,now=None):
    """Only optional review aging; dates of plans/states and expiry are untouched."""
    if base is None:return base
    saved=current(store,row['qq'],row['id'],now)
    if not saved or saved['first_touched']>base:return base
    return max(base,min(base+21*86400,saved['updated']+saved['strength']*3*86400))


def rehearse(store,qq,refs,cycle,now=None):
    now=time.time() if now is None else now
    if not isinstance(refs,list) or len(refs)>4 or any(not isinstance(r,str) for r in refs):raise ValueError('invalid_attention_refs')
    day=datetime.fromtimestamp(now,TZ).date().isoformat()
    with store.atomic():
        store.db.execute('''CREATE TABLE IF NOT EXISTS memory_attention(
            qq TEXT,ref TEXT,signature TEXT,strength REAL,updated REAL,first_touched REAL,cycle TEXT,
            day TEXT,day_count INTEGER,PRIMARY KEY(qq,ref));''') if not exists(store) else None
    touched=[]
    # Propagate to existing entries supported by the same original, without copying facts.
    candidates=list(dict.fromkeys(refs))
    ids=[int(r[4:]) for r in refs if r.startswith('obs:') and r[4:].isdigit()]
    if ids:
        candidates += [r[0] for r in store.db.execute('''SELECT DISTINCT e.id FROM entries e,json_each(e.evidence) j
            WHERE e.qq=? AND j.value IN ('''+','.join('?'*len(ids))+') LIMIT 8',(qq,*ids))]
    for ref in dict.fromkeys(candidates):
        row=source(store,qq,ref)
        if not row:continue
        old=current(store,qq,ref,now)
        if old and (old['cycle']==cycle or now-old['updated']<6*3600 or old['day']==day and old['day_count']>=3):continue
        strength=min(MAX_STRENGTH,(old['strength'] if old else 0)+1)
        first=old['first_touched'] if old else now
        count=old['day_count']+1 if old and old['day']==day else 1
        with store.atomic():
            store.db.execute('INSERT OR REPLACE INTO memory_attention VALUES(?,?,?,?,?,?,?,?,?)',
                             (qq,ref,signature(row),strength,now,first,cycle,day,count))
        touched.append(ref)
    return touched
