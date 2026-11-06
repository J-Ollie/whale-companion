"""Actual-call metadata, native caption contract, isolation and reload ownership."""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
from types import ModuleType,SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock,Mock,patch
sys.path.insert(0,os.environ.get('COMPANION_TEST_PLUGINS',str(Path(__file__).parent)))
from astrbot.core import astr_main_agent
from astrbot.core.platform.astr_message_event import current_message_event
from astrbot.core.provider.register import provider_cls_map,provider_registry
from astrbot_plugin_companion_chat.perception import PerceptionObserver,FRAME
from astrbot_plugin_companion_chat.perception_store import PerceptionStore
from astrbot_plugin_companion_chat.main import CompanionChat
from astrbot_plugin_companion_memory.memory import MemoryStore

A='12345678';B='87654321'
def scope(qq):return 'qq-companion:FriendMessage:'+qq

class Event:
    def __init__(self,qq=A,mid='1001',group=''):
        self.qq=qq;self.group=group;self.unified_msg_origin=scope(qq)
        self.message_obj=NS(message_id=mid);self.extra={};self.message_str='synthetic'
    def get_platform_id(self):return 'qq-companion'
    def get_sender_id(self):return self.qq
    def get_group_id(self):return self.group
    def get_extra(self,k,default=None):return self.extra.get(k,default)
    def set_extra(self,k,value):self.extra[k]=value

class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.store=PerceptionStore(self.tmp.name)
    def test_only_metadata_persisted(self):
        ref='https://private.invalid/audio?token=SECRET_ACCESS';text='PRIVATE_TRANSCRIPT'
        rid=self.store.begin(scope(A),'1001',ref,'audio_transcript');self.store.finish(rid,scope(A),'returned',text)
        with self.store.connect() as db:row=dict(db.execute('SELECT * FROM events').fetchone())
        self.assertNotIn(ref,json.dumps(row));self.assertNotIn(text,json.dumps(row))
        self.assertEqual(row['result_hash'],hashlib.sha256(text.encode()).hexdigest())
    def test_cross_scope_finish_and_read_are_rejected(self):
        rid=self.store.begin(scope(A),'1','local','image_caption')
        self.assertFalse(self.store.finish(rid,scope(B),'returned','caption'))
        self.assertEqual(self.store.recent(scope(B))['events'],[])
    def test_later_error_cannot_erase_returned_result(self):
        rid=self.store.begin(scope(A),'1','local','image_caption')
        self.assertTrue(self.store.finish(rid,scope(A),'returned','caption'))
        self.assertFalse(self.store.finish(rid,scope(A),'failed',error_type='Error'))
    def test_attempt_and_empty_are_not_completed_results(self):
        rid=self.store.begin(scope(A),'1','local','audio_transcript')
        self.assertEqual(self.store.recent(scope(A))['events'][0]['status'],'attempted')
        self.store.finish(rid,scope(A),'returned','  ')
        self.assertEqual(self.store.recent(scope(A))['events'][0]['status'],'empty')
    def test_metadata_does_not_assert_tone_or_direct_vision(self):
        for kind in ('audio_transcript','image_caption'):
            rid=self.store.begin(scope(A),'1',kind,kind);self.store.finish(rid,scope(A),'returned','synthetic')
        out=self.store.recent(scope(A));self.assertTrue(all('不证明' in e['coverage'] for e in out['events']))
    def test_query_is_bounded_and_future_rows_hidden(self):
        for i in range(12):self.store.begin(scope(A),str(i),'x','image_caption')
        self.store.begin(scope(A),'future','x','image_caption',now=10**10)
        result=self.store.recent(scope(A));self.assertLessEqual(len(result['events']),4)
        self.assertTrue(result['more']);self.assertLessEqual(len(json.dumps(result,ensure_ascii=False,separators=(',',':')).encode()),1600)
        self.assertNotIn('future',[r['message_id'] for r in result['events']])
    def test_reopen_preserves_records_and_start_time(self):
        before=self.store.recent(scope(A))['started_at'];rid=self.store.begin(scope(A),'1','x','image_caption')
        other=PerceptionStore(self.tmp.name);self.assertEqual(other.recent(scope(A))['started_at'],before)
        self.assertEqual(other.recent(scope(A))['events'][0]['id'],rid)
    def test_error_text_is_not_stored(self):
        rid=self.store.begin(scope(A),'1','x','image_caption')
        self.store.finish(rid,scope(A),'failed',error_type='SECRET /private/url')
        with self.store.connect() as db:self.assertEqual(db.execute('SELECT error_type FROM events').fetchone()[0],'')

class ObserverTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.calls=[]
        self.stt_result='PRIVATE_TRANSCRIPT';self.spectrum_result='spectrum summary';self.error=None;self.sleeping=False
        case=self
        class Adapter:
            async def get_text(self,audio_url):
                case.calls.append(('stt',audio_url));await asyncio.sleep(0)
                if case.error is not None:raise case.error
                return case.stt_result
        class Pipeline:
            def __init__(self):self.stt=Adapter()
            async def _run(self,event):
                ref='/private/'+str(event.message_obj.message_id)+'.wav'
                a=await self.stt.get_text(ref);b=await self._spectrum_for(ref)
                return (a,b)
            async def _spectrum_for(self,path,ffmpeg='ffmpeg',settings=None):
                case.calls.append(('spectrum',path));return case.spectrum_result
        self.Adapter=Adapter;self.Pipeline=Pipeline;self.pipeline=Pipeline()
        module=ModuleType('synthetic_asr_generation');module.CompanionDashscopeSTT=Adapter
        Star=type('ASRStar',(),{});Star.__module__=module.__name__
        self.module=module;self.patch_modules=patch.dict(sys.modules,{module.__name__:module});self.patch_modules.start();self.addCleanup(self.patch_modules.stop)
        self.memory=MemoryStore(Path(self.tmp.name)/'memory');self.addCleanup(self.memory.close)
        self.stars={'astrbot_plugin_companion_asr':NS(activated=True,star_cls=Star()),
            'astrbot_plugin_multimodal_enhance':NS(activated=True,star_cls=NS(pipeline=self.pipeline)),
            'astrbot_plugin_companion_memory':NS(activated=True,star_cls=NS(store=self.memory))}
        self.chat=NS(ready=True,context=NS(get_registered_star=self.stars.get),
            friends=NS(policy=NS(root=Path(self.tmp.name)/'friends'),owner=A,sleeping=lambda:self.sleeping),
            batch=NS(allowed=lambda e:e.get_sender_id() in (A,B) and not e.get_group_id()))
        self.native_caption=astr_main_agent._request_img_caption
        async def caption(provider_id,cfg,image_urls,plugin_context,montage_refs=None):
            self.calls.append(('caption',tuple(image_urls)))
            if self.error is not None:raise self.error
            return 'PRIVATE_CAPTION'
        self.caption_patch=patch.object(astr_main_agent,'_request_img_caption',caption);self.caption_patch.start();self.addCleanup(self.caption_patch.stop)
        self.observer=PerceptionObserver(self.chat);self.addCleanup(self.observer.close);self.observer.refresh()
    def records(self):
        with self.observer.store.connect() as db:return [dict(r) for r in db.execute('SELECT * FROM events ORDER BY rowid')]
    async def test_records_real_returns_without_calling_again(self):
        result=await self.pipeline._run(Event())
        self.assertEqual(result,('PRIVATE_TRANSCRIPT','spectrum summary'))
        self.assertEqual(len(self.calls),2);self.assertEqual([r['status'] for r in self.records()],['returned','returned'])
        self.assertNotIn('PRIVATE_TRANSCRIPT',json.dumps(self.records()));self.assertNotIn('/private/',json.dumps(self.records()))
    async def test_stt_outside_observed_pipeline_not_misattributed(self):
        self.assertEqual(await self.pipeline.stt.get_text('/private.wav'),'PRIVATE_TRANSCRIPT')
        self.assertEqual(self.records(),[])
    async def test_caption_uses_actual_current_event_and_returns_unchanged(self):
        token=current_message_event.set(Event())
        try:r=await astr_main_agent._request_img_caption('synthetic',{},['/private/picture.jpg'],None)
        finally:current_message_event.reset(token)
        self.assertEqual(r,'PRIVATE_CAPTION');self.assertEqual(len(self.calls),1)
        self.assertEqual(self.records()[0]['kind'],'image_caption')
    async def test_no_event_no_caption_record(self):
        token=current_message_event.set(None)
        try:await astr_main_agent._request_img_caption('synthetic',{},['x'],None)
        finally:current_message_event.reset(token)
        self.assertEqual(self.records(),[])
    async def test_actual_native_caption_contract(self):
        from astrbot.core.provider.provider import Provider
        astr_main_agent._request_img_caption=self.native_caption;self.observer.refresh()
        provider=Mock(spec=Provider);provider.text_chat=AsyncMock(return_value=NS(completion_text='synthetic description'))
        token=current_message_event.set(Event())
        try:result=await astr_main_agent._request_img_caption('synthetic',{'image_caption_prompt':'describe'},['synthetic://image'],NS(get_provider_by_id=lambda _:provider))
        finally:current_message_event.reset(token)
        self.assertEqual(result,'synthetic description');provider.text_chat.assert_awaited_once_with(prompt='describe',image_urls=['synthetic://image'])
        self.assertEqual(self.records()[0]['status'],'returned')
    async def test_parallel_contacts_keep_separate_scope(self):
        await asyncio.gather(self.pipeline._run(Event(A,'1')),self.pipeline._run(Event(B,'2')))
        rows=self.records();self.assertEqual(len(rows),4)
        self.assertTrue(all(r['message_id']=='1' for r in rows if r['scope']==scope(A)))
        self.assertTrue(all(r['message_id']=='2' for r in rows if r['scope']==scope(B)))
        self.assertIsNone(FRAME.get())
    async def test_sleeping_and_groups_capture_nothing(self):
        self.sleeping=True;await self.pipeline._run(Event())
        self.sleeping=False;await self.pipeline._run(Event(group='synthetic-group'))
        self.assertEqual(self.records(),[])
    async def test_unknown_id_or_spoofed_scope_capture_nothing(self):
        await self.pipeline._run(Event(mid=''))
        event=Event();event.unified_msg_origin=scope(B);await self.pipeline._run(event)
        self.assertEqual(self.records(),[])
    async def test_failure_propagates_once_without_error_body(self):
        self.error=ValueError('SECRET private request body')
        with self.assertRaises(ValueError):await self.pipeline._run(Event())
        self.assertEqual(len(self.calls),1);self.assertEqual(self.records()[0]['status'],'failed')
        self.assertEqual(self.records()[0]['error_type'],'ValueError');self.assertNotIn('SECRET',json.dumps(self.records()))
    async def test_cancellation_is_not_a_success(self):
        self.error=asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):await self.pipeline._run(Event())
        self.assertEqual(self.records()[0]['status'],'cancelled');self.assertIsNone(FRAME.get())
    async def test_empty_results_not_completed(self):
        self.stt_result=' ';self.spectrum_result=''
        await self.pipeline._run(Event());self.assertEqual([r['status'] for r in self.records()],['empty','empty'])
    async def test_store_failure_does_not_change_media_result(self):
        with patch.object(self.observer.store,'begin',side_effect=sqlite3.OperationalError('PRIVATE_ERROR')):
            result=await self.pipeline._run(Event())
        self.assertEqual(result,('PRIVATE_TRANSCRIPT','spectrum summary'));self.assertEqual(len(self.calls),2)
    async def test_finish_failure_leaves_attempted_and_preserves_result(self):
        with patch.object(self.observer.store,'finish',side_effect=sqlite3.OperationalError('PRIVATE_ERROR')):
            result=await self.pipeline._run(Event())
        self.assertEqual(result[0],'PRIVATE_TRANSCRIPT');self.assertTrue(all(r['status']=='attempted' for r in self.records()))
    async def test_refresh_does_not_stack_or_register_providers(self):
        registry=list(provider_registry);mapping=dict(provider_cls_map);method=self.Adapter.get_text
        for _ in range(4):self.observer.refresh()
        self.assertIs(self.Adapter.get_text,method);self.assertEqual(provider_registry,registry);self.assertEqual(provider_cls_map,mapping)
        await self.pipeline._run(Event());self.assertEqual(len(self.records()),2)
    async def test_close_restores_original_behavior_without_capture(self):
        self.observer.close();await self.pipeline._run(Event())
        self.assertEqual(len(self.calls),2);self.assertEqual(self.records(),[])
    async def test_old_termination_does_not_remove_new_observer(self):
        newer=PerceptionObserver(self.chat);self.addCleanup(newer.close);newer.refresh();method=self.Pipeline._run
        self.observer.close();self.assertIs(self.Pipeline._run,method)
        await self.pipeline._run(Event());self.assertEqual(len(self.records()),2)
    async def test_adapter_generation_can_refresh_without_import(self):
        calls=self.calls
        class NewAdapter:
            async def get_text(self,audio_url):calls.append(('new_stt',audio_url));return 'new transcript'
        self.module.CompanionDashscopeSTT=NewAdapter;self.pipeline.stt=NewAdapter();self.observer.refresh()
        await self.pipeline._run(Event());self.assertEqual(self.records()[0]['status'],'returned');self.assertEqual(self.calls[0][0],'new_stt')
    async def test_real_asr_adapter_keeps_registration_and_sleep_gate(self):
        from astrbot_plugin_companion_asr import main as real_asr
        metadata=provider_cls_map[real_asr.TYPE];registry=list(provider_registry)
        star=object.__new__(real_asr.CompanionASR)
        self.stars['astrbot_plugin_companion_asr']=NS(activated=True,star_cls=star)
        self.pipeline.stt=real_asr.CompanionDashscopeSTT({'id':'isolated','api_key':'isolated-test-key','model':'qwen-audio-3.0-asr-flash'}, {})
        self.observer.refresh()
        with patch.object(real_asr,'awake',return_value=False),patch.object(real_asr,'MediaResolver') as resolver:
            with self.assertRaises(RuntimeError):await self.pipeline._run(Event())
            resolver.assert_not_called()
        self.assertIs(provider_cls_map[real_asr.TYPE],metadata);self.assertEqual(provider_registry,registry)
        self.assertEqual(self.records()[0]['status'],'failed')
    async def test_source_suppression_hides_metadata_from_tool_query(self):
        sid=self.memory.observe(A,'user','synthetic',origin=scope(A),dedupe='qq:'+A+':1001')
        await self.pipeline._run(Event());self.assertTrue(self.observer.recent(scope(A))['events'])
        self.memory.db.execute('INSERT INTO suppressed_sources VALUES(?,?)',(A,sid))
        self.assertEqual(self.observer.recent(scope(A))['events'],[])
    async def test_unavailable_memory_does_not_expose_source_metadata(self):
        await self.pipeline._run(Event());self.stars.pop('astrbot_plugin_companion_memory')
        self.assertEqual(self.observer.recent(scope(A))['events'],[])
    async def test_self_status_retains_owner_gate_and_three_call_limit(self):
        await self.pipeline._run(Event());root=NS(ready=True,active=NS(umo=scope(A)),friends=self.chat.friends,perceptions=self.observer)
        foreign=await CompanionChat.self_status(root,Event(B),'perception');self.assertTrue(foreign.isError)
        event=Event()
        for _ in range(3):
            result=await CompanionChat.self_status(root,event,'perception');self.assertFalse(result.isError)
            value=json.loads(result.content[0].text);self.assertTrue(value['events'])
        self.assertTrue((await CompanionChat.self_status(root,event,'perception')).isError)

if __name__=='__main__':unittest.main()
