"""Private self-chosen intentions. Not facts, commitments, or public material."""
import json
import re
import sqlite3
import time
import uuid

STATES={'open','paused','abandoned','resolved'}
FORBIDDEN=re.compile(r'https?://|[<>]|\d{5,}|密码|密钥|验证码|token|api.?key',re.I)


class Intentions:
    def __init__(self,path):
        self.path=path
        with self.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS intentions(id TEXT PRIMARY KEY,title TEXT,state TEXT,created REAL,updated REAL)')
            db.execute('CREATE TABLE IF NOT EXISTS revisions(id INTEGER PRIMARY KEY,intent TEXT,at REAL,action TEXT,title TEXT,state TEXT)')
        path.chmod(0o600)

    def connect(self):return sqlite3.connect(self.path,timeout=3)

    def rows(self):
        with self.connect() as db:
            db.row_factory=sqlite3.Row
            return [dict(r) for r in db.execute("SELECT * FROM intentions WHERE state IN ('open','paused') ORDER BY updated DESC LIMIT 8")]

    def apply(self,change):
        if change is None:return None
        if not isinstance(change,dict) or change.get('action') not in ('create','pause','resume','abandon','resolve'):raise ValueError('invalid_intention_change')
        action=change['action'];identifier=change.get('id');title=change.get('title','');now=time.time()
        if action=='create':
            if not isinstance(title,str) or not 1<=len(title.strip())<=100 or FORBIDDEN.search(title):raise ValueError('invalid_intention_title')
            title=title.strip();identifier='intent:'+uuid.uuid4().hex[:16]
        elif not isinstance(identifier,str) or not re.fullmatch(r'intent:[a-f0-9]{16}',identifier):raise ValueError('invalid_intention_reference')
        state={'create':'open','pause':'paused','resume':'open','abandon':'abandoned','resolve':'resolved'}[action]
        with self.connect() as db:
            if action=='create':
                same=db.execute("SELECT id FROM intentions WHERE title=? AND state IN ('open','paused')",(title,)).fetchone()
                if same:return same[0]
                db.execute('INSERT INTO intentions VALUES(?,?,?,?,?)',(identifier,title,state,now,now))
            else:
                row=db.execute('SELECT title,state FROM intentions WHERE id=?',(identifier,)).fetchone()
                if not row or row[1] not in ('open','paused'):raise ValueError('intention_not_available')
                title=row[0];db.execute('UPDATE intentions SET state=?,updated=? WHERE id=?',(state,now,identifier))
            db.execute('INSERT INTO revisions(intent,at,action,title,state) VALUES(?,?,?,?,?)',(identifier,now,action,title,state))
        return identifier

    def counts(self):
        with self.connect() as db:return dict(db.execute('SELECT state,count(*) FROM intentions GROUP BY state'))
