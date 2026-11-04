"""Scoped memory control: durable work board, source-checked topics and quality.

All summaries are projections of leaf evidence. They never grant tool authority,
prove an operation completed, or promote a private source to public knowledge.
"""
import hashlib
import json
import re
import sqlite3
import statistics
import time
from contextlib import closing
from datetime import datetime

from .budget import clip,dump,pack,TOOL_REPLY_BYTES
from .memory import clean,stamp,TZ
from .recall_index import query_tokens,tokens

TOPIC_ID=re.compile(r'bundle:[a-f0-9]{12}\Z')
RESUME=re.compile(r'继续|上次|刚才|做到|进展|约定|还没|任务|计划|哪一步|记得|接着')
OVERVIEW=re.compile(r'总结|梳理|来龙去脉|前因后果|整个经过|跨天|跨会话|几次|一路|怎么发展|主题目录')
APPS={'com.coolapk.market':'酷安','tv.danmaku.bili':'哔哩哔哩','com.baidu.tieba':'百度贴吧','mark.via':'浏览器','com.ss.android.ugc.aweme':'抖音'}
FORGET=re.compile(r'^(?:请|麻烦你|帮我|我想让你|我希望你)?\s*(?:忘记|忘掉|删除.{0,4}记忆|不再记住).+',re.S)


def explicit_forget(text):
    return isinstance(text,str) and bool(FORGET.fullmatch(text.strip())) and not re.search(r'不要忘|别忘|不能忘|不要删|别删',text)


def digest(value):return hashlib.sha256(dump(value).encode()).hexdigest()


class MemoryControl:
    def __init__(self,store):
        self.store=store
        store.db.executescript('''
          CREATE TABLE IF NOT EXISTS memory_bundle_state(qq TEXT,id TEXT,signature TEXT,first_at REAL,last_at REAL,days INTEGER,PRIMARY KEY(qq,id));
          CREATE VIRTUAL TABLE IF NOT EXISTS memory_topics_fts USING fts5(qq UNINDEXED,ref UNINDEXED,words);
          CREATE TABLE IF NOT EXISTS memory_working(qq TEXT PRIMARY KEY,snapshot TEXT,signature TEXT,updated REAL);
          CREATE TABLE IF NOT EXISTS memory_quality(route_id INTEGER PRIMARY KEY,qq TEXT,at REAL,refs TEXT,returned INTEGER,delivered INTEGER DEFAULT 0);
          CREATE TABLE IF NOT EXISTS memory_feedback(qq TEXT,source_id INTEGER,route_id INTEGER,kind TEXT,at REAL,PRIMARY KEY(qq,source_id));
          CREATE TABLE IF NOT EXISTS memory_mutations(id INTEGER PRIMARY KEY,qq TEXT,at REAL,target TEXT,source_hash TEXT,operation TEXT);
        ''')
        store.db.commit()
        from .scenes import SceneStore
        self.scenes=SceneStore(store,self)

    def source_signature(self,qq,refs):
        """Recheck current leaves, entry versions and visibility on every read."""
        leaves=[];sources={}
        blocked={r[0] for r in self.store.db.execute('SELECT observation_id FROM suppressed_sources WHERE qq=?',(qq,))}
        for ref in refs:
            row=self.store.get_entry(qq,ref)
            if not row or row['kind']!='episode' or not self.store.lifecycle.current(row) or row.get('expires') and row['expires']<=time.time():return None
            ids=json.loads(row['evidence'])
            if not ids or set(ids)&blocked:return None
            leaves.append([ref,row['text'],row['evidence'],row['updated'],self.store.lifecycle.describe(row)])
            for sid in ids:
                if type(sid) is not int:return None
                source=self.store.db.execute('SELECT id,role,text,at FROM observations WHERE qq=? AND id=?',(qq,sid)).fetchone()
                if not source or source['role'] not in ('user','assistant'):return None
                sources[sid]=dict(source)
        return digest([leaves,[sources[i] for i in sorted(sources)]]),sources

    def consolidate(self,qq):
        groups={}
        for row in self.store.entries(qq,'episode',limit=100000):
            topic=clean(row['text'].split('：',1)[0],64)
            if topic in ('共同讨论','对方原话','你在对话中说过','日常','聊天','计划','近况','工作','学习','讨论','最近','recent') or len(topic)<2:continue
            groups.setdefault(topic,[]).append(row)
        keep=set()
        with self.store.atomic():
            for topic,rows in groups.items():
                # Preserve anchors from both ends; do not fold summaries into summaries.
                if len(rows)<2:continue
                rows=sorted(rows,key=lambda r:(r['updated'],r['id']))
                chosen=rows if len(rows)<=6 else rows[:2]+rows[-4:]
                refs=[r['id'] for r in chosen];checked=self.source_signature(qq,refs)
                if not checked:continue
                signature,sources=checked
                if len(sources)<2:continue
                selected=sorted(sources.values(),key=lambda s:(s['at'] or 0,s['id']))
                if len(selected)>8:selected=selected[:2]+selected[-6:]
                ids=[s['id'] for s in selected]
                dates={datetime.fromtimestamp(s['at'],TZ).date().isoformat() for s in selected if s['at']}
                times=[s['at'] for s in selected if s['at']]
                text='\n'.join(stamp(s['at'])+' · '+('对方原话' if s['role']=='user' else '助手说过，不证明执行')+' · obs:'+str(s['id'])+'\n'+clip(clean(s['text'],None),240) for s in selected)
                identifier='bundle:'+hashlib.sha256(topic.encode()).hexdigest()[:12];keep.add(identifier)
                fingerprint=digest([refs,ids,text,signature])
                old=self.store.db.execute('SELECT fingerprint FROM memory_compendia WHERE qq=? AND id=?',(qq,identifier)).fetchone()
                if old and old[0]==fingerprint:continue
                self.store.db.execute('INSERT OR REPLACE INTO memory_compendia VALUES(?,?,?,?,?,?,?,?)',(qq,identifier,topic,text,json.dumps(ids),json.dumps(refs),fingerprint,time.time()))
                self.store.db.execute('INSERT OR REPLACE INTO memory_bundle_state VALUES(?,?,?,?,?,?)',(qq,identifier,signature,min(times) if times else None,max(times) if times else None,len(dates)))
                self.store.db.execute('DELETE FROM memory_links WHERE qq=? AND parent=?',(qq,identifier))
                for ref in refs:self.store.db.execute('INSERT INTO memory_links VALUES(?,?,?,?)',(qq,identifier,ref,'quote_bundle'))
                self.store.db.execute('DELETE FROM memory_topics_fts WHERE qq=? AND ref=?',(qq,identifier))
                self.store.db.execute('INSERT INTO memory_topics_fts VALUES(?,?,?)',(qq,identifier,' '.join(sorted(tokens(topic+' '+text)))))
            for row in self.store.db.execute('SELECT id FROM memory_compendia WHERE qq=?',(qq,)).fetchall():
                if row[0] in keep:continue
                self.store.db.execute('DELETE FROM memory_compendia WHERE qq=? AND id=?',(qq,row[0]))
                self.store.db.execute('DELETE FROM memory_bundle_state WHERE qq=? AND id=?',(qq,row[0]))
                self.store.db.execute('DELETE FROM memory_topics_fts WHERE qq=? AND ref=?',(qq,row[0]))
                self.store.db.execute('DELETE FROM memory_links WHERE qq=? AND parent=?',(qq,row[0]))
            self.store.db.execute('DELETE FROM memory_bundle_state WHERE qq=? AND id NOT IN (SELECT id FROM memory_compendia WHERE qq=?)',(qq,qq))
            self.store.db.execute('DELETE FROM memory_topics_fts WHERE qq=? AND ref NOT IN (SELECT id FROM memory_compendia WHERE qq=?)',(qq,qq))

    def bundle(self,qq,identifier):
        if not isinstance(identifier,str) or not TOPIC_ID.fullmatch(identifier):return None
        row=self.store.db.execute('SELECT * FROM memory_compendia WHERE qq=? AND id=?',(qq,identifier)).fetchone()
        state=self.store.db.execute('SELECT * FROM memory_bundle_state WHERE qq=? AND id=?',(qq,identifier)).fetchone()
        if not row or not state:return None
        checked=self.source_signature(qq,json.loads(row['refs']))
        if not checked or checked[0]!=state['signature']:return None
        return {**dict(row),'first_at':state['first_at'],'last_at':state['last_at'],'days':state['days']}

    def topics(self,qq,query='',limit=4,start=None,end=None,exclude=()):
        wanted=sorted(query_tokens(query))[:24]
        if wanted:
            match=' OR '.join('"'+t+'"' for t in wanted)
            rows=self.store.db.execute('SELECT ref FROM memory_topics_fts WHERE memory_topics_fts MATCH ? AND qq=? ORDER BY bm25(memory_topics_fts) LIMIT 80',(match,qq)).fetchall()
            ids=[r[0] for r in rows]
        else:ids=[r[0] for r in self.store.db.execute('SELECT id FROM memory_compendia WHERE qq=? ORDER BY updated DESC',(qq,))]
        out=[]
        for identifier in ids:
            if identifier in exclude:continue
            row=self.bundle(qq,identifier)
            if not row:continue
            if start is not None and (row['last_at'] is None or row['last_at']<start):continue
            if end is not None and (row['first_at'] is None or row['first_at']>=end):continue
            out.append({'id':identifier,'kind':'quote_bundle','topic':row['topic'],
                'text':clip(row['text'],210),'days':row['days'],'first_at':stamp(row['first_at']),'last_at':stamp(row['last_at']),
                'source_ids':json.loads(row['evidence'])[:4],'entry_ids':json.loads(row['refs'])[:4],
                'note':'跨段原话索引，不是独立事实；按编号展开，不能把过去计划当成已完成'})
            if len(out)>=limit:break
        return out

    def working(self,qq,query=''):
        plans=[];topics=[]
        for hit in self.store.index.search(qq,[],16,kinds=('fact',),types=('plan',),browse=True,current=True):
            row=self.store.get_entry(qq,hit['ref'])
            if not row or not self.store.lifecycle.provenance_valid(row):continue
            plans.append({'id':row['id'],'text':clip(row['text'],150),'status':'awaiting_confirmation','source_ids':json.loads(row['evidence'])[:4]})
            if len(plans)>=8:break
        for row in self.store.entries(qq,'continuity',limit=3):
            if not self.store.lifecycle.provenance_valid(row):continue
            topics.append({'id':row['id'],'text':clip(row['text'],160),'source_ids':json.loads(row['evidence'])[:4]})
        latest=self.store.db.execute('SELECT MAX(id) FROM observations WHERE qq=?',(qq,)).fetchone()[0]
        snapshot={'observation_cursor':latest or 0,'plans':plans,'topics':topics}
        signature=digest(snapshot)
        old=self.store.db.execute('SELECT signature FROM memory_working WHERE qq=?',(qq,)).fetchone()
        if not old or old[0]!=signature:
            with self.store.atomic():self.store.db.execute('INSERT OR REPLACE INTO memory_working VALUES(?,?,?,?)',(qq,dump(snapshot),signature,time.time()))
        # Restore from current source truth, never trust an old serialized checkpoint.
        wanted=query_tokens(query);resume=not query.strip() or bool(RESUME.search(query))
        selected=[{**r,'kind':'pending_plan'} for r in plans if resume or wanted&tokens(r['text'])]
        selected+=[{**r,'kind':'open_topic'} for r in topics if resume or wanted&tokens(r['text'])]
        for row in selected:row['note']='持久化交接线索；说过或约好不证明正在执行，也不代表已安排提醒'
        return selected[:4]

    def route(self,qq,route_id,refs):
        with self.store.atomic():
            self.store.db.execute('INSERT OR REPLACE INTO memory_quality(route_id,qq,at,refs,returned) VALUES(?,?,?,?,?)',(route_id,qq,time.time(),json.dumps(refs),len(refs)))
            self.store.db.execute('DELETE FROM memory_quality WHERE route_id NOT IN (SELECT route_id FROM recall_routes)')

    def delivered(self,qq,route_ids):
        with self.store.atomic():
            for route in route_ids[:4]:self.store.db.execute('UPDATE memory_quality SET delivered=1 WHERE qq=? AND route_id=?',(qq,route))

    def feedback(self,qq):
        after=self.store.get_meta('memory_feedback_cursor:'+qq,0)
        rows=self.store.db.execute("SELECT id,text,at FROM observations WHERE qq=? AND role='user' AND id>? ORDER BY id LIMIT 200",(qq,after)).fetchall()
        with self.store.atomic():
            for row in rows:
                label=('reported_wrong' if re.search(r'记错了|记忆.{0,4}(错了|不对)|把别人的.{0,8}(记|当).{0,5}我',row['text']) else
                       'reported_miss' if re.search(r'没记住|又忘了我|我之前.{0,4}(说过|告诉过)',row['text']) else
                       'confirmed_recall' if re.search(r'这次.{0,4}记对了|你终于想起来了',row['text']) else None)
                if not label or not row['at']:continue
                route=self.store.db.execute('SELECT route_id FROM memory_quality WHERE qq=? AND delivered=1 AND at<=? AND at>=? ORDER BY at DESC LIMIT 1',(qq,row['at'],row['at']-600)).fetchone()
                if route:self.store.db.execute('INSERT OR IGNORE INTO memory_feedback VALUES(?,?,?,?,?)',(qq,row['id'],route[0],label,row['at']))
            if rows:self.store.set_meta('memory_feedback_cursor:'+qq,rows[-1]['id'])

    def quality(self,qq):
        since=time.time()-7*86400
        names={r[0] for r in self.store.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        routes=self.store.db.execute('SELECT elapsed_ms,layer,vector_attempts FROM recall_routes WHERE qq=? AND at>=?',(qq,since)).fetchall() if 'recall_routes' in names else []
        amounts=self.store.db.execute('SELECT COUNT(*),COALESCE(SUM(returned=0),0),COALESCE(SUM(delivered),0) FROM memory_quality WHERE qq=? AND at>=?',(qq,since)).fetchone()
        feedback={r[0]:r[1] for r in self.store.db.execute('''SELECT f.kind,COUNT(*) FROM memory_feedback f JOIN observations o ON o.qq=f.qq AND o.id=f.source_id
            WHERE f.qq=? AND f.at>=? AND NOT EXISTS(SELECT 1 FROM suppressed_sources s WHERE s.qq=f.qq AND s.observation_id=f.source_id) GROUP BY f.kind''',(qq,since))}
        times=sorted(r['elapsed_ms'] for r in routes)
        diagnostic=None
        path=self.store.folder/'control-selfcheck.json'
        if path.exists():
            try:
                report=json.loads(path.read_text(encoding='utf-8'))
                diagnostic={k:report.get(k) for k in ('passed','total','at','code_version')}
            except (ValueError,OSError):pass
        return {'window_days':7,'synthetic_selfcheck':diagnostic,'retrieval_calls':len(routes),'median_ms':round(statistics.median(times),3) if times else None,
            'p95_ms':round(times[int((len(times)-1)*.95)],3) if times else None,'vector_attempts':sum(r['vector_attempts'] for r in routes),
            'no_match_calls':amounts[1],'delivered_reply_calls':amounts[2],'user_feedback':feedback,
            'note':'这是运行数据与用户报告，不是客观准确率；检索成功不证明回答正确或模型已使用资料'}

    def procedures(self,qq,owner,query='',identifier=''):
        if str(qq)!=str(owner):raise ValueError('操作经验仅供原获准管理员私聊读取，不开放其他人的手机资料')
        path=self.store.folder.parent/'companion_phone/skills.sqlite3'
        if not path.exists() or path.is_symlink():return []
        out=[]
        with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True,timeout=2)) as db:
            db.row_factory=sqlite3.Row
            for row in db.execute('SELECT id,package,state,updated FROM recipes ORDER BY updated DESC LIMIT 200'):
                if row['package'] not in APPS or row['state'] not in ('active','candidate','suspended'):continue
                ref='procedure:'+row['id']
                if identifier and ref!=identifier:continue
                proofs=db.execute('SELECT session,day,result FROM evidence WHERE recipe=? ORDER BY at DESC LIMIT 20',(row['id'],)).fetchall()
                current=[]
                for proof in proofs:
                    if proof['result']!='focused_search_editor':break
                    current.append(proof)
                sessions=len({p['session'] for p in current});days=len({p['day'] for p in current})
                state=row['state']
                if proofs and proofs[0]['result']!='focused_search_editor':state='suspended'
                elif state=='active' and (sessions<3 or days<2):state='candidate'
                text=APPS[row['package']]+'打开搜索输入框的既有经验；'+str(sessions)+'次独立会话、'+str(days)+'天的连续成功后置条件。'
                if query.strip() and not query_tokens(query)&tokens(text+'操作经验搜索流程'):continue
                out.append({'id':ref,'kind':'procedural','text':text,'status':state,'updated':stamp(row['updated']),
                    'note':'只证明历史上出现聚焦搜索框；当前现场仍须核验，不代表已搜索/找到答案；失败应暂停复用，不能扩大授权'})
                if len(out)>=4:break
        return out

    def refresh(self,qq):
        self.consolidate(qq);self.scenes.refresh(qq);self.working(qq);self.feedback(qq)

    def forget(self,qq,identifier,direct_text,intent_quote):
        if not isinstance(identifier,str) or not re.fullmatch(r'[a-f0-9]{8,32}',identifier):raise ValueError('只接受当前联系人检索得到的长期记忆编号')
        if not explicit_forget(direct_text) or not isinstance(intent_quote,str) or intent_quote not in direct_text or not explicit_forget(intent_quote):
            raise ValueError('当前本人原话没有明确要求忘记；否定、引用或历史请求不授权删除')
        row=self.store.get_entry(qq,identifier)
        if not row or row['kind'] not in ('fact','episode','continuity','impression'):raise ValueError('当前联系人没有可管理的该条记忆')
        target=query_tokens(intent_quote)-tokens('请忘记忘掉删除记忆不再记住这条那个内容记录')
        if identifier not in intent_quote and len(target & tokens(row['topic']+' '+row['text']))<2:raise ValueError('忘记对象还不明确，需要本人指出具体内容，不能按猜测删除')
        if not self.store.forget(qq,identifier):raise ValueError('没有执行遗忘，不能声称已经处理')
        with self.store.atomic():self.store.db.execute('INSERT INTO memory_mutations(qq,at,target,source_hash,operation) VALUES(?,?,?,?,?)',(qq,time.time(),identifier,digest(direct_text),'forget'))
        self.working(qq)
        return {'id':identifier,'status':'recall_suppressed','message':'已停止这条及其原始依据的记忆召回；派生索引随后清理，原聊天与独立备份仍保留。'}
