"""Category routing uses the same identity, sleep, rewrite and budget gates."""
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock
import test_memory_completeness_hooks as fixtures


class LayerHooksTests(unittest.IsolatedAsyncioTestCase):
    setUp=fixtures.PagingHooksTests.setUp
    tearDown=fixtures.PagingHooksTests.tearDown
    event=fixtures.PagingHooksTests.event

    async def test_category_does_not_change_contact_scope(self):
        source=self.p.store.observe(fixtures.A,'user','我明天讨论南极研究')
        self.p.store.put_entry(fixtures.A,'fact','计划.南极','我明天讨论南极研究',[source])
        own=await self.p.tool_recall(self.event(),'南极研究',category='plans')
        data=json.loads(own.content[0].text)
        self.assertEqual(data['category'],'plans');self.assertTrue(data['matches'])
        self.p.context.llm_generate=AsyncMock(return_value=SimpleNamespace(completion_text='{"queries":[]}'))
        other=await self.p.tool_recall(self.event(fixtures.B),'南极研究',category='plans')
        self.assertFalse(json.loads(other.content[0].text)['matches'])
        self.friends.sleeping=lambda:True
        self.assertTrue((await self.p.tool_recall(self.event(),'南极研究',category='plans')).isError)

    async def test_query_rewrite_keeps_requested_category(self):
        source=self.p.store.observe(fixtures.A,'user','一起聊过海底两万里推荐小说')
        entry=self.p.store.put_entry(fixtures.A,'episode','推荐书','一起聊过海底两万里推荐小说',[source])
        self.p.context.llm_generate=AsyncMock(return_value=SimpleNamespace(completion_text='{"queries":["海底两万里"]}'))
        response=await self.p.tool_recall(self.event(),'潜艇冒险故事',category='events')
        data=json.loads(response.content[0].text)
        self.assertEqual(data['category'],'events');self.assertEqual(data['matches'][0]['id'],entry)
        self.p.context.llm_generate.assert_awaited_once()

    async def test_invalid_category_and_call_limit_are_enforced(self):
        response=await self.p.tool_recall(self.event(),'研究',category=fixtures.B)
        self.assertTrue(response.isError)
        source=self.p.store.observe(fixtures.A,'user','我喜欢研究南极冰川')
        self.p.store.put_entry(fixtures.A,'fact','爱好','我喜欢研究南极冰川',[source])
        event=self.event()
        for _ in range(4):
            self.assertFalse((await self.p.tool_recall(event,'南极冰川',category='facts')).isError)
        self.assertTrue((await self.p.tool_recall(event,'南极冰川',category='facts')).isError)

    async def test_empty_directory_does_not_spend_rewrite_model_budget(self):
        self.p.context.llm_generate=AsyncMock()
        response=await self.p.tool_recall(self.event(),category='plans')
        self.assertFalse(response.isError)
        self.assertFalse(json.loads(response.content[0].text)['matches'])
        self.p.context.llm_generate.assert_not_called()


if __name__=='__main__':unittest.main()
