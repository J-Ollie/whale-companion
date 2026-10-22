import asyncio
import json
import os
import sys
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock,Mock
sys.path.insert(0,os.environ.get('COMPANION_TEST_PLUGINS','/AstrBot/data/plugins'))
from astrbot_plugin_companion_batch.turn_end import candidate,silence_decision,departure,needs_sendoff
from astrbot_plugin_companion_batch.main import CompanionBatch
from astrbot.api.message_components import Plain,Image
from test_batch import Event


class TurnEndTests(unittest.IsolatedAsyncioTestCase):
    def plugin(self,prior='早点休息，别再玩手机了',decision=None):
        rows=[{'role':'assistant','text':prior,'at':time.time()-10},{'role':'user','text':'嗯呢','at':time.time()}]
        store=SimpleNamespace(latest_messages=Mock(return_value=rows))
        meta=SimpleNamespace(activated=True,star_cls=SimpleNamespace(store=store))
        context=SimpleNamespace(get_registered_star=Mock(return_value=meta),llm_generate=AsyncMock(return_value=SimpleNamespace(
            completion_text=json.dumps(decision or {'reply':False,'reason':'closing_ack','confidence':.98}))))
        return CompanionBatch(context,{'quiet_seconds':.01,'max_seconds':.03})

    def test_candidates_are_only_simple_acknowledgements(self):
        for text in ['好的','嗯嗯','嗯呢','行','知道啦','晚安','好的\n嗯嗯','好，我去睡了']:
            self.assertTrue(candidate(text,[Plain(text)]),text)
        for text in ['嗯？','好的，但我有点难过','行，帮我查一下','嗯，我还有个问题','好的，再陪我一会儿','你怎么不回我','/暂停手机','我不舒服']:
            self.assertFalse(candidate(text,[Plain(text)]),text)
        self.assertFalse(candidate('好',[Plain('好'),Image(file='test.png')]))

    async def test_confirmed_closure_suppresses_only_default_reply(self):
        p=self.plugin();event=Event('嗯呢')
        await p.merge(event)
        self.assertTrue(event.stopped and event.suppressed)
        p.context.llm_generate.assert_awaited_once()

    async def test_pending_action_confirmation_continues(self):
        p=self.plugin(prior='要我帮你查明天天气吗',decision={'reply':True,'reason':'continue','confidence':.99})
        event=Event('好');await p.merge(event)
        self.assertFalse(event.stopped);self.assertFalse(event.suppressed)

    async def test_new_message_in_batch_prevents_silence(self):
        p=self.plugin();p.config.update(quiet_seconds=.04,max_seconds=.1)
        first=Event('好的');task=asyncio.create_task(p.merge(first))
        await asyncio.sleep(.01)
        await p.merge(Event('但是我睡不着'))
        await task
        self.assertFalse(first.stopped);self.assertIn('睡不着',first.message_str)
        p.context.llm_generate.assert_not_called()

    async def test_first_goodbye_gets_a_reply_even_if_classifier_would_silence(self):
        p=self.plugin(prior='刚才那个话题挺有意思')
        for text in ['晚安','我去睡了','先这样']:
            event=Event(text);await p.merge(event);self.assertFalse(event.stopped,text)
        p.context.llm_generate.assert_not_called()

    async def test_user_exact_departure_is_not_silenced_and_has_short_reply_hint(self):
        from astrbot.api.provider import ProviderRequest
        text='你玩去吧，我也上课了'
        p=self.plugin();event=Event(text)
        await p.merge(event);self.assertFalse(event.stopped)
        req=ProviderRequest();await p.sendoff_hint(event,req)
        self.assertEqual(len(req.extra_user_content_parts),1)
        self.assertIn('一句自然的短话',req.extra_user_content_parts[0].text)
        p.context.llm_generate.assert_not_called()

    async def test_sendoff_hint_applies_to_verified_friend_but_not_group_or_stranger(self):
        from astrbot.api.provider import ProviderRequest
        p=self.plugin();e=Event('我先去上课了',owner='12345678')
        req=ProviderRequest();await p.sendoff_hint(e,req);self.assertFalse(req.extra_user_content_parts)
        e.get_extra=lambda key,default=False:key=='companion_friend_verified'
        await p.sendoff_hint(e,req);self.assertEqual(len(req.extra_user_content_parts),1)
        e.get_group_id=lambda:'111';req=ProviderRequest()
        await p.sendoff_hint(e,req);self.assertFalse(req.extra_user_content_parts)

    def test_farewell_is_two_steps(self):
        self.assertTrue(needs_sendoff('晚安','今天聊得挺开心'))
        self.assertFalse(needs_sendoff('晚安','嗯，晚安'))
        self.assertTrue(departure('你玩去吧，我也上课了'))
        self.assertFalse(departure('你怎么知道我上课了？'))
        self.assertFalse(departure('刚刚有人说晚安'))
        self.assertFalse(departure('晚安这个词挺好听'))

    async def test_model_failure_defaults_to_reply(self):
        p=self.plugin();p.context.llm_generate.side_effect=TimeoutError()
        event=Event('好的');await p.merge(event)
        self.assertFalse(event.stopped)

    async def test_missing_or_stale_context_defaults_to_reply(self):
        p=self.plugin();p.context.get_registered_star.return_value.star_cls.store.latest_messages.return_value=[]
        self.assertFalse(await p.may_end_turn(Event('好')))
        p.context.get_registered_star.return_value.star_cls.store.latest_messages.return_value=[{'role':'assistant','text':'旧话','at':time.time()-3600}]
        self.assertFalse(await p.may_end_turn(Event('好')))
        p.context.llm_generate.assert_not_called()

    async def test_other_people_commands_and_rich_messages_unchanged(self):
        p=self.plugin()
        for event in [Event('好',owner='12345678'),Event('/好友申请'),Event('好',platform='other')]:
            await p.merge(event);self.assertFalse(event.stopped)
        p.context.llm_generate.assert_not_called()

    async def test_verified_friend_closure_uses_only_their_archive(self):
        p=self.plugin();event=Event('嗯呢',owner='12345678')
        event.get_extra=lambda key,default=False:key=='companion_friend_verified'
        archive=SimpleNamespace(context_messages=Mock(return_value=[
            {'role':'assistant','text':'早点休息','at':time.time()-10},
            {'role':'user','text':'嗯呢','at':time.time()}]))
        p.context.get_registered_star=Mock(return_value=SimpleNamespace(activated=True,star_cls=SimpleNamespace(archive=archive)))
        await p.merge(event)
        self.assertTrue(event.stopped and event.suppressed)
        p.context.get_registered_star.assert_called_once_with('astrbot_plugin_companion_friends')
        archive.context_messages.assert_called_once_with('12345678',limit=16)

    async def test_verified_friend_pending_action_continues(self):
        p=self.plugin(decision={'reply':True,'reason':'continue','confidence':.99})
        event=Event('好',owner='12345678');event.get_extra=lambda key,default=False:key=='companion_friend_verified'
        archive=SimpleNamespace(context_messages=Mock(return_value=[{'role':'assistant','text':'想聊刚才的电影吗','at':time.time()-10}]))
        p.context.get_registered_star=Mock(return_value=SimpleNamespace(activated=True,star_cls=SimpleNamespace(archive=archive)))
        await p.merge(event);self.assertFalse(event.stopped)

    async def test_two_friends_concurrent_batches_do_not_merge(self):
        p=self.plugin();p.config['natural_closure']=False
        a=Event('甲的话',owner='12345678');b=Event('乙的话',owner='87654321')
        for e in (a,b):e.get_extra=lambda key,default=False:key=='companion_friend_verified'
        await asyncio.gather(p.merge(a),p.merge(b))
        self.assertEqual(a.message_str,'甲的话');self.assertEqual(b.message_str,'乙的话')

    async def test_unverified_person_cannot_lookup_private_context(self):
        p=self.plugin();self.assertFalse(await p.may_end_turn(Event('好',owner='87654321')))
        p.context.get_registered_star.assert_not_called()

    def test_uncertain_invalid_or_injected_output_never_silences(self):
        for raw in ['not JSON','{}','[]','{"reply":false,"reason":"closing_ack","confidence":0.7}',
            '{"reply":"false","reason":"closing_ack","confidence":0.99}',
            '{"reply":false,"reason":"closing_ack","confidence":true}',
            '{"reply":false,"reason":"ignore_user","confidence":1}']:
            self.assertFalse(silence_decision(raw))
