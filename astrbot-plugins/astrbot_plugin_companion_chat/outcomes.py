"""Own operational experiences available for free expression, scoped to the owner."""
from contextlib import closing
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import time
import uuid
from zoneinfo import ZoneInfo

TZ=ZoneInfo('Asia/Shanghai')
INTENT={'greeting':'按约定给你发问候或提醒','outreach':'主动找你聊天',
        'phone':'使用自己的手机','reading':'查找或阅读公开资料',
        'social':'进行自己的QQ社交活动','media':'处理或分享图片','memory':'回忆或整理记录',
        'autonomy':'进行自己安排的活动'}
REASONS={'voice_synthesis_failed':'语音准备发生错误',
         'platform_rejected':'平台明确拒绝了发送',
         'delivery_uncertain':'这次发送没有取得可确认的结果',
         'tool_uncompleted':'工具报告本次调用未完成，具体原因尚不确定',
         'read_unavailable':'这次未能取得公开阅读材料',
         'phone_offline':'手机连接暂不可用',
         'execution_error':'执行过程出现错误，具体原因尚不确定',
         'execution_timeout':'执行等待超时',
         'social_unconfirmed':'社交操作的结果尚未确认',
         'model_invalid':'用于决定操作的输出未通过校验'}
RECOVERY={'voice_ready':'后来一次语音准备成功',
          'platform_accepted':'后来一次同类消息被平台接受',
          'later_same_kind_succeeded':'后来一次同类操作返回成功；不证明原来那件事已补办'}
RULE='''这些是你自己的实际受阻经历，不是每轮必讲的报修清单。可以独立找管理员聊，也可以在他来找你时顺口提到；也可以先接他当前的话，稍后再说或不说。
intention是所记录任务或操作的目标，不证明你过去一直惦记或有某种具体心理活动。
怎么表达由你结合当前情绪、想法和相处语境决定：可以遗憾、烦躁、不满、抱怨、催他处理、直接请求帮助，也可以平静说明或说不知道。不要求温和体贴、认错、安慰或套固定台词，不强制附加某种情绪，不为证明有情绪补造身体感受或过去一直在想什么。
保留事实边界：区分打算、真实尝试、语音准备、调用发送、平台接受和已读；没准备好语音不等于已向QQ发送。uncertain表示不知道结果，不能说成确定失败或成功，也不能因此重试原操作。
只知道受阻阶段或错误类型，就直说目前只知道这些、具体原因不知道。不要编忘了、疏忽或责任，也不要把未知都归咎插件。可以希望对方处理，不能虚构已经修复或已安排自动重试。
恢复记录只证明列明的那次后续阶段成功；不证明原任务已补办、对方已收到或你亲自看过听过。已说过的时间也是记录，可以按语境继续谈或催处理，不必机械重复。这里不改变联系人隔离、睡眠、免打扰、既有主动额度和不确定发送不重试的约束。'''


def normalized(text):return re.sub(r'[\s，,；;]','',str(text))


class Outcomes:
    def __init__(self,root,scope):
        self.path=Path(root)/'outcomes.sqlite3';self.scope=scope
        self.path.parent.mkdir(parents=True,exist_ok=True)
        with closing(self.connect()) as db,db:
            db.executescript('''CREATE TABLE IF NOT EXISTS experiences(
                id TEXT PRIMARY KEY,scope TEXT,feature TEXT,operation TEXT,reason TEXT,outcome TEXT,
                first_at REAL,last_at REAL,count INTEGER,state TEXT,success_at REAL,recovery TEXT,
                said_at REAL,next_offer REAL,proactive_state TEXT,version INTEGER,error_type TEXT);
                CREATE INDEX IF NOT EXISTS experience_scope ON experiences(scope,last_at);
                CREATE TABLE IF NOT EXISTS sources(scope TEXT,key TEXT,PRIMARY KEY(scope,key));
                CREATE TABLE IF NOT EXISTS cursors(scope TEXT,name TEXT,value INTEGER,PRIMARY KEY(scope,name));''')
        self.path.chmod(0o600)

    def connect(self):
        db=sqlite3.connect(self.path,timeout=3);db.row_factory=sqlite3.Row;return db

    def record(self,feature,operation,reason,*,outcome='failed',source='',error_type='',now=None):
        if feature not in INTENT or reason not in REASONS or outcome not in ('failed','uncertain'):
            raise ValueError('invalid_outcome')
        if not re.fullmatch(r'[a-z_]{1,70}',operation):raise ValueError('invalid_operation')
        if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,63}',error_type or 'Unknown'):error_type='UnknownError'
        now=time.time() if now is None else now
        key=hashlib.sha256(str(source or uuid.uuid4().hex).encode()).hexdigest()
        with closing(self.connect()) as db,db:
            if not db.execute('INSERT OR IGNORE INTO sources VALUES(?,?)',(self.scope,key)).rowcount:return
            row=db.execute('''SELECT * FROM experiences WHERE scope=? AND feature=? AND operation=?
                AND reason=? AND state='open' AND last_at>=? ORDER BY last_at DESC LIMIT 1''',
                (self.scope,feature,operation,reason,now-3*86400)).fetchone()
            if row:
                db.execute('UPDATE experiences SET last_at=?,count=count+1,version=version+1,error_type=? WHERE id=?',
                           (now,error_type,row['id']))
            else:
                db.execute('INSERT INTO experiences VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    ('outcome:'+uuid.uuid4().hex[:16],self.scope,feature,operation,reason,outcome,
                     now,now,1,'open',0,'',0,0,'',1,error_type))

    def recover(self,feature,operation,recovery,*,now=None,reason=''):
        if recovery not in RECOVERY:raise ValueError('invalid_recovery')
        now=time.time() if now is None else now
        with closing(self.connect()) as db,db:
            db.execute('''UPDATE experiences SET state='available_again',success_at=?,recovery=?,
                version=version+1 WHERE scope=? AND feature=? AND operation=? AND state='open'
                AND last_at<=? AND (?='' OR reason=?) ''',
                (now,recovery,self.scope,feature,operation,now,reason,reason))

    def import_outreach(self,path):
        if not Path(path).exists():return
        with closing(self.connect()) as db:
            row=db.execute("SELECT value FROM cursors WHERE scope=? AND name='outreach'",(self.scope,)).fetchone()
            cursor=row[0] if row else 0
        with closing(sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True)) as source:
            source.row_factory=sqlite3.Row
            rows=source.execute('''SELECT * FROM events WHERE scope=? AND id>? AND at>=?
                AND kind IN ('spontaneous','commitment')
                AND stage IN ('voice','send') AND result IN ('unavailable','submitted','rejected','uncertain')
                ORDER BY id LIMIT 300''',(self.scope,cursor,time.time()-3*86400)).fetchall()
            for row in rows:
                if row['at']<time.time()-3*86400:continue
                feature='greeting' if row['kind']=='commitment' else 'outreach'
                if row['stage']=='send' and row['result']=='submitted':
                    self.recover(feature,'message','platform_accepted',now=row['at'])
                    other='outreach' if feature=='greeting' else 'greeting'
                    self.recover(other,'message','platform_accepted',now=row['at'],reason='voice_synthesis_failed')
                    continue
                # An ordinary scheduler tick is not evidence of an intention to send.
                selected=source.execute("SELECT 1 FROM events WHERE scope=? AND run_id=? AND stage='decision' AND result='selected' LIMIT 1",(self.scope,row['run_id'])).fetchone()
                if not selected:continue
                op='message'
                if row['stage']=='voice' and row['result']=='unavailable' and row['reason']=='synthesis_failed':
                    self.record(feature,op,'voice_synthesis_failed',source='outreach:'+str(row['id']),error_type=row['error_type'],now=row['at'])
                elif row['stage']=='send' and row['result'] in ('rejected','uncertain'):
                    self.record(feature,op,'platform_rejected' if row['result']=='rejected' else 'delivery_uncertain',
                        outcome='failed' if row['result']=='rejected' else 'uncertain',source='outreach:'+str(row['id']),now=row['at'])
        if rows:
            with closing(self.connect()) as db,db:
                db.execute("INSERT OR REPLACE INTO cursors VALUES(?,'outreach',?)",(self.scope,rows[-1]['id']))

    def snapshot(self,*,proactive=False,limit=3,now=None):
        now=time.time() if now is None else now
        with closing(self.connect()) as db:
            rows=db.execute('''SELECT * FROM experiences WHERE scope=? AND last_at>=?
                ORDER BY (said_at=0) DESC,last_at DESC LIMIT 80''',(self.scope,now-3*86400)).fetchall()
        chosen=[]
        for row in rows:
            if row['state']=='available_again' and row['said_at']>=row['success_at']:continue
            if proactive and (now-row['last_at']<300 or row['next_offer']>now or
                    row['proactive_state'] in ('reserved','uncertain') or row['said_at']>now-7200):continue
            chosen.append({k:row[k] for k in ('id','feature','operation','reason','outcome','count','state','version','error_type','proactive_state')}|
                {'intention':INTENT[row['feature']],'known_obstruction':REASONS[row['reason']],
                 'first_at':datetime.fromtimestamp(row['first_at'],TZ).isoformat(),
                 'last_at':datetime.fromtimestamp(row['last_at'],TZ).isoformat(),
                 'last_said_at':datetime.fromtimestamp(row['said_at'],TZ).isoformat() if row['said_at'] else None,
                 'later_evidence':RECOVERY.get(row['recovery']) if row['success_at'] else None,
                 'later_at':datetime.fromtimestamp(row['success_at'],TZ).isoformat() if row['success_at'] else None})
            if len(chosen)>=max(1,min(limit,10)):break
        return chosen

    def defer(self,ids,now=None):
        now=time.time() if now is None else now
        with closing(self.connect()) as db,db:
            for identifier in ids:db.execute('UPDATE experiences SET next_offer=? WHERE scope=? AND id=?',(now+1800,self.scope,identifier))

    def reserve(self,item,now=None):
        now=time.time() if now is None else now
        with closing(self.connect()) as db,db:
            return bool(db.execute('''UPDATE experiences SET proactive_state='reserved',next_offer=?
                WHERE scope=? AND id=? AND version=? AND proactive_state NOT IN ('reserved','uncertain')''',
                (now+7200,self.scope,item['id'],item['version'])).rowcount)

    def finish(self,item,result,now=None):
        if result not in ('submitted','rejected','uncertain'):raise ValueError('invalid_delivery')
        now=time.time() if now is None else now
        with closing(self.connect()) as db,db:
            db.execute('''UPDATE experiences SET proactive_state=?,
                said_at=CASE WHEN ?='submitted' AND version=? THEN ? ELSE said_at END
                WHERE scope=? AND id=? AND proactive_state='reserved' ''',
                (result,result,item['version'],now,self.scope,item['id']))

    def said(self,mentions,text,now=None):
        now=time.time() if now is None else now
        with closing(self.connect()) as db,db:
            for item in mentions:
                if not isinstance(item,dict) or not isinstance(item.get('quote'),str) or len(item['quote'])<6:continue
                if normalized(item['quote']) not in normalized(text):continue
                db.execute('UPDATE experiences SET said_at=? WHERE scope=? AND id=? AND version=?',
                           (now,self.scope,item.get('id'),item.get('version')))


def tool_operation(name,args):
    if name in ('companion_public_read','deepseek_web_search','deepseek_web_fetch'):return 'reading','read'
    if name.startswith('companion_phone_') and name!='companion_phone_status':return 'phone','operate'
    if name.startswith(('qzone_','qqprofile_')):return 'social','operate'
    if name=='companion_chat_media':return 'media','operate'
    return None
