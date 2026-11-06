"""Explicit reference links across the extraction cursor; no inferred facts or actions."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from datetime import datetime

sys.path.insert(0,os.environ.get('COMPANION_TEST_PLUGINS',str(Path(__file__).parent)))
from astrbot_plugin_companion_memory.memory import MemoryStore,TZ
from astrbot_plugin_companion_memory.cognition import Cognition
from astrbot_plugin_companion_memory.reflection import prepare
from astrbot_plugin_companion_memory.budget import REFLECTION_BYTES,TOOL_REPLY_BYTES,size

NOW=datetime(2026,11,6,19,tzinfo=TZ).timestamp()
A='12345678';B='87654321';ORIGIN='qq-companion:FriendMessage:'+A


class ClarificationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.store=MemoryStore(self.tmp.name);self.addCleanup(self.store.close);self.c=Cognition(self.store)
    def obs(self,text,qq=A,role='user',at=NOW,origin=ORIGIN,historical=False):
        return self.store.observe(qq,role,text,at=at,origin=origin,historical=historical)
    def pair(self,old='那件事取消了',new='我刚才说取消的是海棠会议'):
        a=self.obs(old,at=NOW-60);b=self.obs(new);return a,b
    def apply(self,old,new,quote='我刚才说取消的是海棠会议',target='海棠会议',supplied=None,qq=A,cursor=None,facts=None):
        return self.store.apply_reflection(qq,{'facts':facts or [],'clarifications':[{
            'source_id':new,'clue_source_id':old,'quote':quote,'object_quote':target}]},
            supplied if supplied is not None else self.store.messages(qq),cursor=old if cursor is None else cursor,now=NOW+5)
    def links(self):return self.store.db.execute('SELECT COUNT(*) FROM memory_clue_links').fetchone()[0]

    def test_explicit_followup_links_both_originals(self):
        old,new=self.pair();r=self.apply(old,new)
        self.assertEqual(r.get('clarifications'),1);self.assertEqual(r['facts'],0)
        for sid in (old,new):
            note=self.c.read(A,'obs:'+str(sid),context=1)['interpretation']
            self.assertEqual(note['object_quote'],'海棠会议')
            self.assertEqual((note['clue_source_id'],note['clarification_source_id']),(old,new))
        self.assertEqual(self.store.entries(A),[])

    def test_dialogue_between_user_messages_does_not_supply_the_object(self):
        old=self.obs('那件事取消了',at=NOW-90)
        self.obs('你说的是哪件？',role='assistant',at=NOW-60)
        new=self.obs('我刚才说的那件事是海棠会议')
        self.assertEqual(self.apply(old,new,quote='我刚才说的那件事是海棠会议').get('clarifications'),1)

    def test_existing_facts_and_plan_statuses_unchanged(self):
        sid=self.obs('明天计划去海棠会议',at=NOW-600)
        self.store.put_entry(A,'fact','计划.海棠','对方说：明天计划去海棠会议',[sid],now=NOW-590)
        tables=('entries','memory_state','entry_revisions')
        before={t:[tuple(r) for r in self.store.db.execute('SELECT * FROM '+t)] for t in tables}
        old,new=self.pair();self.apply(old,new)
        self.assertEqual(before,{t:[tuple(r) for r in self.store.db.execute('SELECT * FROM '+t)] for t in tables})

    def test_object_only_clarification_not_promoted_to_fact(self):
        old,new=self.pair()
        r=self.apply(old,new,facts=[{'source_id':new,'quote':'我刚才说取消的是海棠会议','key':'会议.取消','status':'cancelled'}])
        self.assertEqual(r['facts'],0);self.assertEqual(r.get('clarifications'),1)

    def test_silent_without_explicit_read_context(self):
        old,new=self.pair();self.apply(old,new)
        self.assertNotIn('interpretation',self.c.read(A,'obs:'+str(old)))
        self.assertNotIn('explicit_reference_clarification',self.c.render(A,'海棠会议'))

    def test_old_source_can_cross_processed_cursor_without_advancing_it_again(self):
        old,new=self.pair();payload,supplied,cursor,offset=prepare(self.store,A,old,'',NOW,'后台JSON整理')
        self.assertEqual(payload['待理解原话_仅用于明确澄清']['id'],old)
        self.assertEqual({r['id'] for r in supplied},{old,new});self.assertEqual(cursor,new)
        self.assertEqual(offset,{'id':None,'offset':0})
        self.assertLessEqual(size('后台JSON整理')+size(payload),REFLECTION_BYTES)
        self.assertEqual(self.apply(old,new,supplied=supplied).get('clarifications'),1)

    def test_same_batch_does_not_duplicate_context_source(self):
        old,new=self.pair();payload,supplied,cursor,_=prepare(self.store,A,0,'',NOW,'后台JSON整理')
        self.assertNotIn('待理解原话_仅用于明确澄清',payload)
        self.assertEqual(sum(r['id']==old for r in supplied),1)
        self.assertEqual(self.apply(old,new,supplied=supplied,cursor=0).get('clarifications'),1)

    def test_normal_chat_does_not_add_a_pending_queue_to_extraction(self):
        old=self.obs('那件事取消了',at=NOW-60);self.obs('今天晚饭吃面')
        payload,*_=prepare(self.store,A,old,'',NOW,'后台JSON整理')
        self.assertNotIn('待理解原话_仅用于明确澄清',payload)

    def test_no_output_selection_means_no_link(self):
        old,new=self.pair();self.store.apply_reflection(A,{'facts':[]},self.store.messages(A),cursor=old,now=NOW)
        self.assertEqual(self.links(),0)

    def test_ack_or_pronoun_is_not_a_clarification(self):
        for text,target in [('嗯','海棠会议'),('就是那个','海棠会议'),('我刚才说的是那个','那个'),('我刚才说的是原来那个','原来那个')]:
            old,new=self.pair(new=text);self.apply(old,new,quote=text,target=target);self.assertEqual(self.links(),0)

    def test_not_generated_by_entity_similarity(self):
        old,new=self.pair(new='海棠会议取消了吗？')
        self.apply(old,new,quote='海棠会议取消了吗？');self.assertEqual(self.links(),0)

    def test_multiple_unresolved_candidates_are_not_disambiguated_by_recency(self):
        self.obs('那件事取消了',at=NOW-90);old,new=self.pair()
        self.apply(old,new);self.assertEqual(self.links(),0)

    def test_intervening_user_topic_blocks_old_reference(self):
        old=self.obs('那件事取消了',at=NOW-120);self.obs('换个话题，晚饭吃面',at=NOW-60)
        new=self.obs('我刚才说取消的是海棠会议');self.apply(old,new);self.assertEqual(self.links(),0)

    def test_future_change_clue_cannot_be_closed_as_reference(self):
        old,new=self.pair()
        with self.store.atomic():self.store.continuity.clue(A,old,'future_change')
        self.apply(old,new);self.assertEqual(self.links(),0)

    def test_no_guess_across_time_or_missing_timestamps(self):
        for old_at in (NOW-7201,None,NOW+1):
            with self.subTest(old_at=old_at):
                old=self.obs('那件事取消了',at=old_at,historical=old_at is None)
                if old_at is None:
                    with self.store.atomic():self.store.continuity.clue(A,old)
                new=self.obs('我刚才说取消的是海棠会议');self.apply(old,new);self.assertEqual(self.links(),0)

    def test_different_origin_is_not_same_dialogue(self):
        old=self.obs('那件事取消了',at=NOW-60,origin='group:synthetic')
        new=self.obs('我刚才说取消的是海棠会议');self.apply(old,new);self.assertEqual(self.links(),0)

    def test_no_cross_contact_link_even_with_supplied_foreign_row(self):
        old=self.obs('那件事取消了',qq=B,at=NOW-60)
        new=self.obs('我刚才说取消的是海棠会议')
        supplied=self.store.messages(B)+self.store.messages(A)
        self.apply(old,new,supplied=supplied,cursor=0);self.assertEqual(self.links(),0)

    def test_assistant_cannot_supply_user_clarification(self):
        old=self.obs('那件事取消了',at=NOW-60)
        new=self.obs('我刚才说取消的是海棠会议',role='assistant')
        self.apply(old,new);self.assertEqual(self.links(),0)

    def test_assistant_original_cannot_be_linked_as_user_report(self):
        old=self.obs('那件事取消了',role='assistant',at=NOW-60)
        with self.store.atomic():self.store.continuity.clue(A,old)
        new=self.obs('我刚才说取消的是海棠会议');self.apply(old,new);self.assertEqual(self.links(),0)

    def test_verbatim_target_and_supplied_sources_required(self):
        old,new=self.pair();self.apply(old,new,target='松湾报告');self.assertEqual(self.links(),0)
        self.apply(old,new,supplied=[self.store.messages(A)[-1]]);self.assertEqual(self.links(),0)

    def test_stale_extraction_is_rejected(self):
        old,new=self.pair();supplied=self.store.messages(A)
        self.store.db.execute('UPDATE observations SET text=? WHERE id=?',('我刚才说取消的是松湾报告',new))
        self.apply(old,new,supplied=supplied);self.assertEqual(self.links(),0)

    def test_changed_old_clue_is_not_rebound(self):
        old,new=self.pair();self.store.db.execute('UPDATE observations SET text=? WHERE id=?',('海棠会议没有取消',old))
        self.apply(old,new);self.assertEqual(self.links(),0)

    def test_resolution_invalidates_on_either_source_change(self):
        old,new=self.pair();self.apply(old,new)
        self.store.db.execute('UPDATE observations SET text=? WHERE id=?',('我刚才说取消的是松湾报告',new))
        self.assertNotIn('object_quote',self.store.continuity.clue_note(A,old))
        self.assertIsNone(self.store.continuity.clue_note(A,new))

    def test_suppressed_source_not_resolved_or_exposed(self):
        old,new=self.pair();self.apply(old,new)
        self.store.db.execute('INSERT INTO suppressed_sources VALUES(?,?)',(A,new))
        self.assertNotIn('object_quote',self.store.continuity.clue_note(A,old))
        self.assertIsNone(self.store.continuity.clue_note(A,new))

    def test_cross_contact_read_cannot_see_resolved_object(self):
        old,new=self.pair();self.apply(old,new)
        with self.assertRaises(ValueError):self.c.read(B,'obs:'+str(old),context=1)
        self.assertIsNone(self.store.continuity.clue_note(B,new))

    def test_mismatched_action_does_not_reinterpret_original(self):
        old,new=self.pair(new='我刚才说完成的是海棠报告')
        self.apply(old,new,quote='我刚才说完成的是海棠报告',target='海棠报告');self.assertEqual(self.links(),0)

    def test_nonownership_reference_is_not_turned_into_ownership(self):
        old,new=self.pair(old='不是我的',new='我刚才说不是我的是海棠报告')
        self.assertEqual(self.apply(old,new,quote='我刚才说不是我的是海棠报告',target='海棠报告').get('clarifications'),1)
        self.assertEqual(self.store.entries(A),[])

    def test_question_quoted_or_multiple_assertion_clarification_not_accepted(self):
        for text in ('我刚才说取消的是海棠会议？','我刚才说的是“海棠会议”','我刚才说的是海棠会议，不是松湾报告'):
            old,new=self.pair(new=text);self.apply(old,new,quote=text);self.assertEqual(self.links(),0)

    def test_processed_clarifier_cannot_replay_as_new(self):
        old,new=self.pair();self.apply(old,new,cursor=new);self.assertEqual(self.links(),0)

    def test_repeated_selection_idempotent(self):
        old,new=self.pair();self.apply(old,new);r=self.apply(old,new)
        self.assertNotIn('clarifications',r);self.assertEqual(self.links(),1)

    def test_transaction_rollback_includes_links(self):
        old,new=self.pair()
        with self.assertRaises(RuntimeError):
            with self.store.atomic():self.apply(old,new);raise RuntimeError()
        self.assertEqual(self.links(),0)

    def test_forget_cascades_to_reference_link(self):
        old,new=self.pair();self.apply(old,new)
        key=self.store.put_entry(A,'episode','event.clarification','两条真实原话的对话事件',[old,new],now=NOW)
        self.store.forget(A,key);self.assertEqual(self.links(),0)
        self.assertIsNone(self.store.continuity.clue_note(A,old))

    def test_long_literal_object_still_fits_existing_read_budget(self):
        target='海棠会议'+('甲乙丙丁'*10)
        old,new=self.pair(new='我刚才说取消的是'+target)
        self.assertEqual(self.apply(old,new,quote='我刚才说取消的是'+target,target=target).get('clarifications'),1)
        self.assertLessEqual(size(self.c.read(A,'obs:'+str(old),context=1)),TOOL_REPLY_BYTES)

    def test_source_rows_and_extraction_budgets_unchanged(self):
        old,new=self.pair();before=[tuple(r) for r in self.store.db.execute('SELECT * FROM observations')]
        self.apply(old,new)
        self.assertEqual(before,[tuple(r) for r in self.store.db.execute('SELECT * FROM observations')])
        self.assertIsNone(self.store.get_meta('extraction_usage'))

if __name__=='__main__':unittest.main()
