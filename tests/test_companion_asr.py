import asyncio
import base64
import io
import types
import unittest
import wave
from unittest.mock import patch, AsyncMock
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
            context=types.SimpleNamespace(get_registered_star=lambda name:types.SimpleNamespace(activated=True,star_cls=friend) if name=='astrbot_plugin_companion_friends' else None)
            with patch.object(asr,'_context',context):self.assertEqual(asr.awake(),not sleeping)

    def test_unified_chat_sleep_gate_and_maintenance_take_precedence(self):
        for ready,sleeping,expected in ((True,False,True),(True,True,False),(False,False,False)):
            friends=types.SimpleNamespace(sleeping=lambda:sleeping)
            chat=types.SimpleNamespace(ready=ready,friends=friends)
            legacy=types.SimpleNamespace(sleeping=lambda:False)
            stars={'astrbot_plugin_companion_chat':types.SimpleNamespace(activated=True,star_cls=chat),
                   'astrbot_plugin_companion_friends':types.SimpleNamespace(activated=True,star_cls=legacy)}
            with patch.object(asr,'_context',types.SimpleNamespace(get_registered_star=stars.get)):
                self.assertEqual(asr.awake(),expected)

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


class RegistrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.original_map=dict(asr.provider_cls_map);self.original_registry=list(asr.provider_registry)
        self.original_context=asr._context

    def tearDown(self):
        asr.provider_cls_map.clear();asr.provider_cls_map.update(self.original_map)
        asr.provider_registry[:]=self.original_registry;asr._context=self.original_context

    def own_count(self):return sum(asr.own_adapter(m) for m in asr.provider_registry)

    def test_repeated_registration_and_orphaned_registry_are_idempotent(self):
        for _ in range(5):asr.register_own_adapter(asr.CompanionDashscopeSTT)
        self.assertEqual(self.own_count(),1)
        asr.provider_cls_map.pop(asr.TYPE)
        asr.register_own_adapter(asr.CompanionDashscopeSTT)
        self.assertEqual(self.own_count(),1)

    def test_reimport_after_framework_purges_modules_replaces_only_own_class(self):
        import sys,importlib
        unrelated=object();asr.provider_cls_map['unrelated-test-sentinel']=unrelated
        old=asr.provider_cls_map[asr.TYPE].cls_type
        sys.modules.pop(asr.__name__)
        try:
            new=importlib.import_module(asr.__name__)
            self.assertIsNot(new.CompanionDashscopeSTT,old)
            self.assertIs(new.provider_cls_map[asr.TYPE].cls_type,new.CompanionDashscopeSTT)
            self.assertEqual(self.own_count(),1)
            self.assertIs(asr.provider_cls_map['unrelated-test-sentinel'],unrelated)
            asr.unregister_own_adapter(old)
            self.assertIs(asr.provider_cls_map[asr.TYPE].cls_type,new.CompanionDashscopeSTT)
        finally:
            sys.modules[asr.__name__]=asr
            setattr(sys.modules[asr.__package__],'main',asr)

    def test_foreign_name_collision_is_rejected_without_modifying_registry(self):
        foreign=types.SimpleNamespace(type=asr.TYPE,provider_type=asr.ProviderType.SPEECH_TO_TEXT,
                                      cls_type=type('ForeignSTT',(),{}))
        asr.provider_cls_map[asr.TYPE]=foreign
        before=list(asr.provider_registry)
        with self.assertRaisesRegex(ValueError,'another plugin'):
            asr.register_own_adapter(asr.CompanionDashscopeSTT)
        self.assertIs(asr.provider_cls_map[asr.TYPE],foreign)
        self.assertEqual(asr.provider_registry,before)

    def test_registration_failure_restores_previous_owned_entry(self):
        previous=asr.provider_cls_map[asr.TYPE];before=list(asr.provider_registry)
        with patch.object(asr,'register_provider_adapter',side_effect=RuntimeError('synthetic register error')):
            with self.assertRaises(RuntimeError):asr.register_own_adapter(asr.CompanionDashscopeSTT)
        self.assertIs(asr.provider_cls_map[asr.TYPE],previous)
        self.assertEqual(asr.provider_registry,before)

    async def test_disable_enable_without_module_reimport_recovers_provider(self):
        manager=types.SimpleNamespace(inst_map={},load_provider=AsyncMock(),terminate_provider=AsyncMock())
        context=types.SimpleNamespace(provider_manager=manager,get_config=lambda:{'provider':[
            {'id':'own','type':asr.TYPE,'enable':True}]})
        plugin=object.__new__(asr.CompanionASR);plugin.context=context
        await plugin.initialize();await plugin.terminate()
        self.assertNotIn(asr.TYPE,asr.provider_cls_map)
        await plugin.initialize()
        self.assertIs(asr.provider_cls_map[asr.TYPE].cls_type,asr.CompanionDashscopeSTT)
        self.assertEqual(self.own_count(),1)
        self.assertEqual(manager.load_provider.await_count,2)

    async def test_terminate_unregisters_even_when_provider_termination_fails(self):
        manager=types.SimpleNamespace(inst_map={'own':object()},terminate_provider=AsyncMock(side_effect=RuntimeError('synthetic')))
        context=types.SimpleNamespace(provider_manager=manager,get_config=lambda:{'provider':[
            {'id':'own','type':asr.TYPE,'enable':True}]})
        plugin=object.__new__(asr.CompanionASR);plugin.context=context
        asr._context=context
        with self.assertRaises(RuntimeError):await plugin.terminate()
        self.assertIsNone(asr._context)
        self.assertNotIn(asr.TYPE,asr.provider_cls_map)


if __name__=='__main__':unittest.main()
