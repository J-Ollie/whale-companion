"""Text conversation remains usable even with stale old voice settings/extras."""
import asyncio,unittest
from types import SimpleNamespace
from astrbot_plugin_companion_batch.main import CompanionBatch
from astrbot.api.provider import ProviderRequest
from test_batch import Event

class VoiceRemovalTests(unittest.IsolatedAsyncioTestCase):
 async def test_stale_voice_flags_do_not_initialize_or_route_audio(self):
  plugin=CompanionBatch(None,{'quiet_seconds':.01,'max_seconds':.02,'voice_enabled':True})
  event=Event('现在继续文字聊天。')
  event.extra={'companion_voice_requested':True,'companion_voice_explicit':True}
  event.get_extra=lambda k,d=None:event.extra.get(k,d)
  event.set_extra=lambda k,v:event.extra.__setitem__(k,v)
  await plugin.merge(event)
  self.assertFalse(event.stopped)
  self.assertEqual(event.message_str,'现在继续文字聊天。')
  self.assertFalse(hasattr(plugin,'voice'))
  self.assertFalse(hasattr(plugin,'receive_voice'))
  self.assertFalse(hasattr(plugin,'reply_voice'))

 async def test_task_truth_instructions_survive_without_audio_service(self):
  plugin=CompanionBatch(None,{})
  event=Event('帮我查资料');req=ProviderRequest()
  await plugin.task_truth_hint(event,req)
  self.assertEqual(len(req.extra_user_content_parts),1)
  self.assertIn('先实际调用适用工具',req.extra_user_content_parts[0].text)
  self.assertFalse(hasattr(plugin,'voice'))

 async def test_verified_friend_texts_still_merge_per_sender(self):
  plugin=CompanionBatch(None,{'quiet_seconds':.04,'max_seconds':.1})
  a=Event('第一句',owner='99');b=Event('第二句',owner='99')
  for event in (a,b):event.get_extra=lambda k,d=None:True if k=='companion_friend_verified' else d
  task=asyncio.create_task(plugin.merge(a));await asyncio.sleep(.01)
  await plugin.merge(b);await task
  self.assertEqual(a.message_str,'第一句\n第二句');self.assertTrue(b.stopped)
  self.assertFalse(plugin.pending)

if __name__=='__main__':unittest.main()
