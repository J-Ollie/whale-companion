"""Card-first recall with evidence-only archive fallback and bounded telemetry."""
import asyncio
import json
import re
import time

from .recall_index import weak_recall
from .budget import pack,TOOL_REPLY_BYTES
from .control import OVERVIEW
from .query_plan import make_plan

EXACT_QUOTE=re.compile(r'原话|原文|聊天记录|逐字|当时.{0,4}(怎么说|说了什么)|具体说了什么')


class LayeredRecall:
    def __init__(self,plugin,qq,event,*,budget=None,audit=True,allow_compare=True,window=None):
        self.p=plugin;self.qq=qq;self.event=event;self.c=plugin.cognition()
        self.trace=[];self.vectors=0;self.started=time.perf_counter()
        self.budget=budget;self.audit=audit;self.allow_compare=allow_compare;self.window=window;self.plan=None

    def check(self):
        if self.p.resting():raise ValueError('已进入睡眠休息，醒来后再回忆')

    def topical(self):
        return getattr(self.p,'config',{}).get('recall_topical_confidence',True)

    def weak(self, query, matches):
        return weak_recall(query,matches,topical=self.topical(),named_side=self.budget is not None)

    async def run(self,query,start_day='',end_day='',limit=6,category='all'):
        if not isinstance(query,str):raise ValueError('回忆关键词应为文字')
        if len(query.strip())>120:raise ValueError('回忆关键词最多120字')
        planning=getattr(self.p,'config',{}).get('recall_query_planning',True)
        if planning:
            self.plan=make_plan(query,start_day,end_day,now=time.time())
            query,start_day,end_day=self.plan.query,self.plan.start_day,self.plan.end_day
            self.window=self.plan.window or self.window
            if self.allow_compare and self.plan.sides:
                return await self.compare(self.plan.sides,start_day,end_day,limit,category)
            if not query.strip() and (start_day or end_day) and category=='all':category='conversation'
        seen=self.event.get_extra('companion_recall_sent',[])
        raw_first=category=='conversation' or category=='all' and bool(EXACT_QUOTE.search(query))
        layers=['archive'] if raw_first else ['memory','archive'] if category=='all' else ['memory']
        historical=bool(start_day or end_day or re.search(r'过去|以前|上次|当时|之前|曾经|历史',query))
        options=dict(start_day=start_day,end_day=end_day,limit=limit,category=category)
        if planning:options.update(time_window=self.window,infer_relative=False)
        best=None;chosen=layers[0];searched={};already=False

        if category=='scenes' or category=='all' and not raw_first and OVERVIEW.search(query):
            self.check()
            directory=self.c.search(self.qq,query,start_day,end_day,limit,category='scenes',exclude=seen)
            self.trace.append({'layer':'scenes','stage':'source_checked_scenario','returned':len(directory['matches'])})
            if category=='scenes' or directory['matches'] and not self.weak(query,directory['matches']):
                return self.finish(directory,'scenes')

        if category=='topics' or category=='all' and OVERVIEW.search(query):
            self.check()
            directory=self.c.search(self.qq,query,start_day,end_day,limit,category='topics',exclude=seen)
            self.trace.append({'layer':'topics','stage':'source_checked_directory','returned':len(directory['matches'])})
            if category=='topics' or directory['matches'] and not self.weak(query,directory['matches']):
                return self.finish(directory,'topics')

        def lexical(layer,**extra):
            self.check()
            settings={**options,**extra}
            if self.plan and self.plan.variants:settings['variants']=tuple(settings.get('variants',()))+self.plan.variants
            data=self.c.search(self.qq,query,exclude=seen,layer=layer,**settings)
            self.trace.append({'layer':layer,'stage':'fts','returned':len(data['matches'])})
            return data

        async def vector(layer):
            semantic=getattr(self.p,'semantic',None)
            if not semantic or not query.strip():return []
            if self.budget is not None and (self.budget['vectors']>=2 or self.vectors>=1):
                self.trace.append({'layer':layer,'stage':'shared_vector_budget','returned':0});return []
            if self.budget is not None:self.budget['vectors']+=1
            self.check();self.vectors+=1
            try:
                refs=await semantic.recall(self.qq,query,historical=historical,layer=layer)
            except asyncio.CancelledError:raise
            except Exception:return []
            self.check();self.trace.append({'layer':layer,'stage':'vector','returned':len(refs)})
            return refs

        for layer in layers:
            data=lexical(layer);refs=[]
            if not data['matches'] and seen:
                previous=self.c.search(self.qq,query,layer=layer,**options)
                if previous['matches'] and not self.weak(query,previous['matches']):
                    data['message']='相关条目本轮已经提供，请使用已有内容；需要更多依据可按编号读取。'
                    best=data;chosen=layer;already=True;break
            if query.strip() and self.weak(query,data['matches']):
                refs=await vector(layer)
                if refs:data=lexical(layer,semantic_refs=refs)
            searched[layer]=refs
            strong=data['matches'] and not self.weak(query,data['matches'])
            if best is None or not best['matches'] or strong and not self.topical():
                best=data;chosen=layer
            elif layer=='archive' and data['matches']:
                # Fusion scores across separate KB calls are not calibrated
                # confidence. Keep nonduplicate raw evidence when cards remain
                # weak, instead of treating the first vector hit as certain.
                cards=list(best['matches']);covered={r['id'] for r in cards}
                covered.update('obs:'+str(i) for r in cards for i in r.get('source_ids',[]))
                originals=[r for r in data['matches'] if r['id'] not in covered]
                # A full weak card list must not crowd out every new original.
                # Reserve its first anchor, retain the card order, then fill any
                # remaining space. No score is treated as cross-layer confidence.
                if self.topical() and originals:
                    cards=[originals[0]]+cards+originals[1:]
                else:cards.extend(originals)
                best=pack({'scope':'current_sender_only','category':category},[('matches',cards[:limit])],TOOL_REPLY_BYTES-350)
                best['message']='浅层匹配仍弱，附非重复原话候选供核验；候选不是已经确认的事实。';chosen='mixed'
            elif strong:
                best=data;chosen=layer
            if not query.strip() or not self.weak(query,data['matches']):break

        if not already and query.strip() and self.weak(query,best['matches']):
            if self.budget is not None and self.budget['rewrites']>=1:variants=[]
            else:
                if self.budget is not None:self.budget['rewrites']+=1
                variants=await self.p.expand_query(self.event,query)
            self.check()
            if variants:
                for layer in layers:
                    data=lexical(layer,variants=variants,semantic_refs=searched.get(layer,()))
                    if data['matches']:
                        best=data;chosen=layer
                        # Do not let a later raw hit replace a verified card.
                        if layer=='memory' or not self.weak(query,data['matches']):break

        return self.finish(best,chosen)

    async def compare(self,sides,start_day,end_day,limit,category):
        if isinstance(limit,bool) or not isinstance(limit,int) or not 1<=limit<=8:raise ValueError('返回条数范围为1至8')
        budget={'vectors':0,'rewrites':0};parts=[]
        for side_index,side in enumerate(sides):
            self.check()
            child=LayeredRecall(self.p,self.qq,self.event,budget=budget,audit=False,allow_compare=False,window=self.window)
            data=await child.run(side,start_day,end_day,limit,category)
            parts.append(data['matches']);self.vectors+=child.vectors
            self.trace.extend({**row,'side':side_index} for row in child.trace)
        rows=[];seen=set()
        for i in range(max(map(len,parts),default=0)):
            for part in parts:
                if i>=len(part) or part[i]['id'] in seen:continue
                row=part[i];rows.append(row);seen.add(row['id'])
        result=pack({'scope':'current_sender_only','category':category,'comparison_candidates':[len(x) for x in parts]},
            [('matches',rows[:limit])],TOOL_REPLY_BYTES-350)
        result['message']='分别检索两侧并交错提供候选；候选数量不证明两侧均有充分依据，缺失一侧不能补造。'
        return self.finish(result,'comparison')

    def finish(self,best,chosen):
        self.check()
        best['retrieval']={'layer':{'memory':'memory_cards','archive':'conversation_archive','mixed':'memory_cards_with_archive','topics':'topic_overview','scenes':'scene_overview','comparison':'comparison_candidates'}[chosen],
                           'archive_searched':any(t['layer']=='archive' for t in self.trace)}
        if self.plan and (self.plan.start_day or self.plan.end_day):
            best['query_plan']={'time_scope':'message_recorded_at','start_day':self.plan.start_day,'end_day':self.plan.end_day,'part':self.plan.part}
        best=pack({k:v for k,v in best.items() if k!='matches'},[('matches',best['matches'])],TOOL_REPLY_BYTES)
        if not self.audit:return best
        with self.p.store.db:
            self.p.store.db.execute('CREATE TABLE IF NOT EXISTS recall_routes(id INTEGER PRIMARY KEY,qq TEXT,at REAL,layer TEXT,elapsed_ms REAL,vector_attempts INTEGER,stages TEXT)')
            identifier=self.p.store.db.execute('INSERT INTO recall_routes(qq,at,layer,elapsed_ms,vector_attempts,stages) VALUES(?,?,?,?,?,?)',
                (self.qq,time.time(),chosen,round((time.perf_counter()-self.started)*1000,3),self.vectors,json.dumps(self.trace)))
            self.c.control.route(self.qq,identifier.lastrowid,[r['id'] for r in best['matches']])
            self.p.store.db.execute('DELETE FROM recall_routes WHERE id NOT IN (SELECT id FROM recall_routes ORDER BY id DESC LIMIT 1000)')
        if hasattr(self.event,'set_extra'):
            old=self.event.get_extra('companion_memory_route_ids',[])
            self.event.set_extra('companion_memory_route_ids',(old+[identifier.lastrowid])[-4:])
        return best
