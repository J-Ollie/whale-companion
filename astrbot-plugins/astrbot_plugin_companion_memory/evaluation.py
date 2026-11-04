"""Daily local regression gates. Synthetic temporary data, zero model or QQ I/O."""
import tempfile
import time
from pathlib import Path

VERSION='memory-scenes-1.12.0'


def run_selfcheck():
    from .memory import MemoryStore
    from .cognition import Cognition
    results=[]
    def check(name,condition):results.append({'case':name,'passed':bool(condition)})
    with tempfile.TemporaryDirectory(prefix='whale-memory-selfcheck-') as folder:
        store=MemoryStore(Path(folder)/'memory')
        try:
            c=Cognition(store);a='12345678';b='87654321'
            source=store.observe(a,'user','我不吃合成香菜',at=time.time()-120)
            fact=store.put_entry(a,'fact','饮食偏好','我不吃合成香菜',[source])
            check('same_person_fact',any(r['id']==fact for r in c.search(a,'合成香菜',category='facts')['matches']))
            check('cross_person_denied',not c.search(b,'合成香菜',category='facts')['matches'])
            rows=[]
            for i in range(2):
                sid=store.observe(a,'user','第'+str(i)+'次讨论合成海岛',at=time.time()-(i+1)*86400)
                rows.append(store.put_entry(a,'episode','event.'+str(i),'合成海岛：讨论旅行',[sid]))
            c.consolidate(a);topics=c.control.topics(a,'合成海岛')
            check('cross_day_topic',bool(topics and topics[0]['days']==2))
            scenes=c.control.scenes.search(a,'合成海岛')
            check('source_checked_scene',bool(scenes))
            check('scene_cross_person_denied',not c.control.scenes.search(b,'合成海岛'))
            if topics:
                store.forget(a,rows[0]);check('topic_forget_cascade',not c.control.bundle(a,topics[0]['id']))
            else:check('topic_forget_cascade',False)
            check('scene_forget_invalidates',bool(scenes) and c.control.scenes.get(a,scenes[0]['id']) is None)
            from .fusion import rrf
            fused=rrf([['a','b'],['c','b']],{'a','b','c'})
            check('rrf_shared_candidate',max(fused,key=fused.get)=='b')
            sid=store.observe(a,'user','我明天要讨论合成计划')
            plan=store.put_entry(a,'fact','计划','我明天要讨论合成计划',[sid])
            with store.db:store.db.execute("UPDATE memory_state SET status='cancelled' WHERE qq=? AND id=?",(a,plan))
            check('cancelled_not_pending',not any(r['id']==plan for r in c.control.working(a,'继续')))
            try:c.control.forget(a,fact,'不要忘记我不吃合成香菜','不要忘记我不吃合成香菜')
            except ValueError:blocked=True
            else:blocked=False
            check('negative_forget_denied',blocked and store.get_entry(a,fact) is not None)
            with store.db:store.db.execute('UPDATE observations SET text=? WHERE id=?',('合成来源已经更正',source))
            check('changed_source_invalidates_fact',not c.search(a,'合成香菜',category='facts',semantic_refs=[fact])['matches'])
            check('context_budget',len(c.render(a,'继续').encode())<=2600)
        finally:store.close()
    return {'code_version':VERSION,'at':time.time(),'total':len(results),'passed':sum(r['passed'] for r in results),
            'cases':results,'scope':'synthetic_regression_only','model_calls':0,'qq_messages':0}
