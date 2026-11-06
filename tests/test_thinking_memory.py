"""Opt-in private reflection and attention; synthetic state, no external execution."""
import asyncio
import json
from datetime import datetime
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock,Mock,patch

import test_thinking as fixture
from test_thinking import NOW
from astrbot_plugin_companion_memory.cognition import Cognition
from astrbot_plugin_companion_memory.attention import rehearse,boost,current,exists
from astrbot_plugin_companion_phone.memory_reflection import PrivateMemory


class MemoryThinkingTests(unittest.IsolatedAsyncioTestCase):
    responses=fixture.ThinkingTests.responses
    thought=fixture.ThinkingTests.thought
    decision=fixture.ThinkingTests.decision

    def setUp(self):
        fixture.ThinkingTests.setUp(self)
        self.cog=Cognition(self.memory.store)
        self.memory.cognition=Mock(return_value=self.cog)
        self.user=self.memory.store.observe('owner','user','PRIVATE_USER_MARKER 我想讨论这次的回复',at=NOW-20)
        self.reply=self.memory.store.observe('owner','assistant','PRIVATE_REPLY_MARKER 我当时回答得太急了',at=NOW-10)
        self.memory.store.observe('other','user','OTHER_CONTACT_SECRET',at=NOW-5)

    def ask(self,action='recent',**kw):return {'memory_request':dict(action=action,**kw)}

    def private_note(self,**kw):
        return self.thought(focus='我想重新想想与这个人的交流',question='我当时是不是太快下判断',
                            growth_wish='我愿意先把对方的话听完再回答',practice='回应前核对自己是否理解了意思',**kw)

    async def test_memory_is_optional_not_preloaded(self):
        self.responses(self.thought(),self.decision())
        await self.a.consider()
        self.memory.cognition.assert_not_called()
        self.assertNotIn('PRIVATE',str(self.context.llm_generate.await_args_list))

    async def test_self_chooses_recent_chat_and_own_reply_then_reflects(self):
        count=0
        async def model(**kw):
            nonlocal count;count+=1
            if count==1:
                self.assertNotIn('PRIVATE',kw['prompt'])
                result=self.ask()
            elif count==2:
                self.assertIn('PRIVATE_REPLY_MARKER',kw['prompt'])
                self.assertIn('PRIVATE_USER_MARKER',kw['prompt'])
                self.assertNotIn('OTHER_CONTACT_SECRET',kw['prompt'])
                result=self.private_note(memory_focus_ids=['obs:'+str(self.reply)])
            else:
                self.assertNotIn('可浏览软件',kw['prompt'])
                result={'activity':'idle'}
            return SimpleNamespace(completion_text=json.dumps(result,ensure_ascii=False))
        self.context.llm_generate.side_effect=model
        await self.a.consider()
        self.assertEqual(count,3)
        self.assertEqual(self.t.store.rows('runs')[0]['activity'],'private_reflection')
        self.assertEqual(self.t.store.rows('runs')[0]['focus'],'')
        with self.t.store.connect() as db:self.assertEqual(db.execute('SELECT count(*) FROM memory_reflections').fetchone()[0],1)
        self.assertTrue(boost(self.memory.store,'owner','obs:'+str(self.reply),NOW)>0)
        self.assertFalse(self.t.store.rows('goals'))
        self.assertFalse(self.t.store.rows('trials'))
        self.assertNotIn('我当时',json.dumps(self.t.public_context(),ensure_ascii=False))
        self.assertIsNone(self.t.store.meta('last_thought'))

    async def test_search_read_then_reflect_with_separate_budget(self):
        ref='obs:'+str(self.reply)
        self.responses(self.ask('search',query='回答得太急',category='conversation'),
                       self.ask('read',identifier=ref),self.private_note(memory_focus_ids=[ref]),{'activity':'idle'})
        await self.a.consider()
        self.assertEqual(self.t.store.rows('runs')[0]['status'],'completed')
        self.assertEqual(self.t.store.rows('runs')[0]['calls'],4)
        self.assertGreater(boost(self.memory.store,'owner',ref,NOW),0)

    async def test_private_reflection_cannot_route_to_public_tools(self):
        self.responses(self.ask(),self.private_note(),{'activity':'read','reading_id':'topic:animals'})
        self.a._read_public=AsyncMock()
        await self.a.consider()
        self.a._read_public.assert_not_called()
        self.assertFalse(self.t.store.rows('trials'))
        self.assertEqual(self.t.store.rows('runs')[0]['status'],'invalid_output')

    async def test_forgetting_during_reflection_discards_derived_result(self):
        count=0
        async def model(**kw):
            nonlocal count;count+=1
            if count==1:result=self.ask()
            else:
                with self.memory.store.atomic():self.memory.store.db.execute('INSERT INTO suppressed_sources VALUES(?,?)',('owner',self.reply))
                result=self.private_note()
            return SimpleNamespace(completion_text=json.dumps(result,ensure_ascii=False))
        self.context.llm_generate.side_effect=model
        await self.a.consider()
        with self.t.store.connect() as db:self.assertEqual(db.execute('SELECT count(*) FROM memory_reflections').fetchone()[0],0)
        self.assertFalse(exists(self.memory.store))

    async def test_forgotten_reflection_not_reinjected(self):
        self.responses(self.ask(),self.private_note(),{'activity':'idle'})
        await self.a.consider()
        self.assertIn('先前对与当前联系人',self.t.private_context())
        with self.memory.store.atomic():self.memory.store.db.execute('INSERT INTO suppressed_sources VALUES(?,?)',('owner',self.reply))
        self.assertEqual(self.t.private_context(),'')

    async def test_unoffered_and_cross_contact_references_cannot_be_read(self):
        recall=PrivateMemory(self.t)
        with self.assertRaises(Exception):recall.request({'action':'read','identifier':'obs:9999'})
        with self.assertRaises(Exception):recall.request({'action':'recent','qq':'other'})
        with self.assertRaises(Exception):recall.request({'action':'forget','identifier':'obs:'+str(self.reply)})

    async def test_growth_intent_from_private_memory_stays_private(self):
        self.responses(self.ask(),self.private_note(),{'activity':'idle','growth_action':'start'})
        await self.a.consider()
        self.assertFalse(self.t.store.rows('trials'))
        self.assertFalse(self.memory.store.entries('owner','growth',now=NOW))
        self.assertIn('我想重新想想',self.t.private_context())
        self.assertNotIn('我愿意',self.t.private_context())

    async def test_private_attention_requires_actually_recalled_reference(self):
        self.responses(self.ask(),self.private_note(memory_focus_ids=['obs:99999']))
        await self.a.consider()
        self.assertFalse(exists(self.memory.store))
        self.assertEqual(self.t.store.rows('runs')[0]['status'],'invalid_output')

    async def test_social_reflection_cannot_request_private_memory(self):
        self.responses(self.ask())
        with self.assertRaises(ValueError):await self.t.social_reflection(lambda:None)
        self.memory.cognition.assert_not_called()

    async def test_memory_disabled_or_sleeping_prevents_recall(self):
        self.memory.config['enabled']=False
        self.responses(self.ask())
        await self.a.consider()
        self.memory.cognition.assert_not_called()
        self.memory.config['enabled']=True
        self.memory.resting=lambda:True
        self.assertFalse(PrivateMemory(self.t).available())

    def test_readonly_recall_bytes_and_calls_are_bounded(self):
        recall=PrivateMemory(self.t)
        for _ in range(3):
            try:recall.request({'action':'recent'})
            except Exception:break
        self.assertLessEqual(recall.bytes,4200)
        with self.assertRaises(Exception):recall.request({'action':'recent'})


class AttentionTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        from astrbot_plugin_companion_memory.memory import MemoryStore
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.store=MemoryStore(self.tmp.name);self.addCleanup(self.store.close)
        self.sid=self.store.observe('owner','user','我喜欢红茶，也想认真讨论问题',at=NOW)
        self.ref='obs:'+str(self.sid)

    def test_attention_decays_and_repeated_rehearsal_slows_loss(self):
        rehearse(self.store,'owner',[self.ref],'one',NOW)
        a=boost(self.store,'owner',self.ref,NOW)
        b=boost(self.store,'owner',self.ref,NOW+14*86400)
        self.assertAlmostEqual(b,a/2)
        rehearse(self.store,'owner',[self.ref],'two',NOW+14*86400)
        self.assertGreater(boost(self.store,'owner',self.ref,NOW+14*86400),a)

    def test_replay_and_tight_loops_cannot_amplify_attention(self):
        rehearse(self.store,'owner',[self.ref],'same',NOW)
        for i in range(20):rehearse(self.store,'owner',[self.ref],'loop'+str(i),NOW+60)
        self.assertAlmostEqual(current(self.store,'owner',self.ref,NOW)['strength'],1)

    def test_scope_and_explicit_forgetting_dominate_rehearsal(self):
        self.assertEqual(rehearse(self.store,'other',[self.ref],'x',NOW),[])
        rehearse(self.store,'owner',[self.ref],'x',NOW)
        entry=self.store.put_entry('owner','fact','tea','我喜欢红茶',[self.sid],now=NOW)
        self.store.forget('owner',entry)
        self.assertEqual(boost(self.store,'owner',self.ref,NOW),0)
        self.assertEqual(rehearse(self.store,'owner',[self.ref],'again',NOW+86400),[])

    def test_no_truth_validity_or_original_content_is_rewritten(self):
        identifier=self.store.put_entry('owner','fact','tea','我喜欢红茶',[self.sid],now=NOW,expires=NOW+100)
        before=self.store.get_entry('owner',identifier)
        rehearse(self.store,'owner',[self.ref],'x',NOW+1)
        self.assertEqual(self.store.get_entry('owner',identifier),before)
        self.assertEqual(self.store.db.execute('SELECT text FROM observations WHERE id=?',(self.sid,)).fetchone()[0],'我喜欢红茶，也想认真讨论问题')

    def test_relevant_rehearsed_memory_gets_bounded_ranking_boost(self):
        a=self.store.put_entry('owner','fact','tea-a','我喜欢红茶',[self.sid],now=NOW)
        sid=self.store.observe('owner','user','我喜欢红茶',at=NOW+1)
        b=self.store.put_entry('owner','fact','tea-b','我喜欢红茶',[sid],now=NOW+1)
        cog=Cognition(self.store)
        with patch('time.time',return_value=NOW+2):
            rehearse(self.store,'owner',[a],'x',NOW+2)
            rows=cog.search('owner','红茶',category='facts')['matches']
        self.assertEqual(rows[0]['id'],a)
        self.assertLessEqual(boost(self.store,'owner',a,NOW+2),.25)
        self.assertEqual(cog.search('owner','完全不相关的宇宙飞船',category='facts')['matches'],[])

    def test_review_aging_can_be_delayed_without_reviving_hard_dates(self):
        item=self.store.put_entry('owner','continuity','discussion','一起讨论问题',[self.sid],now=NOW)
        rehearse(self.store,'owner',[item],'x',NOW+6*86400)
        row=self.store.get_entry('owner',item)
        self.assertTrue(self.store.lifecycle.current(row,NOW+8*86400))
        with patch('time.time',return_value=NOW+8*86400):
            refs=[r['ref'] for r in self.store.index.search('owner',['讨论'],10,current=True)]
        self.assertIn(item,refs)
        self.assertFalse(self.store.lifecycle.current(row,NOW+15*86400))
        dated=self.store.put_entry('owner','fact','today','今天我有点累',[self.sid],now=NOW)
        rehearse(self.store,'owner',[dated],'d',NOW)
        self.assertFalse(self.store.lifecycle.current(self.store.get_entry('owner',dated),NOW+2*86400))

    def test_late_recall_does_not_make_old_impression_current(self):
        item=self.store.put_entry('owner','continuity','discussion','一起讨论问题',[self.sid],now=NOW)
        rehearse(self.store,'owner',[item],'late',NOW+8*86400)
        self.assertFalse(self.store.lifecycle.current(self.store.get_entry('owner',item),NOW+8*86400))

    def test_changed_source_invalidates_old_strength(self):
        rehearse(self.store,'owner',[self.ref],'x',NOW)
        with self.store.atomic():self.store.db.execute('UPDATE observations SET text=? WHERE id=?',('更正后的原话',self.sid))
        self.assertEqual(boost(self.store,'owner',self.ref,NOW),0)


if __name__=='__main__':unittest.main()
