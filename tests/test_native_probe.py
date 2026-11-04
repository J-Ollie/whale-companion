"""One-shot local maintenance permits never bypass sleep or native readiness."""
import os,sys,tempfile,unittest,json,time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock,Mock,patch
sys.path.insert(0,os.environ.get('COMPANION_TEST_PLUGINS',str(Path(__file__).parent)))
from astrbot_plugin_companion_phone.main import CompanionPhone


class ProbeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.p=CompanionPhone.__new__(CompanionPhone);self.p.root=Path(self.tmp.name)
        self.p.executor=SimpleNamespace(allowed_apps=lambda:['com.vivo.weather']);self.p.executor_allowed=Mock(return_value=True)
        self.p.phone=SimpleNamespace(native_available=Mock(return_value=True),open_app=AsyncMock(),
            screenshot=AsyncMock(return_value=(b'fake-image',(1080,2376),(818,1800))),
            ui_observation=AsyncMock(return_value={'available':True,'nodes':[{}]}),key=AsyncMock())
        self.nonce='a'*32

    def permit(self):
        now=time.time();(self.p.root/'native-test-permit.json').write_text(json.dumps({'nonce':self.nonce,'created':now-1,'expires':now+60}))

    async def test_no_permit_or_wrong_nonce_cannot_operate(self):
        self.assertFalse((await self.p.executor_self_test(self.nonce))['ok']);self.permit()
        self.assertFalse((await self.p.executor_self_test('b'*32))['ok']);self.p.phone.open_app.assert_not_called()

    async def test_sleep_cannot_be_bypassed_by_operator_permit(self):
        self.permit();self.p.executor_allowed.return_value=False
        self.assertEqual((await self.p.executor_self_test(self.nonce))['code'],'not_awake_or_native_unavailable')
        self.p.phone.open_app.assert_not_called()

    async def test_probe_is_single_use_and_returns_only_metadata(self):
        self.permit()
        with patch('asyncio.sleep',AsyncMock()):result=await self.p.executor_self_test(self.nonce)
        self.assertTrue(result['ok']);self.assertNotIn('fake-image',json.dumps(result))
        self.p.phone.key.assert_awaited_once_with('BACK')
        self.assertFalse((await self.p.executor_self_test(self.nonce))['ok'])
