"""Message-time filtering, bilateral evidence and bounded fallback. No external I/O."""
from datetime import datetime
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock,patch

sys.path.insert(0,os.environ.get('COMPANION_TEST_PLUGINS',str(Path(__file__).parent)))
from astrbot_plugin_companion_memory.query_plan import make_plan,comparison_sides
from astrbot_plugin_companion_memory.memory import MemoryStore,TZ
from astrbot_plugin_companion_memory.cognition import Cognition
from astrbot_plugin_companion_memory.retrieval import LayeredRecall

NOW=datetime(2026,11,5,19,tzinfo=TZ).timestamp()
def at(day,hour=10):return datetime.fromisoformat(day).replace(tzinfo=TZ,hour=hour).timestamp()


class PlanTests(unittest.TestCase):
    def test_relative_days(self):
        for q,day in [('今天聊了什么','2026-11-05'),('我们昨天说过什么','2026-11-04'),('前天讨论了什么','2026-11-03')]:
            with self.subTest(q=q):
                p=make_plan(q,now=NOW);self.assertEqual((p.start_day,p.end_day),(day,day));self.assertEqual(p.query,'')

    def test_previous_week_uses_calendar_week(self):
        p=make_plan('上周我们讨论了冷杉',now=NOW)
        self.assertEqual((p.start_day,p.end_day),('2026-10-26','2026-11-01'))

    def test_month_crosses_year_boundary(self):
        p=make_plan('上个月聊过什么',now=at('2026-01-05'))
        self.assertEqual((p.start_day,p.end_day),('2025-12-01','2025-12-31'))

    def test_recent_days_include_today(self):
        p=make_plan('最近3天讨论冷杉',now=NOW)
        self.assertEqual((p.start_day,p.end_day),('2026-11-03','2026-11-05'))

    def test_explicit_filters_take_precedence(self):
        p=make_plan('昨天晚上讨论冷杉','2026-11-01','2026-11-02',now=NOW)
        self.assertEqual((p.start_day,p.end_day),('2026-11-01','2026-11-02'));self.assertIsNone(p.window)

    def test_evening_is_half_open_window(self):
        p=make_plan('2026年11月2日晚上，我点的歌是什么？',now=NOW)
        self.assertEqual(p.window,(at('2026-11-02',18),at('2026-11-03',0)))
        self.assertEqual(p.variants,('点歌','唱歌'))

    def test_birthday_date_does_not_filter_message_time(self):
        p=make_plan('2020年11月2日是我生日，我说过吗？',now=NOW)
        self.assertEqual(p.start_day,'');self.assertIsNone(p.window)

    def test_quoted_relative_date_is_not_a_time_instruction(self):
        p=make_plan('我说“昨天”时是什么意思',now=NOW)
        self.assertEqual(p.start_day,'')

    def test_two_unrelated_dates_do_not_become_one_global_filter(self):
        p=make_plan('2026年11月1日和2026年11月3日有什么区别',now=NOW)
        self.assertEqual(p.start_day,'')

    def test_named_sides_and_ambiguous_temporal_sides(self):
        self.assertEqual(comparison_sides('“枫桥方案”和“松湾方案”有什么区别？'),('枫桥方案','松湾方案'))
        self.assertEqual(comparison_sides('以前和现在有什么区别'),())

    def test_invalid_date_and_unbounded_recent_range_rejected(self):
        with self.assertRaises(ValueError):make_plan('2026年2月30日讨论什么',now=NOW)
        with self.assertRaises(ValueError):make_plan('最近99天聊了什么',now=NOW)

    def test_quoted_entity_may_contain_a_conjunction(self):
        self.assertEqual(comparison_sides('“研究和生活”与“工作安排”有什么区别？'),('研究和生活','工作安排'))

    def test_three_unquoted_sides_are_not_forced_into_two(self):
        self.assertEqual(comparison_sides('枫桥和松湾和青岬有什么区别'),())

    def test_two_relative_periods_do_not_become_one_global_window(self):
        p=make_plan('上周和本周有什么区别',now=NOW)
        self.assertEqual(p.start_day,'');self.assertEqual(p.sides,('上周','本周'))


class RoutingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.store=MemoryStore(self.tmp.name);self.c=Cognition(self.store)
        self.qq='12345678';self.sleep=False
        self.plugin=SimpleNamespace(store=self.store,cognition=lambda:self.c,resting=lambda:self.sleep,
            config={},semantic=SimpleNamespace(recall=AsyncMock(return_value=[])),expand_query=AsyncMock(return_value=[]))
        self.extra={};self.event=SimpleNamespace(get_extra=lambda key,default=None:self.extra.get(key,default),
            set_extra=lambda key,value:self.extra.__setitem__(key,value))
        self.clock=patch('time.time',return_value=NOW);self.clock.start()

    def tearDown(self):self.clock.stop();self.store.close();self.tmp.cleanup()

    def obs(self,text,day='2026-11-04',hour=10,qq=None):return self.store.observe(qq or self.qq,'user',text,at=at(day,hour))
    def fact(self,text,topic):
        source=self.obs(text);return self.store.put_entry(self.qq,'fact',topic,text,[source])

    async def test_time_only_returns_scoped_day_not_generic_chatter(self):
        target=self.obs('蓝港讨论通过了')
        self.obs('蓝港讨论还没开始',day='2026-11-03')
        result=await LayeredRecall(self.plugin,self.qq,self.event).run('昨天聊了什么')
        self.assertEqual([r['id'] for r in result['matches']],['obs:'+str(target)])
        self.assertEqual(result['query_plan']['time_scope'],'message_recorded_at')

    async def test_hour_filter_is_applied_before_top_k(self):
        target=self.obs('长背景。'*100+'冷杉讨论决定向东航行',day='2026-11-02',hour=20)
        for i in range(160):self.obs('冷杉讨论 '+str(i),day='2026-11-02',hour=10)
        result=await LayeredRecall(self.plugin,self.qq,self.event).run('2026年11月2日晚上讨论冷杉')
        self.assertEqual([r['id'] for r in result['matches']],['obs:'+str(target)])

    async def test_date_constraints_revalidate_semantic_candidates(self):
        old=self.obs('芦苇船只去了西岸',day='2026-11-01')
        self.plugin.semantic.recall.return_value=['obs:'+str(old)]
        result=await LayeredRecall(self.plugin,self.qq,self.event).run('昨天原话芦苇船只')
        self.assertFalse(result['matches'])

    async def test_bilateral_lookup_reserves_both_named_sides(self):
        a=self.fact('枫桥方案乘火车','枫桥.交通')
        for i in range(8):self.fact('枫桥方案乘火车沿途看山 '+str(i),'枫桥.杂项'+str(i))
        b=self.fact('松湾方案坐轮船','松湾.交通')
        result=await LayeredRecall(self.plugin,self.qq,self.event).run('枫桥方案和松湾方案有什么区别？')
        self.assertIn(b,[r['id'] for r in result['matches']])
        self.assertTrue(any('枫桥' in r.get('text','') for r in result['matches']))
        self.assertEqual(result['retrieval']['layer'],'comparison_candidates')
        self.assertLessEqual(len(json.dumps(result,ensure_ascii=False,separators=(',',':')).encode()),1800)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM recall_routes').fetchone()[0],1)

    async def test_bilateral_query_does_not_double_provider_budget(self):
        self.obs('只有普通闲聊')
        await LayeredRecall(self.plugin,self.qq,self.event).run('铃杉方案和雾角方案有什么区别')
        self.assertLessEqual(self.plugin.semantic.recall.await_count,2)
        self.assertLessEqual(self.plugin.expand_query.await_count,1)

    async def test_shared_generic_suffix_does_not_replace_the_other_named_side(self):
        a=self.fact('雾桥行程准备骑车','雾桥.出行')
        other=self.obs('风岛行程准备步行')
        result=await LayeredRecall(self.plugin,self.qq,self.event).run('雾桥行程和风岛行程有什么区别')
        self.assertIn(a,[r['id'] for r in result['matches']])
        self.assertIn('obs:'+str(other),[r['id'] for r in result['matches']])

    async def test_sleep_during_comparison_stops_next_stage(self):
        async def sleep(*args,**kw):self.sleep=True;return []
        self.plugin.semantic.recall.side_effect=sleep
        with self.assertRaises(ValueError):await LayeredRecall(self.plugin,self.qq,self.event).run('铃杉方案和雾角方案有什么区别')
        self.assertEqual(self.plugin.semantic.recall.await_count,1)

    async def test_comparison_does_not_widen_fact_category_to_raw_archive(self):
        self.obs('枫桥方案乘火车');self.obs('松湾方案坐轮船')
        result=await LayeredRecall(self.plugin,self.qq,self.event).run('枫桥方案和松湾方案有什么区别',category='facts')
        self.assertFalse(result['matches']);self.assertFalse(result['retrieval']['archive_searched'])

    async def test_time_only_does_not_cross_person(self):
        self.obs('第三人的私人内容',qq='87654321')
        result=await LayeredRecall(self.plugin,self.qq,self.event).run('昨天聊了什么')
        self.assertFalse(result['matches'])

    async def test_switch_off_keeps_old_route(self):
        self.obs('蓝港讨论通过了')
        self.plugin.config['recall_query_planning']=False
        result=await LayeredRecall(self.plugin,self.qq,self.event).run('蓝港讨论')
        self.assertNotIn('query_plan',result)

    async def test_changed_source_cannot_reenter_via_automatic_recent_context(self):
        source=self.obs('海棠装饰使用白纸')
        self.store.put_entry(self.qq,'fact','海棠.装饰','海棠装饰使用白纸',[source])
        with self.store.db:self.store.db.execute('UPDATE observations SET text=? WHERE id=?',('原话已更正为别的主题',source))
        self.assertNotIn('obs:'+str(source),[r['id'] for r in self.c.last_exchange(self.qq,'另一个话题')])
        self.assertEqual(self.c.read(self.qq,'obs:'+str(source))['text'],'原话已更正为别的主题')

    async def test_new_unlinked_correction_is_still_available(self):
        source=self.obs('刚才说错了，海棠装饰应该用白纸')
        self.assertIn('obs:'+str(source),[r['id'] for r in self.c.last_exchange(self.qq,'海棠装饰')])


if __name__=='__main__':unittest.main()
