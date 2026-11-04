"""Recover real source gaps, not imaginary history; enforce existing budgets and gates."""
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock,patch

sys.path.insert(0,os.environ.get('COMPANION_TEST_PLUGINS',str(Path(__file__).parent)))
from astrbot_plugin_companion_memory.memory import MemoryStore
from astrbot_plugin_companion_memory.cognition import Cognition
from astrbot_plugin_companion_memory.history_gap import recent_gap
from astrbot_plugin_companion_memory.main import CompanionMemory
from astrbot_plugin_companion_memory.budget import CONTEXT_BYTES,LAYER_BYTES,size
from astrbot_plugin_companion_memory.usage_audit import context_metadata
from astrbot.core.provider.entities import ProviderRequest
from astrbot.core.agent.message import Message,dump_messages_with_checkpoints

A='12345678'; B='87654321'
ORIGIN='qq-companion:FriendMessage:'+A


class RecoveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.store=MemoryStore(self.tmp.name);self.addCleanup(self.store.close)
        self.c=Cognition(self.store);self.now=time.time()
        self.anchor=self.add('现在准备好了吧',age=200)
        self.clarification=self.add('我已经接好了画图功能，你之前答应画自己的样子',age=180)
        self.latest=self.add('请画一张全身照',age=170)
        self.store.observe(A,'assistant','你说的准备好是指什么',at=self.now-150,origin=ORIGIN)
        self.contexts=[{'role':'user','content':[{'type':'text','text':'现在准备好了吧'},
            {'type':'text','text':'<system_reminder>synthetic time</system_reminder>'}]},
            {'role':'assistant','content':'你说的准备好是指什么'}]

    def add(self,text,age=100,qq=A,origin=ORIGIN):
        return self.store.observe(qq,'user',text,at=self.now-age,origin=origin)

    def gaps(self,contexts=None,**kwargs):
        return recent_gap(self.c,A,self.contexts if contexts is None else contexts,origin=ORIGIN,at=self.now,**kwargs)

    def plugin(self):
        p=CompanionMemory.__new__(CompanionMemory)
        p.owner=A;p.umo=ORIGIN;p.config={'enabled':True};p.store=self.store
        p.resting=lambda:False;p.friends_plugin=lambda:None
        return p

    def event(self,text='我不是告诉你了吗',qq=A):
        extra={}
        return SimpleNamespace(message_str=text,created_at=self.now,get_sender_id=lambda:qq,
            unified_msg_origin='qq-companion:FriendMessage:'+qq,
            get_extra=lambda k,d=None:extra.get(k,d),set_extra=lambda k,v:extra.__setitem__(k,v))

    async def test_partial_nonempty_window_recovers_missing_explanation(self):
        self.assertEqual([r['id'] for r in self.gaps()],['obs:'+str(self.clarification),'obs:'+str(self.latest)])

    async def test_existing_history_is_not_duplicated(self):
        history=self.contexts+[{'role':'user','content':'我已经接好了画图功能，你之前答应画自己的样子'},
                               {'role':'user','content':'请画一张全身照'}]
        self.assertFalse(self.gaps(history))

    async def test_empty_reset_and_unanchored_window_do_not_reload_old_history(self):
        self.assertFalse(self.gaps([]))
        self.assertFalse(self.gaps([{'role':'user','content':'完全不同的新话题'}]))

    async def test_normal_trimming_does_not_reload_records_before_anchor(self):
        history=[{'role':'user','content':'请画一张全身照'}]
        self.assertFalse(self.gaps(history))

    async def test_other_contact_and_platform_are_excluded(self):
        self.add('另外一个人的私聊',qq=B)
        self.add('其他平台相同账号的资料',origin='other:FriendMessage:'+A)
        texts=' '.join(r['text'] for r in self.gaps())
        self.assertNotIn('另外一个人',texts);self.assertNotIn('其他平台',texts)

    async def test_suppressed_and_retired_sources_stay_out(self):
        with self.store.db:
            self.store.db.execute('insert into suppressed_sources values (?,?)',(A,self.clarification))
        self.assertNotIn('obs:'+str(self.clarification),[r['id'] for r in self.gaps()])

    async def test_retired_anchor_locates_gap_without_reactivating_retired_sources(self):
        with patch.object(self.c,'retired_source',side_effect=lambda qq,i:i in (self.anchor,self.latest)):
            gaps=self.gaps()
        self.assertEqual([r['id'] for r in gaps],['obs:'+str(self.clarification)])

    async def test_future_old_unknown_timestamp_and_commands_stay_out(self):
        self.add('未来消息',age=-1);self.add('昨天的话',age=7200);self.add('/不自动执行命令')
        self.store.observe(A,'user','时间未知',origin=ORIGIN,historical=True)
        texts=' '.join(r['text'] for r in self.gaps())
        for value in ('未来','昨天','命令','时间未知'):self.assertNotIn(value,texts)

    async def test_observation_cutoff_excludes_current_and_later_arrivals(self):
        current=self.add('当前问题',age=20);self.add('稍后另一轮',age=10)
        result=self.gaps(before_id=current)
        self.assertTrue(all(int(r['id'][4:])<current for r in result))

    async def test_batch_parts_already_in_history_are_not_injected(self):
        history=self.contexts+[{'role':'user','content':'我已经接好了画图功能，你之前答应画自己的样子\n请画一张全身照'}]
        self.assertFalse(self.gaps(history))

    async def test_request_hook_provider_assembly_receives_gap_without_changing_prompt(self):
        p=self.plugin();event=self.event();req=ProviderRequest(prompt=event.message_str,contexts=self.contexts)
        await p.inject(event,req)
        assembled=await req.assemble_context()
        body=json.dumps(assembled,ensure_ascii=False)
        self.assertIn('已经接好了画图功能',body);self.assertIn('请画一张全身照',body)
        self.assertEqual(req.prompt,event.message_str);self.assertIs(req.contexts,self.contexts)
        self.assertIn('obs:'+str(self.clarification),event.get_extra('companion_recall_sent'))
        # Temporary context must reach the provider, then disappear from persisted history.
        saved=dump_messages_with_checkpoints([Message.model_validate(assembled)])
        self.assertNotIn('history_gap',json.dumps(saved))

    async def test_sleep_and_unauthorized_sender_do_not_read_recovery(self):
        p=self.plugin();event=self.event();req=ProviderRequest(prompt=event.message_str,contexts=self.contexts)
        p.resting=lambda:True
        await p.inject(event,req);self.assertEqual(req.extra_user_content_parts,[])
        p.resting=lambda:False
        self.assertEqual(p.request_context(self.event(qq=B),self.contexts),'')

    async def test_global_layer_budgets_and_full_source_remain_available(self):
        for i in range(6):self.add('不同的长原话'+str(i)+'鲸'*1200,age=100-i)
        p=self.plugin();event=self.event();body=p.request_context(event,self.contexts)
        self.assertLessEqual(size(body),CONTEXT_BYTES)
        data=json.loads(body[body.index('{'):body.rindex('}')+1])
        self.assertTrue(data['history_gap']);self.assertLessEqual(size({'items':data['history_gap']}),LAYER_BYTES['history_gap'])
        for item in data['history_gap']:
            self.assertTrue(item['excerpt'])
            self.assertGreater(len(self.c.read(A,item['id'])['text']),len(item['text']))
        self.assertTrue(context_metadata(body)['refs'])

    async def test_source_lookup_is_read_only(self):
        before=list(self.store.db.execute('select * from observations order by id'))
        self.gaps()
        self.assertEqual([tuple(x) for x in before],[tuple(x) for x in self.store.db.execute('select * from observations order by id')])

    async def test_recovery_uses_window_that_survives_later_budget_hook(self):
        p=self.plugin();event=self.event()
        history=[{'role':'user','content':'鲸'*5000},{'role':'assistant','content':'一条巨大旧轮'}]
        body=p.request_context(event,history)
        data=json.loads(body[body.index('{'):body.rindex('}')+1])
        self.assertTrue(data['last_exchange'])
        self.assertLessEqual(size(body),CONTEXT_BYTES)


if __name__=='__main__':unittest.main()
