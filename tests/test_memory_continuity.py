"""Temporal precision, source-bound revisions, unresolved clues and dialogue budgets."""
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
from astrbot_plugin_companion_memory.continuity import event_time,unresolved,future_change,terminal_status,relation
from astrbot_plugin_companion_memory.budget import size,CONTEXT_BYTES,TOOL_REPLY_BYTES

def at(value):return datetime.fromisoformat(value).replace(tzinfo=TZ).timestamp()
NOW=at('2026-11-05T22:00:00')

class TimeTests(unittest.TestCase):
    def test_delayed_month_report_has_month_precision(self):
        t=event_time('我上个月换工作了',NOW)
        self.assertEqual((t['start'],t['end'],t['precision']),('2026-10-01','2026-10-31','month'))
        self.assertEqual(t['kind'],'reported')

    def test_uses_message_time_and_crosses_year(self):
        self.assertEqual(event_time('昨天搬家了',at('2026-01-01'))['start'],'2025-12-31')
        self.assertEqual(event_time('我上个月换工作了',at('2026-01-20'))['end'],'2025-12-31')

    def test_leap_month_and_year_precision(self):
        self.assertEqual(event_time('我2024年2月搬家了',NOW)['end'],'2024-02-29')
        self.assertEqual(event_time('我去年毕业了',NOW)['precision'],'year')

    def test_explicit_day(self):
        self.assertEqual(event_time('我在2026年10月3日搬家了',NOW)['start'],'2026-10-03')
        self.assertEqual(event_time('2026-10-03搬家了',NOW)['precision'],'day')

    def test_ambiguous_or_nonasserted_times_stay_raw(self):
        for q in ('昨天聊过，我上个月搬家了','昨天到今天都在忙','我2026年2月30日搬家了','如果明天搬家','他说昨天搬家了','我“昨天”搬家了','昨天搬家了吗？'):
            with self.subTest(q=q):self.assertIsNone(event_time(q,NOW))

    def test_unknown_source_timestamp(self):self.assertIsNone(event_time('昨天搬家了',None))

    def test_negated_or_future_results_do_not_close_plan(self):
        for q in ('海棠会议没有取消','海棠报告还没完成','海棠报告尚未完成','我打算完成海棠报告','明天完成海棠报告','我计划完成海棠报告','我不会取消海棠会议'):
            self.assertIsNone(terminal_status(q),q)
            self.assertNotIn(relation(q),('cancellation','completion'))
        self.assertEqual(terminal_status('海棠报告已经完成了'),'completed')
        self.assertEqual(terminal_status('明天的海棠会议取消了'),'cancelled')

    def test_planned_time_is_not_completed_event(self):
        self.assertEqual(event_time('我明天搬家',NOW)['kind'],'planned')
        self.assertTrue(future_change('我打算下个月换工作'))
        self.assertFalse(future_change('明天的行程已经取消了'))

    def test_short_entity_statements_not_blanket_rejected(self):
        for q in ('那件事取消了','不是我的','后来取消了','我一定会实现的'):self.assertTrue(unresolved(q),q)
        for q in ('不是我的书','我喜欢茶','明天的海棠会议取消了','那件事是海棠会议'):self.assertFalse(unresolved(q),q)

class ContinuityTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.store=MemoryStore(Path(self.tmp.name));self.c=Cognition(self.store)
    def tearDown(self):self.store.close();self.tmp.cleanup()
    def obs(self,text,qq='A',when=NOW):return self.store.observe(qq,'user',text,at=when)
    def write(self,text,topic='工作',qq='A',when=NOW):
        sid=self.obs(text,qq,when);key=self.store.put_entry(qq,'fact',topic,'对方说：'+text,[sid],now=when+10)
        return key,sid
    def update(self,key,text,topic='工作',when=NOW+100):
        sid=self.obs(text,when=when)
        supplied=self.store.messages('A')
        result=self.store.apply_reflection('A',{'facts':[{'key':topic,'quote':text,'source_id':sid,'action':'update','target_id':key}]},supplied,now=when+10)
        return result,sid

    def test_record_time_separate_from_event_time(self):
        key,sid=self.write('我上个月换工作了')
        card=self.c.read('A',key)
        self.assertEqual(card['event_time']['start'],'2026-10-01')
        self.assertTrue(card['updated'].startswith('2026-11-05'))
        self.assertEqual(card['source_ids'],[sid])

    def test_no_metadata_for_unsupported_quote(self):
        sid=self.obs('我昨天只是讨论过搬家')
        key=self.store.put_entry('A','fact','搬家','对方说：我昨天搬家了',[sid],now=NOW)
        self.assertNotIn('event_time',self.c.describe(self.store.get_entry('A',key)))

    def test_correction_is_not_previous_true_preference(self):
        key,_=self.write('我喜欢红茶','饮食.茶')
        result,_=self.update(key,'你记错了，我一直喜欢绿茶','饮食.茶')
        self.assertEqual(result['facts'],1)
        card=self.c.read('A',key,context=1)
        self.assertEqual(card['revision_kind'],'correction')
        self.assertEqual(card['history'][0]['relation'],'correction')
        self.assertIn('红茶',card['history'][0]['previous_text'])
        self.assertNotIn('history',self.c.read('A',key))

    def test_change_keeps_old_source(self):
        key,sid=self.write('我喜欢红茶','饮食.茶')
        self.update(key,'我现在改成喜欢绿茶了','饮食.茶')
        h=self.c.read('A',key,context=1)['history'][0]
        self.assertEqual(h['relation'],'change');self.assertEqual(h['previous_source_ids'],[sid])

    def test_cross_contact_cannot_read_history(self):
        key,_=self.write('我喜欢红茶','饮食.茶');self.update(key,'我现在改成喜欢绿茶了','饮食.茶')
        with self.assertRaises(ValueError):self.c.read('B',key,context=1)

    def test_mutated_old_source_not_reintroduced_as_history(self):
        key,sid=self.write('我喜欢红茶','饮食.茶');self.update(key,'我现在改成喜欢绿茶了','饮食.茶')
        self.store.db.execute('UPDATE observations SET text=? WHERE id=?',('我喜欢白茶',sid))
        self.assertNotIn('history',self.c.read('A',key,context=1))

    def test_already_invalid_old_source_cannot_be_recaptured(self):
        key,sid=self.write('我喜欢红茶','饮食.茶')
        self.store.db.execute('UPDATE observations SET text=? WHERE id=?',('我喜欢白茶',sid))
        self.update(key,'我现在改成喜欢绿茶了','饮食.茶')
        self.assertNotIn('history',self.c.read('A',key,context=1))

    def test_current_source_edit_invalidates_metadata_and_read(self):
        key,sid=self.write('我上个月换工作了')
        self.store.db.execute('UPDATE observations SET text=? WHERE id=?',('我昨天换工作了',sid))
        self.assertEqual(self.store.continuity.describe(self.store.get_entry('A',key)),{})
        with self.assertRaises(ValueError):self.c.read('A',key)

    def test_clue_is_raw_not_fact_and_does_not_force_question(self):
        sid=self.obs('那件事取消了')
        result=self.store.apply_reflection('A',{'facts':[{'key':'事件','quote':'那件事取消了','source_id':sid}]},self.store.messages('A'),now=NOW)
        self.assertEqual(result['facts'],0);self.assertEqual(self.store.entries('A'),[])
        self.assertNotIn('interpretation',self.c.read('A','obs:'+str(sid)))
        self.assertIn('interpretation',self.c.read('A','obs:'+str(sid),context=1))
        self.assertIsNone(self.store.continuity.clue_note('B',sid))

    def test_clue_detects_source_changes_and_suppression(self):
        sid=self.obs('不是我的');self.assertIsNotNone(self.store.continuity.clue_note('A',sid))
        self.store.db.execute('INSERT INTO suppressed_sources VALUES(?,?)',('A',sid))
        self.assertIsNone(self.store.continuity.clue_note('A',sid))

    def test_future_change_cannot_replace_current_fact(self):
        key,_=self.write('我现在在枫桥公司工作')
        result,sid=self.update(key,'我打算下个月把工作改成在松湾公司')
        self.assertEqual(result['facts'],0)
        self.assertIn('枫桥公司',self.store.get_entry('A',key)['text'])
        self.assertEqual(self.store.continuity.clue_note('A',sid)['reason'],'future_change')

    def test_explicit_plan_cancellation_still_updates(self):
        key,_=self.write('明天计划去海棠会议','行程.海棠')
        result,_=self.update(key,'明天的海棠会议已经取消了','行程.海棠')
        self.assertEqual(result['facts'],1)
        self.assertEqual(self.c.read('A',key)['revision_kind'],'cancellation')

    def test_forget_removes_new_projection(self):
        key,sid=self.write('我上个月换工作了');self.store.continuity.clue('A',sid)
        self.assertTrue(self.store.forget('A',key))
        self.assertIsNone(self.store.continuity.clue_note('A',sid))
        self.assertFalse(self.store.db.execute('SELECT 1 FROM memory_continuity WHERE entry_id=?',(key,)).fetchone())

    def test_forget_covers_old_revision_sources(self):
        key,sid=self.write('我喜欢红茶','饮食.茶');self.update(key,'我现在改成喜欢绿茶了','饮食.茶')
        self.store.forget('A',key)
        with self.assertRaises(ValueError):self.c.read('A','obs:'+str(sid))

    def test_duplicate_does_not_create_extra_revision(self):
        key,sid=self.write('我上个月换工作了')
        self.store.put_entry('A','fact','工作','对方说：我上个月换工作了',[sid],now=NOW+30)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM memory_continuity').fetchone()[0],1)

    def test_transaction_rollback_includes_metadata(self):
        try:
            with self.store.atomic():self.write('我昨天换工作了');raise RuntimeError()
        except RuntimeError:pass
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM memory_continuity').fetchone()[0],0)

    def test_no_backfill_and_reopen_preserves_metadata(self):
        key,_=self.write('我上个月换工作了');self.store.close();self.store=MemoryStore(Path(self.tmp.name));self.c=Cognition(self.store)
        self.assertEqual(self.c.read('A',key)['event_time']['precision'],'month')
        self.store.db.execute('DELETE FROM memory_continuity')
        self.assertNotIn('event_time',self.c.read('A',key))

    def test_internal_annotations_preserve_context_and_read_budgets(self):
        key,_=self.write('我上个月换工作了','工作.公司')
        self.update(key,'我现在把工作改成在松湾公司','工作.公司')
        self.assertLessEqual(size(self.c.read('A',key,context=1)),TOOL_REPLY_BYTES)
        rendered=self.c.render('A','工作')
        self.assertLessEqual(size(rendered),CONTEXT_BYTES)
        for line in ('我还没核实，不能保证','必须向用户追问','不能随便给你一个原因或保证'):self.assertNotIn(line,rendered)

if __name__=='__main__':unittest.main()
