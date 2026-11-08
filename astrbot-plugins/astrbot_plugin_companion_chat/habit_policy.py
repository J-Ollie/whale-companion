"""Cheap, conservative context guards. No inferred feelings or learned permissions."""
import hashlib
import json
import re

POLICY='dialogue-habits-v2'
ROUTES={'thanks':'acknowledge_without_new_question','closing':'allow_natural_closing',
        'acknowledgement':'acknowledge_understanding_without_new_action'}
PHRASES={'thanks':('谢谢','谢谢你','谢啦','辛苦了','辛苦啦'),
         'closing':('晚安','拜拜','回头聊','先这样','改天聊'),
         'acknowledgement':('我知道了','我明白了','明白了','了解了','收到啦','我了解了')}
SENSITIVE=re.compile(r'难过|伤心|生气|委屈|不舒服|难受|焦虑|害怕|敷衍|不耐烦|不想聊|取消|删除|忘记|权限|授权|暂停|提醒|发布|发送|答应|承诺|明天|明早|下周|稍后|待会|会帮|会给|已经|完成|要不要|帮我|转发|发说说|答复|确认|矛盾|冲突|更正|误会|求助|救命|自杀|轻生|[?？“”「」『』]')
PREFIXES={'好的','嗯嗯','其实','所以','我在','那就','这个','不过','可以','谢谢','晚安'}


def digest(value):return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False).encode()).hexdigest()


def classify(event,situation):
    if event.get_group_id() or situation.get('scope')!='private':return None,'unsupported_scope'
    parts=getattr(getattr(event,'message_obj',None),'message',[]) or []
    if any(type(part).__name__!='Plain' for part in parts):return None,'non_plain_or_quoted_material'
    if event.get_extra('companion_material_context',None):return None,'material_context'
    text=re.sub(r'[。！!，,\s]+$','',event.message_str or '')
    route=next((k for k,values in PHRASES.items() if text in values),None)
    if route is None:return None,'ambiguous_or_complex_message'
    history=situation.get('conversation',[])
    if situation.get('conversation_complete_for_habits') is False:return None,'incomplete_context'
    if not history or history[-1].get('role')!='assistant':return None,'no_previous_reply'
    if any(len(str(row.get('content','')))>360 or SENSITIVE.search(str(row.get('content',''))) for row in history):
        return None,'sensitive_or_unresolved_context'
    own=situation.get('self',{});versions=situation.get('versions',{})
    if any(key not in versions for key in ('common_style','common_intention','attitude','relationships')) or not own.get('emotion'):
        return None,'current_state_unknown'
    mood=json.dumps(own['emotion'],ensure_ascii=False)
    if any(x in mood for x in ('较激动','生气','愤怒','难过','低落','害怕','委屈')):return None,'emotion_requires_fresh_understanding'
    if own.get('rhythm',{}).get('mode')!='awake':return None,'not_awake'
    if any(row.get('status')=='pending' for row in situation.get('commitments',[])):return None,'unfinished_commitment'
    deps=digest({'policy':POLICY,'style':versions['common_style'],'principle':versions['common_intention'],
                 'attitude':versions['attitude'],'relationships':versions['relationships'],'emotion':own['emotion']})
    return {'route':route,'deps':deps,'context_kind':'plain_reply_followed_by_'+route},'clear_context'
