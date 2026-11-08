from . import quota as q
"""Optional read-only owner-scoped recollection, isolated from public activity."""
from datetime import datetime
import hashlib
import json
import time

from .phone import PhoneError
from .rhythm import TZ


def encoded(value):
    return json.dumps(value,ensure_ascii=False,separators=(',',':'))


class PrivateMemory:
    MAX_CALLS=3  # Within the existing four-call recall ceiling.
    MAX_BYTES=4200

    def __init__(self, thinking):
        self.t=thinking
        self.memory=thinking.memory()
        self.calls=0
        self.bytes=0
        self.refs={}
        self.offered=set()

    def available(self):
        m=self.memory
        return bool(m and m.owner==self.t.phone.owner and m.umo==self.t.phone.umo
                    and m.config.get('enabled',True) and not m.resting())

    def guard(self):
        self.t.guard()
        if not self.available():raise PhoneError('private_memory_unavailable')

    def snapshot(self, ref):
        m=self.memory
        if ref.startswith('obs:') and ref[4:].isdigit():
            sid=int(ref[4:])
            row=m.store.db.execute('''SELECT id,role,text,at FROM observations o WHERE qq=? AND id=?
                AND NOT EXISTS(SELECT 1 FROM suppressed_sources s WHERE s.qq=o.qq AND s.observation_id=o.id)''',(m.owner,sid)).fetchone()
            value=dict(row) if row else None
        else:
            row=m.store.get_entry(m.owner,ref)
            value=row if row and m.cognition().visible(m.owner,row) else None
        return hashlib.sha256(encoded(value).encode()).hexdigest() if value else None

    def validate(self):
        self.guard()
        if any(self.snapshot(ref)!=digest for ref,digest in self.refs.items()):
            raise PhoneError('private_memory_sources_changed')

    def capture(self, data):
        candidates=[]
        def visit(item):
            if isinstance(item,dict):
                if isinstance(item.get('id'),str):candidates.append(item['id'])
                for sid in item.get('source_ids',[]):
                    if type(sid) is int:candidates.append('obs:'+str(sid))
                for value in item.values():visit(value)
            elif isinstance(item,list):
                for value in item:visit(value)
        visit(data)
        for ref in candidates:
            digest=self.snapshot(ref)
            if digest:
                self.refs[ref]=digest
                self.offered.add(ref)

    def request(self, request):
        self.validate()
        if (not isinstance(request,dict) or set(request)-{'action','query','identifier','start_day','end_day','category','offset'}):
            raise PhoneError('invalid_private_memory_request')
        if (not q.enabled(self.t.phone.config) and self.calls>=self.MAX_CALLS) or self.MAX_BYTES-self.bytes<300:
            raise PhoneError('private_memory_budget_exhausted')
        self.calls+=1
        cog=self.memory.cognition();owner=self.memory.owner
        action=request.get('action')
        remaining=min(1800,self.MAX_BYTES-self.bytes)
        if action=='recent':
            today=datetime.fromtimestamp(time.time(),TZ).date().isoformat()
            start,end=cog.date_range(request.get('start_day') or today,request.get('end_day') or request.get('start_day') or today)
            rows=self.memory.store.db.execute('''SELECT id FROM observations o WHERE qq=? AND at>=? AND at<? AND at<=?
                AND NOT EXISTS(SELECT 1 FROM suppressed_sources s WHERE s.qq=o.qq AND s.observation_id=o.id)
                ORDER BY id DESC LIMIT 8''',(owner,start,end,time.time())).fetchall()
            matches=[]
            for row in rows:
                page=cog.read(owner,'obs:'+str(row['id']),max_bytes=600)
                candidate={'scope':'private_owner_only','matches':matches+[page],
                           'message':'按真实角色和时间回顾；只是一页原话，不证明做过或从未发生。'}
                if len(encoded(candidate).encode())>remaining:break
                matches.append(page)
            data={'scope':'private_owner_only','matches':matches,'message':'自己的回复是当时说过的话；没有记录不等于没有发生。'}
        elif action=='search':
            data=cog.search(owner,request.get('query',''),request.get('start_day',''),request.get('end_day',''),
                            limit=4,category=request.get('category','all'))
            data['scope']='private_owner_only'
            while len(encoded(data).encode())>remaining and data.get('matches'):data['matches'].pop()
        elif action=='read':
            identifier=request.get('identifier')
            if identifier not in self.offered:raise PhoneError('private_memory_reference_not_offered')
            data=cog.read(owner,identifier,offset=request.get('offset',0),max_bytes=remaining-60)
            data['scope']='private_owner_only'
        elif action=='notes':
            notes=[]
            with self.t.store.connect() as db:
                rows=db.execute('SELECT * FROM memory_reflections WHERE scope=? ORDER BY at DESC LIMIT 5',(self.t.store.scope,)).fetchall()
            for row in rows:
                refs=json.loads(row['refs'])
                if any(self.snapshot(ref)!=digest for ref,digest in refs.items()):continue
                note={k:row[k] for k in ('at','focus','question','growth_wish','practice')}
                note['references']=list(refs)
                if len(encoded(notes+[note]).encode())>remaining-150:break
                notes.append(note);self.refs.update(refs);self.offered.update(refs)
            data={'scope':'private_owner_only','notes':notes,'message':'这是旧的主观反思，不是新的事实依据。'}
        else:
            raise PhoneError('private_memory_readonly_actions_only')
        self.guard()
        # Metadata added by this adapter must fit the same byte budget too.
        if len(encoded(data).encode())>remaining:raise PhoneError('private_memory_payload_budget')
        self.capture(data)
        self.bytes+=len(encoded(data).encode())
        record=getattr(self.memory,'record_usage',None)
        if record:record(owner,'self_reflection_recall',list(self.refs),bytes=len(encoded(data).encode()),boundary='private_owner_only')
        return data

    def save(self, thought, now):
        self.validate()
        # No transcript copies, and no fake observations/user statements are created.
        with self.t.store.connect() as db:
            db.execute('INSERT INTO memory_reflections VALUES(?,?,?,?,?,?,?,?)',
                       (self.t.run_id,self.t.store.scope,now,thought['focus'],thought['question'],
                        thought['growth_wish'],thought['practice'],encoded(self.refs)))


def private_thought(value):
    from .compat import peer
    clean=peer('astrbot_plugin_companion_memory.memory').clean
    from .common_cognition import principle
    result={}
    for key,limit in (('focus',120),('question',80),('next_step',80),('growth_wish',80),('practice',80)):
        text=value.get(key,'')
        if not isinstance(text,str) or len(text)>limit:raise ValueError('invalid_private_reflection')
        result[key]=clean(text,limit)
    result.update(trial_id='',attitude=value.get('attitude','consider'))
    result['general_principle']=principle(value.get('general_principle'))
    if result['attitude'] not in ('consider','continue','adjust','keep','abandon'):
        raise ValueError('invalid_private_reflection_attitude')
    return result
