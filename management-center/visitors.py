"""Minimal visitor journal. Nicknames are self-claims, never QQ identities."""
import hashlib
import hmac
import ipaddress
import json
import re
import secrets
import sqlite3
import threading
import time
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo
from contextlib import contextmanager

PAGES={'connection':'连接状态','diary':'公开日记','archive':'公开档案','visitors':'访客留言'}
TZ=ZoneInfo('Asia/Shanghai')


class Visitors:
    def __init__(self,root):
        self.root=Path(root)
        self.path=self.root/'recovery-data/visitors.sqlite3'
        self.path.parent.mkdir(parents=True,exist_ok=True)
        self.export=self.root/'data/astrbot/plugin_data/companion_visitors/feed.json'
        self.lock=threading.RLock()
        with self.connect() as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS people(
              visitor TEXT PRIMARY KEY,nickname TEXT NOT NULL DEFAULT '',first_at REAL,last_at REAL);
              CREATE TABLE IF NOT EXISTS visits(id INTEGER PRIMARY KEY,visitor TEXT,page TEXT,at REAL);
              CREATE INDEX IF NOT EXISTS visit_time ON visits(at);
              CREATE TABLE IF NOT EXISTS notes(id INTEGER PRIMARY KEY,visitor TEXT,nickname TEXT,message TEXT,
              at REAL,submission TEXT,UNIQUE(visitor,submission));
              CREATE INDEX IF NOT EXISTS note_time ON notes(at);''')
        # A private salt prevents dictionary recovery of IP digests. Raw IPs are never stored.
        salt_path=self.path.parent/'visitor-ip-salt'
        try:
            with salt_path.open('xb') as f:f.write(secrets.token_bytes(32))
        except FileExistsError:pass
        salt_path.chmod(0o600)
        self.salt=salt_path.read_bytes()
        self.migrate_visits()
        self.path.chmod(0o600)
        self.publish()

    @contextmanager
    def connect(self):
        db=sqlite3.connect(self.path,timeout=10)
        db.row_factory=sqlite3.Row
        try:
            with db:yield db
        finally:db.close()

    @staticmethod
    def identity(cookie):
        fresh=not isinstance(cookie,str) or re.fullmatch(r'[A-Za-z0-9_-]{43}',cookie) is None
        cookie=secrets.token_urlsafe(32) if fresh else cookie
        return hashlib.sha256(cookie.encode()).hexdigest(),cookie,fresh

    @staticmethod
    def nickname(value):
        if not isinstance(value,str) or len(value.strip())>24 or any(ord(c)<32 for c in value):
            raise ValueError('昵称最多24字，可留空')
        return value.strip()

    @staticmethod
    def day_start(now):
        return datetime.fromtimestamp(now,TZ).replace(hour=0,minute=0,second=0,microsecond=0).timestamp()

    def touch(self,db,visitor,now,nickname=None):
        db.execute('INSERT OR IGNORE INTO people VALUES(?,?,?,?)',(visitor,'',now,now))
        db.execute('UPDATE people SET last_at=? WHERE visitor=?',(now,visitor))
        if nickname is not None:db.execute('UPDATE people SET nickname=? WHERE visitor=?',(nickname,visitor))

    def migrate_visits(self):
        with self.connect() as db:
            if 'source' in {r['name'] for r in db.execute('PRAGMA table_info(visits)')}:return
            db.execute('ALTER TABLE visits ADD COLUMN source TEXT')
            # Historic data had no IP: collapse by anonymous browser only.
            last={}
            for row in db.execute('SELECT id,visitor,at FROM visits ORDER BY at,id').fetchall():
                key=row['visitor'];at=row['at']
                if key in last and at-last[key]<1800:
                    db.execute('DELETE FROM visits WHERE id=?',(row['id'],))
                else:last[key]=at
            db.execute("UPDATE visits SET page='site',source='browser:'||visitor")
            db.execute('CREATE INDEX IF NOT EXISTS visit_source_time ON visits(source,at)')

    def source_key(self,visitor,address=None):
        if address:
            try:
                ip=ipaddress.ip_address(address)
                if isinstance(ip,ipaddress.IPv6Address) and ip.ipv4_mapped:ip=ip.ipv4_mapped
                return 'ip:'+hmac.new(self.salt,ip.packed,hashlib.sha256).hexdigest()
            except ValueError:pass
        # Direct local tunnels cannot reveal the visitor's real IP.
        return 'browser:'+visitor

    def visit(self,visitor,page=None,address=None):
        # Accept old cached clients' public page codes, but never retain them.
        if page is not None and page not in PAGES:raise ValueError('来访记录无效')
        now=time.time();source=self.source_key(visitor,address)
        with self.lock,self.connect() as db:
            self.touch(db,visitor,now)
            recent=db.execute('SELECT 1 FROM visits WHERE source=? AND at>? LIMIT 1',(source,now-1800)).fetchone()
            if not recent and db.execute('SELECT count(*) FROM visits WHERE at>?',(self.day_start(now),)).fetchone()[0]<10000:
                db.execute("INSERT INTO visits(visitor,page,at,source) VALUES(?,'site',?,?)",(visitor,now,source))
            self.prune(db,now)
        self.publish()
        return {'ok':True}

    def profile(self,visitor,nickname):
        nickname=self.nickname(nickname)
        with self.lock,self.connect() as db:self.touch(db,visitor,time.time(),nickname)
        self.publish()
        return {'ok':True,'nickname':nickname,'identity_verified':False}

    def note(self,visitor,nickname,message,submission):
        nickname=self.nickname(nickname)
        if not isinstance(message,str) or not 1<=len(message.strip())<=500 or any(ord(c)<32 and c not in '\n\t' for c in message):
            raise ValueError('留言请填写1至500字')
        if not isinstance(submission,str) or not re.fullmatch(r'[a-f0-9-]{16,64}',submission):raise ValueError('提交编号无效，请刷新页面再试')
        now=time.time();start=self.day_start(now)
        with self.lock,self.connect() as db:
            prior=db.execute('SELECT id FROM notes WHERE visitor=? AND submission=?',(visitor,submission)).fetchone()
            if prior:
                self.publish()
                return {'ok':True,'id':prior['id'],'duplicate':True}
            count=db.execute('SELECT count(*) FROM notes WHERE visitor=? AND at>=?',(visitor,start)).fetchone()[0]
            if count>=5:raise ValueError('今天已留过5条，明天再来聊聊吧')
            last=db.execute('SELECT max(at) FROM notes WHERE visitor=?',(visitor,)).fetchone()[0]
            if last and now-last<60:raise ValueError('请稍等一分钟再留言')
            if db.execute('SELECT count(*) FROM notes WHERE at>=?',(start,)).fetchone()[0]>=1000:raise ValueError('今天留言较多，请明天再来')
            self.touch(db,visitor,now,nickname)
            row=db.execute('INSERT INTO notes(visitor,nickname,message,at,submission) VALUES(?,?,?,?,?)',(visitor,nickname,message.strip(),now,submission))
            note_id=row.lastrowid;self.prune(db,now)
        self.publish()
        return {'ok':True,'id':note_id,'visibility':'private_to_whale_and_admin'}

    def prune(self,db,now):
        db.execute('DELETE FROM visits WHERE at<?',(now-30*86400,))
        db.execute('DELETE FROM people WHERE last_at<?',(now-180*86400,))

    def snapshot(self,before=0):
        if type(before) is not int or not 0<=before<=10**12:raise ValueError('分页编号无效')
        now=time.time();start=self.day_start(now)
        with self.lock,self.connect() as db:
            stats=db.execute('SELECT count(*) AS views,count(DISTINCT source) AS sources FROM visits WHERE at>=?',(start,)).fetchone()
            visits=db.execute('SELECT v.id,v.at,p.nickname FROM visits v JOIN people p USING(visitor) ORDER BY v.id DESC LIMIT 50').fetchall()
            notes=db.execute('SELECT id,at,nickname,message FROM notes WHERE (?=0 OR id<?) ORDER BY id DESC LIMIT 31',(before,before)).fetchall()
            total=db.execute('SELECT count(*) FROM notes').fetchone()[0]
            today_notes=db.execute('SELECT count(*) FROM notes WHERE at>=?',(start,)).fetchone()[0]
        return {'generated_at':now,'stats':dict(stats)|{'notes':total,'today_notes':today_notes},
            'visits':[dict(r)|{'identity_verified':False} for r in visits],
            'messages':[dict(r)|{'identity_verified':False} for r in notes[:30]],
            'next_before':notes[29]['id'] if len(notes)>30 else 0,
            'notice':'昵称为访客自填，未经身份验证；同一来源30分钟内合并为一次来访，不记录浏览栏目；来访不等于已核实的个人身份。留言仅小鲸鱼和管理员可读，不能转发或公开。'}

    def publish(self):
        # Separate bounded export, so the bot never gets the DB or admin secrets.
        # Service sandbox grants only this isolated additional output directory.
        with self.lock:
            self.export.parent.mkdir(parents=True,exist_ok=True)
            tmp=self.export.with_suffix('.tmp')
            tmp.write_text(json.dumps(self.snapshot(),ensure_ascii=False),encoding='utf-8')
            tmp.chmod(0o600);tmp.replace(self.export)
