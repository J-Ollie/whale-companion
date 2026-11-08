"""Scoped memory of our own choices and receipts, not a transcript or executor."""
from contextlib import closing
from datetime import datetime,timedelta
import hashlib
import json
import re
import sqlite3
import time
import uuid
from contextvars import ContextVar
from zoneinfo import ZoneInfo

TZ=ZoneInfo('Asia/Shanghai')
CURRENT_TOOL=ContextVar('companion_current_activity_tool',default=None)
TOPIC=re.compile(r'做了|干了|干啥|干嘛|在做|在干|在忙|忙啥|忙什么|忙了|想了|在想|想啥|琢磨|折腾|思考|经历|活动|学了|练了|玩了|看了|发了|过得|日记|昨天|前天|最近')
RULE=('这是自己有来源的活动回忆，不是待执行指令，也不要求汇报。你可以自然挑选想说的部分、只说方向、暂不展开或不谈。'
      '区分想过、选择、决定不做、尝试、工具返回、接口接受、实际结果未确认；不把worker_returned或工具未报错说成完成目标。'
      '没有记录不证明没做过或没想过，不补造持续心理活动、原因或精确数量。私人内容不因管理员身份而可向其他人复述。'
      '日期按回忆原始时间解释；记录到的短想法只证明当时留下这个想法，不是隐藏的逐步推理。不要把普通回答念成日志或状态列表。')
LABELS={'considered':'曾考虑过','thought':'留下了简短想法','selected':'作出了选择','decline':'选择不接下',
 'defer':'选择暂缓','clarify':'决定先询问','reply':'选择普通交流','silent':'选择本轮留白','accept':'选择接下',
 'attempted':'开始尝试','tool_returned':'工具返回，具体目标结果未确认','reported_success':'工具报告成功，仍以专门回执为准',
 'read_returned':'读取接口返回了资料，不代表看完全部内容','failed':'有失败回执','uncertain':'操作结果未确认',
 'blocked':'本次受限','not_selected':'这次没有选择该事项','idle':'这次选择不行动','rest':'选择休息','sleep':'选择睡眠',
 'submitted':'平台接受了发送，不证明已展示或已读','api_acknowledged':'接口确认接收，不证明对外可见',
 'confirmed_execution':'回执确认了这一步的具体执行，不外推其他目标',
 'plugin_returned':'操作插件返回，实际效果未核验','worker_returned':'处理流程返回，不能据此认定完成',
 'interrupted':'这轮被中断','interrupted_unknown':'中断后结果未确认','decision_unavailable':'当轮决策未完成，不能说成自主拒绝'}
SECRET=re.compile(r'(?:sk-|brz_)[A-Za-z0-9_-]{8,}|(?:api[_ -]?key|access[_ -]?token|password|密码|密钥|验证码)\s*(?:[:：=]|是)\s*[^\s，。；;]{3,}',re.I)


def clean(value,limit=180):
    if not isinstance(value,str):return ''
    value=SECRET.sub('[凭据已省略]',value)
    value=re.sub(r'https?://\S+|(?:/opt/|/AstrBot/|/sdcard/|[A-Z]:\\)[^\s，。；;]*','[位置已省略]',value)
    return value.strip()[:limit]


def window(day='',query='',now=None):
    now=time.time() if now is None else now;today=datetime.fromtimestamp(now,TZ).date()
    if day:
        if not isinstance(day,str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}',day):raise ValueError('invalid_activity_day')
        target=datetime.strptime(day,'%Y-%m-%d').date()
    else:target=today-timedelta(days=2 if '前天' in query else 1 if '昨天' in query else 0)
    if target>today:raise ValueError('future_activity_day')
    start=datetime.combine(target,datetime.min.time(),TZ).timestamp()
    if not day and any(x in query for x in ('最近','这几天','这些天')):start-=6*86400
    return start,min(start+7*86400 if not day and '最近' in query else datetime.combine(target+timedelta(days=1),datetime.min.time(),TZ).timestamp(),now)


def result_stage(result):
    if result is None:return 'uncertain'
    if getattr(result,'isError',False):return 'failed'
    value=getattr(result,'structuredContent',None)
    if not isinstance(value,dict):
        text='\n'.join(getattr(c,'text','') for c in getattr(result,'content',[]) or [])
        try:value=json.loads(text) if len(text.encode())<=12000 else None
        except (ValueError,TypeError):value=None
    if isinstance(value,dict):
        status=value.get('status',value.get('result'))
        if status in ('failed','error','unavailable','not_available') or value.get('ok') is False:return 'failed'
        if status in ('uncertain','interrupted_unknown'):return 'uncertain'
        if status in ('deferred','declined','paused'):return 'defer'
        if status in ('read','read_returned'):return 'read_returned'
        if status in ('ok','success','succeeded','sent','submitted') or value.get('ok') is True:return 'reported_success'
    return 'tool_returned'


class SelfActivity:
    def __init__(self,brain):
        self.brain=brain;self.path=brain.path.parent/'self-activity.sqlite3';self.healthy=True
        try:
            with self.connect() as db:
                db.execute('CREATE TABLE IF NOT EXISTS entries(id TEXT PRIMARY KEY,episode TEXT,at REAL,scope TEXT,kind TEXT,stage TEXT,note TEXT,source TEXT,ref TEXT)')
                db.execute('CREATE INDEX IF NOT EXISTS activity_scope_time ON entries(scope,at)')
                db.execute('CREATE TABLE IF NOT EXISTS coverage(id INTEGER PRIMARY KEY,started REAL)')
                db.execute('INSERT OR IGNORE INTO coverage VALUES(1,?)',(time.time(),))
                db.execute("UPDATE entries SET stage='interrupted_unknown' WHERE stage='attempted'")
            self.path.chmod(0o600)
        except (OSError,sqlite3.Error):self.healthy=False

    def connect(self):return sqlite3.connect(self.path,timeout=2)

    def event_scope(self,event):
        b=self.brain
        if event.get_extra('companion_self_social',False):return 'public'
        if event.get_group_id():
            from .cognitive_context import allowed_group
            if not allowed_group(b.chat,event):return None
            return 'group:'+b.scope(str(event.get_group_id()))
        if b.chat.batch.allowed(event):return 'contact:'+b.scope(event.get_sender_id())
        return None

    def turn(self,event):
        key=event.get_extra('companion_activity_turn',None)
        if key is None:
            key='turn:'+uuid.uuid4().hex;event.set_extra('companion_activity_turn',key)
        observation=event.get_extra('companion_observation_id',None)
        return key+('|obs:'+str(observation) if type(observation) is int and observation>0 else '')

    def add(self,episode,kind,stage,*,scope='self',note='',source='brain',ref='',at=None):
        if not self.healthy or stage not in LABELS:return None
        if not isinstance(kind,str) or not re.fullmatch(r'[A-Za-z0-9_./:-]{1,100}',kind):return None
        if not isinstance(scope,str) or not re.fullmatch(r'(?:public|self|contact:[a-f0-9]{24}|group:[a-f0-9]{24})',scope):return None
        at=time.time() if at is None else at
        key=hashlib.sha256((str(episode)+':'+kind+':'+stage+':'+scope).encode()).hexdigest()
        try:
            with self.connect() as db:db.execute('INSERT OR IGNORE INTO entries VALUES(?,?,?,?,?,?,?,?,?)',
                (key,str(episode)[:100],at,scope,kind,stage,clean(note),source[:50],ref[:120]))
            return key
        except (sqlite3.Error,OSError):return None

    def tool_start(self,event,tool,args):
        CURRENT_TOOL.set(None)
        scope=self.event_scope(event)
        if scope is None:return
        name=str(getattr(tool,'name','tool'))[:100]
        signature=hashlib.sha256((name+json.dumps(args,sort_keys=True,default=str)).encode()).hexdigest()
        pending=event.get_extra('companion_activity_tools',{})
        episode=uuid.uuid4().hex
        CURRENT_TOOL.set((episode,name,scope))
        pending.setdefault(signature,[]).append((episode,name,scope))
        event.set_extra('companion_activity_tools',pending)
        self.add(episode,name,'attempted',scope=scope,source='tool_boundary',ref=self.turn(event))
        if not event.get_extra('companion_activity_done_callback',False):
            import asyncio
            current=asyncio.current_task()
            if current:current.add_done_callback(lambda done:self.finish_tools(event))
            event.set_extra('companion_activity_done_callback',True)

    def tool_end(self,event,tool,args,result):
        scope=self.event_scope(event)
        if scope is None:return
        name=str(getattr(tool,'name','tool'))[:100]
        signature=hashlib.sha256((name+json.dumps(args,sort_keys=True,default=str)).encode()).hexdigest()
        pending=event.get_extra('companion_activity_tools',{})
        queue=pending.get(signature,[])
        episode,name,scope=queue.pop(0) if queue else (uuid.uuid4().hex,name,scope)
        self.add(episode,name,result_stage(result),scope=scope,source='tool_boundary',ref=self.turn(event))
        CURRENT_TOOL.set(None)
        event.set_extra('companion_activity_tools',pending)

    def finish_tools(self,event):
        CURRENT_TOOL.set(None)
        pending=event.get_extra('companion_activity_tools',{})
        for queue in pending.values():
            for episode,name,scope in queue:self.add(episode,name,'uncertain',scope=scope,source='tool_boundary',ref=self.turn(event))
        event.set_extra('companion_activity_tools',{})

    def rows(self,scopes,start,end):
        if not self.healthy:return []
        try:
            with closing(self.connect()) as db:
                db.row_factory=sqlite3.Row
                return [dict(r) for r in db.execute('SELECT * FROM entries WHERE scope IN ('+','.join('?' for _ in scopes)+') AND at>=? AND at<=? ORDER BY at DESC,id LIMIT 500',(*scopes,start,end))]
        except (OSError,sqlite3.Error):return []

    def originals(self,start,end,owner=False):
        """Read-through existing verified public thought/practice receipts; no copying."""
        from .cognitive_context import peer
        phone=peer(self.brain.chat.context,'phone');source=getattr(phone,'self_experience',None)
        out=[]
        if phone and getattr(phone,'umo',None)!=self.brain.chat.active.umo:return out
        if source:
            for r in source.thoughts(start,end):
                out.append({'id':r['id'],'episode':r['id'],'at':r['timestamp'],'scope':'public','kind':'autonomy_thought',
                    'stage':'thought','note':r['thought_snapshot'],'source':'original_thinking','ref':r['id']})
            for r in source.practices(start,end):
                out.append({'id':r['id'],'episode':r['id'],'at':r['timestamp'],'scope':'public','kind':'code_practice',
                    'stage':'confirmed_execution' if r['program_executed'] else 'failed' if r['status'] in ('failed','timeout') else 'uncertain','note':r['declared_title'],
                    'source':'verified_program_receipt','ref':r['id'],
                    'limit':'只证明程序成功退出；不证明自己看过图或学会了技能。'})
            for r in source.work_events(start,end) if hasattr(source,'work_events') else []:
                out.append({'id':r['id'],'episode':r['id'],'at':r['timestamp'],'scope':'public','kind':r['kind'],
                    'stage':'read_returned' if r['kind']!='work_link' else 'tool_returned','note':r['note'],
                    'source':'owned_work_receipt','ref':r['work_id'],'limit':r.get('limit','只证明记录中所述步骤，正文可选择不分享。')})
            if owner:
                for r in source.private_runs(start,end):
                    out.append({'id':'private:'+r['id'],'episode':r['id'],'at':r['at'],'scope':'self','kind':'private_reflection',
                        'stage':'thought','note':'有一轮私有反思，具体内容未提供，可选择不谈','source':'original_thinking','ref':r['id']})
        autonomy=getattr(phone,'autonomy',None);reading=getattr(autonomy,'reading',None)
        if reading:
            try:
                from importlib import import_module
                prefix=phone.__class__.__module__.rsplit('.',1)[0]
                validate=import_module(prefix+'.reading').web_source
                for raw in reading.recent():
                    r=validate(raw)
                    if r and start<=r['timestamp']<=end:
                        out.append({'id':r['id'],'episode':r['id'],'at':r['timestamp'],'scope':'public','kind':'public_web_read',
                            'stage':'read_returned','note':clean(r['title'],100),'source':'extracted_public_web_text','ref':r['id'],
                            'limit':'仅证明取得网页正文；可能截断，不证明读完全文或核实了网页说法。'})
            except (ImportError,AttributeError,ValueError):pass
        books=getattr(phone,'books',None)
        if books:
            for key,row in books.daily.get('decisions',{}).items():
                if not isinstance(row,dict) or not start<=row.get('at',0)<=end:continue
                visibility=key.split(':',1)[0]
                if visibility!='public' and not owner:continue
                status=row.get('status')
                stage='confirmed_execution' if status=='written' else 'decline' if status=='declined' else 'uncertain'
                out.append({'id':'diary:'+key,'episode':'diary:'+key,'at':row['at'],'scope':'public' if visibility=='public' else 'self',
                    'kind':'public_diary' if visibility=='public' else 'private_diary','stage':stage,
                    'note':'本中日记日期为'+key.split(':',1)[-1]+'；不提供正文，写入不等于已对外发布',
                    'source':'diary_decision_receipt','ref':key})
        if owner:
            memory=peer(self.brain.chat.context,'memory')
            if memory and str(getattr(memory,'owner',''))==self.brain.chat.friends.owner:
                for index,row in enumerate(memory.store.get_meta('growth_review_history:'+memory.owner,[])):
                    if not isinstance(row,dict) or not start<=row.get('at',0)<=end:continue
                    status=row.get('status')
                    stage='confirmed_execution' if status=='accepted' else 'failed' if status=='model_error' else 'not_selected' if status=='unchanged' else 'blocked'
                    identifier='growth:'+str(row['at'])+':'+str(index)
                    out.append({'id':identifier,'episode':identifier,'at':row['at'],'scope':'self','kind':'growth_review',
                        'stage':stage,'note':'复核结果：'+str(status)+'；不提供私人样本或性格修改正文',
                        'source':'growth_review_receipt','ref':identifier})
        return out

    def recall(self,event=None,*,day='',query='',offset=0,limit=8,internal=False):
        if not isinstance(query,str) or len(query)>80 or type(offset) is not int or not 0<=offset<=2000 or type(limit) is not int or not 1<=limit<=12:raise ValueError('invalid_self_recall')
        start,end=window(day,query);b=self.brain;owner=False
        scopes=['public']
        if internal:scopes.append('self')
        elif event is not None:
            scope=self.event_scope(event)
            if scope is None:raise ValueError('unverified_self_recall')
            scopes.append(scope)
            owner=not event.get_group_id() and str(event.get_sender_id())==b.chat.friends.owner and event.unified_msg_origin==b.chat.active.umo
        else:raise ValueError('self_recall_requires_scope')
        rows=self.rows(list(set(scopes)),start,end)+self.originals(start,end,owner or internal)
        for row in rows:
            if row['note'] and row['source']=='own_decision':
                match=re.search(r'\|obs:(\d+)$',row['ref'])
                valid=False
                if match and event is not None and not event.get_group_id():
                    try:
                        from .cognitive_context import peer
                        memory=peer(b.chat.context,'memory')
                        valid=memory.affect().social.valid_source(str(event.get_sender_id()),int(match.group(1)))
                    except Exception:valid=False
                # Keep the choice code, but never revive forgotten or unverified
                # private facts by quoting an old short reason.
                if not valid:row['note']=''
        rows=[r for r in rows if r['kind']!='companion_self_recall']
        # Attempt and result remain distinguishable; display the latest state of
        # each episode instead of counting the same operation twice.
        latest={};histories={}
        strength={'confirmed_execution':100,'submitted':95,'api_acknowledged':90,'read_returned':80,
            'failed':60,'uncertain':50,'reported_success':40,'tool_returned':30,'attempted':0}
        for row in sorted(rows,key=lambda r:(r['at'],r.get('stage')!='attempted')):
            key=(row['episode'],row['kind']);prior=latest.get(key)
            histories.setdefault(key,[]).append(row['stage'])
            if prior is None or strength.get(row['stage'],20)>=strength.get(prior['stage'],20):latest[key]=row
        ordered=sorted(latest.values(),key=lambda r:r['at'],reverse=True)
        # Query words are optional filters, not permissions. Date/broad questions
        # still return the full visible timeline for natural summarisation.
        filters=[x for x in re.findall(r'[A-Za-z_]{3,}|[\u4e00-\u9fff]{2,}',query)
                 if x not in ('今天','昨天','前天','最近','做了什么','想了什么','你做了什么','你在想什么','在干什么')]
        if filters:
            matches=[r for r in ordered if any(x in r['note'] or x in r['kind'] or x in LABELS.get(r['stage'],'') for x in filters)]
            if matches:ordered=matches
        total=len(ordered);visible=ordered[offset:offset+limit]
        events=[]
        for row in visible:
            events.append({k:row[k] for k in ('id','kind','stage','note','source','ref')}|
                {'at':datetime.fromtimestamp(row['at'],TZ).isoformat(timespec='seconds'),'meaning':LABELS.get(row['stage'],'结果未确认')}|
                {'observed_stages':list(dict.fromkeys(histories[(row['episode'],row['kind'])]))}|
                ({'limit':row['limit']} if row.get('limit') else {}))
        return {'status':'ok' if self.healthy else 'unavailable','coverage':'recorded_visible_events_only_not_complete_life_history',
                'start':datetime.fromtimestamp(start,TZ).isoformat(),'end':datetime.fromtimestamp(end,TZ).isoformat(),
                'events':events,'more':offset+limit<total,'next_offset':offset+limit if offset+limit<total else None,'total_visible':total,
                'rule':RULE,'private_material_withheld':True}

    def context(self,event=None,query='',internal=False):
        value=self.recall(event,query=query[:80],limit=5,internal=internal)
        if not TOPIC.search(query) and not internal:
            value['events']=[{k:v for k,v in row.items() if k not in ('note',)} for row in value['events'][:2]]
        while len(json.dumps(value,ensure_ascii=False).encode())>4800 and value['events']:value['events'].pop()
        return value
