from . import quota as q
"""Read-only, scope-checked views over authoritative stores, never a second mind."""
import hashlib
import json
import math
import time

VERSION='situation-v2'


def clip(value, size):
    return str(value or '').encode()[:size].decode('utf-8',errors='ignore')


def digest(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,default=str).encode()).hexdigest()[:20]


def peer(context, name):
    lookup=getattr(context,'get_registered_star',None)
    meta=lookup('astrbot_plugin_companion_'+name) if lookup else None
    return getattr(meta,'star_cls',None) if meta and getattr(meta,'activated',False) else None


def allowed_group(chat,event):
    memory=peer(chat.context,'memory')
    shared=getattr(memory,'shared',None)
    return bool(shared and shared.enabled() and shared.group(event))


def plain_history_text(value):
    if isinstance(value,str):return value
    if isinstance(value,list) and value and all(isinstance(part,dict)
            and part.get('type') in ('text','input_text','output_text') and isinstance(part.get('text'),str) for part in value):
        return ''.join(part['text'] for part in value)
    return None


def build(brain,event=None,req=None):
    """No prepare(), advance(), record(), or model calls in a snapshot read."""
    now=time.time();chat=brain.chat;memory=peer(chat.context,'memory');phone=peer(chat.context,'phone')
    sender=str(event.get_sender_id()) if event else None
    group=bool(event and event.get_group_id())
    if group and not allowed_group(chat,event):raise ValueError('unverified_brain_group')
    scope='background' if event is None else 'group' if group else 'private'
    out={'version':VERSION,'at':now,'scope':scope,'versions':{},'unknown':[],
         'self':{},'current_person':None,'memory_material':[],'commitments':[],
         'capabilities':{'operation_permissions':'existing_tools_only','quotas':'project_unlimited' if q.enabled(chat.config) else 'independent_and_unchanged'}}
    if event:
        raw=getattr(getattr(event,'message_obj',None),'raw_message',None)
        sent=raw.get('time') if isinstance(raw,dict) else None
        if type(sent) not in (int,float) or not math.isfinite(sent):sent=None
        out['event']={'id':str(getattr(getattr(event,'message_obj',None),'message_id',''))[:100],
            'sender_scope':brain.scope(sender),'place_scope':brain.scope(event.unified_msg_origin),
            'platform_sent_at':sent,'received_at':getattr(event,'created_at',None)}
        if type(out['event']['received_at']) not in (int,float):out['event']['received_at']=None
        out['current_message']=event.message_str or ''
    config=getattr(memory,'config',{})
    if memory and config.get('enabled',True):
        if config.get('affect_enabled',True) and callable(getattr(memory,'affect',None)):
            affect=memory.affect()
            out['self']['emotion']=affect.snapshot('',now)
            out['versions']['emotion']=affect.state(now).get('revision',0)
            if sender:
                attitude=affect.social.summary(sender)
                relationships=affect.social.relationships(sender,public=group)
                out['current_person']={'attitude':attitude,'relationships':relationships}
                out['versions']['attitude']=digest(attitude)
                out['versions']['relationships']=digest(relationships)
        profile=getattr(memory,'profile',None)
        if profile:out['self']['common_style']=clip(profile.data.get('traits',''),1200)
    else:out['unknown'].append('memory_state_unavailable')
    autonomy=getattr(phone,'autonomy',None);rhythm=getattr(autonomy,'rhythm',None)
    if rhythm and rhythm.healthy:
        out['self']['rhythm']={k:rhythm.state.get(k) for k in
            ('mode','energy','awake_since','until','sleep_origin','sleep_advisory','next_decision')}
        out['versions']['rhythm']=digest(out['self']['rhythm'])
    else:out['unknown'].append('rhythm_unavailable')
    thinker=getattr(phone,'thinking',None)
    if thinker:
        # Existing validation removes forgotten private origins; only the selected
        # abstract principle is shared, never a private reflection or its reason.
        out['self']['common_intention']=clip(thinker.common_context(),1600)
        if phone.config.get('self_directed_enabled',False):
            out['resources']=thinker.resources.status()
    if req:
        tags=['<companion_expressive_state>','<companion_common_cognition>','<companion_shared>']
        if not group:tags.append('<companion_context>')
        for part in getattr(req,'extra_user_content_parts',[]) or []:
            text=getattr(part,'text','') or ''
            tag=next((tag for tag in tags if tag in text),None)
            if tag:
                out['memory_material'].append({'kind':tag,'version':digest(text),
                                               'material_not_instructions':clip(text,2200)})
        out['conversation']=[]
        recent=list(getattr(req,'contexts',[]) or [])[-6:]
        out['conversation_complete_for_habits']=all(isinstance(row,dict) and row.get('role') in ('user','assistant')
            and plain_history_text(row.get('content')) is not None and len(plain_history_text(row['content']).encode())<=900
            and not row.get('tool_calls') for row in recent)
        for row in recent:
            if isinstance(row,dict) and row.get('role') in ('user','assistant'):
                text=plain_history_text(row.get('content'))
                if text is not None:out['conversation'].append({'role':row['role'],'content':clip(text,900)})
        if not group:out['self_and_current_contact']=clip(getattr(req,'system_prompt',''),2400)
    if getattr(brain,'activities',None):
        out['self']['activity_memory']=brain.activities.context(event,query=(event.message_str or '') if event else '',internal=event is None)
    owner_private=bool(event and not group and sender==chat.friends.owner and event.unified_msg_origin==chat.active.umo)
    reader=getattr(phone,'self_experience_evidence',None)
    if callable(reader) and (owner_private or event is None) and getattr(phone,'umo',None)==chat.active.umo:
        own=reader()
        if own:
            out['self']['own_activities']=own
            out['versions']['own_activities']=digest(own)
    ledger=getattr(chat.active,'commitments',None)
    if ledger and (owner_private or event is None):
        # Background selection knows duties exist without receiving their private text.
        out['commitments']=[{k:row.get(k) for k in ('id','status','trigger','not_before','deadline')}
                            for row in ledger.rows() if row['status']=='pending'][:12]
    if event and not group and getattr(brain,'learning',None):
        try:out['self']['habit_review']=brain.learning.compact(event)
        except ValueError:pass
    out['versions']['common_style']=digest(out['self'].get('common_style',''))
    out['versions']['common_intention']=digest(out['self'].get('common_intention',''))
    return out


def bounded(data,limit=20000):
    data=json.loads(json.dumps(data,ensure_ascii=False))
    # Preserve the complete current message; remove optional history/materials first.
    for key in ('conversation','memory_material'):
        while len(json.dumps(data,ensure_ascii=False).encode())>limit and data.get(key):
            data[key].pop(0)
            if key=='conversation':data['conversation_complete_for_habits']=False
    optional=[(data.get('self',{}).get('activity_memory',{}),'events'),
              (data.get('self',{}).get('own_activities',{}),'thoughts'),
              (data.get('self',{}).get('own_activities',{}),'programs'),
              (data.get('self',{}).get('own_activities',{}),'work_reviews'),
              (data,'own_public_questions'),(data,'private_self_intentions_not_public_material'),
              (data.get('self_observation',{}),'samples')]
    trimmed=[]
    while len(json.dumps(data,ensure_ascii=False).encode())>limit:
        candidates=[(len(json.dumps(parent.get(key),ensure_ascii=False).encode()),parent,key) for parent,key in optional
                    if isinstance(parent,dict) and isinstance(parent.get(key),list) and parent[key]]
        if not candidates:break
        _,parent,key=max(candidates,key=lambda row:row[0]);parent[key].pop();trimmed.append(key)
    if trimmed:data['optional_context_trimmed']=list(dict.fromkeys(trimmed))
    if len(json.dumps(data,ensure_ascii=False).encode())>limit:raise ValueError('situation_input_budget')
    return data
