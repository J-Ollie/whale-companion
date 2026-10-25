"""Dependency-free UTF-8 text budgets, not a claim about billed token counts."""
import json

CONTEXT_BYTES=2600
SHARED_BYTES=900
HISTORY_BYTES=7000
HISTORY_MESSAGES=12
TOOL_BYTES=4200
TOOL_REPLY_BYTES=1800
REFLECTION_BYTES=11000
REFLECTION_OUTPUT_TOKENS=900


def dump(value):
    return json.dumps(value,ensure_ascii=False,separators=(',',':'))


def size(value):
    return len((value if isinstance(value,str) else dump(value)).encode('utf-8'))


def clip(text,limit):
    raw=str(text).encode('utf-8')
    if len(raw)<=limit:return str(text)
    return raw[:max(0,limit-3)].decode('utf-8','ignore')+'…' if limit>=3 else ''


def pack(header,groups,limit):
    """Keep JSON well formed, prioritise groups, and never cut a record in half."""
    data=dict(header);used=set();groups=list(groups)
    data.update({key:[] for key,_ in groups})
    for key,items in groups:
        for item in items:
            identifier=item.get('id') if isinstance(item,dict) else None
            if identifier and identifier in used:continue
            data[key].append(item)
            if size(data)>limit:data[key].pop();continue
            if identifier:used.add(identifier)
    return data


def trim_history(messages,limit=HISTORY_BYTES,max_messages=HISTORY_MESSAGES):
    """Keep recent complete user turns, including whole tool-call/result chains."""
    turns=[]
    for message in messages or []:
        if not isinstance(message,dict):continue
        if message.get('role')=='user':turns.append([])
        if turns:turns[-1].append(message)
    chosen=[];cost=0;count=0
    for turn in reversed(turns):
        amount=size(turn)
        if cost+amount>limit or count+len(turn)>max_messages:break
        chosen.insert(0,turn);cost+=amount;count+=len(turn)
    return [message for turn in chosen for message in turn]


def public_brief(data,query=''):
    people=[{'name':clip(r.get('name',''),45),'public_role':clip(r.get('public_role',''),90)} for r in data.get('known_people',[])[:3]]
    names=data.get('other_acquaintance_display_names',[])
    names=[n for n in names if n in query]+[n for n in names if n not in query]
    return dump(pack({},[('known_people',people),('other_acquaintance_display_names',names[:12]),
        ('public_diary',[{'day':r.get('day'),'text':clip(r.get('text',''),240)} for r in data.get('public_diary',[])[-1:]])],SHARED_BYTES))
