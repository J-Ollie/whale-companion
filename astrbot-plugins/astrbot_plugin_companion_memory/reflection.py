"""Bounded incremental extraction. Partial long messages keep a durable offset."""
from .budget import size,dump,clip,pack,REFLECTION_BYTES
from .memory import stamp


def prepare(store,qq,cursor,suffix,now,system,*,relationship=None):
    pending=store.messages(qq,after=cursor,limit=24)
    if not pending:return None
    payload={'北京时间':stamp(now),'已处理消息截止id':cursor,'成长功能':not bool(suffix),'消息':[]}
    if relationship is not None:
        payload['关系评估'] = relationship
    # Relevant existing facts, not fifty arbitrary facts on every background call.
    query=' '.join(r['text'][:80] for r in pending[:3])
    facts=store.retrieve(qq,query,limit=8)
    # Conflicted/past plans must be visible to the updater, although omitted from default chat.
    for hit in store.index.search(qq,[query],30):
        row=store.get_entry(qq,hit['ref'])
        if row and row['kind']=='fact' and row['id'] not in {r['id'] for r in facts}:
            facts.insert(0,row)
        if len(facts)>=12:break
    facts=facts[:8]
    payload['facts']=[{'id':r['id'],'key':clip(r['topic'],60),'原话':clip(r['text'],210),'pinned':bool(r['pinned'])} for r in facts]
    payload['facts']=pack({},[('facts',payload['facts'])],1800)['facts']
    topics=[]
    for hit in store.index.search(qq,[query],12,kinds=('episode',),current=True):
        row=store.get_entry(qq,hit['ref'])
        if not row:continue
        topic=row['text'].split('：',1)[0][:40]
        if topic not in topics and topic!='共同讨论':topics.append(topic)
        if len(topics)>=6:break
    payload['既有事件主题_仅同一对象事项才复用']=topics
    if store.db.execute("SELECT 1 FROM sqlite_master WHERE name='memory_scenes'").fetchone():
        rows=store.db.execute("SELECT id,title FROM memory_scenes WHERE qq=? AND status='active' ORDER BY updated DESC LIMIT 4",(qq,))
        payload['既有场景_仅同一对象事项可关联']=[{'id':r[0],'title':clip(r[1],70)} for r in rows]
    offset=store.get_meta('reflection_offset'+suffix,{'id':None,'offset':0})
    supplied=[];new_cursor=cursor;new_offset={'id':None,'offset':0}
    for row in pending:
        start=offset.get('offset',0) if offset.get('id')==row['id'] else 0
        text=row['text'][start:]
        chunk=text.encode('utf-8')[:2100].decode('utf-8','ignore')
        item={'id':row['id'],'role':row['role'],'时间':stamp(row['at']),'text':chunk}
        payload['消息'].append(item)
        if size(system)+size(payload)>REFLECTION_BYTES-100:
            payload['消息'].pop();break
        supplied.append({**row,'text':chunk})
        if len(chunk)<len(text):
            new_offset={'id':row['id'],'offset':start+len(chunk)};break
        new_cursor=row['id']
    if not supplied:return None
    clue=store.continuity.references.context(qq,supplied,cursor)
    if clue:
        payload['待理解原话_仅用于明确澄清']={'id':clue['id'],'role':'user','时间':stamp(clue['at']),'text':clue['text']}
        if size(system)+size(payload)<=REFLECTION_BYTES-100:supplied.append(clue)
        else:payload.pop('待理解原话_仅用于明确澄清')
    return payload,supplied,new_cursor,new_offset
