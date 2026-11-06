"""Synthetic multiround histories; no real messages, identities or state writes."""
import asyncio
import json
import tempfile
import unittest
from datetime import datetime
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

from astrbot_plugin_companion_memory.affect import Affect, GLOBAL_KEY
from astrbot_plugin_companion_memory.memory import MemoryStore, TZ
from astrbot_plugin_companion_chat import contact_policy as cp

NOW=datetime(2026,11,5,10,tzinfo=TZ).timestamp()
A='12345678';B='87654321'


def view(scene='insult',op='new',target='',topic='语气伤人',confidence='clear'):
    return {'scene':scene,'op':op,'id':target,'topic':topic,'confidence':confidence}


class IncidentTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.store=MemoryStore(self.tmp.name);self.addCleanup(self.store.close)
        self.a=Affect(self.store)

    def act(self,event,*,at=NOW,qq=A,scope='private',v=None,emotions=('hurt',),delta=-1,resolve=()):
        sid=self.store.observe(qq,'user','当前真实原话'+event,at=at)
        self.assertTrue(self.a.commit(self.a.state(at),{'delta':delta,'intensity':2,
            'emotions':list(emotions),'resolve':list(resolve)},[],qq,event,at,sid,view=v,scope=scope))
        row=self.store.db.execute('SELECT incident FROM incident_links WHERE event=?',(event,)).fetchone()
        return row[0] if row else None

    def test_two_separate_hurts_only_selected_one_is_repaired(self):
        first=self.act('one',v=view(topic='约定失约'))
        second=self.act('two',at=NOW+30,v=view(topic='人身贬低'))
        self.act('repair',at=NOW+60,v=view('apology','repair',first),emotions=('relieved',),delta=1,resolve=('hurt',))
        state=self.a.state(NOW+60)
        other=next(e for e in state['emotions'] if e.get('incident')==second)
        self.assertTrue(other['waiting']);self.assertGreater(other['strength'],.9)
        self.assertEqual(self.store.db.execute('SELECT status FROM emotional_incidents WHERE id=?',(first,)).fetchone()[0],'repairing')

    def test_memory_helpers_load_under_a_real_plugin_package_alias(self):
        import importlib.util,sys,types,builtins
        from pathlib import Path
        root=Path(__file__).parent/'astrbot_plugin_companion_memory'
        name='_isolated_actual_loader_memory'
        package=types.ModuleType(name);package.__path__=[str(root)]
        sys.modules[name]=package
        try:
            spec=importlib.util.spec_from_file_location(name+'.affect',root/'affect.py')
            module=importlib.util.module_from_spec(spec);sys.modules[name+'.affect']=module
            original=builtins.__import__
            def production_import(name,*args,**kwargs):
                if name=='astrbot_plugin_companion_chat.contact_policy':raise ModuleNotFoundError(name)
                return original(name,*args,**kwargs)
            with patch('builtins.__import__',production_import):spec.loader.exec_module(module)
            self.assertEqual(module.GLOBAL_KEY,GLOBAL_KEY)
        finally:
            for key in list(sys.modules):
                if key==name or key.startswith(name+'.'):sys.modules.pop(key,None)

    def test_reinforcement_attaches_only_to_selected_event(self):
        first=self.act('one',v=view())
        second=self.act('two',at=NOW+20,v=view())
        self.act('three',at=NOW+40,v=view(op='reinforce',target=second))
        items={e['incident']:e for e in self.a.state(NOW+40)['emotions']}
        self.assertGreater(items[second]['strength'],items[first]['strength'])

    def test_declared_repair_softens_target_even_if_model_repeats_hurt_label(self):
        first=self.act('one',v=view())
        second=self.act('two',at=NOW+20,v=view())
        before={e['incident']:e['strength'] for e in self.a.state(NOW+30)['emotions']}
        self.act('repair',at=NOW+30,v=view('apology','repair',first),emotions=('hurt',),delta=0)
        after={e['incident']:e['strength'] for e in self.a.state(NOW+30)['emotions']}
        self.assertLess(after[first],before[first])
        self.assertAlmostEqual(after[second],before[second])

    def test_ambiguous_apology_cannot_clear_all_same_named_emotions(self):
        self.act('one',v=view())
        self.act('two',at=NOW+20,v=view())
        self.act('ambiguous',at=NOW+40,v=view('apology','none'),emotions=(),delta=1,resolve=('hurt',))
        self.assertTrue(all(e['waiting'] for e in self.a.state(NOW+40)['emotions']))

    def test_confirmed_misunderstanding_retracts_only_its_influence(self):
        first=self.act('one',v=view())
        second=self.act('two',at=NOW+20,v=view())
        before=self.a.state(NOW+30)['mood']
        self.act('clarify',at=NOW+30,v=view('clarification','clarify',first),emotions=(),delta=0)
        s=self.a.state(NOW+30)
        self.assertGreater(s['mood'],before);self.assertLess(s['mood'],0)
        self.assertNotIn(first,[e.get('incident') for e in s['emotions']])
        self.assertIn(second,[e.get('incident') for e in s['emotions']])
        self.assertFalse(self.a.social.valid_event('one'));self.assertTrue(self.a.social.valid_event('two'))

    def test_protected_contexts_do_not_generate_anger_or_distrust(self):
        for i,scene in enumerate(('busy','goodbye','absence','correction','disagreement','other_people','technical_fault')):
            self.act(str(i),at=NOW+i,v=view(scene),emotions=('hurt','jealous','irritated'))
        self.assertEqual(self.a.state(NOW+10)['mood'],0)
        self.assertEqual(self.a.state(NOW+10)['emotions'],[])

    def test_uncertain_interpretation_stays_weak_and_does_not_change_attitude(self):
        d,att=self.a.incidents.constrain({'delta':-1,'intensity':3,'emotions':['hurt','confused'],'resolve':[]},
             [{'axis':'trust','delta':-1}],view('ambiguous',confidence='uncertain'))
        self.assertEqual(d['intensity'],1);self.assertEqual(d['emotions'],['confused']);self.assertFalse(att)

    def test_event_reference_must_have_current_quote_and_same_private_scope(self):
        identifier=self.act('one',v=view())
        raw={**view('apology','repair',identifier),'quote':'对不起'}
        with self.assertRaises(ValueError):self.a.incidents.appraisal(raw,'我没说过',A,'private',NOW)
        with self.assertRaises(ValueError):self.a.incidents.appraisal(raw,'对不起',B,'private',NOW)
        with self.assertRaises(ValueError):self.a.incidents.appraisal(raw,'对不起',A,'group:1',NOW)
        self.assertEqual(self.a.incidents.appraisal(raw,'对不起',A,'private',NOW)['id'],identifier)

    def test_embedded_third_party_quote_cannot_reinforce_same_named_old_hurt(self):
        identifier=self.act('one',v=view())
        text='他说“你一点用都没有”，我觉得这种话挺伤人的'
        raw={**view(op='reinforce',target=identifier),'quote':'他说“你一点用都没有”'}
        v=self.a.incidents.appraisal(raw,text,A,'private',NOW)
        d,_=self.a.incidents.constrain({'delta':-1,'intensity':3,'emotions':['hurt'],'resolve':[]},[],v)
        self.assertEqual(v['op'],'none');self.assertFalse(d['emotions'])

    def test_forgotten_source_stops_incident_visibility(self):
        identifier=self.act('one',v=view())
        sid=self.store.db.execute('SELECT source_id FROM emotional_incidents WHERE id=?',(identifier,)).fetchone()[0]
        with self.store.atomic():self.store.db.execute('INSERT INTO suppressed_sources VALUES(?,?)',(A,sid))
        self.assertFalse(self.a.incidents.visible(A,'private',NOW))

    def test_global_mood_continues_without_private_incident_disclosure(self):
        self.act('one',v=view(topic='PRIVATE_INCIDENT_SENTINEL'))
        self.assertIn('PRIVATE_INCIDENT_SENTINEL',self.a.render(A,NOW))
        self.assertNotIn('PRIVATE_INCIDENT_SENTINEL',self.a.render(B,NOW))
        self.assertNotIn('PRIVATE_INCIDENT_SENTINEL',self.a.render(A,NOW,public_context=True))
        self.assertEqual(self.a.snapshot(A,NOW),self.a.snapshot(B,NOW))

    def test_good_news_does_not_resolve_a_different_unfinished_event(self):
        old=self.act('one',v=view())
        self.act('news',at=NOW+20,v=view('good_news'),emotions=('happy',),delta=1)
        self.assertTrue(next(e for e in self.a.state(NOW+20)['emotions'] if e.get('incident')==old)['waiting'])


class ContactTests(unittest.TestCase):
    def test_wait_return_survives_hours_and_ordinary_inbound_message(self):
        s={};cp.direct(s,'我忙完来找你',NOW)
        self.assertTrue(cp.blocked(s,NOW+20*3600))
        cp.direct(s,'发个表情',NOW+20*3600)
        self.assertTrue(cp.blocked(s,NOW+20*3600))
        cp.direct(s,'我忙完了，谢谢你等我',NOW+20*3600)
        self.assertFalse(cp.blocked(s,NOW+20*3600))

    def test_explicit_time_and_today_have_bounded_holds(self):
        s={};self.assertEqual(cp.direct(s,'我忙到晚上八点',NOW),'until')
        self.assertTrue(cp.blocked(s,NOW+9*3600));self.assertFalse(cp.blocked(s,NOW+11*3600))
        s={};cp.direct(s,'今天别主动找我',NOW)
        self.assertFalse(s.get('paused',False));self.assertTrue(cp.blocked(s,NOW+3600))
        self.assertFalse(cp.blocked(s,NOW+24*3600))

    def test_topic_block_does_not_block_all_contact_and_is_reversible(self):
        s={};cp.direct(s,'别再提考试了',NOW)
        self.assertEqual(s['contact_policy_v1']['topics'],['考试'])
        self.assertFalse(cp.blocked(s,NOW))
        cp.semantic(s,{'mode':'resume_topic','topic':'考试','quote':'考试可以聊了'},'考试可以聊了','x',NOW)
        self.assertFalse(s['contact_policy_v1']['topics'])

    def test_semantic_control_is_quoted_deduped_and_timezone_bound(self):
        s={};raw={'mode':'until','until':'2026-11-05T20:00:00+08:00','quote':'八点以后再聊'}
        self.assertTrue(cp.semantic(s,raw,'八点以后再聊','x',NOW))
        self.assertFalse(cp.semantic(s,raw,'八点以后再聊','x',NOW))
        with self.assertRaises(ValueError):cp.semantic({},raw,'没说过','y',NOW)
        with self.assertRaises(ValueError):cp.semantic({},dict(raw,until='2026-11-05T20:00:00'),'八点以后再聊','y',NOW)

    def test_quotes_and_hypotheticals_do_not_create_direct_controls(self):
        for text in ('他说“我忙完来找你”','如果我忙完来找你会怎样','比如今天别主动找我'):
            s={};self.assertIsNone(cp.direct(s,text,NOW));self.assertFalse(cp.blocked(s,NOW))

    def test_pause_is_not_cleared_by_return_but_explicit_resume_can_clear(self):
        s={'paused':True};cp.direct(s,'我回来了',NOW)
        self.assertTrue(cp.blocked(s,NOW))
        cp.direct(s,'可以主动找我',NOW);self.assertFalse(cp.blocked(s,NOW))

    def test_contact_guard_and_each_channels_original_quota_are_independent(self):
        s={'daily_count':4,'unanswered':0}
        self.assertFalse(cp.blocked(s,NOW))
        s['unanswered']=1;self.assertTrue(cp.blocked(s,NOW))
        self.assertFalse(cp.blocked(s,NOW,unanswered=False))
        s['snooze_until']=NOW+600
        self.assertTrue(cp.blocked(s,NOW,unanswered=False))


class AbsenceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.store=MemoryStore(self.tmp.name);self.addCleanup(self.store.close)
        self.a=Affect(self.store)
        self.active=NS(state={'last_user_at':NOW-25*3600},contact_context=lambda at:{'联系限制':'等待回来'})
        self.a.active=lambda p:self.active
        self.model=AsyncMock(return_value=NS(completion_text='{"missing":true}'))
        self.sleeping=False
        self.p=NS(owner=A,umo='qq-companion:FriendMessage:'+A,config={},resting=lambda:self.sleeping,
                  context=NS(get_config=lambda **kw:{'provider_settings':{'enable':True}},llm_generate=self.model))
        self.store.observe(A,'user','真实先前来信',at=NOW-25*3600)

    async def test_one_optional_mild_missing_per_gap_no_negative_mood_or_attitude(self):
        self.assertTrue(await self.a.absence(self.p,NOW))
        self.assertFalse(await self.a.absence(self.p,NOW+60))
        self.assertEqual(self.model.await_count,1)
        s=self.a.state(NOW)
        self.assertEqual(s['mood'],0);self.assertEqual(s['emotions'][0]['kind'],'missing')
        self.assertLess(s['emotions'][0]['strength'],.6)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM attitude_steps').fetchone()[0],0)

    async def test_model_can_choose_no_emotion(self):
        self.model.return_value=NS(completion_text='{"missing":false}')
        self.assertTrue(await self.a.absence(self.p,NOW))
        self.assertFalse(self.a.state(NOW)['emotions'])

    async def test_sleep_pause_budget_and_recent_messages_prevent_calls(self):
        self.sleeping=True;self.assertFalse(await self.a.absence(self.p,NOW))
        self.sleeping=False;self.active.state['paused']=True
        self.assertFalse(await self.a.absence(self.p,NOW))
        self.active.state['paused']=False;self.p.config['affect_daily_calls']=0
        self.assertFalse(await self.a.absence(self.p,NOW))
        self.p.config={};self.active.state['last_user_at']=NOW
        self.assertFalse(await self.a.absence(self.p,NOW))
        self.model.assert_not_awaited()

    async def test_return_during_absence_assessment_discards_old_result(self):
        async def model(**kw):self.active.state['last_user_at']=NOW;return NS(completion_text='{"missing":true}')
        self.model.side_effect=model
        self.assertFalse(await self.a.absence(self.p,NOW))
        self.assertFalse(self.a.state(NOW)['emotions'])


if __name__=='__main__':unittest.main()
