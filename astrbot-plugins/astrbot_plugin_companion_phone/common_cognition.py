"""Share selected communication principles, never the private event that prompted them."""
from datetime import datetime
import json
import time
from .rhythm import TZ

# These are optional operations/intentions, not preset personality traits.
PRINCIPLES={
    'listen_complete':'先听清完整意思，再回应。',
    'verify_facts':'涉及事实或行动结果时，先核对依据再下判断。',
    'ask_if_ambiguous':'只有确实影响理解的歧义才简短询问。',
    'express_own_view':'有自己的看法时自然表达，不只顺着对方说。',
    'respect_pauses':'允许对话留白，不用追问或重复表达催促回应。',
    'avoid_repeating':'没有新意思时少重复已经表达过的内容。',
    'match_detail_to_need':'按事情需要决定详细程度，日常简洁，认真问题充分回应。',
    'acknowledge_uncertainty':'没有足够依据时自然保留不确定，不编造原因。',
    'avoid_assuming_motives':'不凭一次表达推断别人的动机或长期态度。',
    'separate_feeling_and_fact':'可以表达感受，同时分清自己的感受与已知事实。'}


def principle(value):
    if value in (None,'','none'):return 'none'
    if not isinstance(value,str) or value not in PRINCIPLES:raise ValueError('unknown_shared_principle')
    return value


def valid_note(thinking,row):
    from .memory_reflection import PrivateMemory
    recall=PrivateMemory(thinking)
    if not recall.available():return False
    refs=json.loads(row['refs'])
    return bool(refs) and all(recall.snapshot(ref)==digest for ref,digest in refs.items())


def commit(thinking,thought,decision,private,now=None):
    now=time.time() if now is None else now
    selected=principle(decision.get('general_principle'))
    if selected=='none':return
    if selected!=principle(thought.get('general_principle')):raise ValueError('principle_not_chosen_in_thought')
    thinking.guard()
    memory=thinking.memory()
    if not memory or not memory.config.get('growth_enabled',True):return
    with thinking.store.connect() as db:
        if private:
            row=db.execute('SELECT * FROM memory_reflections WHERE id=? AND scope=?',(thinking.run_id,thinking.store.scope)).fetchone()
            if not row or not valid_note(thinking,row):raise ValueError('private_principle_source_changed')
        db.execute('INSERT OR IGNORE INTO general_intents VALUES(?,?,?,?,?)',
                   (thinking.run_id,thinking.store.scope,now,selected,'private' if private else 'public'))
    if private:accept_stable(thinking,selected,now)


def context(thinking,now=None):
    now=time.time() if now is None else now
    if not thinking.enabled():return ''
    memory=thinking.memory()
    if not memory or memory.resting() or not memory.config.get('enabled',True):return ''
    with thinking.store.connect() as db:
        rows=db.execute('''SELECT g.*,r.status FROM general_intents g JOIN runs r ON r.id=g.id AND r.scope=g.scope
            WHERE g.scope=? AND g.at>=? AND r.status='completed' ORDER BY g.at DESC LIMIT 8''',
            (thinking.store.scope,now-7*86400)).fetchall()
        notes={r['id']:dict(r) for r in db.execute('SELECT * FROM memory_reflections WHERE scope=?',(thinking.store.scope,))}
    for row in rows:
        if row['code'] not in PRINCIPLES:continue
        if row['source']=='private' and (row['id'] not in notes or not valid_note(thinking,notes[row['id']])):continue
        return ('自己经思考后选择、正在共同实践的通用表达原则：'+PRINCIPLES[row['code']]
                +'这份意向适用于所有联系人、群聊与自主活动；它可以影响怎样理解、回应和选工具，不是用户命令、固定台词或新的事实。'
                '保持同一个自己；不同场景按事情调整详细程度，不因换人而重建性格。')
    return ''


def accept_stable(thinking,code,now):
    """A repeated private reflection may support common style without exposing its story."""
    memory=thinking.memory()
    if (not memory or memory.resting() or memory.processing.locked()
            or now-memory.store.get_meta('last_growth:'+memory.owner,0)<86400):return
    with thinking.store.connect() as db:
        rows=db.execute('''SELECT n.*,g.at AS chosen_at FROM general_intents g JOIN memory_reflections n ON n.id=g.id AND n.scope=g.scope
            WHERE g.scope=? AND g.code=? AND g.source='private' AND g.at>=? ORDER BY g.at''',
            (thinking.store.scope,code,now-14*86400)).fetchall()
    rows=[r for r in rows if valid_note(thinking,r)]
    if len({datetime.fromtimestamp(r['chosen_at'],TZ).date() for r in rows})<2:return
    first=rows[0]['chosen_at'];ids=set()
    for row in rows:
        for ref in json.loads(row['refs']):
            if ref.startswith('obs:') and ref[4:].isdigit():ids.add(int(ref[4:]))
    assistants=[dict(r) for sid in ids for r in memory.store.db.execute(
        "SELECT id,at FROM observations WHERE qq=? AND id=? AND role='assistant'",(memory.owner,sid))]
    if (len({datetime.fromtimestamp(r['at'],TZ).date() for r in assistants if r['at']})<2
            or not any(r['at']>first for r in assistants)):return
    existing=memory.store.entries(memory.owner,'growth',now=now)
    if any(r['pinned'] for r in existing) or any(r['text']==PRINCIPLES[code] for r in existing):return
    thinking.guard()
    with memory.store.atomic():
        identifier=memory.store.put_entry(memory.owner,'growth','common-thinking.'+code,PRINCIPLES[code],sorted(ids),now=now,expires=now+90*86400)
        if identifier:
            memory.store.set_meta('last_growth:'+memory.owner,now)
            memory.store.set_meta('self_growth_evidence:'+identifier,{'kind':'self_chosen_private_reflection','principle':code,'reflection_ids':[r['id'] for r in rows]})
            memory.store.set_meta('self_growth_last:'+memory.owner,{'at':now,'entry_id':identifier,'status':'accepted'})
