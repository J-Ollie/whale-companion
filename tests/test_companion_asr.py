import asyncio
import base64
import io
import types
import unittest
import wave
from unittest.mock import patch
from astrbot_plugin_companion_asr import main as asr


def wav_data(seconds=1, rate=16000):
    buf=io.BytesIO()
    with wave.open(buf,'wb') as f:
        f.setnchannels(1);f.setsampwidth(2);f.setframerate(rate);f.writeframes(b'\0\0'*(seconds*rate))
    return base64.b64encode(buf.getvalue()).decode()


class AdapterTests(unittest.IsolatedAsyncioTestCase):
    def test_native_payload_contains_no_chat_context(self):
        p=asr.build_payload('qwen-audio-3.0-asr-flash','data:audio/wav;base64,abc',16000)
        self.assertEqual(p['parameters'],{'format':'wav','sample_rate':'16000'})
        self.assertEqual(p['input']['messages'],[{'role':'user','content':[{'type':'input_audio','input_audio':{'data':'data:audio/wav;base64,abc'}}]}])

    def test_parse_native_and_nested(self):
        self.assertEqual(asr.parse_transcript({'output':{'text':' hello '}}),'hello')
        self.assertEqual(asr.parse_transcript({'output':{'output':{'text':'world'}}}),'world')
        with self.assertRaises(RuntimeError):asr.parse_transcript({'output':{}})

    def test_sleep_gate_closed_without_context(self):
        with patch.object(asr,'_context',None):self.assertFalse(asr.awake())

    def test_sleep_gate_uses_friends_policy(self):
        for sleeping in [True,False]:
            friend=types.SimpleNamespace(sleeping=lambda:sleeping)
            context=types.SimpleNamespace(get_registered_star=lambda name:types.SimpleNamespace(activated=True,star_cls=friend))
            with patch.object(asr,'_context',context):self.assertEqual(asr.awake(),not sleeping)

    async def test_asleep_never_resolves_audio(self):
        provider=asr.CompanionDashscopeSTT({'id':'test','api_key':'isolated-test-key','model':'qwen-audio-3.0-asr-flash'}, {})
        with patch.object(asr,'awake',return_value=False),patch.object(asr,'MediaResolver') as resolver:
            with self.assertRaisesRegex(RuntimeError,'asleep'):await provider.get_text('/never/read/private.wav')
            resolver.assert_not_called()

    async def test_sleep_during_conversion_never_calls_network(self):
        encoded=wav_data()
        class Resolver:
            def __init__(self,*a,**k):pass
            async def to_base64_data(self,**k):return types.SimpleNamespace(base64_data=encoded,to_data_url=lambda:'data:audio/wav;base64,'+encoded)
        provider=asr.CompanionDashscopeSTT({'id':'test','api_key':'isolated-test-key','model':'qwen-audio-3.0-asr-flash'}, {})
        with patch.object(asr,'awake',side_effect=[True,False]),patch.object(asr,'MediaResolver',Resolver),patch.object(asr.aiohttp,'ClientSession') as client:
            with self.assertRaisesRegex(RuntimeError,'asleep'):await provider.get_text('/synthetic.wav')
            client.assert_not_called()

    async def test_duration_limit_before_network(self):
        encoded=wav_data(seconds=301,rate=8000)
        class Resolver:
            def __init__(self,*a,**k):pass
            async def to_base64_data(self,**k):return types.SimpleNamespace(base64_data=encoded)
        provider=asr.CompanionDashscopeSTT({'id':'test','api_key':'isolated-test-key','model':'qwen-audio-3.0-asr-flash'}, {})
        with patch.object(asr,'awake',return_value=True),patch.object(asr,'MediaResolver',Resolver),patch.object(asr.aiohttp,'ClientSession') as client:
            with self.assertRaisesRegex(ValueError,'five minutes'):await provider.get_text('/synthetic.wav')
            client.assert_not_called()

    async def test_restart_loads_only_own_saved_provider(self):
        loaded=[]
        async def load(c):loaded.append(c['id'])
        manager=types.SimpleNamespace(inst_map={},load_provider=load)
        context=types.SimpleNamespace(provider_manager=manager,get_config=lambda:{'provider':[
            {'id':'own','type':asr.TYPE,'enable':True},
            {'id':'other','type':'openai_chat_completion','enable':True},
            {'id':'disabled','type':asr.TYPE,'enable':False}]})
        plugin=object.__new__(asr.CompanionASR);plugin.context=context
        with patch.object(asr,'_context',None):await plugin.initialize()
        self.assertEqual(loaded,['own'])


if __name__=='__main__':unittest.main()
