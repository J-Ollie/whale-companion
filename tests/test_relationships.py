import asyncio
import json
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from astrbot_plugin_companion_memory.memory import MemoryStore
from astrbot_plugin_companion_memory.social_state import SocialState

NOW = 1793952000
PERSON = '12345678'
OTHER = '87654321'


class RelationshipTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.store = MemoryStore(folder.name)
        self.addCleanup(self.store.close)
        self.social = SocialState(self.store)
        self.flow = self.social.relationship_flow
        self.refs = [self.store.observe(PERSON,'user','今天我们一起核对了几条资料',at=NOW-86400),
                     self.store.observe(PERSON,'user','这次合作很顺利，我也很尊重你的决定',at=NOW-20)]

    def review(self, **choice):
        return self.flow.apply_review(PERSON, {'sources':self.refs,**choice}, self.flow.review_context(PERSON), NOW)

    def test_whale_forms_ordinary_privately_from_review(self):
        self.assertTrue(self.review(action='ordinary', label='朋友'))
        self.assertEqual(self.social.relationships(PERSON), [{'事实':'朋友','披露范围':'private'}])
        self.assertFalse(self.social.relationships(PERSON,public=True))
        self.assertFalse(self.social.relationships(OTHER))

    def test_unsupported_labels_and_foreign_evidence_do_not_become_facts(self):
        self.assertFalse(self.review(action='ordinary',label='家人'))
        foreign = self.store.observe(OTHER,'user','和另一个人聊天',at=NOW-600)
        self.assertFalse(self.review(action='ordinary',label='挚友',sources=[foreign,self.refs[0]]))
        self.assertFalse(self.review(action='ordinary',label='挚友',sources=[self.refs[0]]*2))
        self.assertFalse(self.social.relationships(PERSON))

    def test_human_proposal_needs_explicit_consent_from_each_party(self):
        identifier, _ = self.flow.propose(PERSON,'important','长期搭档',NOW)
        self.assertTrue(self.review(action='accept',id=identifier,kind='important'))
        self.assertFalse(self.social.relationships(PERSON))
        with self.assertRaises(ValueError):self.flow.decide(OTHER,identifier,'同意',NOW)
        with self.assertRaises(ValueError):self.flow.decide(OTHER,identifier,'同意',NOW,administrator=True)
        self.flow.decide(PERSON,identifier,'同意',NOW)
        self.assertEqual(self.social.relationships(PERSON)[0]['事实'],'长期搭档')
        self.assertTrue(self.flow.withdraw(PERSON,identifier,NOW))
        self.assertFalse(self.social.relationships(PERSON))

    def test_whale_can_propose_but_cannot_sign_for_the_contact(self):
        self.assertTrue(self.review(action='propose',kind='important',label='合作伙伴',human=True,admin=True))
        row = self.flow.pending(PERSON)
        self.assertEqual((row['whale'],row['human'],row['admin']),(1,0,0))
        self.flow.decide(PERSON,row['id'],'拒绝',NOW)
        self.assertFalse(self.review(action='propose',kind='important',label='合作伙伴'))
        self.assertFalse(self.social.relationships(PERSON))

    def test_family_requires_separate_admin_verification_even_with_both_signatures(self):
        identifier, kind = self.flow.propose(PERSON,'important','异姓家人',NOW)
        self.assertEqual(kind,'家人')
        self.flow.decide(PERSON,identifier,'同意',NOW)
        with self.assertRaises(ValueError):self.flow.decide(PERSON,identifier,'同意',NOW,administrator=True)
        self.assertFalse(self.review(action='accept',id=identifier,kind='important'))
        self.assertTrue(self.review(action='accept',id=identifier,kind='family'))
        self.assertFalse(self.social.relationships(PERSON))
        self.assertFalse(self.flow.context(OTHER)['提议'])
        self.assertEqual(self.flow.context(OTHER,administrator=True)['待你核验的家人提议'][0]['编号'],identifier)
        self.flow.decide(OTHER,identifier,'同意',NOW,administrator=True)
        self.assertEqual(self.social.relationships(PERSON)[0]['事实'],'异姓家人')

    def test_late_review_cannot_revive_withdrawn_proposal(self):
        identifier,_ = self.flow.propose(PERSON,'important','长期搭档',NOW)
        context = self.flow.review_context(PERSON)
        self.flow.withdraw(PERSON,identifier,NOW+1)
        self.assertFalse(self.flow.apply_review(PERSON,{'action':'accept','kind':'important','id':identifier,'sources':self.refs},context,NOW+2))
        self.assertFalse(self.social.relationships(PERSON))

    def test_superseded_evidence_is_rechecked_before_save(self):
        context = self.flow.review_context(PERSON)
        with self.store.atomic():self.store.db.execute('INSERT INTO suppressed_sources VALUES(?,?)',(PERSON,self.refs[0]))
        self.assertFalse(self.flow.apply_review(PERSON,{'action':'ordinary','label':'朋友','sources':self.refs},context,NOW))


class PrivateEntryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        from test_affect_hooks import HookTests
        self.base = HookTests()
        self.base.setUp()
        self.addCleanup(self.base.tearDown)
        self.plugin = self.base.p
        self.plugin.import_friend_archive = Mock(return_value=0)
        self.plugin.processing = asyncio.Lock()

    async def message(self, text, qq=PERSON, *, verified=True, group=False):
        from astrbot.api.message_components import Plain
        event = self.base.event(qq,verified,group)
        event.message_obj.message = [Plain(text)]
        event.message_str = text
        event.stop_event = Mock()
        event.plain_result = lambda value:value
        return [value async for value in self.plugin.relationship_message(event)]

    async def test_verified_private_proposal_then_independent_background_review(self):
        await self.message('关系提议 重要 长期搭档')
        flow = self.plugin.affect().social.relationship_flow
        identifier = flow.pending(PERSON)['id']
        await self.message(f'确认关系 {identifier} 同意')
        self.assertFalse(flow.social.relationships(PERSON))
        a = self.base.store.observe(PERSON,'user','昨天做完了合作的第一部分',at=NOW-86400)
        b = self.base.store.observe(PERSON,'user','今天我们一起核对剩下的工作',at=NOW)
        self.base.store.set_meta('cursor:'+PERSON,b)
        self.plugin.context.llm_generate.return_value = SimpleNamespace(completion_text=json.dumps(
            {'relationship':{'action':'accept','kind':'important','id':identifier,'sources':[a,b]}}))
        await self.plugin.reflect(qq=PERSON,force=True)
        self.assertEqual(flow.social.relationships(PERSON)[0]['事实'],'长期搭档')
        self.assertEqual(self.base.store.get_meta('cursor:'+PERSON),b)

    async def test_group_and_unverified_message_cannot_create_proposal(self):
        await self.message('关系提议 重要 长期搭档',group=True)
        await self.message('关系提议 重要 长期搭档',verified=False)
        self.assertIsNone(self.plugin.affect().social.relationship_flow.pending(PERSON))

    async def test_friend_cannot_approve_family_even_with_correct_number(self):
        flow = self.plugin.affect().social.relationship_flow
        identifier,_ = flow.propose(PERSON,'family','家人',NOW)
        flow.decide(PERSON,identifier,'同意',NOW)
        with self.base.store.atomic():self.base.store.db.execute('UPDATE relationship_proposals SET whale=1 WHERE id=?',(identifier,))
        await self.message(f'关系核验 {identifier} 同意')
        self.assertFalse(flow.social.relationships(PERSON))
        await self.message(f'关系核验 {identifier} 同意',qq=self.plugin.owner)
        self.assertTrue(flow.social.relationships(PERSON))
