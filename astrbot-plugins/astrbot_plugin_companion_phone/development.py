"""Accept a self-chosen, practised preference without manufacturing user evidence."""
import hashlib
import json
import time
from .compat import peer


def adopt(memory, thinking, trial_id, receipt_ids, now=None):
    now = time.time() if now is None else now
    thinking.guard()
    if (str(memory.owner) != str(thinking.phone.owner) or memory.umo != thinking.phone.umo
            or memory.resting() or not memory.config.get('enabled',True)
            or not memory.config.get('growth_enabled',True) or memory.processing.locked()):
        return 'growth_unavailable'
    proof = thinking.store.practice_evidence(trial_id,receipt_ids,now)
    if not proof:
        return 'insufficient_practice'
    valid_traits=peer('astrbot_plugin_companion_memory.profile').valid_traits
    direction = proof['trial']['direction']
    if not valid_traits(direction):
        return 'private_or_unsafe'
    # Re-read surviving real public sources. A stored model summary is insufficient.
    sources = list(thinking.a.reading.state['receipts']) + thinking.a.recent()
    sources += [s for row in thinking.a.exploration.rows for s in row['sources']]
    by_id = {s['id']:s for s in sources if thinking.a.exploration.source(s)}
    for row in proof['receipts']:
        source = by_id.get(row['source_id'])
        if not source or source.get('timestamp') != row['at']:
            return 'source_unavailable'
        if source.get('content_hash') and source['content_hash'] != row['fingerprint']:
            return 'source_changed'
        if not source.get('content_hash'):
            normalized=thinking.a.exploration.source(source)
            fingerprint=hashlib.sha256(json.dumps(normalized,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
            if fingerprint!=row['fingerprint']:return 'source_changed'
    store, owner = memory.store, memory.owner
    if now-store.get_meta('last_growth:'+owner,0) < 86400:
        return 'growth_cooldown'
    existing = store.entries(owner,'growth',now=now)
    if any(r['pinned'] for r in existing):
        return 'protected'
    if direction == memory.profile.data.get('traits') or any(r['text']==direction for r in existing):
        return 'unchanged'
    thinking.guard()
    with store.atomic():
        # These references are execution evidence, deliberately not observation/user IDs.
        identifier = store.put_entry(owner,'growth','self-direction',direction,[],now=now,expires=now+90*86400)
        if not identifier:
            return 'protected'
        store.set_meta('self_growth_evidence:'+identifier,{
            'kind':'self_chosen_practice','scope':thinking.store.scope,'trial_id':trial_id,
            'practice':proof['trial']['practice'], 'receipt_ids':receipt_ids,
            'sources':[{k:r[k] for k in ('source_id','at','kind','fingerprint')} for r in proof['receipts']]})
        store.set_meta('last_growth:'+owner,now)
        store.set_meta('self_growth_last:'+owner,{'at':now,'entry_id':identifier,'trial_id':trial_id,'status':'accepted'})
    return 'accepted'
