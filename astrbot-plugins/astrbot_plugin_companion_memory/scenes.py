"""Evidence-backed scenario cards: facts, pending work, changes and timeline.

Adapted in spirit from TencentDB-Agent-Memory L2, keeping private QQ scopes,
leaf provenance and the existing extraction budget. No model-authored fact text
is accepted here: the model may propose links, never replace source evidence.
"""
import hashlib
import json
import re
import time
from .budget import clip,dump
from .memory import stamp,SECRET
from .recall_index import query_tokens,tokens

SCENE=re.compile(r'scene:[a-f0-9]{12}\Z')
IMAGE=('生图','出图','绘图','画图','生成图片','图片生成')
STOP=tokens('用户 对方 管理员 小鲸鱼 目前 关于 问题 如何 讨论 今天 计划 方案 内容 对话 事情 任务 进行 能力')|{'f1nger'}


def keywords(text):
    result=query_tokens(text)-STOP
    if any(w in text for w in IMAGE):result.add('concept:image_generation')
    return result


def hashof(value):return hashlib.sha256(dump(value).encode()).hexdigest()


class SceneStore:
    def __init__(self,store,control):
        self.store=store;self.control=control
        store.db.executescript('''
          CREATE TABLE IF NOT EXISTS memory_scenes(qq TEXT,id TEXT,title TEXT,refs TEXT,body TEXT,signature TEXT,revision INTEGER,updated REAL,status TEXT,PRIMARY KEY(qq,id));
          CREATE TABLE IF NOT EXISTS memory_scene_versions(id INTEGER PRIMARY KEY,qq TEXT,scene_id TEXT,at REAL,previous TEXT);
          CREATE VIRTUAL TABLE IF NOT EXISTS memory_scenes_fts USING fts5(qq UNINDEXED,ref UNINDEXED,words);
          CREATE TABLE IF NOT EXISTS memory_scene_links(qq TEXT,scene_id TEXT,entry_id TEXT,PRIMARY KEY(qq,scene_id,entry_id));
        ''');store.db.commit()

    def member(self,qq,identifier):
        row=self.store.get_entry(qq,identifier)
        if not row or row['kind'] not in ('fact','episode','continuity'):return None
        if not self.store.lifecycle.provenance_valid(row):return None
        blocked={r[0] for r in self.store.db.execute('SELECT observation_id FROM suppressed_sources WHERE qq=?',(qq,))}
        ids=json.loads(row['evidence'])
        if set(ids)&blocked:return None
        sources=[]
        for sid in ids:
            source=self.store.db.execute('SELECT id,role,text,at FROM observations WHERE qq=? AND id=?',(qq,sid)).fetchone()
            if not source:return None
            sources.append(dict(source))
        return row,self.store.lifecycle.describe(row),sources

    def signature(self,qq,refs):
        members=[]
        for ref in refs:
            member=self.member(qq,ref)
            if not member:return None
            row,state,sources=member
            members.append([row,state,sources])
        return hashof(members) if members else None

    def accept_links(self,qq,proposals,supplied,cursor):
        """Existing/new scene assignment only, anchored to this extraction batch."""
        if not isinstance(proposals,list):return 0
        allowed={r['id']:r for r in supplied if r.get('qq')==qq};accepted=0
        for proposal in proposals[:3]:
            if not isinstance(proposal,dict):continue
            ids=proposal.get('source_ids',[])
            if not isinstance(ids,list) or not ids or any(type(s) is not int or s not in allowed for s in ids):continue
            if not any(s>cursor for s in ids):continue
            title=proposal.get('title','');identifier=proposal.get('id','')
            if not isinstance(identifier,str):continue
            if identifier:
                old=self.store.db.execute('SELECT title FROM memory_scenes WHERE qq=? AND id=?',(qq,identifier)).fetchone()
                if not old or not SCENE.fullmatch(identifier):continue
                title=old[0]
            elif isinstance(title,str) and 2<=len(title)<=48 and not SECRET.search(title) and len(keywords(title))>=2:
                identifier='scene:'+hashlib.sha256(title.encode()).hexdigest()[:12]
            else:continue
            # A semantic link is a navigation proposal, not permission to merge
            # unrelated entities, rewrite facts or change their audience.
            material=' '.join(allowed[s]['text'] for s in ids)
            if not keywords(title)&keywords(material):continue
            refs=[]
            for sid in ids[:6]:
                rows=self.store.db.execute("SELECT DISTINCT e.id FROM entries e JOIN json_each(e.evidence) j ON j.value=? WHERE e.qq=? AND e.kind IN ('fact','episode','continuity')",(sid,qq))
                refs.extend(r[0] for r in rows if self.member(qq,r[0]))
            if not refs:continue
            with self.store.atomic():
                self.store.db.execute("INSERT OR IGNORE INTO memory_scenes VALUES(?,?,?,?,?,?,?,?,?)",(qq,identifier,title,'[]','{}','',0,0,'pending'))
                for ref in dict.fromkeys(refs):self.store.db.execute('INSERT OR IGNORE INTO memory_scene_links VALUES(?,?,?)',(qq,identifier,ref))
            accepted+=1
        return accepted

    def build(self,qq,title,refs):
        facts=[];constraints=[];pending=[];changes=[];questions=[];sources={};live=[]
        for ref in refs[:32]:
            member=self.member(qq,ref)
            if not member:continue
            row,state,evidence=member;live.append(ref)
            item={'id':ref,'text':row['text'],'status':state['status'],'source_kind':state.get('source_kind','unknown'),'source_ids':json.loads(row['evidence'])[:4]}
            current=self.store.lifecycle.current(row) and not (row['expires'] and row['expires']<=time.time())
            if row['kind']=='fact':
                if current and state['type']=='semantic':
                    (constraints if re.search(r'禁止|不得|不要|不吃|不喜欢|不能',row['text']) else facts).append(item)
                elif current and state['type']=='plan':
                    item['status']='needs_review' if state.get('review_due') else 'awaiting_confirmation';pending.append(item)
                elif state['status']!='active' or not current:changes.append(item)
            for source in evidence:
                sources[source['id']]=source
                if source['role']=='user' and re.search(r'还没解决|仍不清楚|尚未确定|还没找到',source['text']):
                    questions.append({'id':'obs:'+str(source['id']),'text':clip(source['text'],180),'status':'user_reported_unresolved'})
        timeline=sorted(sources.values(),key=lambda s:(s['at'] or 0,s['id']))
        if len(timeline)>8:timeline=timeline[:2]+timeline[-6:]
        recent=timeline[-1] if timeline else None
        first=next((s for s in timeline if s['role']=='user'),None)
        summary='围绕“'+title+'”，当前有'+str(len(facts)+len(constraints))+'条有效明确说法、'+str(len(pending))+'项待确认计划，保留'+str(len(changes))+'项历史状态线索。'
        if recent:summary+='最近原话：'+clip(recent['text'],120)
        body={'kind':'scenario_card','topic':title,'summary':summary,
            'background':{'id':'obs:'+str(first['id']),'text':clip(first['text'],240)} if first else None,
            'current_facts':facts[:6],'constraints':constraints[:4],'pending':pending[:4],
            'changes':changes[-4:],'open_questions':questions[-3:],
            'timeline':[{'id':'obs:'+str(s['id']),'role':s['role'],'time':stamp(s['at']),'text':clip(s['text'],210)} for s in timeline],
            'entry_ids':live,'source_ids':[s['id'] for s in timeline],
            'note':'场景为有来源的结构化工作背景；明确说法仍是本人自述/人工确认，计划与助手原话不证明实际执行；不自动推断人格或对外公开。'}
        return body,live

    def refresh(self,qq):
        # Seed from source-validated topic bundles. Single isolated messages
        # stay atomic unless the extractor explicitly proposes a scene.
        for bundle in self.control.topics(qq,limit=100000):
            raw=self.control.bundle(qq,bundle['id'])
            identifier='scene:'+bundle['id'].split(':',1)[1]
            with self.store.atomic():
                self.store.db.execute('INSERT OR IGNORE INTO memory_scenes VALUES(?,?,?,?,?,?,?,?,?)',(qq,identifier,raw['topic'],'[]','{}','',0,0,'pending'))
                for ref in json.loads(raw['refs']):self.store.db.execute('INSERT OR IGNORE INTO memory_scene_links VALUES(?,?,?)',(qq,identifier,ref))
        for old in self.store.db.execute('SELECT * FROM memory_scenes WHERE qq=?',(qq,)).fetchall():
            title=old['title'];wanted=keywords(title)
            refs=[r[0] for r in self.store.db.execute('SELECT entry_id FROM memory_scene_links WHERE qq=? AND scene_id=?',(qq,old['id']))]
            for hit in self.store.index.search(qq,[title],32,kinds=('fact','continuity'),current=True):
                row=self.store.get_entry(qq,hit['ref'])
                if not row:continue
                common=wanted&keywords(row['topic']+' '+row['text'])
                if len(common)>=2 or 'concept:image_generation' in common:refs.append(row['id'])
            refs=sorted(set(refs),key=lambda ref:((self.store.get_entry(qq,ref) or {}).get('updated',0),ref))[-32:]
            body,live=self.build(qq,title,refs)
            signature=self.signature(qq,live)
            state='active' if live else 'withdrawn'
            encoded=dump(body)
            if old['signature']==signature and old['body']==encoded and old['status']==state:continue
            with self.store.atomic():
                if old['revision']:
                    self.store.db.execute('INSERT INTO memory_scene_versions(qq,scene_id,at,previous) VALUES(?,?,?,?)',(qq,old['id'],time.time(),dump(dict(old))))
                self.store.db.execute('UPDATE memory_scenes SET refs=?,body=?,signature=?,revision=?,updated=?,status=? WHERE qq=? AND id=?',(dump(live),encoded,signature,old['revision']+1,time.time(),state,qq,old['id']))
                self.store.db.execute('DELETE FROM memory_scene_links WHERE qq=? AND scene_id=?',(qq,old['id']))
                for ref in live:self.store.db.execute('INSERT INTO memory_scene_links VALUES(?,?,?)',(qq,old['id'],ref))
                self.store.db.execute('DELETE FROM memory_scenes_fts WHERE qq=? AND ref=?',(qq,old['id']))
                if state=='active':self.store.db.execute('INSERT INTO memory_scenes_fts VALUES(?,?,?)',(qq,old['id'],' '.join(sorted(tokens(title+' '+encoded)))))

    def get(self,qq,identifier):
        if not isinstance(identifier,str) or not SCENE.fullmatch(identifier):return None
        row=self.store.db.execute("SELECT * FROM memory_scenes WHERE qq=? AND id=? AND status='active'",(qq,identifier)).fetchone()
        if not row or self.signature(qq,json.loads(row['refs']))!=row['signature']:return None
        return {'id':identifier,**json.loads(row['body']),'revision':row['revision'],'updated':stamp(row['updated'])}

    def search(self,qq,query='',limit=4,exclude=(),start=None,end=None):
        wanted=sorted(query_tokens(query))[:24]
        if query.strip() and not wanted:return []
        if wanted:
            match=' OR '.join('"'+w+'"' for w in wanted)
            ids=[r[0] for r in self.store.db.execute('SELECT ref FROM memory_scenes_fts WHERE memory_scenes_fts MATCH ? AND qq=? ORDER BY bm25(memory_scenes_fts) LIMIT 80',(match,qq))]
        else:ids=[r[0] for r in self.store.db.execute("SELECT id FROM memory_scenes WHERE qq=? AND status='active' ORDER BY updated DESC",(qq,))]
        out=[]
        for identifier in ids:
            if identifier in exclude:continue
            card=self.get(qq,identifier)
            if not card:continue
            if query.strip() and not keywords(query)&keywords(card['topic']+' '+card['summary']):continue
            if start is not None or end is not None:
                times=[r[0] for sid in card['source_ids'] for r in self.store.db.execute('SELECT at FROM observations WHERE qq=? AND id=?',(qq,sid)) if r[0]]
                if not times or start is not None and max(times)<start or end is not None and min(times)>=end:continue
            out.append({'id':identifier,'kind':'scenario_card','topic':card['topic'],'text':clip(card['summary'],330),
                'revision':card['revision'],'entry_ids':card['entry_ids'][:4],'source_ids':card['source_ids'][:4],
                'note':'先看场景，再按编号分页核对当前说法、未决项与时间线；不是独立执行证明'})
            if len(out)>=limit:break
        return out

    def render(self,card):
        text=card['summary']+'\n\n'+card['note']+'\n'
        for key,title in [('background','背景'),('current_facts','当前明确说法'),('constraints','明确限制'),('pending','待确认计划'),('changes','历史变化'),('open_questions','待核对问题'),('timeline','原话时间线')]:
            values=card.get(key,[]) or [];values=[values] if isinstance(values,dict) else values
            if not values:continue
            text+='\n## '+title+'\n'
            for row in values:text+='\n'+row['id']+' · '+str(row.get('status',row.get('time','')))+'\n'+row['text']+'\n'
        return text
