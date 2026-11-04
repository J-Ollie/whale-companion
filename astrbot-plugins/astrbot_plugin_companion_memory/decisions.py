"""Conservative extraction decisions. Raw quotes and entry revisions survive.

Unlike a model-controlled multi-target merger, only a source-verifiable
restatement retaining the old assertion can merge, within one private owner.
"""
import json
import re
from .lifecycle import CHANGE


def decide(store,qq,fact,topic,quote,source_id,subject):
    action=fact.get('action','update' if fact.get('replaces_id') else 'add')
    target=fact.get('target_id') or fact.get('replaces_id')
    old=store.get_entry(qq,str(target)) if target else store.db.execute(
        "SELECT * FROM entries WHERE qq=? AND kind='fact' AND topic=?",(qq,topic)).fetchone()
    old=dict(old) if old else None
    if action not in ('add','skip','update','merge'):return 'reject','unsupported_action',None
    if action=='skip':return 'skip','extractor_selected_no_write',None
    if action=='add' and target:return 'reject','add_cannot_replace_target',None
    if target and (not old or old['kind']!='fact' or old['pinned']):return 'reject','target_unavailable_or_fixed',None
    if action in ('update','merge') and not target:return 'reject','exact_target_required',None
    if old and old['pinned']:return 'reject','fixed_entry',None
    if action=='add' and old and quote in old['text']:
        return 'skip','duplicate_assertion_preserve_original_evidence',None
    if action=='merge':
        if not store.lifecycle.provenance_valid(old):return 'reject','stale_merge_source',None
        previous=old['text'].split('说：',1)[-1]
        # No free merged_content: the new continuous quote must explicitly
        # repeat the entire old assertion and then add information.
        if previous not in quote or previous==quote or CHANGE.search(quote) or re.search(r'不|没|无|未|并非',quote):
            return 'reject','merge_must_retain_exact_positive_assertion',None
        if not re.search(r'也|还|另外|补充|以及|并且',quote):return 'reject','explicit_addition_required',None
        state=store.lifecycle.describe(old)
        if state['type']!='semantic' or not store.lifecycle.current(old):return 'reject','merge_only_current_semantic_fact',None
        evidence=list(dict.fromkeys(json.loads(old['evidence'])+[source_id]))
        if len(evidence)>4 or len(subject+'说：'+quote)>400:return 'reject','merge_evidence_or_text_budget',None
        return 'merge','source_restates_and_extends_assertion',{'topic':old['topic'],'evidence':evidence,'id':old['id']}
    return action,'validated_quote',{'topic':old['topic'] if target else topic,'evidence':[source_id],'id':old['id'] if old else None}


def audit(store,qq,source_id,target,action,reason,now):
    store.db.execute('INSERT INTO memory_update_decisions(qq,at,source_id,entry_id,action,reason) VALUES(?,?,?,?,?,?)',
        (qq,now,source_id,target,action,reason))
    store.db.execute('DELETE FROM memory_update_decisions WHERE id NOT IN (SELECT id FROM memory_update_decisions ORDER BY id DESC LIMIT 2000)')
