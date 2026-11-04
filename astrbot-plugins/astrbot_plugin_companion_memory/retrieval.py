"""Card-first recall with evidence-only archive fallback and bounded telemetry."""
import asyncio
import json
import re
import time

from .recall_index import weak_recall
from .budget import pack,TOOL_REPLY_BYTES
from .control import OVERVIEW

EXACT_QUOTE=re.compile(r'原话|原文|聊天记录|逐字|当时.{0,4}(怎么说|说了什么)|具体说了什么')


class LayeredRecall:
    def __init__(self,plugin,qq,event):
        self.p=plugin;self.qq=qq;self.event=event;self.c=plugin.cognition()
        self.trace=[];self.vectors=0;self.started=time.perf_counter()

    def check(self):
        if self.p.resting():raise ValueError('已进入睡眠休息，醒来后再回忆')

    async def run(self,query,start_day='',end_day='',limit=6,category='all'):
        if not isinstance(query,str):raise ValueError('回忆关键词应为文字')
        seen=self.event.get_extra('companion_recall_sent',[])
        raw_first=category=='conversation' or category=='all' and bool(EXACT_QUOTE.search(query))
        layers=['archive'] if raw_first else ['memory','archive'] if category=='all' else ['memory']
        historical=bool(start_day or end_day or re.search(r'过去|以前|上次|当时|之前|曾经|历史',query))
        options=dict(start_day=start_day,end_day=end_day,limit=limit,category=category)
        best=None;chosen=layers[0];searched={};already=False

        if category=='scenes' or category=='all' and not raw_first and OVERVIEW.search(query):
            self.check()
            directory=self.c.search(self.qq,query,start_day,end_day,limit,category='scenes',exclude=seen)
            self.trace.append({'layer':'scenes','stage':'source_checked_scenario','returned':len(directory['matches'])})
            if category=='scenes' or directory['matches'] and not weak_recall(query,directory['matches']):
                return self.finish(directory,'scenes')

        if category=='topics' or category=='all' and OVERVIEW.search(query):
            self.check()
            directory=self.c.search(self.qq,query,start_day,end_day,limit,category='topics',exclude=seen)
            self.trace.append({'layer':'topics','stage':'source_checked_directory','returned':len(directory['matches'])})
            if category=='topics' or directory['matches'] and not weak_recall(query,directory['matches']):
                return self.finish(directory,'topics')

        def lexical(layer,**extra):
            self.check()
            data=self.c.search(self.qq,query,exclude=seen,layer=layer,**options,**extra)
            self.trace.append({'layer':layer,'stage':'fts','returned':len(data['matches'])})
            return data

        async def vector(layer):
            semantic=getattr(self.p,'semantic',None)
            if not semantic or not query.strip():return []
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
                if previous['matches'] and not weak_recall(query,previous['matches']):
                    data['message']='相关条目本轮已经提供，请使用已有内容；需要更多依据可按编号读取。'
                    best=data;chosen=layer;already=True;break
            if query.strip() and weak_recall(query,data['matches']):
                refs=await vector(layer)
                if refs:data=lexical(layer,semantic_refs=refs)
            searched[layer]=refs
            if best is None or not best['matches'] or data['matches'] and not weak_recall(query,data['matches']):
                best=data;chosen=layer
            elif layer=='archive' and data['matches']:
                # Fusion scores across separate KB calls are not calibrated
                # confidence. Keep nonduplicate raw evidence when cards remain
                # weak, instead of treating the first vector hit as certain.
                cards=list(best['matches']);covered={r['id'] for r in cards}
                covered.update('obs:'+str(i) for r in cards for i in r.get('source_ids',[]))
                cards.extend(r for r in data['matches'] if r['id'] not in covered)
                best=pack({'scope':'current_sender_only','category':category},[('matches',cards[:limit])],TOOL_REPLY_BYTES-350)
                best['message']='浅层匹配仍弱，附非重复原话候选供核验；候选不是已经确认的事实。';chosen='mixed'
            if not query.strip() or not weak_recall(query,data['matches']):break

        if not already and query.strip() and weak_recall(query,best['matches']):
            variants=await self.p.expand_query(self.event,query)
            self.check()
            if variants:
                for layer in layers:
                    data=lexical(layer,variants=variants,semantic_refs=searched.get(layer,()))
                    if data['matches']:
                        best=data;chosen=layer
                        # Do not let a later raw hit replace a verified card.
                        if layer=='memory' or not weak_recall(query,data['matches']):break

        return self.finish(best,chosen)

    def finish(self,best,chosen):
        self.check()
        best['retrieval']={'layer':{'memory':'memory_cards','archive':'conversation_archive','mixed':'memory_cards_with_archive','topics':'topic_overview','scenes':'scene_overview'}[chosen],
                           'archive_searched':any(t['layer']=='archive' for t in self.trace)}
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
