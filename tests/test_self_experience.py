"""Owned experience reaches chat/checker/diary without fabricated perception."""
from datetime import datetime, timedelta
import hashlib
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, Mock

import test_thinking as fixtures
import test_brain_phase2 as brain_fixtures
from test_thinking import NOW, SCOPE
from astrbot_plugin_companion_phone.self_experience import SelfExperience, TZ
from astrbot_plugin_companion_phone.activity import public_source
from astrbot_plugin_companion_phone.books import Books
from astrbot_plugin_companion_phone.main import CompanionPhone
from astrbot_plugin_companion_phone.process_trace import record
from astrbot_plugin_companion_phone.thinking import bounded, THINK
from astrbot_plugin_companion_chat import cognitive_context, grounding


class ExperienceTests(unittest.IsolatedAsyncioTestCase):
    setUp = fixtures.ThinkingTests.setUp

    def reader(self, scope=SCOPE):
        return SelfExperience(self.root, scope)

    def program(self, at=NOW, scope=SCOPE, identifier='a'*32, status='succeeded', bad_hash=False):
        code='print("PRIVATE_STDOUT_SENTINEL")'
        digest=hashlib.sha256(code.encode()).hexdigest()
        receipt={'kind':'program_execution_not_fact_verification','id':'practice:'+identifier,'code_hash':digest,
                 'status':status,'exit_code':0 if status=='succeeded' else 1,
                 'stdout':'PRIVATE_STDOUT_SENTINEL 第一反应想看','stderr':'',
                 'artifacts':[{'id':'practice:'+identifier+':plot:0','format':'png'}]}
        with self.t.store.connect() as db:
            db.execute('INSERT INTO autonomy_practice_runs VALUES(?,?,?,?,?,?,?,?,?,?)',
                       (scope,identifier,'b'*32,at,'隔天回看练习',code,'bad' if bad_hash else digest,status,json.dumps(receipt),''))

    def thought(self, private=False, at=NOW, focus='想回头看看折线'):
        run=self.t.store.begin(at)
        self.t.store.update_run(run,status='completed',activity='private_reflection' if private else 'idle')
        record(self.t.store,run,'thought',{'focus':focus},private=private)
        return run

    def test_verified_dated_execution_not_stdout_or_visual_perception(self):
        self.program();snapshot=self.reader().snapshot()
        self.assertEqual(snapshot['program_days']['2026-11-05']['verified_successes'],1)
        row=snapshot['programs'][0]
        self.assertTrue(row['program_executed']);self.assertEqual(row['generated_plot_files'],1)
        self.assertEqual(row['at'][:10],'2026-11-05')
        self.assertEqual(snapshot['first_saved_program_attempt_at'][:10],'2026-11-05')
        serialized=self.reader().context()
        self.assertNotIn('PRIVATE_STDOUT_SENTINEL',serialized)
        self.assertIn('生成图文件不证明自己看过图',serialized)
        self.assertIn('可选择不谈',serialized)

    def test_other_scope_future_and_invalid_scope_are_not_read(self):
        self.program(at=NOW+60)
        self.program(scope='other:FriendMessage:friend',identifier='c'*32)
        self.assertEqual(self.reader().snapshot(),{})
        self.assertEqual(self.reader('invalid').snapshot(),{})

    def test_failed_unknown_and_tampered_receipts_are_not_success(self):
        self.program(bad_hash=True)
        self.program(identifier='c'*32,status='execution_error')
        self.program(identifier='d'*32,status='interrupted_unknown')
        rows=self.reader().snapshot()['programs']
        self.assertTrue(all(not row['program_executed'] for row in rows))
        self.assertEqual({r['status'] for r in rows},{'unverified','execution_error','interrupted_unknown'})

    def test_public_short_thought_and_private_metadata_are_separate(self):
        self.thought();self.thought(private=True,focus='PRIVATE_THOUGHT_SENTINEL')
        snapshot=self.reader().snapshot()
        self.assertEqual(len(snapshot['thoughts']),1)
        self.assertEqual(snapshot['completed_private_reflections'],1)
        self.assertNotIn('PRIVATE_THOUGHT_SENTINEL',json.dumps(snapshot))
        sources=self.reader().diary_sources(NOW-86400,NOW)
        private=[s for s in sources if not s['public_content']]
        self.assertEqual(len(private),1);self.assertFalse(public_source(private[0]))
        self.assertTrue(public_source(next(s for s in sources if s['public_content'])))

    def test_read_is_bounded_and_does_not_modify_database(self):
        for i in range(20):self.program(identifier=format(i,'032x'))
        self.thought()
        before=self.t.store.path.read_bytes()
        snapshot=self.reader().snapshot()
        self.assertLessEqual(len(json.dumps(snapshot,ensure_ascii=False).encode()),3400)
        self.assertEqual(len(snapshot['programs']),3)
        self.assertEqual(self.t.store.path.read_bytes(),before)

    def test_missing_or_corrupt_store_is_not_created_or_migrated(self):
        p=self.root/'absent';p.mkdir()
        reader=SelfExperience(p,SCOPE)
        self.assertEqual(reader.snapshot(),{});self.assertFalse(reader.path.exists())
        reader.path.write_text('unrecognized')
        self.assertEqual(reader.snapshot(),{});self.assertEqual(reader.path.read_text(),'unrecognized')

    async def test_actual_owner_context_hook_contains_execution_and_no_friend_leak(self):
        self.program()
        self.p.self_experience=self.reader()
        self.p.self_experience_evidence=lambda:self.p.self_experience.snapshot()
        self.p.recent_activity=lambda **kw:CompanionPhone.recent_activity(self.p,**kw)
        self.p.outbound=NS(state={'shares':[]});self.p.visitor_inbox=None
        self.p.thinking=self.t;self.p.task_authorized=lambda e:False
        self.p.owner_event=lambda e:e.owner
        self.a.context=lambda **kw:'existing phone activity'
        self.a.status_context=lambda **kw:'phone status'
        req=NS(extra_user_content_parts=[])
        await CompanionPhone._inject_phone_context(self.p,NS(owner=True),req)
        material='\n'.join(part.text for part in req.extra_user_content_parts)
        self.assertIn('verified_successes',material);self.assertIn('隔天回看练习',material)
        req=NS(extra_user_content_parts=[])
        await CompanionPhone._inject_phone_context(self.p,NS(owner=False),req)
        self.assertEqual(req.extra_user_content_parts,[])

    def test_thinking_receives_dated_practice_not_an_old_claim(self):
        self.program()
        self.p.self_experience_evidence=lambda:self.reader().snapshot()
        payload=self.t.public_context()
        self.assertTrue(payload['自身实践记录']['programs'][0]['program_executed'])
        self.assertNotIn('thoughts',payload['自身实践记录'])
        self.assertIn('自身实践记录',bounded(THINK,payload))

    async def test_normal_autonomy_with_recent_programs_uses_existing_two_calls(self):
        self.config.update(self_directed_enabled=True,practice_enabled=True)
        for i in range(3):self.program(identifier=format(i,'032x'))
        self.p.self_experience_evidence=lambda:self.reader().snapshot()
        self.context.llm_generate.side_effect=[
            NS(completion_text=json.dumps(fixtures.ThinkingTests.thought(self))),
            NS(completion_text=json.dumps(fixtures.ThinkingTests.decision(self)))]
        await self.a.consider()
        self.assertEqual(self.context.llm_generate.await_count,2)
        self.assertEqual(self.t.store.rows('runs')[0]['status'],'completed')
        self.assertIn('first_saved_program_attempt_at',self.context.llm_generate.await_args_list[0].kwargs['prompt'])

    async def test_diary_collects_today_preserves_old_pages_and_allows_declining(self):
        today=datetime.fromtimestamp(NOW,TZ).replace(hour=0,minute=0,second=0,microsecond=0)
        yesterday=(today-timedelta(days=1)).date().isoformat()
        self.program();self.program(at=NOW-86400,identifier='c'*32)
        old={'id':'public:daily:'+yesterday,'day':yesterday,'text':'old unchanged','kind':'daily','at':NOW-86400}
        path=self.root/'public-diary.jsonl';path.write_text(json.dumps(old)+'\n',encoding='utf-8')
        b=Books(self.root,experience=self.reader());b.daily['enabled_day']=today.date().isoformat()
        b.collect_self_experience(NOW)
        self.assertEqual(len(b.daily['sources']),1)
        self.assertEqual(path.read_text(encoding='utf-8'),json.dumps(old)+'\n')
        self.assertIn('程序成功退出',json.dumps(b.day_evidence(today.date().isoformat(),'public'),ensure_ascii=False))
        model=AsyncMock(return_value='{"write":false}')
        await b.tick(model,now=today.timestamp()+23.5*3600)
        self.assertEqual(b.read('public')[0],old)
        self.assertEqual(b.read('public')[-1]['kind'],'no_entry')
        self.assertEqual(b.daily['decisions']['public:'+today.date().isoformat()]['status'],'declined')

    def test_private_reflection_source_cannot_reach_public_diary(self):
        self.thought(private=True,focus='PRIVATE_THOUGHT_SENTINEL')
        b=Books(self.root,experience=self.reader());b.daily['enabled_day']='2026-11-05'
        b.collect_self_experience(NOW)
        self.assertNotIn('私人回忆',json.dumps(b.day_evidence('2026-11-05','public'),ensure_ascii=False))
        self.assertIn('私人回忆',json.dumps(b.day_evidence('2026-11-05','private'),ensure_ascii=False))

    def test_source_visibility_and_shape_are_required(self):
        self.program();source=self.reader().diary_sources(NOW-1,NOW)[0]
        self.assertTrue(public_source(source))
        for change in ({'public_content':False},{'id':'chat:1'},{'kind':'private_self_experience'},{'action':'phone_input'},{'app':'私聊'}):
            self.assertFalse(public_source(source|change))


class ChatExperienceTests(unittest.IsolatedAsyncioTestCase):
    setUp=brain_fixtures.PhaseTwoTests.setUp
    phone=brain_fixtures.PhaseTwoTests.phone
    memory=brain_fixtures.PhaseTwoTests.memory
    event=brain_fixtures.PhaseTwoTests.event
    request=brain_fixtures.PhaseTwoTests.request

    def test_brain_owner_and_background_receive_same_snapshot_other_contacts_do_not(self):
        p=self.phone();self.memory()
        snapshot={'programs':[{'at':'2026-11-07T15:09:37+08:00','program_executed':True}]}
        p.self_experience_evidence=Mock(return_value=snapshot)
        data=cognitive_context.build(self.brain,self.event(),self.request())
        self.assertEqual(data['self']['own_activities'],snapshot)
        version=data['versions']['own_activities']
        self.assertEqual(cognitive_context.build(self.brain)['versions']['own_activities'],version)
        p.self_experience_evidence.reset_mock()
        other=cognitive_context.build(self.brain,self.event('456'),self.request())
        self.assertNotIn('own_activities',other['self']);p.self_experience_evidence.assert_not_called()
        group=self.event();group.get_group_id=lambda:'999';group.unified_msg_origin='test:GroupMessage:999'
        self.assertNotIn('own_activities',cognitive_context.build(self.brain,group)['self'])
        p.self_experience_evidence.assert_not_called()

    def test_checker_gets_same_execution_evidence_and_rejects_mismatched_owner_scope(self):
        p=self.phone();p.self_experience_evidence=Mock(return_value={'programs':[{'program_executed':True}]})
        a=self.chat.active;a.context=self.chat.context;a.state={};a.commitments=NS(evidence=lambda:[])
        self.assertTrue(grounding.evidence(a)['own_activities']['programs'][0]['program_executed'])
        p.umo='test:FriendMessage:other';p.self_experience_evidence.reset_mock()
        self.assertNotIn('own_activities',grounding.evidence(a));p.self_experience_evidence.assert_not_called()


if __name__=='__main__':unittest.main()
