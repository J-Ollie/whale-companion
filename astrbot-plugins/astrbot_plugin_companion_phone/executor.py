"""Phone-initiated, authenticated executor transport; durable at-most-once job identities."""
import asyncio
import hashlib
import hmac
import json
import secrets
import sqlite3
import time
import uuid
from contextlib import closing

OPS={'state','xml','screenshot','tap','swipe','key','open_app','resolve','input'}
NATIVE_EXCLUDED={'com.tencent.mobileqq'}  # QQ comments retain their existing verified ADB path.


class ExecutorError(Exception):
    pass


class Executor:
    def __init__(self,root,guard,apps):
        self.root=root;self.guard=guard;self.apps=apps
        self.path=root/'executor-jobs.sqlite3';self.socket=root/'executor-control.sock'
        self.key_path=root/'executor-credentials.json'
        self.state_path=root/'executor-status.json'
        self.server=None;self.status={};self.seen=0;self.lock=asyncio.Lock();self.waiters={};self.payloads={}
        self.paired=False
        self.wake=asyncio.Event()
        if self.state_path.exists():
            previous=json.loads(self.state_path.read_text())
            self.paired=previous.get('paired') is True
            self.status={'paused':previous.get('paused') is True}
        if self.key_path.exists():
            obj=json.loads(self.key_path.read_text());self.token=obj['token']
            if not isinstance(self.token,str) or len(self.token)<40:raise ValueError('Invalid executor credential')
        else:
            self.token=secrets.token_urlsafe(32)
            self.key_path.write_text(json.dumps({'token':self.token}));self.key_path.chmod(0o600)
        with closing(self.db()) as db,db:
            db.execute('CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY,op TEXT NOT NULL,args TEXT NOT NULL,'
                'created REAL NOT NULL,expires REAL NOT NULL,status TEXT NOT NULL,lease TEXT,boot TEXT,result TEXT)')
            db.execute("UPDATE jobs SET status='uncertain',result='process_restarted' WHERE status IN ('queued','leased')")
        self.path.chmod(0o600)

    def db(self):
        db=sqlite3.connect(self.path,timeout=2);db.row_factory=sqlite3.Row;return db

    def allowed_apps(self):return sorted(set(self.apps())-NATIVE_EXCLUDED)

    def online(self):
        return time.monotonic()-self.seen<18 and bool(self.status.get('ready')) and not self.status.get('paused',True)

    def usable(self):return self.online() and bool(self.guard())

    async def start(self):
        self.socket.unlink(missing_ok=True)
        self.server=await asyncio.start_unix_server(self.handle,path=str(self.socket),limit=1200000)
        self.socket.chmod(0o600)

    async def close(self):
        if self.server:self.server.close();await self.server.wait_closed()
        self.socket.unlink(missing_ok=True)
        with closing(self.db()) as db,db:
            db.execute("UPDATE jobs SET status='uncertain' WHERE status IN ('queued','leased')")
        for event in self.waiters.values():event.set()

    def authenticate(self,token):return isinstance(token,str) and hmac.compare_digest(token,self.token)

    def poll(self,body):
        boot=body.get('boot','')
        if not isinstance(boot,str) or not 10<=len(boot)<=80:raise ExecutorError('invalid_device')
        for key in ('paused','ready','locked','screen_on'):
            if type(body.get(key)) is not bool:raise ExecutorError('invalid_device')
        foreground=body.get('foreground','')
        if foreground not in self.allowed_apps():foreground=''
        self.status={k:body.get(k) for k in ('boot','paused','ready','locked','screen_on','model','android','sdk')}
        self.status['foreground']=foreground;self.seen=time.monotonic()
        self.paired=self.paired or self.status['ready']
        saved={'paired':self.paired,'paused':self.status['paused'],'ready':self.status['ready'],
            'at':time.time(),'locked':self.status['locked']}
        temp=self.state_path.with_suffix('.tmp');temp.write_text(json.dumps(saved));temp.chmod(0o600);temp.replace(self.state_path)
        active=self.usable();job=None;now=time.time()
        with closing(self.db()) as db,db:
            db.execute("UPDATE jobs SET status='uncertain' WHERE status='leased' AND boot!=?",(boot,))
            db.execute("UPDATE jobs SET status='expired' WHERE status='queued' AND expires<?",(now,))
            if not active:
                db.execute("UPDATE jobs SET status='cancelled' WHERE status='queued'")
            else:
                row=db.execute("SELECT * FROM jobs WHERE status='queued' AND expires>? ORDER BY created LIMIT 1",(now,)).fetchone()
                if row:
                    lease=uuid.uuid4().hex
                    db.execute("UPDATE jobs SET status='leased',lease=?,boot=? WHERE id=? AND status='queued'",(lease,boot,row['id']))
                    job={'id':row['id'],'lease':lease,'op':row['op'],'args':json.loads(row['args']),
                        'ttl_ms':max(0,int((row['expires']-now)*1000)),'allowed_apps':self.allowed_apps()}
        return {'ok':True,'role_active':active,'job':job,'allowed_apps':self.allowed_apps()}

    def result(self,body):
        identifier=body.get('id','');lease=body.get('lease','');boot=body.get('boot','')
        if not isinstance(identifier,str) or len(identifier)!=32:raise ExecutorError('invalid_result')
        result=body.get('result')
        if not isinstance(result,dict) or type(result.get('ok')) is not bool:raise ExecutorError('invalid_result')
        encoded=json.dumps(result,ensure_ascii=False)
        if len(encoded.encode())>1000000:raise ExecutorError('result_too_large')
        with closing(self.db()) as db,db:
            row=db.execute('SELECT * FROM jobs WHERE id=?',(identifier,)).fetchone()
            if not row or not hmac.compare_digest(row['lease'] or '',str(lease)) or row['boot']!=boot:
                raise ExecutorError('wrong_lease')
            if row['status'] in ('done','failed'):return {'ok':True,'duplicate':True}
            if row['status'] not in ('leased','uncertain'):raise ExecutorError('invalid_job_state')
            status='done' if result['ok'] else 'uncertain' if result.get('uncertain') else 'failed'
            # Screenshots/UI text live only until the waiting observer consumes them.
            summary=json.dumps({'ok':result['ok'],'code':str(result.get('code',''))[:80],
                'result_sha256':hashlib.sha256(encoded.encode()).hexdigest()})
            db.execute('UPDATE jobs SET status=?,result=? WHERE id=?',(status,summary,identifier))
        self.payloads[identifier]=result
        state=body.get('state')
        if isinstance(state,dict):
            for key in ('screen_on','locked','paused','ready'):
                if type(state.get(key)) is bool:self.status[key]=state[key]
            if state.get('foreground') in self.allowed_apps():self.status['foreground']=state['foreground']
            else:self.status['foreground']=''
            self.seen=time.monotonic()
        if identifier in self.waiters:self.waiters[identifier].set()
        else:self.payloads.pop(identifier,None)
        return {'ok':True}

    async def rpc(self,op,args):
        if op not in OPS or not isinstance(args,dict) or len(json.dumps(args).encode())>3000:
            raise ExecutorError('unsupported_operation')
        if not self.usable():raise ExecutorError('executor_unavailable_or_resting')
        async with self.lock:
            if not self.usable():raise ExecutorError('executor_unavailable_or_resting')
            if self.status.get('locked') and op not in ('key','resolve'):
                raise ExecutorError('phone_locked')
            identifier=uuid.uuid4().hex;now=time.time();event=asyncio.Event();self.waiters[identifier]=event
            with closing(self.db()) as db,db:
                db.execute('INSERT INTO jobs(id,op,args,created,expires,status) VALUES(?,?,?,?,?,?)',
                    (identifier,op,json.dumps(args,ensure_ascii=False),now,now+22,'queued'))
            self.wake.set()
            try:
                await asyncio.wait_for(event.wait(),24)
                result=self.payloads.pop(identifier,None)
                if result is None or not result.get('ok'):
                    raise ExecutorError(str((result or {}).get('code','execution_not_confirmed'))[:80])
                return result
            except (asyncio.TimeoutError,asyncio.CancelledError):
                with closing(self.db()) as db,db:
                    db.execute("UPDATE jobs SET status=CASE WHEN status='queued' THEN 'cancelled' ELSE 'uncertain' END "
                        "WHERE id=? AND status IN ('queued','leased')",(identifier,))
                # Never fall through to ADB after a native action might have run.
                if asyncio.current_task().cancelling():raise
                raise ExecutorError('result_uncertain_do_not_repeat') from None
            finally:
                self.waiters.pop(identifier,None);self.payloads.pop(identifier,None)

    async def handle(self,reader,writer):
        result={'ok':False,'code':'invalid_request'}
        try:
            raw=await asyncio.wait_for(reader.readline(),5)
            if len(raw)>1100000:raise ExecutorError('request_too_large')
            request=json.loads(raw)
            if not self.authenticate(request.get('token')):raise ExecutorError('unauthorized')
            body=request.get('body')
            if not isinstance(body,dict):raise ExecutorError('invalid_body')
            if request.get('action')=='poll':
                self.wake.clear();result=self.poll(body)
                if result.get('role_active') and result.get('job') is None:
                    try:await asyncio.wait_for(self.wake.wait(),7)
                    except asyncio.TimeoutError:pass
                    result=self.poll(body)
            elif request.get('action')=='result':result=self.result(body)
            else:raise ExecutorError('unsupported_action')
        except (ExecutorError,ValueError,TypeError,KeyError,asyncio.TimeoutError) as exc:
            result={'ok':False,'code':str(exc)[:80] if isinstance(exc,ExecutorError) else 'invalid_request'}
        except Exception:result={'ok':False,'code':'temporarily_unavailable'}
        try:writer.write(json.dumps(result,ensure_ascii=False).encode()+b'\n');await writer.drain()
        finally:writer.close();await writer.wait_closed()
