from . import quota as q
"""Scoped drafts, durable wake signals and request reservations; no external executor."""
from datetime import datetime
import hashlib
import json
import math
import re
import time
import uuid

from .phone import PhoneError
from .rhythm import TZ


def enabled(thinking):
    return bool(thinking.phone.config.get('self_directed_enabled', False))


class Resources:
    def __init__(self, thinking):
        self.t = thinking

    def reserve(self, count=1):
        if not enabled(self.t):
            return
        if type(count) is not int or not 1 <= count <= 12:
            raise ValueError('invalid_request_reservation')
        day = datetime.fromtimestamp(time.time(), TZ).date().isoformat()
        limit = max(1, min(240, int(self.t.phone.config.get('autonomy_daily_model_calls', 72))))
        with self.t.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT used FROM autonomy_resources WHERE scope=? AND day=?',
                             (self.t.store.scope, day)).fetchone()
            used = row['used'] if row else 0
            if not q.enabled(self.t.phone.config) and used + count > limit:
                raise PhoneError('autonomy_daily_model_budget')
            db.execute('INSERT INTO autonomy_resources VALUES(?,?,?) ON CONFLICT(scope,day) DO UPDATE SET used=excluded.used',
                       (self.t.store.scope, day, used + count))

    def status(self):
        day = datetime.fromtimestamp(time.time(), TZ).date().isoformat()
        with self.t.store.connect() as db:
            row = db.execute('SELECT used FROM autonomy_resources WHERE scope=? AND day=?',
                             (self.t.store.scope, day)).fetchone()
        limit = max(1, min(240, int(self.t.phone.config.get('autonomy_daily_model_calls', 72))))
        used = row['used'] if row else 0
        return {'day': day, 'used': used, 'limit':None if q.enabled(self.t.phone.config) else limit, 'remaining':None if q.enabled(self.t.phone.config) else max(0, limit-used),
                'unit': 'reserved_model_requests', 'currency_cost': None}


class Workspace:
    """SQLite is authoritative. IDs never map to user-selected filesystem paths."""
    def __init__(self, thinking):
        self.t = thinking

    def valid(self, row, private=None):
        if row['visibility'] == 'public':
            return True
        refs = json.loads(row['refs'])
        return bool(private and refs and all(private.snapshot(k) == v for k, v in refs.items()))

    def list(self, visibility='public', private=None, offset=0):
        if visibility not in ('public', 'private') or type(offset) is not int or not 0 <= offset <= 10000:
            raise ValueError('invalid_workspace_scope_or_page')
        with self.t.store.connect() as db:
            rows = db.execute('SELECT * FROM autonomy_works WHERE scope=? AND visibility=? ORDER BY updated DESC,id LIMIT 8 OFFSET ?',
                              (self.t.store.scope, visibility, offset)).fetchall()
        return [{k:r[k] for k in ('id','title','revision','updated')} for r in rows if self.valid(r, private)]

    def request(self, request, private=None):
        self.t.guard()
        if not enabled(self.t) or not isinstance(request, dict):
            raise ValueError('workspace_unavailable')
        if self.t.private_run and private is None or private is not None and private.t is not self.t:
            raise ValueError('workspace_scope_mismatch')
        if set(request)-{'action','id','title','body','revision','offset','tool'}:
            raise ValueError('invalid_workspace_fields')
        visibility = 'private' if private is not None else 'public'
        if private is not None:
            private.validate()
        action = request.get('action')
        if action == 'list':
            return {'kind':'workspace_drafts_not_evidence', 'visibility':visibility,
                    'items':self.list(visibility, private, request.get('offset',0))}
        if action == 'goals':
            if private is not None:raise ValueError('private_workspace_cannot_read_public_goals')
            offset = request.get('offset',0)
            if type(offset) is not int or not 0 <= offset <= 10000:raise ValueError('invalid_goal_page')
            with self.t.store.connect() as db:
                rows = db.execute('SELECT * FROM goals WHERE scope=? ORDER BY updated DESC,id LIMIT 8 OFFSET ?',
                                  (self.t.store.scope,offset)).fetchall()
            return {'kind':'own_question_archive','items':[dict(r) for r in rows],'next_offset':offset+len(rows)}
        if action in ('history','chain'):
            memory=self.t.work_memory
            return memory.history(request.get('id',''),private) if action=='history' else memory.chain(request.get('id',''),private)
        if action == 'request_tool':
            title = request.get('tool','')
            if not isinstance(title,str) or not 1 <= len(title) <= 80 or re.search(r'https?://|[<>]|\d{5,}|密码|密钥|token',title,re.I):
                raise ValueError('invalid_tool_request')
            if private is not None:
                raise ValueError('private_tool_request_denied')
            identifier = hashlib.sha256(title.encode()).hexdigest()[:16]
            with self.t.store.connect() as db:
                db.execute('INSERT OR IGNORE INTO autonomy_tool_requests VALUES(?,?,?,?,?)',
                           (self.t.store.scope,identifier,title,time.time(),'requested'))
            return {'kind':'tool_request_only','id':identifier,'installed':False}
        identifier = request.get('id','')
        if identifier and (not isinstance(identifier,str) or not re.fullmatch(r'work:[a-f0-9]{12}',identifier)):
            raise ValueError('invalid_workspace_id')
        with self.t.store.connect() as db:
            if action == 'write':db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM autonomy_works WHERE scope=? AND id=? AND visibility=?',
                             (self.t.store.scope,identifier,visibility)).fetchone() if identifier else None
            if action == 'read':
                if not row or not self.valid(row,private):
                    raise ValueError('workspace_not_accessible')
                offset = request.get('offset',0)
                if getattr(self.t,'work_memory',None):
                    return self.t.work_memory.inspect(identifier,revision=request.get('revision',0),offset=offset,private=private)|{'kind':'workspace_draft_not_evidence'}
                if type(offset) is not int or not 0 <= offset <= len(row['body']):
                    raise ValueError('invalid_workspace_offset')
                body = row['body'][offset:offset+800]
                return {'kind':'workspace_draft_not_evidence','id':identifier,'revision':row['revision'],
                    'title':row['title'],'body':body,'next_offset':offset+len(body),'more':offset+len(body)<len(row['body'])}
            if action != 'write':
                raise ValueError('invalid_workspace_action')
            title, body = request.get('title',''), request.get('body','')
            if (not isinstance(title,str) or not 1 <= len(title) <= 80 or not isinstance(body,str)
                    or not 1 <= len(body.encode()) <= 6000 or '\x00' in title+body
                    or re.search(r'(?:sk-|brz_)[A-Za-z0-9_-]{8,}|BEGIN .*PRIVATE KEY|(?:密码|密钥|验证码)\s*[:：]',title+body,re.I)):
                raise ValueError('invalid_workspace_text')
            expected = request.get('revision',0)
            if type(expected) is not int or expected != (row['revision'] if row else 0) or (identifier and not row):
                raise ValueError('workspace_revision_conflict')
            if row and not self.valid(row,private):
                raise ValueError('workspace_source_changed')
            refs = dict(private.refs) if private is not None else {}
            if private is not None and not refs:
                raise ValueError('private_workspace_requires_sources')
            identifier = identifier or 'work:'+uuid.uuid4().hex[:12]
            revision = expected+1
            db.execute('INSERT INTO autonomy_work_revisions VALUES(?,?,?,?,?,?,?,?)',
                       (self.t.store.scope,identifier,revision,time.time(),title,body,visibility,json.dumps(refs)))
            db.execute('INSERT INTO autonomy_works VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(scope,id) DO UPDATE SET '
                       'title=excluded.title,body=excluded.body,revision=excluded.revision,updated=excluded.updated,refs=excluded.refs',
                       (self.t.store.scope,identifier,visibility,title,body,revision,time.time(),json.dumps(refs)))
        return {'kind':'workspace_saved_not_execution_evidence','id':identifier,'revision':revision,'visibility':visibility}


class WakeSignals:
    def __init__(self, thinking):
        self.t = thinking

    def notify(self, kind, fingerprint, at=None):
        if not enabled(self.t):
            return
        if kind not in ('public_source','phone_reconnected') or not re.fullmatch(r'[a-f0-9]{12,64}',fingerprint):
            raise ValueError('invalid_public_wake_signal')
        at = time.time()+300 if at is None else at
        if type(at) not in (int,float) or not math.isfinite(at) or not time.time() <= at <= time.time()+7*86400:
            raise ValueError('invalid_wake_time')
        with self.t.store.connect() as db:
            db.execute('INSERT OR IGNORE INTO autonomy_wakes VALUES(?,?,?,?,0)',
                       (self.t.store.scope,kind+':'+fingerprint,kind,at))

    def tick(self, online=None):
        if not enabled(self.t):
            return
        now = time.time()
        if online is not None:
            previous = self.t.store.meta('phone_online',None)
            if previous is not bool(online):self.t.store.set_meta('phone_online',bool(online))
            if previous is False and online:
                # Coalesce flapping into one signal per hour; no device operation is replayed.
                self.notify('phone_reconnected',hashlib.sha256(str(int(now//3600)).encode()).hexdigest()[:16])
        rhythm = self.t.a.rhythm
        if not rhythm or not rhythm.healthy or rhythm.state['mode'] != 'awake':
            return
        with self.t.store.connect() as db:
            row = db.execute('SELECT min(at) AS at FROM autonomy_wakes WHERE scope=? AND used=0',
                             (self.t.store.scope,)).fetchone()
        if row and row['at'] is not None and row['at'] < rhythm.state['next_decision']:
            rhythm.state['next_decision'] = row['at']
            rhythm.save(force=True)

    def consume(self):
        with self.t.store.connect() as db:
            db.execute('UPDATE autonomy_wakes SET used=1 WHERE scope=? AND at<=? AND used=0',
                       (self.t.store.scope,time.time()))

    def schedule(self, decision, goal=''):
        if not enabled(self.t) or 'next_minutes' not in decision:
            return
        delay = decision['next_minutes']
        if type(delay) not in (int,float) or not math.isfinite(delay) or not 5 <= delay <= 10080:
            raise ValueError('invalid_self_wake_delay')
        now = time.time()
        at = now+delay*60
        rhythm = self.t.a.rhythm
        if rhythm and rhythm.state['mode'] == 'awake':
            rhythm.state['next_decision'] = at
            rhythm.save(force=True)
            self.t.a.state['next_check'] = at
        self.t.store.set_meta('self_wake_at',at)
        if goal:
            with self.t.store.connect() as db:
                db.execute("UPDATE goals SET next_at=? WHERE scope=? AND id=? AND status='open'",(at,self.t.store.scope,goal))


SCHEMA = '''
CREATE TABLE IF NOT EXISTS autonomy_resources(scope TEXT,day TEXT,used INTEGER NOT NULL,PRIMARY KEY(scope,day));
CREATE TABLE IF NOT EXISTS autonomy_works(scope TEXT,id TEXT,visibility TEXT,title TEXT,body TEXT,revision INTEGER,updated REAL,refs TEXT,PRIMARY KEY(scope,id));
CREATE TABLE IF NOT EXISTS autonomy_work_revisions(scope TEXT,id TEXT,revision INTEGER,at REAL,title TEXT,body TEXT,visibility TEXT,refs TEXT,PRIMARY KEY(scope,id,revision));
CREATE TABLE IF NOT EXISTS autonomy_tool_requests(scope TEXT,id TEXT,title TEXT,at REAL,status TEXT,PRIMARY KEY(scope,id));
CREATE TABLE IF NOT EXISTS autonomy_wakes(scope TEXT,id TEXT,kind TEXT,at REAL,used INTEGER,PRIMARY KEY(scope,id));
'''
