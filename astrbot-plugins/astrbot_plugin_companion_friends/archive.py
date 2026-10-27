"""Private administrator archive. Independent of LLM memory and outreach quotas."""
import base64
import hashlib
import json
import re
import sqlite3
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit


class ChatArchive:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.media = self.root / 'media'
        self.media.mkdir(exist_ok=True)
        self.path = self.root / 'chats.sqlite3'
        with self.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS chats(id INTEGER PRIMARY KEY, qq TEXT NOT NULL,
                  name TEXT NOT NULL, direction TEXT NOT NULL, at REAL NOT NULL,
                  message_id TEXT NOT NULL, segments TEXT NOT NULL, UNIQUE(qq,direction,message_id));
                CREATE INDEX IF NOT EXISTS chat_person ON chats(qq,id);
            ''')
        self.path.chmod(0o600)

    def connect(self):
        return sqlite3.connect(self.path, timeout=5)

    def context_messages(self,qq,limit=16):
        if not re.fullmatch(r'\d{5,12}',str(qq)):return []
        with self.connect() as db:
            rows=db.execute('SELECT direction,at,segments FROM chats WHERE qq=? ORDER BY id DESC LIMIT ?',
                            (str(qq),max(1,min(30,int(limit))))).fetchall()
        result=[]
        for direction,at,raw in reversed(rows):
            parts=json.loads(raw)
            text=''.join(p.get('text','') if p.get('type')=='text' else '[非文字消息]' for p in parts)
            result.append({'role':'assistant' if direction=='out' else 'user','at':at,'text':text[:800]})
        return result

    def contact_summary(self,qq):
        if not re.fullmatch(r'\d{5,12}',str(qq)):return {}
        with self.connect() as db:
            row=db.execute('SELECT COUNT(*),MIN(at),MAX(at) FROM chats WHERE qq=?',(str(qq),)).fetchone()
            name=db.execute("SELECT name FROM chats WHERE qq=? AND direction='in' AND name!='' ORDER BY id DESC LIMIT 1",(str(qq),)).fetchone()
        return {'recorded_messages':row[0],'first_recorded_at':row[1],'last_recorded_at':row[2],
                'display_name':name[0] if name else ''}

    def public_names(self,owner):
        with self.connect() as db:
            rows=db.execute("""SELECT name FROM chats c WHERE direction='in' AND qq!=? AND name!=''
                AND id=(SELECT MAX(id) FROM chats n WHERE n.qq=c.qq AND n.direction='in' AND n.name!='')
                ORDER BY at DESC LIMIT 200""",(str(owner),)).fetchall()
        names=[]
        for (name,) in rows:
            name=name.strip()
            if (not 1<=len(name)<=30 or re.search(r'[<>\r\n{}]|\d{5,}|密码|密钥|token|管理员',name,re.I)
                or name.casefold()=='f1nger' or name in names):continue
            names.append(name)
        return names

    def interaction_activity(self,owner,now=None):
        """Administrator-only receipt counts; never read transcript segments."""
        from datetime import datetime,timedelta
        from zoneinfo import ZoneInfo
        zone=ZoneInfo('Asia/Shanghai')
        local=datetime.fromtimestamp(time.time() if now is None else now,zone)
        start=local.replace(hour=0,minute=0,second=0,microsecond=0)
        end=start+timedelta(days=1)
        with self.connect() as db:
            rows=db.execute('''SELECT direction,COUNT(*),COUNT(DISTINCT qq) FROM chats
                WHERE qq!=? AND at>=? AND at<? GROUP BY direction''',
                (str(owner),start.timestamp(),end.timestamp())).fetchall()
        counts={direction:(messages,people) for direction,messages,people in rows}
        received,senders=counts.get('in',(0,0))
        sent,recipients=counts.get('out',(0,0))
        return {'date':local.date().isoformat(),'timezone':'Asia/Shanghai',
                'received_messages':received,'received_from_people':senders,
                'confirmed_replies':sent,'replied_to_people':recipients,
                'source':'已保存的实际收发记录；仅反映归档启用后记录，不代表所有QQ历史'}

    def record(self, qq, name, direction, message, message_id='', at=None):
        if not re.fullmatch(r'\d{5,12}', str(qq)) or direction not in ('in', 'out'):
            return None
        segments = []
        if isinstance(message, str):
            message = [{'type': 'text', 'data': {'text': message}}]
        for part in message if isinstance(message, list) else []:
            if not isinstance(part, dict): continue
            kind = str(part.get('type', 'unknown'))[:30]
            data = part.get('data') or {}
            if not isinstance(data, dict): continue
            if kind == 'text':
                segments.append({'type': kind, 'text': str(data.get('text', ''))})
            elif kind == 'image':
                source = str(data.get('url') or data.get('file') or '')
                segments.append({'type': kind, 'source': source[:12000000]})
            else:
                segments.append({'type': kind, 'label': str(data.get('summary') or data.get('name') or data.get('id') or '')[:150]})
        if not segments: segments = [{'type': 'unknown', 'label': '非文字消息'}]
        with self.connect() as db:
            cur = db.execute('INSERT OR IGNORE INTO chats(qq,name,direction,at,message_id,segments) VALUES(?,?,?,?,?,?)',
                (str(qq), str(name)[:100], direction, at or time.time(), str(message_id or time.time_ns()), json.dumps(segments, ensure_ascii=False)))
            return cur.lastrowid if cur.rowcount else None

    def cache_images(self, identifier):
        if not identifier: return
        with self.connect() as db:
            row = db.execute('SELECT segments FROM chats WHERE id=?', (identifier,)).fetchone()
        if not row: return
        parts = json.loads(row[0])
        for part in parts:
            source = part.pop('source', '')
            if not source: continue
            try:
                data = self.image_bytes(source)
                if data.startswith(b'\x89PNG\r\n\x1a\n'): ext = 'png'
                elif data.startswith(b'\xff\xd8\xff'): ext = 'jpg'
                elif data.startswith((b'GIF87a', b'GIF89a')): ext = 'gif'
                elif data[:4] == b'RIFF' and data[8:12] == b'WEBP': ext = 'webp'
                else: continue
                name = hashlib.sha256(data).hexdigest() + '.' + ext
                target = self.media / name
                if not target.exists():
                    target.write_bytes(data); target.chmod(0o600)
                part['media'] = name
            except Exception:
                part['label'] = '原图未能缓存'
        with self.connect() as db:
            db.execute('UPDATE chats SET segments=? WHERE id=?', (json.dumps(parts, ensure_ascii=False), identifier))

    @staticmethod
    def image_bytes(source):
        limit = 8 * 1024 * 1024
        if source.startswith('base64://'):
            data = base64.b64decode(source[9:], validate=True)
        elif source.startswith(('/AstrBot/data/', 'file:///AstrBot/data/')):
            path = Path(source.removeprefix('file://')).resolve()
            if not path.is_relative_to(Path('/AstrBot/data').resolve()) or path.stat().st_size > limit: raise ValueError('path')
            data = path.read_bytes()
        else:
            url = urlsplit(source)
            host = (url.hostname or '').lower()
            if url.scheme != 'https' or url.port not in (None, 443) or url.username or url.password: raise ValueError('url')
            if not any(host == domain or host.endswith('.' + domain) for domain in ('qpic.cn', 'gtimg.cn', 'multimedia.nt.qq.com')): raise ValueError('host')
            # QQ CDN only; redirects are rejected rather than followed elsewhere.
            class NoRedirect(urllib.request.HTTPRedirectHandler):
                def redirect_request(self, *args, **kwargs): return None
            with urllib.request.build_opener(NoRedirect).open(source, timeout=8) as response:
                data = response.read(limit + 1)
        if len(data) > limit: raise ValueError('image too large')
        return data
