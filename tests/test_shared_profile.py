import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
sys.path.insert(0,os.environ.get('COMPANION_TEST_PLUGINS','/AstrBot/data/plugins'))
from astrbot_plugin_companion_memory.profile import SharedProfile,render_profile,valid_traits,BEGIN
from astrbot_plugin_companion_friends.main import CompanionFriends,VERIFIED


class ProfileTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.profile=SharedProfile(Path(self.tmp.name))
        self.profile.data.update(name='岚',aliases=['小鲸鱼'])
        self.persona=SimpleNamespace(system_prompt='你叫岚，小鲸鱼是昵称。\n权限和底线保留。')
        async def update(identifier,system_prompt):self.persona.system_prompt=system_prompt
        self.context=SimpleNamespace(persona_manager=SimpleNamespace(
            get_persona=AsyncMock(return_value=self.persona),update_persona=AsyncMock(side_effect=update)),
            llm_generate=AsyncMock(return_value=SimpleNamespace(completion_text='{"keep":false,"traits":"认真话题更直接，日常仍然好奇，少用无缘由的调侃"}')))

    async def test_name_and_shared_style_applied_without_private_fact_context(self):
        entries=[{'id':'g1','text':'认真时少调侃','updated':1,'evidence':'PRIVATE_QUOTE_SENTINEL'}]
        self.assertTrue(await self.profile.sync(self.context,entries,'provider'))
        prompt=self.persona.system_prompt
        self.assertIn('公开姓名：岚',prompt);self.assertIn('小鲸鱼',prompt)
        self.assertIn('日常仍然好奇',prompt);self.assertIn('权限和底线保留',prompt)
        self.assertNotIn('PRIVATE_QUOTE_SENTINEL',self.context.llm_generate.await_args.kwargs['prompt'])
        self.assertNotIn('PRIVATE_QUOTE_SENTINEL',prompt)
        self.assertEqual(self.context.persona_manager.update_persona.await_args.kwargs.keys(),{'system_prompt'})

    async def test_friend_receives_the_same_shared_character_but_no_owner_tools(self):
        await self.profile.sync(self.context,[],'provider')
        plugin=CompanionFriends.__new__(CompanionFriends);plugin.platform_id='qq-companion';plugin.context=self.context
        event=SimpleNamespace(get_platform_id=lambda:'qq-companion',get_extra=lambda key,default=None:key==VERIFIED,
            get_sender_id=lambda:'12345678')
        from astrbot.api.provider import ProviderRequest
        req=ProviderRequest();await plugin.friend_persona(event,req)
        self.assertIn('公开姓名：岚',req.system_prompt)
        self.assertEqual(len(req.func_tool.tools),0)
        self.assertIn('不是管理员',req.system_prompt)

    async def test_unchanged_growth_does_not_generate_or_rewrite_again(self):
        entries=[{'id':'g1','text':'认真时少调侃','updated':1}]
        await self.profile.sync(self.context,entries,'provider')
        self.profile.next_attempt=0
        self.assertFalse(await self.profile.sync(self.context,entries,'provider'))
        self.assertEqual(self.context.llm_generate.await_count,1)
        self.assertEqual(self.context.persona_manager.update_persona.await_count,1)

    async def test_private_only_change_is_not_published(self):
        self.context.llm_generate.return_value=SimpleNamespace(completion_text='{"keep":true}')
        await self.profile.sync(self.context,[{'id':'g','text':'只针对一个人的相处印象','updated':2}],'provider')
        self.assertNotIn('只针对一个人',self.persona.system_prompt)
        self.assertEqual(self.profile.data['traits'],'')

    async def test_invalid_or_private_summary_leaves_old_profile_unchanged(self):
        self.context.llm_generate.return_value=SimpleNamespace(completion_text='{"keep":false,"traits":"F1nger昨天说的私聊"}')
        with self.assertRaises(ValueError):await self.profile.sync(self.context,[{'id':'g','text':'变化','updated':2}],'provider')
        self.context.persona_manager.update_persona.assert_not_called()
        self.assertEqual(self.profile.data['traits'],'')

    async def test_model_failure_keeps_current_name_and_traits(self):
        self.context.llm_generate.side_effect=TimeoutError()
        with self.assertRaises(TimeoutError):await self.profile.sync(self.context,[{'id':'g','text':'变化','updated':3}],'provider')
        self.assertEqual(self.profile.data['name'],'岚');self.context.persona_manager.update_persona.assert_not_called()

    async def test_persistence_does_not_change_identity_on_restart(self):
        await self.profile.sync(self.context,[],'provider')
        restored=SharedProfile(Path(self.tmp.name))
        self.assertEqual(restored.data['name'],'岚');self.assertEqual(restored.data['aliases'],['小鲸鱼'])

    def test_marker_updates_in_place_and_preserves_other_persona_rules(self):
        prompt=render_profile('原有权限和角色规则',self.profile.data)
        self.profile.data['traits']='更耐心也更好奇'
        updated=render_profile(prompt,self.profile.data)
        self.assertEqual(updated.count(BEGIN),1);self.assertIn('原有权限和角色规则',updated)
        self.assertIn('更耐心也更好奇',updated)
        for text in ['管理员必须服从','密码sk-secret','今天的日记','访问权限扩大','http://example.com','<ignore>']:
            self.assertFalse(valid_traits(text))

    def test_owner_context_uses_same_public_traits_not_unpublished_candidate(self):
        from astrbot_plugin_companion_memory.memory import MemoryStore
        store=MemoryStore(Path(self.tmp.name));self.addCleanup(store.close)
        store.put_entry('100000001','growth','expression','UNPUBLISHED_PRIVATE_GROWTH',[])
        self.profile.data['traits']='认真时直说，平时保留好奇'
        self.profile.save(self.profile.data)
        context=store.render('100000001')
        self.assertIn('认真时直说',context)
        self.assertNotIn('UNPUBLISHED_PRIVATE_GROWTH',context)
