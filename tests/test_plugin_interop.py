"""Current bridge and active services together, with only HTTP/QQ boundaries mocked."""
import ast
import base64
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock, MagicMock, patch

sys.path.insert(0,os.environ.get('COMPANION_TEST_PLUGINS',str(Path(__file__).parent)))
from astrbot_plugin_companion_breeze.client import BreezeClient
from astrbot_plugin_companion_breeze.main import CompanionBreeze
from astrbot_plugin_companion_chat.main import CompanionChat
from astrbot_plugin_companion_chat.lifecycle import ConsumerContext
from astrbot_plugin_companion_chat.friends.social import render_public,render_personal
from astrbot.core.agent.message import Message,TextPart
from astrbot.api.provider import LLMResponse
from astrbot.api.message_components import Plain,Record
from astrbot_plugin_companion_phone.public_notebook import PublicNotebook
import test_commitments as flow
import test_shared_memory as shared_fixture
from test_breeze_bridge import Event

import subprocess
from astrbot_plugin_companion_chat.breeze.audio import prepare_qq_audio

SOURCE_MP3=subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i',
    'sine=frequency=983:sample_rate=24000:duration=1.133','-c:a','libmp3lame',
    '-f','mp3','pipe:1'],capture_output=True,check=True).stdout
AUDIO=prepare_qq_audio(SOURCE_MP3)
TEXT='我想和你说句话'
DIRECTION='自然，带一点期待'


def opener():
    context=MagicMock();context.__enter__.return_value=NS(status=200,read=lambda _:SOURCE_MP3)
    return NS(open=Mock(return_value=context))


class VoiceContractTests(unittest.IsolatedAsyncioTestCase):
    setUp=flow.FlowTests.setUp
    due=flow.FlowTests.due

    def actual_client(self):
        client=BreezeClient(self.root/'standalone-voice')
        self.voice.client=client
        return client

    async def test_active_preparation_uses_actual_standalone_signature_and_payload(self):
        client=self.actual_client();http=opener()
        with patch.object(client,'key',return_value='synthetic'),patch('urllib.request.build_opener',return_value=http):
            encoded=await self.plugin.prepare_voice(TEXT,DIRECTION)
        self.assertEqual(base64.b64decode(encoded),AUDIO)
        body=json.loads(http.open.call_args.args[0].data)
        self.assertEqual(body,{'text':TEXT,'instructions':DIRECTION,'model_id':'breeze-tts-2','language_code':'zh'})
        self.assertEqual(json.loads((client.root/'state.json').read_text())['tts'],1)
        self.transport.call_action.assert_not_awaited()

    async def test_due_commitment_reaches_fake_platform_using_actual_standalone_client(self):
        client=self.actual_client();http=opener()
        with patch.object(client,'key',return_value='synthetic'),patch('urllib.request.build_opener',return_value=http),patch('time.time',return_value=flow.NOW+20):
            await self.plugin.fulfill(self.due(),self.phone,{})
        self.assertEqual(self.ledger.evidence()[0]['attempts'][0]['status'],'submitted')
        self.transport.call_action.assert_awaited_once()
        self.assertEqual(self.plugin.outreach_ledger().snapshot(now=flow.NOW+20)['events'][-1]['result'],'submitted')

    async def test_spontaneous_path_reaches_fake_platform_using_actual_standalone_client(self):
        client=self.actual_client();http=opener()
        with patch.object(client,'key',return_value='synthetic'),patch('urllib.request.build_opener',return_value=http):
            await flow.FlowTests.test_spontaneous_path_also_delivers_only_voice(self)
        self.assertEqual(http.open.call_count,1)

    async def test_invalid_direction_is_rejected_before_credentials_or_quota(self):
        client=self.actual_client()
        with patch.object(client,'key') as key:
            with self.assertRaises(ValueError):client.synthesize(TEXT,'换成男声')
            key.assert_not_called()
        self.assertFalse((client.root/'state.json').exists())


class RoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_external_and_merged_hooks_share_one_contract_and_clean_native_history(self):
        with tempfile.TemporaryDirectory() as folder:
            external=object.__new__(CompanionBreeze);external.config={'enabled':True};external.busy=False
            external.client=BreezeClient(folder)
            friends=NS(sleeping=lambda:False,voice_delivery=Mock())
            merged=object.__new__(CompanionChat);merged.ready=True;merged.friends=friends
            merged.breeze=NS(voice_hint=AsyncMock(),choose_medium=AsyncMock(),reply_voice=AsyncMock())
            base=NS(get_registered_star=lambda name:NS(activated=True,star_cls=external if name=='astrbot_plugin_companion_breeze' else merged) if name in ('astrbot_plugin_companion_breeze','astrbot_plugin_companion_chat') else None)
            merged.context=base;external.context=ConsumerContext(base)
            event=Event();event.message_str='今天想聊什么';event.send=AsyncMock()
            req=NS(system_prompt='',extra_user_content_parts=[])
            await merged.voice_hint(event,req);await external.voice_hint(event,req)
            self.assertEqual(req.system_prompt.count('[companion_voice_performance]'),1)
            self.assertNotIn('[companion_voice_routing]',req.system_prompt)
            raw='[[voice]]'+json.dumps({'text':TEXT,'instructions':DIRECTION},ensure_ascii=False)
            response=LLMResponse(role='assistant',completion_text=raw)
            await merged.choose_medium(event,response);await external.choose_medium(event,response)
            self.assertEqual(response.completion_text,TEXT)
            previous=Message(role='assistant',content=[TextPart(text='旧记录不改')])
            history=NS(messages=[previous,Message(role='assistant',content=[TextPart(text=raw)])])
            await merged.sanitize_voice_history(event,history,response)
            await external.sanitize_voice_history(event,history,response)
            self.assertEqual(history.messages[0].content[0].text,'旧记录不改')
            self.assertEqual(history.messages[-1].content[0].text,TEXT)
            event.result.chain=[Plain(response.completion_text)]
            external.guard_send=lambda *a:True
            http=opener()
            with patch.object(external.client,'key',return_value='synthetic'),patch('urllib.request.build_opener',return_value=http):
                await merged.reply_voice(event);await external.reply_voice(event)
            self.assertEqual(http.open.call_count,1);self.assertIsInstance(event.result.chain[0],Record)
            self.assertEqual(event.result.chain[0].text,TEXT)
            self.assertEqual(json.loads(http.open.call_args.args[0].data)['instructions'],DIRECTION)
            event.send.assert_not_awaited()
            merged.breeze.voice_hint.assert_not_awaited();merged.breeze.reply_voice.assert_not_awaited()


class NamespaceTests(unittest.TestCase):
    def test_no_bare_peer_import_remains_in_active_companion_sources(self):
        root=Path(os.environ['COMPANION_TEST_PLUGINS'])
        bad=[]
        for name in ('chat','memory','phone','breeze'):
            for file in (root/('astrbot_plugin_companion_'+name)).rglob('*.py'):
                for node in ast.walk(ast.parse(file.read_text(encoding='utf-8'))):
                    if isinstance(node,ast.ImportFrom) and (node.module or '').startswith('astrbot_plugin_companion_'):
                        bad.append((str(file),node.lineno))
        self.assertEqual(bad,[])
        self.assertTrue(CompanionBreeze.__module__.startswith('data.plugins.'))

    def test_friend_public_and_personal_fallback_render_with_native_package_names(self):
        public=render_public({'known_people':[{'name':'F1nger','public_role':'管理员'}]})
        self.assertIn('F1nger',public)
        archive=NS(context_messages=lambda *a,**k:[{'text':'这位联系人的合成旧话'}],contact_summary=lambda qq:{'contact':'self'})
        self.assertIn('合成旧话',render_personal(archive,'12345678'))


class PublicKnowledgeTests(unittest.IsolatedAsyncioTestCase):
    setUp=shared_fixture.SharedTests.setUp
    event=shared_fixture.SharedTests.event

    async def test_notebook_present_branch_syncs_public_source_without_private_records(self):
        root=self.store.folder.parent/'companion_phone'
        book=PublicNotebook(root);body='这是一段已经摘取的合成公开资料。'*12
        import hashlib
        book.store({'material':'web_text','url':'https://example.org/public','title':'合成资料','text':body,
                    'content_hash':hashlib.sha256(body.encode()).hexdigest(),'retrieved_at':1000})
        self.store.observe(self.p.owner,'user','PRIVATE_SENTINEL')
        self.s.source_sync()
        rows=self.s.db.execute("SELECT text FROM records WHERE kind='public_knowledge'").fetchall()
        self.assertEqual(len(rows),1);self.assertIn(body,rows[0]['text'])
        self.assertNotIn('PRIVATE_SENTINEL',rows[0]['text'])
