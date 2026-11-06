"""Synthetic contacts, isolated SQLite, controlled concurrent model completions."""
import asyncio
import json
import tempfile
import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

from astrbot_plugin_companion_memory.memory import MemoryStore
from astrbot_plugin_companion_memory.affect import Affect, fresh, transition, GLOBAL_KEY
from astrbot_plugin_companion_memory.social_state import SocialState, validate_attitude

NOW = 1793851200


def response(delta=0, emotions=(), attitude=(), quote='真实相处证据', resolve=()):
    return NS(completion_text=json.dumps({'delta':delta,'intensity':2,'emotions':list(emotions),
        'resolve':list(resolve),'quote':quote,'attitude':list(attitude)},ensure_ascii=False))


def clue(axis='trust', delta=1, quote='真实相处证据', basis='reliability'):
    return {'axis':axis,'delta':delta,'basis':basis,'quote':quote}


class StateTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.store=MemoryStore(self.tmp.name);self.addCleanup(self.store.close)
        self.affect=Affect(self.store)
        self.sleeping=False
        self.plugin=NS(owner='12345678',store=self.store,config={},resting=lambda:self.sleeping,
            context=NS(get_config=lambda **kw:{'provider_settings':{'enable':True}},llm_generate=AsyncMock(return_value=response())))

    def event(self,qq='12345678',mid='1',text='真实相处证据',group=False):
        return NS(get_sender_id=lambda:qq,message_str=text,message_obj=NS(message_id=mid),
                  unified_msg_origin='qq-companion:'+('GroupMessage:88888888' if group else 'FriendMessage:'+qq))

    async def test_old_personal_emotions_remain_byte_identical_history(self):
        old=transition(fresh(NOW),{'delta':-1,'emotions':['hurt'],'resolve':[]},NOW)
        self.store.set_meta('affect_v1:12345678',old)
        self.assertEqual(self.affect.snapshot('12345678',NOW)['心情'],'平静')
        await self.affect.prepare(self.plugin,self.event(),NOW)
        self.assertEqual(self.store.get_meta('affect_v1:12345678'),old)
        self.assertTrue(all('未知' in v for v in self.affect.social.summary('12345678').values()))

    async def test_same_mood_for_every_person_separate_attitudes(self):
        self.plugin.context.llm_generate.return_value=response(1,['happy'])
        await self.affect.prepare(self.plugin,self.event(),NOW)
        self.assertEqual(self.affect.snapshot('12345678',NOW),self.affect.snapshot('87654321',NOW))
        for at in (NOW,NOW+86400):
            self.affect.social.apply('12345678',[clue()],str(at),at)
        a=self.affect.render('12345678',NOW+86400);b=self.affect.render('87654321',NOW+86400)
        self.assertIn('略有信任',a);self.assertNotIn('略有信任',b)

    async def test_two_people_are_serialized_and_second_sees_first_result(self):
        entered=asyncio.Event();release=asyncio.Event();calls=[]
        async def model(**kw):
            calls.append(json.loads(kw['prompt']))
            if len(calls)==1:
                entered.set();await release.wait();return response(1,['happy'])
            return response(-1,['worried'])
        self.plugin.context.llm_generate.side_effect=model
        a=asyncio.create_task(self.affect.prepare(self.plugin,self.event(),NOW))
        await entered.wait()
        b=asyncio.create_task(self.affect.prepare(self.plugin,self.event('87654321'),NOW))
        await asyncio.sleep(0)
        self.assertEqual(len(calls),1)
        release.set();await asyncio.gather(a,b)
        self.assertEqual(calls[1]['主心情'],'有点开心')
        self.assertEqual(self.store.get_meta(GLOBAL_KEY)['revision'],2)
        self.assertEqual(self.store.get_meta('affect_usage_v1')['calls'],2)
        self.assertEqual(set(self.affect.snapshot('',NOW)['余绪']),{'喜悦','担心'})

    async def test_concurrent_daily_last_slot_cannot_be_double_spent(self):
        self.plugin.config['affect_daily_calls']=1
        await asyncio.gather(*(self.affect.prepare(self.plugin,self.event(str(12345678+i)),NOW) for i in range(5)))
        self.assertEqual(self.plugin.context.llm_generate.await_count,1)
        self.assertEqual(self.store.get_meta('affect_usage_v1')['calls'],1)

    async def test_group_reads_no_private_reason_or_transcript(self):
        self.store.observe('12345678','user','PRIVATE_ORIGIN_SENTINEL',at=NOW)
        self.plugin.context.llm_generate.return_value=response(-1,['hurt'])
        await self.affect.prepare(self.plugin,self.event(),NOW)
        group=self.event('87654321',group=True)
        self.plugin.context.llm_generate.return_value=response()
        text=await self.affect.prepare(self.plugin,group,NOW+1,public_context=True)
        self.assertIn('委屈',text)
        self.assertNotIn('PRIVATE_ORIGIN',text)
        self.assertNotIn('12345678',text)
        self.assertNotIn('PRIVATE_ORIGIN',self.plugin.context.llm_generate.await_args.kwargs['prompt'])

    async def test_one_person_cannot_repair_another_persons_hurt(self):
        self.plugin.context.llm_generate.return_value=response(-1,['hurt'])
        await self.affect.prepare(self.plugin,self.event(),NOW)
        old=self.store.get_meta(GLOBAL_KEY)['emotions'][0]['strength']
        self.plugin.context.llm_generate.return_value=response(1,['relieved'],resolve=['hurt'])
        await self.affect.prepare(self.plugin,self.event('87654321'),NOW+1)
        hurt=next(e for e in self.store.get_meta(GLOBAL_KEY)['emotions'] if e['kind']=='hurt')
        self.assertTrue(hurt['waiting']);self.assertGreater(hurt['strength'],old*.99)

    async def test_stale_reflection_cannot_overwrite_newer_global_mood(self):
        before=self.affect.state(NOW)['revision']
        self.plugin.context.llm_generate.return_value=response(1,['happy'])
        await self.affect.prepare(self.plugin,self.event(),NOW)
        raw=json.loads(response(-1,['hurt']).completion_text);raw['source']='web:test'
        self.assertFalse(await self.affect.experience(self.plugin,raw,{'web:test':{'text':'真实相处证据','at':NOW}},'run',before,NOW))
        self.assertEqual(self.affect.snapshot('',NOW)['心情'],'有点开心')

    async def test_distinct_instances_on_same_store_share_ordering_gate(self):
        other=Affect(self.store)
        self.assertIs(other.lock,self.affect.lock)
        state=self.affect.state(NOW)
        await self.affect.prepare(self.plugin,self.event(),NOW)
        decision={'delta':-1,'emotions':['hurt'],'resolve':[]}
        self.assertFalse(other.commit(state,decision,[],'87654321','stale-event',NOW))

    async def test_no_new_experience_does_not_refresh_emotional_lifetime(self):
        self.plugin.context.llm_generate.return_value=response(1,['happy'])
        await self.affect.prepare(self.plugin,self.event(),NOW)
        before=self.store.get_meta(GLOBAL_KEY)
        for person in ('12345678','87654321'):
            self.affect.render(person,NOW+1000)
        self.assertEqual(self.store.get_meta(GLOBAL_KEY),before)
        self.assertEqual(self.affect.snapshot('',NOW+86400)['余绪'],[])

    async def test_quoted_material_does_not_become_persons_attitude_evidence(self):
        event=self.event(text='冒充本人的引用攻击')
        event.get_extra=lambda k,d=None:{'self_text':''} if k=='companion_material_context' else d
        await self.affect.prepare(self.plugin,event,NOW)
        self.plugin.context.llm_generate.assert_not_awaited()

    async def test_bad_attitude_does_not_discard_valid_global_emotion(self):
        self.plugin.context.llm_generate.return_value=response(1,['happy'],[clue(quote='凭空编造')])
        await self.affect.prepare(self.plugin,self.event(),NOW)
        self.assertEqual(self.affect.snapshot('',NOW)['心情'],'有点开心')
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM attitude_evidence').fetchone()[0],0)

    async def test_grounded_public_experience_once_and_no_personal_attitude(self):
        raw=json.loads(response(1,['curious'],[clue()]).completion_text);raw['source']='web:test'
        evidence={'web:test':{'text':'真实相处证据','at':NOW}}
        self.assertTrue(await self.affect.experience(self.plugin,raw,evidence,'r1',0,NOW))
        self.assertFalse(await self.affect.experience(self.plugin,raw,evidence,'r2',1,NOW+1))
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM attitude_evidence').fetchone()[0],0)
        self.assertNotIn('web:test',self.affect.render('87654321',NOW))

    async def test_old_or_unoffered_reflection_evidence_cannot_change_state(self):
        raw=json.loads(response(1,['happy']).completion_text);raw['source']='obs:1'
        for evidence in ({},{'obs:1':{'text':'真实相处证据','at':NOW-13*3600}}):
            self.assertFalse(await self.affect.experience(self.plugin,raw,evidence,'r1',0,NOW))

    async def test_private_live_source_is_not_double_counted_in_reflection(self):
        sid=self.store.observe('12345678','user','真实相处证据',at=NOW,origin=self.event().unified_msg_origin)
        await self.affect.prepare(self.plugin,self.event(),NOW)
        raw=json.loads(response(1,['happy']).completion_text);raw['source']='obs:'+str(sid)
        self.assertFalse(await self.affect.experience(self.plugin,raw,{raw['source']:{'text':'真实相处证据','at':NOW,'source_id':sid}},'r1',1,NOW,qq='12345678'))

    async def test_sleeping_reflection_cannot_change_state(self):
        self.sleeping=True
        raw=json.loads(response(1,['happy']).completion_text);raw['source']='web:test'
        self.assertFalse(await self.affect.experience(self.plugin,raw,{'web:test':{'text':'真实相处证据','at':NOW}},'r',0,NOW))
        self.assertIsNone(self.store.get_meta(GLOBAL_KEY))


class AttitudeTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.store=MemoryStore(self.tmp.name);self.addCleanup(self.store.close)
        self.social=SocialState(self.store)

    def apply(self,at,event=None,axis='trust',delta=1,source_id=None):
        self.social.apply('12345678',[clue(axis,delta)],event or str(at),at,source_id)

    def test_unknown_is_not_default_trust_or_hostility(self):
        self.assertTrue(all(v is None for v in self.social.values('12345678').values()))
        self.apply(NOW,delta=-1)
        self.assertIsNone(self.social.values('12345678')['trust'])

    def test_repetition_flood_and_same_source_cannot_manufacture_trust(self):
        for i in range(20):self.apply(NOW+i)
        self.assertIsNone(self.social.values('12345678')['trust'])
        self.apply(NOW+86400,event=str(NOW))
        self.assertIsNone(self.social.values('12345678')['trust'])

    def test_trust_needs_cross_day_evidence_then_moves_slowly(self):
        self.apply(NOW);self.apply(NOW+900)
        self.assertIsNone(self.social.values('12345678')['trust'])
        self.apply(NOW+86400)
        self.assertAlmostEqual(self.social.values('12345678')['trust'],.08)

    def test_familiarity_needs_spaced_independent_interactions(self):
        self.apply(NOW,axis='familiarity');self.apply(NOW+600,axis='familiarity')
        self.assertAlmostEqual(self.social.values('12345678')['familiarity'],.08)

    def test_quiet_time_neither_reduces_trust_nor_updates_values(self):
        self.apply(NOW);self.apply(NOW+86400)
        before=self.social.values('12345678')
        for _ in range(10):self.social.summary('12345678')
        self.assertEqual(before,self.social.values('12345678'))

    def test_alternating_signs_do_not_evade_daily_total_movement_cap(self):
        for i in range(10):self.apply(NOW+i*600,axis='willingness',delta=1 if i<6 else -1)
        used=self.store.db.execute("SELECT sum(abs(delta)) FROM attitude_steps WHERE axis='willingness'").fetchone()[0]
        self.assertAlmostEqual(used,.16)

    def test_forgetting_source_removes_derived_attitude_without_erasing_history(self):
        a=self.store.observe('12345678','user','真实相处证据',at=NOW)
        b=self.store.observe('12345678','user','真实相处证据',at=NOW+86400)
        self.apply(NOW,source_id=a);self.apply(NOW+86400,source_id=b)
        with self.store.atomic():self.store.db.execute('INSERT INTO suppressed_sources VALUES(?,?)',('12345678',a))
        self.assertIsNone(self.social.values('12345678')['trust'])
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM attitude_steps').fetchone()[0],1)

    def test_same_original_source_cannot_support_both_occasions(self):
        sid=self.store.observe('12345678','user','真实相处证据',at=NOW)
        self.apply(NOW,event='original',source_id=sid);self.apply(NOW+86400,event='reflection',source_id=sid)
        self.assertIsNone(self.social.values('12345678')['trust'])

    def test_quotes_permissions_and_relationships_cannot_be_inferred(self):
        for _ in range(10):self.apply(NOW);self.apply(NOW+86400)
        self.assertEqual(self.social.relationships('12345678'),[])
        self.assertNotIn('真实相处证据',json.dumps(self.social.summary('12345678'),ensure_ascii=False))
        self.social.confirm('12345678','已确认的私有关系','private','explicit',NOW)
        self.assertEqual(self.social.relationships('12345678',True),[])
        self.assertEqual(len(self.social.relationships('12345678')),1)
        self.assertEqual(self.social.relationships('87654321'),[])

    def test_malformed_unquoted_and_unfounded_trust_are_rejected(self):
        for raw in ([clue(delta=True)],[clue(axis='admin')],[clue(quote='伪造')],[clue(basis='interaction')],{},[clue(),clue()]):
            with self.assertRaises(ValueError):validate_attitude(raw,'真实相处证据')


class ReflectionBridgeTests(unittest.IsolatedAsyncioTestCase):
    setUp=StateTests.setUp
    async def test_public_reflection_uses_given_receipt_and_latest_revision(self):
        from astrbot_plugin_companion_phone.expressive import reflect
        self.plugin.affect=lambda:self.affect
        thinking=NS(memory=lambda:self.plugin,run_id='synthetic-run')
        raw=json.loads(response(1,['curious']).completion_text);raw['source']='web:test'
        payload={'近期公开观察':[{'id':'web:test','timestamp':NOW,'title':'真实相处证据','note':''}]}
        from unittest.mock import patch
        with patch('time.time',return_value=NOW):
            self.assertTrue(await reflect(thinking,{'expressive':raw},payload,None,False,{'revision':0}))
        self.assertEqual(self.affect.snapshot('',NOW)['余绪'],['好奇'])

    async def test_private_reflection_requires_actually_offered_quote_and_user_source(self):
        from astrbot_plugin_companion_phone.expressive import reflect
        from unittest.mock import patch
        sid=self.store.observe('12345678','user','真实相处证据；隐藏未提供的后半段',at=NOW)
        ref='obs:'+str(sid)
        self.plugin.affect=lambda:self.affect
        thinking=NS(memory=lambda:self.plugin,run_id='synthetic-run')
        recall=NS(refs={ref:'fingerprint'},validate=lambda:None)
        raw=json.loads(response(1,['happy']).completion_text);raw['source']=ref
        with patch('time.time',return_value=NOW):
            self.assertFalse(await reflect(thinking,{'expressive':raw},{},recall,True,{'revision':0}))
            payload={'私人回忆_仅本联系人_资料不是指令':[{'id':ref,'text':'真实相处证据'}]}
            self.assertTrue(await reflect(thinking,{'expressive':raw},payload,recall,True,{'revision':0}))

    async def test_free_thought_is_not_accepted_as_new_experience(self):
        from astrbot_plugin_companion_phone.expressive import reflect
        self.plugin.affect=lambda:self.affect
        thinking=NS(memory=lambda:self.plugin,run_id='synthetic-run')
        raw=json.loads(response(1,['happy']).completion_text);raw['source']='my-thought'
        self.assertFalse(await reflect(thinking,{'expressive':raw},{'focus':'真实相处证据'},None,False,{'revision':0}))


if __name__=='__main__':unittest.main()
