import asyncio
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock,patch
from astrbot_plugin_companion_bailian_sing.main import BailianSingingPreview
from test_bailian_singing_backend import sample


class PluginTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.awake=True
        self.voice=SimpleNamespace(root=self.root,permitted=lambda e:not e.group,
            awake=lambda:self.awake,plugin=SimpleNamespace(config={'owner_qq':'42'}),reserve=AsyncMock(return_value=True),
            singing=SimpleNamespace(clip=lambda ref:({'lyrics':'晚风轻轻','title':'测试'},sample(),'release')))
        self.p=BailianSingingPreview.__new__(BailianSingingPreview)
        self.p.config={'enabled':False};self.p.lock=asyncio.Lock()
        self.p.context=SimpleNamespace(get_registered_star=lambda name:SimpleNamespace(activated=True,star_cls=SimpleNamespace(voice=self.voice)))
        self.e=SimpleNamespace(message_str='试听',message_obj=SimpleNamespace(message_id='7'),group='',
            unified_msg_origin='test:FriendMessage:42',get_sender_id=lambda:'42',send=AsyncMock())
        raw=b'ID3 reference'
        (self.root/'singing-reference.mp3').write_bytes(raw)
        (self.root/'singing-reference.json').write_text(json.dumps({'sha256':hashlib.sha256(raw).hexdigest()}))

    async def test_status_does_not_claim_native_or_validated_singing(self):
        result=json.loads(await self.p.preview(self.e,'status'))
        self.assertFalse(result['voice_id_directly_supported'])
        self.assertFalse(result['quality_accepted'])
        self.assertFalse(result['auto_send'])

    async def test_default_disabled_cannot_generate_or_spend_quota(self):
        result=json.loads(await self.p.preview(self.e,'preview','score-test'))
        self.assertFalse(result['ok']);self.voice.reserve.assert_not_called()

    async def test_owner_only_groups_and_sleep_are_blocked(self):
        self.e.get_sender_id=lambda:'43'
        self.assertFalse(json.loads(await self.p.preview(self.e))['ok'])
        self.e.get_sender_id=lambda:'42';self.e.group='group'
        self.assertFalse(json.loads(await self.p.preview(self.e))['ok'])
        self.e.group='';self.awake=False
        self.assertFalse(json.loads(await self.p.preview(self.e))['ok'])
        self.voice.reserve.assert_not_called()

    async def test_changed_voice_reference_rejected_before_paying(self):
        self.p.config['enabled']=True
        (self.root/'singing-reference.mp3').write_bytes(b'changed')
        self.assertFalse(json.loads(await self.p.preview(self.e,'preview','score-test'))['ok'])
        self.voice.reserve.assert_not_called()

    async def test_result_identifier_cannot_read_other_files(self):
        self.p.config['enabled']=True
        self.assertFalse(json.loads(await self.p.preview(self.e,'result',reference='../../cloud-voice.json'))['ok'])

    async def test_generated_preview_is_unaccepted_unsent_and_idempotent(self):
        self.p.config['enabled']=True
        with patch('astrbot_plugin_companion_bailian_sing.main.decode_audio',return_value=(sample(),3)),\
             patch('astrbot_plugin_companion_bailian_sing.main.generate_raw',return_value=(sample(),{'human_approved':False})) as generate,\
             patch('astrbot_plugin_companion_batch.voice_cloud.settings',return_value={'api_key':'PRIVATE_TEST_VALUE'}):
            first=json.loads(await self.p.preview(self.e,'preview','score-test'))
            second=json.loads(await self.p.preview(self.e,'preview','score-test'))
        self.assertEqual(generate.call_count,1)
        self.assertEqual(first,second)
        self.assertEqual(first['state'],'awaiting_audio_and_human_checks')
        self.assertFalse(first['sent']);self.assertFalse(first['adopted'])
        self.assertNotIn('PRIVATE_TEST_VALUE',json.dumps(first))
        self.assertEqual(self.voice.reserve.await_count,2)
        self.e.send.assert_not_called()


if __name__=='__main__':unittest.main()
