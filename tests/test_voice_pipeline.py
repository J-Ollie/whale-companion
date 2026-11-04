"""Run the actual RespondStage and receipt wrapper with an entirely mocked gateway."""
import os,sys,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock,Mock,patch
sys.path.insert(0,os.environ.get('COMPANION_TEST_PLUGINS',str(Path(__file__).parent)))
import test_voice as fixture
from test_voice import Event,OWNER
from astrbot_plugin_companion_friends.main import CompanionFriends
from astrbot_plugin_companion_memory.main import CompanionMemory
from astrbot.core.message.message_event_result import MessageEventResult,ResultContentType
from astrbot.api.message_components import Plain,Record
from astrbot.core.pipeline.respond.stage import RespondStage
from aiocqhttp import CQHttp


class ActionFailed(Exception):retcode=100


class PipelineTests(unittest.IsolatedAsyncioTestCase):
 setUp=fixture.VoiceTests.setUp
 mock_audio=fixture.VoiceTests.mock_audio

 async def test_actual_pipeline_sends_once_and_remembers_receipt_in_both_segment_modes(self):
  for segmented,reject in [(False,False),(True,False),(False,True),(True,True)]:
   with self.subTest(segmented=segmented,reject=reject):
    bot=Mock();adapter=SimpleNamespace(get_client=lambda:bot)
    friend=CompanionFriends.__new__(CompanionFriends)
    friend.owner=OWNER;friend.platform_id='qq-companion';friend.bot_qq='100000002'
    friend.context=SimpleNamespace(get_platform_inst=lambda _:adapter)
    friend.save_chat=Mock();friend.policy=SimpleNamespace(retry_notifications=AsyncMock())
    calls=0
    async def gateway(*args,**kwargs):
     nonlocal calls
     calls+=1
     if reject and calls==1:raise ActionFailed()
     return {'message_id':-99}
    underlying=AsyncMock(side_effect=gateway)
    with patch.object(CQHttp,'call_action',underlying):
     await friend.initialize()
     try:
      self.friends.voice_delivery=friend.voice_delivery
      self.mock_audio();e=Event(voice=False,text='你好')
      e.result=MessageEventResult(chain=[Plain('这是一句自然回复。')]);e.result.result_content_type=ResultContentType.LLM_RESULT
      e.get_sender_name=lambda:'synthetic';e.get_platform_name=lambda:'aiocqhttp'
      e._outline_chain=lambda chain:str([p.type for p in chain])
      e.clear_result=lambda:None
      async def original_send(chain):
       await CQHttp.call_action(bot,'send_private_msg',user_id=OWNER,message=[{'type':'record' if isinstance(p,Record) else 'text','data':{}} for p in chain.chain])
      e.send=original_send;e.set_extra('companion_voice_choice','voice')
      await self.v.outgoing(e)
      stage=RespondStage.__new__(RespondStage);stage.platform_settings={};stage.enable_seg=segmented
      stage.only_llm_result=True;stage.interval_method='random';stage.interval=[0,0]
      memory=CompanionMemory.__new__(CompanionMemory);memory.allowed=lambda e:True;memory.record_bot=Mock()
      async def after(event,kind):await memory.observe_bot(event);return False
      with patch('astrbot.core.pipeline.respond.stage.call_event_hook',side_effect=after):await stage.process(e)
      self.assertEqual(underlying.await_count,2 if reject else 1)
      first=underlying.await_args_list[0].kwargs['message'];self.assertEqual([p['type'] for p in first],['record'])
      if reject:self.assertEqual([p['type'] for p in underlying.await_args_list[1].kwargs['message']],['text'])
      memory.record_bot.assert_called_once_with('这是一句自然回复。')
     finally:await friend.terminate()


if __name__=='__main__':unittest.main()
