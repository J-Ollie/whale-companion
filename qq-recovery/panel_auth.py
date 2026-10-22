"""User-chosen password in addition to the existing SSH/bootstrap credential."""
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from pathlib import Path


class PanelAuth:
    def __init__(self, folder):
        self.folder=Path(folder)
        self.folder.mkdir(parents=True,exist_ok=True)
        self.path=self.folder/'panel-password.json'
        self.lock=threading.RLock()
        self.sessions={}
        self.guest_sessions={}
        self.failures=[]
        self.credential=None
        if self.path.exists():
            value=json.loads(self.path.read_text(encoding='utf-8'))
            # Corruption is not a reason to allow setting a new password.
            if value.get('algorithm')!='scrypt-v1' or len(bytes.fromhex(value['salt']))!=16 or len(bytes.fromhex(value['hash']))!=32:
                raise ValueError('Invalid panel password file; restore its backup')
            self.credential=value

    def info(self):
        return {'setup_required':self.credential is None,'password_required':True,'guest_available':self.credential is not None}

    @staticmethod
    def digest(password,salt):
        return hashlib.scrypt(password.encode('utf-8'),salt=bytes.fromhex(salt),n=16384,r=8,p=1,dklen=32,maxmem=64*1024*1024).hex()

    def setup(self,password,confirm):
        if not isinstance(password,str) or not 8<=len(password)<=128 or len(password.encode('utf-8'))>512:
            raise ValueError('密码请设置为8–128个字符')
        if password!=confirm:raise ValueError('两次输入的密码不一致')
        with self.lock:
            if self.credential is not None or self.path.exists():raise ValueError('密码已经设置，请使用密码登录')
            salt=secrets.token_hex(16)
            value={'algorithm':'scrypt-v1','salt':salt,'hash':self.digest(password,salt)}
            # O_EXCL prevents parallel first-setup requests from replacing an existing secret.
            fd=os.open(self.path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
            with os.fdopen(fd,'w',encoding='utf-8') as stream:
                json.dump(value,stream);stream.flush();os.fsync(stream.fileno())
            self.credential=value
            return self.issue()

    def issue(self):
        now=time.time()
        self.sessions={key:end for key,end in self.sessions.items() if end>now}
        if len(self.sessions)>=20:self.sessions.pop(next(iter(self.sessions)))
        session=secrets.token_urlsafe(32)
        self.sessions[hashlib.sha256(session.encode()).hexdigest()]=now+12*3600
        return {'session':session,'role':'admin'}

    def login(self,password):
        with self.lock:
            now=time.time()
            self.failures=[at for at in self.failures if at>now-300]
            if len(self.failures)>=5:raise ValueError('密码错误次数过多，请5分钟后再试')
            if self.credential is None:raise ValueError('请先设置管理密码')
            valid=isinstance(password,str) and len(password)<=128 and hmac.compare_digest(
                self.digest(password,self.credential['salt']),self.credential['hash'])
            if not valid:
                self.failures.append(now)
                raise ValueError('密码不正确')
            self.failures=[]
            return self.issue()

    def guest(self):
        with self.lock:
            if self.credential is None:raise ValueError('请先完成管理员密码设置')
            now=time.time()
            self.guest_sessions={key:end for key,end in self.guest_sessions.items() if end>now}
            if len(self.guest_sessions)>=20:self.guest_sessions.pop(next(iter(self.guest_sessions)))
            session=secrets.token_urlsafe(32)
            self.guest_sessions[hashlib.sha256(session.encode()).hexdigest()]=now+4*3600
            return {'session':session,'role':'guest'}

    def role(self,session):
        if not isinstance(session,str) or not 40<=len(session)<=100:return None
        with self.lock:
            if self.credential is None:return None
            digest=hashlib.sha256(session.encode()).hexdigest()
            now=time.time()
            if self.sessions.get(digest,0)>now:return 'admin'
            if self.guest_sessions.get(digest,0)>now:return 'guest'
            return None

    def authorized(self,session):
        return self.role(session)=='admin'
