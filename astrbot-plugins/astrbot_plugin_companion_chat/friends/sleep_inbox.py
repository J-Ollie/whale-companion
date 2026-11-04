"""Durable unread transport events. No model calls while the character rests."""
import json
import sqlite3
import time


class SleepInbox:
    def __init__(self,root):
        self.path=root/'sleep-inbox.sqlite3'
        with self.connect() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS unread(
                id INTEGER PRIMARY KEY, sender TEXT NOT NULL, message_key TEXT UNIQUE NOT NULL,
                received REAL NOT NULL, raw TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending')''')
        self.path.chmod(0o600)

    def connect(self):
        return sqlite3.connect(self.path,timeout=5)

    def add(self,raw):
        sender=str(raw['user_id'])
        key=sender+':'+str(raw.get('message_id') or time.time_ns())
        with self.connect() as db:
            db.execute('INSERT OR IGNORE INTO unread(sender,message_key,received,raw) VALUES(?,?,?,?)',
                       (sender,key,time.time(),json.dumps(raw,ensure_ascii=False)))

    def pending(self):
        with self.connect() as db:
            rows=db.execute("SELECT id,sender,raw FROM unread WHERE state='pending' ORDER BY id").fetchall()
        groups={}
        for identifier,sender,raw in rows:
            groups.setdefault(sender,[]).append((identifier,json.loads(raw)))
        return groups

    def reserve_followup(self, raw):
        """Atomic durable claim; no second pipeline handles this same text."""
        sender = str(raw.get('user_id', ''))
        mid = str(raw.get('message_id', ''))
        if not sender.isdigit() or not mid or raw.get('message_type') != 'private':
            return None
        with self.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS followup_claims(unread_id INTEGER PRIMARY KEY)')
            row = db.execute("INSERT OR IGNORE INTO unread(sender,message_key,received,raw,state) VALUES(?,?,?,?, 'dispatched')",
                             (sender, sender+':'+mid, time.time(), json.dumps(raw, ensure_ascii=False)))
            if not row.rowcount:
                claimed = db.execute('SELECT u.id FROM unread u JOIN followup_claims c ON c.unread_id=u.id WHERE u.message_key=?', (sender+':'+mid,)).fetchone()
                if claimed:
                    return -claimed[0]  # Same transport event already belongs to this wait.
                return None
            identifier = row.lastrowid
            db.execute('INSERT INTO followup_claims VALUES(?)', (identifier,))
        return identifier

    def finish_followups(self, ids, state):
        if not ids:
            return
        if state not in ('pending', 'completed'):
            raise ValueError('Invalid followup disposition')
        with self.connect() as db:
            db.executemany('UPDATE unread SET state=? WHERE id=? AND id IN (SELECT unread_id FROM followup_claims)', [(state, i) for i in ids])
            db.executemany('DELETE FROM followup_claims WHERE unread_id=?', [(i,) for i in ids])

    def recover_followups(self):
        """Only our unfinished claims are retried after reload/process recovery."""
        with self.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS followup_claims(unread_id INTEGER PRIMARY KEY)')
            db.execute("UPDATE unread SET state='pending' WHERE id IN (SELECT unread_id FROM followup_claims) AND state='dispatched'")
            db.execute('DELETE FROM followup_claims')

    def has_pending(self,sender):
        with self.connect() as db:
            return db.execute("SELECT 1 FROM unread WHERE sender=? AND state='pending' LIMIT 1",(sender,)).fetchone() is not None

    def mark(self,ids,state):
        if state not in ('pending','dispatched','completed','blocked'):raise ValueError('Invalid inbox state')
        with self.connect() as db:
            db.executemany('UPDATE unread SET state=? WHERE id=?',[(state,int(i)) for i in ids])
