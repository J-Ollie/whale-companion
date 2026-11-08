"""Context-validated habits: real receipts, independent shadow checks and self choice."""
from collections import Counter
from datetime import datetime
import json
import re
import sqlite3
import time
from zoneinfo import ZoneInfo
from .habit_policy import POLICY,ROUTES,SENSITIVE,PREFIXES,digest,classify

KEY='companion_brain_choice_v1'

class Learning:
    def __init__(self,brain):
        self.b=brain;self.path=brain.path.parent/'learning.sqlite3'
        with self.connect() as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS samples(id TEXT PRIMARY KEY,scope TEXT,at REAL,route TEXT,deps TEXT,chars INTEGER,questions INTEGER,prefix TEXT,text TEXT,ref TEXT);
CREATE TABLE IF NOT EXISTS habits(scope TEXT,route TEXT,phase TEXT,updated REAL,PRIMARY KEY(scope,route));
CREATE TABLE IF NOT EXISTS assessments(sample TEXT PRIMARY KEY,verdict TEXT,quote TEXT,at REAL);
CREATE TABLE IF NOT EXISTS assessment_history(id INTEGER PRIMARY KEY,sample TEXT,verdict TEXT,at REAL);
CREATE TABLE IF NOT EXISTS habit_history(id INTEGER PRIMARY KEY,scope TEXT,route TEXT,phase TEXT,at REAL);
CREATE TABLE IF NOT EXISTS habit_hits(turn TEXT PRIMARY KEY,scope TEXT,route TEXT,phase TEXT,at REAL,deps TEXT);
CREATE TABLE IF NOT EXISTS habit_sample_evidence(sample TEXT PRIMARY KEY,policy TEXT,source_hash TEXT,context_kind TEXT,decision_source TEXT,decision_choice TEXT);
CREATE TABLE IF NOT EXISTS habit_shadow_evidence(turn TEXT PRIMARY KEY,policy TEXT,source_hash TEXT,matched INTEGER,decision_source TEXT,decision_choice TEXT);
CREATE TABLE IF NOT EXISTS habit_paths(turn TEXT PRIMARY KEY,at REAL,scope TEXT,route TEXT,path TEXT,reason TEXT,deps TEXT);
CREATE INDEX IF NOT EXISTS habit_sample_scope ON samples(scope,route,at);
CREATE INDEX IF NOT EXISTS habit_path_time ON habit_paths(at);''')
        self.path.chmod(0o600)

    def connect(self):return sqlite3.connect(self.path,timeout=2)

    def scope(self,event=None):
        if event is None:return 'contact:'+self.b.scope(self.b.chat.friends.owner)
        if event.get_group_id():raise ValueError('habit_review_private_only')
        scope=self.b.activities.event_scope(event)
        if not scope or not scope.startswith('contact:'):raise ValueError('unverified_habit_contact')
        return scope

    def source_hash(self,reference,event=None):
        match=re.search(r'\|obs:(\d+)$',reference or '')
        if not match:return None
        try:
            from .cognitive_context import peer
            memory=peer(self.b.chat.context,'memory')
            sender=str(event.get_sender_id()) if event else self.b.chat.friends.owner
            if not memory.affect().social.valid_source(sender,int(match[1])):return None
            row=memory.store.db.execute('SELECT qq,role,text,origin FROM observations WHERE qq=? AND id=?',(sender,int(match[1]))).fetchone()
            return digest(tuple(row)) if row else None
        except (AttributeError,TypeError,sqlite3.Error):return None

    def source_valid(self,row,event=None):
        try:scope=self.scope(event)
        except ValueError:return False
        if row['scope']!=scope:return False
        current=self.source_hash(row['ref'],event)
        expected=row['source_hash'] if 'source_hash' in row.keys() else None
        return bool(current and expected and current==expected)

    def route(self,event,situation):
        case,_=classify(event,situation)
        return (case['route'],case['deps']) if case else None

    def trace(self,event,case,path,reason):
        scope=self.b.activities.event_scope(event)
        if not scope:return
        with self.connect() as db:
            db.execute('INSERT OR REPLACE INTO habit_paths VALUES(?,?,?,?,?,?,?)',
                (self.b.activities.turn(event),time.time(),scope,(case or {}).get('route',''),path,reason,(case or {}).get('deps','')))
        event.set_extra('companion_habit_path',{'path':path,'reason':reason,'route':(case or {}).get('route','')})

    def choose(self,event,situation):
        case,reason=classify(event,situation)
        if case is None:self.trace(event,None,'slow',reason);return None
        scope=self.scope(event);case['scope']=scope
        case['source_hash']=self.source_hash(self.b.activities.turn(event),event)
        with self.connect() as db:row=db.execute('SELECT phase FROM habits WHERE scope=? AND route=?',(scope,case['route'])).fetchone()
        phase=row[0] if row else 'unseen';case['phase']=phase
        event.set_extra('companion_learning_case',case)
        if phase!='active':self.trace(event,case,'slow',phase);return None
        proof=self.readiness(scope,case['route'],case['deps'],event=event)
        if not proof['ready']:
            self.phase(scope,case['route'],'paused',event=event)
            with self.connect() as db:db.execute('INSERT OR IGNORE INTO habit_hits VALUES(?,?,?,?,?,?)',
                (self.b.activities.turn(event),scope,case['route'],'fallback',time.time(),case['deps']))
            self.trace(event,case,'slow','evidence_or_state_invalidated');return None
        self.trace(event,case,'fast','self_enabled_and_validated')
        with self.connect() as db:db.execute('INSERT OR IGNORE INTO habit_hits VALUES(?,?,?,?,?,?)',
            (self.b.activities.turn(event),scope,case['route'],'active',time.time(),case['deps']))
        return {'choice':'reply','note':'沿用自己选择的低影响处理策略，按当前情绪、态度和共同性格自然表达；普通收尾也可留白，不新增行动或承诺。',
                'source':'learned_habit','habit':case['route'],'strategy':ROUTES[case['route']],'allow_natural_silence':True}

    def observe_decision(self,event,choice):
        case=event.get_extra('companion_learning_case',{}) or {}
        if not case or case.get('phase')!='shadow' or choice.get('source')!='self_choice':return
        reference=self.b.activities.turn(event);current=self.source_hash(reference,event)
        if not current or current!=case.get('source_hash'):return
        matched=choice.get('choice') in ({'reply','silent'} if case['route']=='closing' else {'reply'})
        with self.connect() as db:
            db.execute('INSERT OR IGNORE INTO habit_hits VALUES(?,?,?,?,?,?)',(reference,case['scope'],case['route'],'shadow',time.time(),case['deps']))
            db.execute('INSERT OR IGNORE INTO habit_shadow_evidence VALUES(?,?,?,?,?,?)',
                (reference,POLICY,current,int(matched),choice['source'],choice['choice']))
        if not matched:self.phase(case['scope'],case['route'],'paused',event=event)

    def record_reply(self,event,text,confirmed):
        if not confirmed or not isinstance(text,str) or not text:return
        scope=self.b.activities.event_scope(event)
        if scope is None:return
        case=event.get_extra('companion_learning_case',{}) or {};choice=event.get_extra(KEY,{}) or {}
        reference=self.b.activities.turn(event);current=self.source_hash(reference,event)
        safe=bool(case and current and current==case.get('source_hash') and case.get('scope')==scope
            and choice.get('source')=='self_choice' and choice.get('choice')=='reply'
            and len(text)<=240 and not SENSITIVE.search(text)
            and not re.search(r'https?://|\d{5,}|密码|密钥|验证码|token|api.?key',text,re.I))
        route=case.get('route','') if safe else '';identifier=digest([reference,text])
        prefix=text.strip()[:2];prefix=prefix if prefix in PREFIXES else ''
        with self.connect() as db:
            db.execute('INSERT OR IGNORE INTO samples VALUES(?,?,?,?,?,?,?,?,?,?)',
                (identifier,scope,time.time(),route,case.get('deps',''),len(text),text.count('?')+text.count('？'),prefix,text if safe else '',reference))
            if route:
                db.execute('INSERT OR IGNORE INTO habit_sample_evidence VALUES(?,?,?,?,?,?)',
                    (identifier,POLICY,current,case['context_kind'],choice['source'],choice['choice']))
                db.execute("INSERT OR IGNORE INTO habits VALUES(?,?,'candidate',?)",(scope,route,time.time()))

    def readiness(self,scope,route,deps=None,event=None):
        with self.connect() as db:
            db.row_factory=sqlite3.Row
            rows=db.execute("SELECT s.*,a.verdict,e.source_hash FROM samples s JOIN habit_sample_evidence e ON e.sample=s.id LEFT JOIN assessments a ON a.sample=s.id WHERE s.scope=? AND s.route=? AND e.policy=? AND e.decision_source='self_choice' AND e.decision_choice='reply' ORDER BY s.at DESC LIMIT 200",(scope,route,POLICY)).fetchall()
        rows=[r for r in rows if self.source_valid(r,event)]
        # Preserve context boundaries. The largest compatible evidence group is
        # shown for review; the actual fast path always supplies current deps.
        if deps is None:
            groups=Counter(r['deps'] for r in rows if r['verdict']=='fit')
            deps=groups.most_common(1)[0][0] if groups else None
        selected=[r for r in rows if r['deps']==deps]
        fits=list({r['ref'].rsplit('|obs:',1)[-1]:r for r in selected if r['verdict']=='fit'}.values())
        counter=sum(r['verdict']=='counterexample' for r in selected)
        days=len({datetime.fromtimestamp(r['at'],ZoneInfo('Asia/Shanghai')).date() for r in fits})
        with self.connect() as db:
            db.row_factory=sqlite3.Row
            shadows=db.execute("SELECT h.scope,h.turn AS ref,e.source_hash,e.matched FROM habit_hits h JOIN habit_shadow_evidence e ON e.turn=h.turn WHERE h.scope=? AND h.route=? AND h.phase='shadow' AND h.deps=? AND e.policy=? AND e.decision_source='self_choice'",(scope,route,deps,POLICY)).fetchall()
        shadows=[r for r in shadows if self.source_valid(r,event)]
        matches=len({r['ref'].rsplit('|obs:',1)[-1] for r in shadows if r['matched']})
        disagreements=sum(not r['matched'] for r in shadows)
        missing=[]
        if len(fits)<5:missing.append('five_independent_fit_events')
        if days<2:missing.append('two_calendar_days')
        if matches<2:missing.append('two_matching_slow_path_observations')
        if counter:missing.append('unresolved_counterexample')
        if disagreements:missing.append('shadow_disagreed')
        return {'ready':not missing,'fit_events':len(fits),'days':days,'shadow_matches':matches,'counterexamples':counter,
                'shadow_disagreements':disagreements,'unreviewed_events':len({r['ref'] for r in rows if r['verdict'] is None}),
                'missing':missing,'candidate_evidence_only':True}

    def supported(self,scope,route,deps=None,event=None):return self.readiness(scope,route,deps,event)['ready']

    def phase(self,scope,route,phase,event=None):
        if route not in ROUTES or phase not in ('shadow','active','paused','retired'):raise ValueError('invalid_habit_phase')
        proof=self.readiness(scope,route,event=event)
        if phase=='active' and not proof['ready']:raise ValueError('habit_not_validated')
        if phase=='shadow' and (proof['fit_events']<3 or proof['counterexamples']):raise ValueError('habit_not_ready_for_shadow')
        with self.connect() as db:
            exists=db.execute('SELECT phase FROM habits WHERE scope=? AND route=?',(scope,route)).fetchone()
            if not exists:raise ValueError('unknown_habit')
            if exists[0]==phase:return
            db.execute('UPDATE habits SET phase=?,updated=? WHERE scope=? AND route=?',(phase,time.time(),scope,route))
            db.execute('INSERT INTO habit_history(scope,route,phase,at) VALUES(?,?,?,?)',(scope,route,phase,time.time()))

    def review_context(self,event=None):
        scope=self.scope(event)
        with self.connect() as db:
            db.row_factory=sqlite3.Row
            samples=db.execute("SELECT s.*,e.source_hash,e.context_kind,a.verdict FROM samples s JOIN habit_sample_evidence e ON e.sample=s.id LEFT JOIN assessments a ON a.sample=s.id WHERE s.scope=? AND s.route!='' AND e.policy=? ORDER BY (a.sample IS NULL) DESC,s.at ASC LIMIT 30",(scope,POLICY)).fetchall()
            phases=[dict(r) for r in db.execute('SELECT route,phase FROM habits WHERE scope=?',(scope,))]
        samples=[{'id':r['id'],'route':r['route'],'context_kind':r['context_kind'],'actual_sent_text':r['text'],
                  'previous_verdict':r['verdict']} for r in samples if self.source_valid(r,event)][:6]
        for row in phases:row.update(strategy=ROUTES.get(row['route'],'unsupported_legacy_route'),progress=self.readiness(scope,row['route'],event=event))
        return {'samples':samples,'habits':phases,'policy':POLICY,
            'note':'这些是本私有范围实际发送过的自身表达；fit只是自己的复核判断，不是用户满意或客观效果。可unknown或保持原样；需自己选择旁路和启用。'}

    def apply_review(self,value,offered,event=None):
        if value is None:return {'ratings':0}
        if not isinstance(value,dict):raise ValueError('invalid_learning_review')
        scope=self.scope(event);allowed={r['id'] for r in offered['samples']};ratings=value.get('ratings',[])
        if not isinstance(ratings,list) or len(ratings)>3:raise ValueError('invalid_learning_ratings')
        updates=[]
        with self.connect() as db:
            db.row_factory=sqlite3.Row
            for item in ratings:
                if not isinstance(item,dict) or item.get('id') not in allowed or item.get('verdict') not in ('fit','counterexample','unknown'):raise ValueError('unoffered_learning_sample')
                row=db.execute('SELECT s.*,e.source_hash FROM samples s JOIN habit_sample_evidence e ON e.sample=s.id WHERE s.id=? AND e.policy=?',(item['id'],POLICY)).fetchone()
                quote=item.get('quote','')
                if not row or not isinstance(quote,str) or not quote or quote not in row['text'] or not self.source_valid(row,event):raise ValueError('unverified_learning_source')
                updates.append((row,item['verdict'],quote))
            # Validate the whole ratings list before any writes, retain revisions.
            for row,verdict,quote in updates:
                db.execute('INSERT INTO assessments VALUES(?,?,?,?) ON CONFLICT(sample) DO UPDATE SET verdict=excluded.verdict,quote=excluded.quote,at=excluded.at',(row['id'],verdict,quote,time.time()))
                db.execute('INSERT INTO assessment_history(sample,verdict,at) VALUES(?,?,?)',(row['id'],verdict,time.time()))
        for row,verdict,_ in updates:
            if verdict=='counterexample':self.phase(scope,row['route'],'paused',event)
        change=value.get('habit');result={'ratings':len(updates)}
        if change is not None:
            if not isinstance(change,dict) or not any(r['route']==change.get('route') for r in offered['habits']):raise ValueError('unoffered_habit')
            self.phase(scope,change['route'],change.get('phase'),event);result['habit_phase']=change['phase']
        return result

    def compact(self,event):
        scope=self.scope(event)
        with self.connect() as db:phases=db.execute('SELECT route,phase FROM habits WHERE scope=?',(scope,)).fetchall()
        return {'habits':[{'route':r,'phase':p} for r,p in phases],
                'optional_self_review_tool':'companion_habits','review_is_optional':True}

    def mirror(self,event=None):
        scope=self.b.activities.event_scope(event) if event else None
        with self.connect() as db:
            rows=db.execute('SELECT chars,questions,prefix FROM samples WHERE at>=?'+(' AND scope=?' if scope else ''),
                (time.time()-7*86400,scope) if scope else (time.time()-7*86400,)).fetchall()
        return {'window_days':7,'confirmed_sent_samples':len(rows),'average_characters':round(sum(r[0] for r in rows)/len(rows),1) if rows else None,
            'question_mark_messages':sum(r[1]>0 for r in rows),'repeated_generic_openers':Counter(r[2] for r in rows if r[2]).most_common(4),
            'note':'只描述有回执的表达，不推断对方已读或感受，也不规定越短越好。'}

    def status(self):
        with self.connect() as db:
            phases=dict(db.execute('SELECT phase,count(*) FROM habits GROUP BY phase'))
            hits=dict(db.execute('SELECT phase,count(*) FROM habit_hits GROUP BY phase'))
            paths=dict(db.execute('SELECT path,count(*) FROM habit_paths WHERE at>=? GROUP BY path',(time.time()-7*86400,)))
            reasons=dict(db.execute('SELECT reason,count(*) FROM habit_paths WHERE at>=? GROUP BY reason',(time.time()-7*86400,)))
        return {'policy':POLICY,'phases':phases,'hits':hits,'paths_last_seven_days':paths,'path_reasons':reasons,
            'requires_self_choice':True,'cached_emotions_or_permissions':False,'seeded_active_habits':False,
            'unreported_token_savings':None,'validation':'scoped_sources_and_independent_slow_path_observations'}
