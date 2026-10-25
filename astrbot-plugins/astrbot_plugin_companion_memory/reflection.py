"""Bounded incremental extraction. Partial long messages keep a durable offset."""
from .budget import size,dump,clip,pack,REFLECTION_BYTES
from .memory import stamp


def prepare(store,qq,cursor,suffix,now,system):
    pending=store.messages(qq,after=cursor,limit=24)
    if not pending:return None
    payload={'北京时间':stamp(now),'已处理消息截止id':cursor,'成长功能':not bool(suffix),'消息':[]}
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
    # A small known earlier user anchor allows slow growth without all recent history.
    anchors=store.db.execute("SELECT * FROM observations WHERE qq=? AND role='user' AND id<=? AND at IS NOT NULL AND at<? AND at>? ORDER BY id DESC LIMIT 1",(qq,cursor,now-86400,now-14*86400)).fetchall()
    for raw in anchors:
        row=dict(raw);item={'id':row['id'],'role':row['role'],'时间':stamp(row['at']),'text':row['text'].encode('utf-8')[:300].decode('utf-8','ignore')}
        payload['消息'].insert(0,item)
        if size(system)+size(payload)>REFLECTION_BYTES-100:payload['消息'].pop(0)
        else:supplied.insert(0,{**row,'text':item['text']})
    return payload,supplied,new_cursor,new_offset
