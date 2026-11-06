"""Common intentions without private causes, and same-person cross-scene affect."""
import json
import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock,Mock,patch
import test_thinking_memory as fixture
from test_thinking import NOW
from astrbot_plugin_companion_phone.common_cognition import PRINCIPLES


class CommonIntentTests(unittest.IsolatedAsyncioTestCase):
    setUp=fixture.MemoryThinkingTests.setUp
    responses=fixture.MemoryThinkingTests.responses
    thought=fixture.MemoryThinkingTests.thought
    decision=fixture.MemoryThinkingTests.decision
    ask=fixture.MemoryThinkingTests.ask
    private_note=fixture.MemoryThinkingTests.private_note

    async def choose_principle(self):
        self.responses(self.ask(),self.private_note(general_principle='listen_complete'),
                       {'activity':'idle','general_principle':'listen_complete'})
        await self.a.consider()

    async def test_private_origin_yields_same_common_rule_without_private_story(self):
        await self.choose_principle()
        common=self.t.common_context()
        self.assertIn(PRINCIPLES['listen_complete'],common)
        self.assertNotIn('PRIVATE_',common)
        self.assertNotIn('我想重新想想',common)
        self.assertNotIn('我愿意',common)
        self.assertIn('适用于所有联系人',common)
        self.assertIn(PRINCIPLES['listen_complete'],self.t.public_context()['共同思考意向'])
        self.assertFalse(self.memory.store.entries('owner','growth',now=NOW))

    async def test_decision_cannot_choose_a_different_rule_than_thought(self):
        self.responses(self.ask(),self.private_note(general_principle='listen_complete'),
                       {'activity':'idle','general_principle':'express_own_view'})
        await self.a.consider()
        self.assertNotIn('自己经思考后选择',self.t.common_context())
        self.assertEqual(self.t.store.rows('runs')[0]['status'],'invalid_output')

    async def test_free_text_can_never_cross_the_common_boundary(self):
        self.responses(self.ask(),self.private_note(general_principle='PRIVATE_USER_MARKER'))
        await self.a.consider()
        self.assertNotIn('PRIVATE_',self.t.common_context())
        self.assertEqual(self.t.store.rows('runs')[0]['status'],'invalid_output')

    async def test_forgotten_source_removes_unpublished_common_intent(self):
        await self.choose_principle()
        with self.memory.store.atomic():self.memory.store.db.execute('INSERT INTO suppressed_sources VALUES(?,?)',('owner',self.reply))
        self.assertNotIn('自己经思考后选择',self.t.common_context())

    async def test_private_detail_no_longer_injects_person_specific_style(self):
        await self.choose_principle()
        private=self.t.private_context()
        self.assertNotIn('growth_wish',private)
        self.assertNotIn('practice',private)
        self.assertIn('不因这份私有材料形成另一套',private)

    async def test_common_intention_is_explicitly_provided_to_next_decision(self):
        await self.choose_principle()
        self.a.rhythm.state['next_decision']=NOW-1
        self.responses(self.thought(),self.decision())
        await self.a.consider()
        payload=json.loads(self.context.llm_generate.await_args.kwargs['prompt'])
        self.assertIn(PRINCIPLES['listen_complete'],payload['共同思考意向'])

    def test_cross_day_private_self_reviews_can_enter_common_growth(self):
        from astrbot_plugin_companion_phone.memory_reflection import PrivateMemory
        from astrbot_plugin_companion_phone.common_cognition import commit
        for at in (NOW-86400,NOW):
            if at<NOW:
                self.memory.store.observe('owner','user','第一天真实讨论的原话',at=at-20)
                self.memory.store.observe('owner','assistant','第一天的真实回复',at=at-10)
            self.t.run_id=self.t.store.begin(at)
            with patch('time.time',return_value=at):
                recall=PrivateMemory(self.t)
                recall.request({'action':'recent'})
                thought=self.private_note(general_principle='listen_complete')
                recall.save(thought,at)
                commit(self.t,thought,{'general_principle':'listen_complete'},True,at)
            self.t.store.update_run(self.t.run_id,status='completed')
        growth=self.memory.store.entries('owner','growth',now=NOW)
        self.assertEqual(growth[0]['text'],PRINCIPLES['listen_complete'])
        self.assertNotIn('PRIVATE_',growth[0]['text'])


class CommonHookTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        from test_affect_hooks import HookTests
        self.base=HookTests();self.base.setUp();self.addCleanup(self.base.tearDown)
        self.p=self.base.p
        self.common='统一的说话意向：先听完整再回应。'
        phone=NS(owner=self.p.owner,thinking=NS(common_context=lambda:self.common))
        self.p.context.get_registered_star=lambda name:NS(activated=True,star_cls=phone)

    async def test_owner_friend_and_authorized_group_receive_identical_intent(self):
        from astrbot_plugin_companion_memory.main import CompanionMemory
        group=self.base.event('12345678',group=True)
        self.p.shared=NS(enabled=lambda:True,group=lambda event:'group:test' if event is group else None)
        texts=[]
        for event in (self.base.event(),self.base.event('12345678',True),group):
            req=NS(extra_user_content_parts=[])
            await CompanionMemory.inject_common_cognition(self.p,event,req)
            texts.append(req.extra_user_content_parts[0].text)
        self.assertEqual(len(set(texts)),1)
        self.assertIn(self.common,texts[0])

    async def test_invalid_group_and_unverified_person_do_not_receive_state(self):
        from astrbot_plugin_companion_memory.main import CompanionMemory
        for event in (self.base.event('12345678'),self.base.event('12345678',group=True)):
            req=NS(extra_user_content_parts=[])
            await CompanionMemory.inject_common_cognition(self.p,event,req)
            self.assertFalse(req.extra_user_content_parts)

    async def test_owner_phone_and_memory_attach_one_shared_principle(self):
        from astrbot_plugin_companion_phone.main import CompanionPhone
        from astrbot_plugin_companion_memory.main import CompanionMemory
        from astrbot.core.agent.message import TextPart
        phone = NS(autonomy=NS(context=lambda **kw:'当前作息'), outbound=NS(state={'shares':[]}),
                   social_compat=None, thinking=NS(common_context=lambda:self.common))
        text = CompanionPhone.recent_activity(phone, compact=True)
        req = NS(extra_user_content_parts=[TextPart(text=text).mark_as_temp()])
        await CompanionMemory.inject_common_cognition(self.p,self.base.event(),req)
        self.assertEqual(len(req.extra_user_content_parts),1)
        self.assertEqual(req.extra_user_content_parts[0].text.count(self.common),1)
        self.assertIn(self.common, CompanionPhone.recent_activity(phone))

    async def test_same_person_group_uses_existing_affect_without_private_history(self):
        from astrbot_plugin_companion_memory.affect import fresh
        from astrbot_plugin_companion_memory.main import CompanionMemory
        person='12345678';state=fresh(NOW);state['mood']=1.8
        self.p.store.set_meta('affect_global_v3',state)
        self.p.store.observe(person,'user','PRIVATE_HISTORY_DO_NOT_SEND',at=NOW)
        group=self.base.event(person,group=True)
        self.p.shared=NS(enabled=lambda:True,group=lambda event:'group:test' if event is group else None)
        req=NS(extra_user_content_parts=[])
        with patch('time.time',return_value=NOW):await CompanionMemory.inject_affect(self.p,group,req)
        self.assertIn('开心',req.extra_user_content_parts[0].text)
        self.assertIn('跨私聊、群聊与场景连续',req.extra_user_content_parts[0].text)
        if self.p.context.llm_generate.await_args:
            self.assertNotIn('PRIVATE_HISTORY_DO_NOT_SEND',self.p.context.llm_generate.await_args.kwargs['prompt'])
        self.assertGreater(self.p.store.get_meta('affect_global_v3')['mood'],1)


if __name__=='__main__':unittest.main()
