import json
import unittest
from types import SimpleNamespace
import test_memory_completeness_hooks as fixture
from astrbot_plugin_companion_memory.usage_audit import record
import sqlite3
from contextlib import closing


class UsageHooks(unittest.IsolatedAsyncioTestCase):
    setUp=fixture.PagingHooksTests.setUp
    tearDown=fixture.PagingHooksTests.tearDown
    event=fixture.PagingHooksTests.event

    async def test_request_records_only_final_attached_parts_not_full_prompt(self):
        source=self.p.store.observe(fixture.A,'user','我喜欢看鲸鱼')
        entry=self.p.store.put_entry(fixture.A,'fact','喜欢','我喜欢看鲸鱼',[source])
        text=self.p.cognition().render(fixture.A,'鲸鱼');event=self.event()
        req=SimpleNamespace(extra_user_content_parts=[SimpleNamespace(text=text)])
        await self.p.audit_request(event,req)
        path=self.p.store.folder/'usage-audit.sqlite3'
        with closing(sqlite3.connect(path)) as db:
            row=db.execute('SELECT qq,stage,metadata FROM usage').fetchone();self.assertEqual(row[0],fixture.A);self.assertEqual(row[1],'request_attached');self.assertIn(entry,json.loads(row[2])['refs']);self.assertNotIn('我喜欢看鲸鱼',row[2])

    async def test_forged_friend_does_not_create_request_audit(self):
        event=self.event(verified=False);await self.p.audit_request(event,SimpleNamespace(extra_user_content_parts=[]))
        self.assertFalse((self.p.store.folder/'usage-audit.sqlite3').exists())

    async def test_unreadable_block_is_unknown_not_absent(self):
        await self.p.audit_request(self.event(),SimpleNamespace(extra_user_content_parts=[SimpleNamespace(text='<companion_context>broken</companion_context>')]))
        with closing(sqlite3.connect(self.p.store.folder/'usage-audit.sqlite3')) as db:
            row=json.loads(db.execute('SELECT metadata FROM usage').fetchone()[0])
        self.assertEqual(row['reason'],'context_unreadable');self.assertEqual(row['refs'],[])

    async def test_tool_read_ranges_and_payload_budget_are_unchanged(self):
        sid=self.p.store.observe(fixture.A,'user','鲸'*3000);event=self.event()
        result=await self.p.tool_read(event,'obs:'+str(sid),offset=50)
        self.assertFalse(result.isError);data=json.loads(result.content[0].text)
        with closing(sqlite3.connect(self.p.store.folder/'usage-audit.sqlite3')) as db:
            rows=[(r[0],json.loads(r[1])) for r in db.execute('SELECT stage,metadata FROM usage')]
        self.assertEqual([r[0] for r in rows],['read','tool_result']);self.assertEqual(rows[0][1]['ranges'][0]['offset'],50);self.assertEqual(rows[1][1]['ranges'][0]['end'],50+len(data['text']))
        self.assertLessEqual(len(result.content[0].text.encode()),fixture.TOOL_REPLY_BYTES)


if __name__=='__main__':unittest.main()
