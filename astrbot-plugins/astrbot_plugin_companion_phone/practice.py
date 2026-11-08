from . import quota as q
"""Public-branch Python experiments; the host worker owns the fixed container profile."""
import asyncio
from datetime import datetime,timedelta
import hashlib
import json
import math
import re
import time
import uuid

from .phone import PhoneError
from .rhythm import TZ
from .self_directed import enabled

SCHEMA='''CREATE TABLE IF NOT EXISTS autonomy_practice_runs(
scope TEXT,id TEXT,run_id TEXT,created REAL,title TEXT,code TEXT,hash TEXT,status TEXT,result TEXT,work_id TEXT,
PRIMARY KEY(scope,id));'''

EVIDENCE_SCOPE = {'program_run_only':True,'generated_plot_is_not_image_review':True,
                  'title_comment_stdout_are_not_experience_evidence':True,
                  'prior_artifact_files_available_to_new_program':False}


class Practice:
    def __init__(self,thinking):
        self.t=thinking
        self.socket=thinking.phone.root/'practice-worker.sock'

    def enabled(self):
        return enabled(self.t) and self.t.phone.config.get('practice_enabled',False)

    def day_range(self):
        today=datetime.fromtimestamp(time.time(),TZ).replace(hour=0,minute=0,second=0,microsecond=0)
        return today.timestamp(),(today+timedelta(days=1)).timestamp()

    def status(self):
        start,end=self.day_range()
        with self.t.store.connect() as db:
            used=db.execute('SELECT count(*) FROM autonomy_practice_runs WHERE scope=? AND created>=? AND created<?',
                            (self.t.store.scope,start,end)).fetchone()[0]
            rows=db.execute('SELECT id,title,status,created FROM autonomy_practice_runs WHERE scope=? ORDER BY created DESC LIMIT 3',
                            (self.t.store.scope,)).fetchall()
        limit=max(1,min(24,int(self.t.phone.config.get('practice_daily_limit',12))))
        return {'enabled':self.enabled(),'worker_socket_present':self.socket.exists(),'used':used,'limit':None if q.enabled(self.t.phone.config) else limit,
                'remaining':None if q.enabled(self.t.phone.config) else max(0,limit-used),'recent':[dict(r) for r in rows],
                'capabilities':['python_standard_library','numeric_line_plot'], 'third_party_install':False}

    def guard(self):
        self.t.guard()
        if not self.enabled() or self.t.private_run:
            raise PhoneError('public_practice_unavailable')

    def validate(self,request):
        if not isinstance(request,dict) or set(request)-{'action','id','title','code','work_id','revision','plot_index'}:
            raise ValueError('invalid_practice_fields')
        action=request.get('action','run')
        if action not in ('run','read','review','list','capabilities'):raise ValueError('invalid_practice_action')
        if action=='run':
            code=request.get('code','');title=request.get('title','Python练习')
            if (not isinstance(code,str) or not 1<=len(code.encode())<=6000 or '\x00' in code
                    or not isinstance(title,str) or not 1<=len(title)<=64
                    or re.search(r'(?:sk-|brz_)[A-Za-z0-9_-]{8,}|BEGIN .*PRIVATE KEY|(?:密码|密钥|验证码)\s*[:：]',title+code,re.I)):
                raise ValueError('invalid_practice_code_or_title')
        return action

    async def exchange(self,identifier,code):
        reader=None;writer=None
        try:
            reader,writer=await asyncio.wait_for(asyncio.open_unix_connection(str(self.socket),limit=40000),timeout=2)
            writer.write(json.dumps({'id':identifier,'code':code}).encode()+b'\n')
            await writer.drain()
            task=asyncio.create_task(reader.readline())
            try:
                deadline=time.monotonic()+18
                while not task.done():
                    self.guard()
                    if time.monotonic()>deadline:raise asyncio.TimeoutError()
                    await asyncio.wait({task},timeout=.25)
                self.guard()
                raw=await task
                if not raw or len(raw)>40000:raise ValueError('invalid_practice_response')
                result=json.loads(raw)
            finally:
                if not task.done():task.cancel()
                await asyncio.gather(task,return_exceptions=True)
        finally:
            if writer:
                writer.close()
                try:await writer.wait_closed()
                except OSError:pass
        if (not isinstance(result,dict) or result.get('id')!=identifier
                or result.get('code_hash')!=hashlib.sha256(code.encode()).hexdigest()
                or result.get('status') not in ('succeeded','execution_error','timeout','output_limit','unavailable','interrupted_unknown')
                or not isinstance(result.get('stdout'),str) or not isinstance(result.get('stderr'),str)
                or len((result['stdout']+result['stderr']).encode())>20000):
            raise ValueError('unverified_practice_response')
        return result

    def render(self,identifier,plots):
        from PIL import Image,ImageDraw,ImageFont
        if not isinstance(plots,list) or len(plots)>2:raise ValueError('invalid_practice_plots')
        artifacts=[]
        scope=hashlib.sha256(self.t.store.scope.encode()).hexdigest()[:16]
        for index,plot in enumerate(plots):
            if not isinstance(plot,dict):raise ValueError('invalid_practice_plot')
            x,y=plot.get('x'),plot.get('y');title=plot.get('title','')
            if (not isinstance(x,list) or not isinstance(y,list) or not 2<=len(x)==len(y)<=256
                    or any(type(v) not in (int,float) or not math.isfinite(v) or abs(v)>1e12 for v in x+y)
                    or not isinstance(title,str) or len(title)>80):raise ValueError('invalid_practice_plot_data')
            image=Image.new('RGB',(720,420),'#fafcff');draw=ImageDraw.Draw(image)
            font=ImageFont.load_default()
            for candidate in ('/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc','/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc'):
                try:font=ImageFont.truetype(candidate,16);break
                except OSError:pass
            try:draw.text((55,20),title,fill='#172c45',font=font)
            except UnicodeError:draw.text((55,20),title.encode('ascii',errors='replace').decode(),fill='#172c45',font=font)
            draw.line((60,60,60,365,685,365),fill='#52677a',width=2)
            xmin,xmax=min(x),max(x);ymin,ymax=min(y),max(y)
            points=[(60+(v-xmin)/(xmax-xmin or 1)*625,365-(w-ymin)/(ymax-ymin or 1)*285) for v,w in zip(x,y)]
            draw.line(points,fill='#177baf',width=3)
            draw.text((60,380),format(xmin,'.4g'),fill='#52677a')
            draw.text((630,380),format(xmax,'.4g'),fill='#52677a')
            draw.text((5,60),format(ymax,'.4g'),fill='#52677a')
            draw.text((5,350),format(ymin,'.4g'),fill='#52677a')
            target=self.t.phone.root/'practice-artifacts'/scope/identifier/(str(index)+'.png')
            target.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
            image.save(target,format='PNG');target.chmod(0o600)
            artifacts.append({'id':'practice:'+identifier+':plot:'+str(index),'format':'png','title':title,
                              'image_hash':hashlib.sha256(target.read_bytes()).hexdigest(),
                              'relative_path':str(target.relative_to(self.t.phone.root))})
        return artifacts

    def save(self,identifier,status,result):
        with self.t.store.connect() as db:
            db.execute('UPDATE autonomy_practice_runs SET status=?,result=? WHERE scope=? AND id=?',
                       (status,json.dumps(result,ensure_ascii=False),self.t.store.scope,identifier))

    async def request(self,request):
        self.guard()
        action=self.validate(request)
        if action=='review':return await self.t.work_memory.review_image(request.get('id',''),request.get('plot_index',0))
        if action in ('list','capabilities'):return {'kind':'practice_capabilities_and_records',**self.status()}
        if action=='read':
            identifier=request.get('id','')
            if not isinstance(identifier,str) or not re.fullmatch(r'practice:[a-f0-9]{32}',identifier):raise ValueError('invalid_practice_id')
            with self.t.store.connect() as db:
                row=db.execute('SELECT title,hash,status,result FROM autonomy_practice_runs WHERE scope=? AND id=?',
                               (self.t.store.scope,identifier[9:])).fetchone()
            if not row:raise ValueError('practice_not_accessible')
            result=json.loads(row['result']) if row['result'] else None
            if result:
                result={**result,'stdout':result.get('stdout','')[:700],'stderr':result.get('stderr','')[:500]}
            return {'kind':'prior_program_run_not_fact_verification',**dict(row),'result':result,'id':identifier,'work_id':(result or {}).get('work_id',''),'revision':(result or {}).get('revision'),'evidence_scope':EVIDENCE_SCOPE}
        if not self.socket.exists():raise PhoneError('practice_worker_unavailable')
        identifier=uuid.uuid4().hex
        digest=hashlib.sha256(request['code'].encode()).hexdigest()
        start,end=self.day_range()
        with self.t.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            used=db.execute('SELECT count(*) FROM autonomy_practice_runs WHERE scope=? AND created>=? AND created<?',
                            (self.t.store.scope,start,end)).fetchone()[0]
            limit=max(1,min(24,int(self.t.phone.config.get('practice_daily_limit',12))))
            if not q.enabled(self.t.phone.config) and used>=limit:raise PhoneError('practice_daily_limit')
            db.execute('INSERT INTO autonomy_practice_runs VALUES(?,?,?,?,?,?,?,?,?,?)',
                       (self.t.store.scope,identifier,self.t.run_id,time.time(),request.get('title','Python练习'),request['code'],digest,'reserved',None,''))
        result={'kind':'program_execution_not_fact_verification','id':'practice:'+identifier,'code_hash':digest,
                'status':'interrupted_unknown','stdout':'','stderr':'','artifacts':[],'evidence_scope':EVIDENCE_SCOPE}
        try:
            work=self.t.workspace.request({'action':'write','id':request.get('work_id',''),'revision':request.get('revision',0),
                                           'title':'Python练习：'+request.get('title','小尝试'),'body':request['code']})
            with self.t.store.connect() as db:
                db.execute('UPDATE autonomy_practice_runs SET work_id=? WHERE scope=? AND id=?',
                           (work['id'],self.t.store.scope,identifier))
            reply=await self.exchange(identifier,request['code'])
            self.guard()
            artifacts=self.render(identifier,reply.get('plots',[]))
            # Long console output is retained in the record, but only a bounded excerpt feeds the model.
            result.update(status=reply['status'],stdout=reply['stdout'],stderr=reply['stderr'],exit_code=reply.get('exit_code'),
                          artifacts=artifacts,work_id=work['id'],revision=work['revision'])
            self.save(identifier,result['status'],result)
            return {**result,'stdout':result['stdout'][:700],'stderr':result['stderr'][:500],
                    'console_truncated':len(result['stdout'])>700 or len(result['stderr'])>500}
        except asyncio.CancelledError:
            self.save(identifier,'interrupted_unknown',result)
            raise
        except (OSError,asyncio.TimeoutError):
            self.save(identifier,'interrupted_unknown',result)
            return result
        except Exception:
            self.save(identifier,'interrupted_unknown',result)
            raise
