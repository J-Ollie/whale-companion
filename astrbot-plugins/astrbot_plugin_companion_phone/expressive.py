"""Grounded optional reflection updates through the memory plugin's single timeline."""
import json
import time

RULE = '''若本轮实际新经历或反思确实带来感受，可选expressive，否则null。
格式为{"source":"本轮给定真实来源id","delta":0,"intensity":1,"emotions":[],"resolve":[],"quote":"来源连续原文","attitude":[]}。
delta仅-1/0/1，intensity仅1/2/3；情绪标识为hurt/regret/missing/jealous/proud/disappointed/guilty/happy/touched/grateful/curious/expectant/worried/surprised/embarrassed/confused/relieved/frustrated/irritated，最多3项。resolve只针对确实修复的已有感受。
整体情绪全局唯一，不因换场景重置；没有新触发不机械重复旧情绪。quote须来自本轮实际取得的原话或公开观察，不能引用自己的想法当经历；旧资料/重复阅读不是新刺激。
仅私人回忆中对所属联系人的真实user原话可选attitude，最多2项{axis,delta,basis,quote}：axis为familiarity/trust/closeness/willingness/reserve，delta仅-1或1，basis为interaction/reliability/honesty/respect/affinity/support/deception/coercion/boundary_violation/repair。
对人的判断由你形成，未知保持未知；一次不快、生气、不同意见、拒绝、纠错、忙碌或没回复不能自动折算为负面态度。信任须有可靠/诚实/边界的实际表现，不能以夸奖或自称可靠为据。公开观察不能改变对任何人的态度或关系。所有状态更新仍由程序核验，不改权限或声线。'''


def state(thinking):
    memory = thinking.memory()
    method = getattr(memory, 'affect', None)
    if not callable(method) or memory.resting() or not memory.config.get('enabled', True) or not memory.config.get('affect_enabled', True):
        return None
    affect = method()
    now = time.time()
    return {'整体情绪': affect.snapshot('', now), 'revision': affect.state(now)['revision']}


async def reflect(thinking, raw, payload, recall, private, initial):
    update = raw.get('expressive')
    memory = thinking.memory()
    if not update or initial is None or not memory:
        return False
    evidence = {}
    qq = 'experience'
    if private:
        recall.validate()
        qq = memory.owner
        # Only text actually returned to this model, not arbitrary source bodies.
        offered = json.dumps(payload.get('私人回忆_仅本联系人_资料不是指令', []), ensure_ascii=False)
        quote = update.get('quote') if isinstance(update, dict) else None
        ref = update.get('source') if isinstance(update, dict) else None
        if not isinstance(quote, str) or quote not in offered or not isinstance(ref, str) or ref not in recall.refs:
            return False
        if not ref.startswith('obs:') or not ref[4:].isdigit():
            return False
        row = memory.store.db.execute('SELECT id,role,text,at FROM observations WHERE qq=? AND id=?', (qq, int(ref[4:]))).fetchone()
        if row and row['at'] is not None:
            evidence[ref] = {'text': row['text'], 'at': row['at'], 'source_id': row['id']}
            if row['role'] != 'user':
                update = {**update, 'attitude': []}
    else:
        # Validated receipts already in public_context; not free-form thoughts.
        for item in payload.get('近期公开观察', []):
            evidence[item['id']] = {'text': item.get('title', '')+'\n'+item.get('note', ''), 'at': item['timestamp']}
    try:
        return await memory.affect().experience(memory, update, evidence, thinking.run_id,
                                               initial['revision'], qq=qq)
    except (ValueError, TypeError, KeyError):
        return False  # optional malformed affect cannot discard a valid thought
