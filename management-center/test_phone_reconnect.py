import asyncio
import importlib.util
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock,Mock
from phone_health import PhoneHealth,endpoint
from test_guest import GuestHttpTests


class ReconnectTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.runner=Mock(return_value=b'{"ok":true,"code":"connected"}')
        self.health=PhoneHealth(self.root,self.runner,'qq-companion-astrbot')

    def test_only_paired_phone_and_valid_connection_port_are_allowed(self):
        for ip,port in [('127.0.0.1',123),('100.64.0.10','4;reboot'),('100.64.0.10',True),
                        ('100.64.0.10',0),('100.64.0.10',65536)]:
            with self.assertRaises(ValueError):self.health.reconnect(ip,port)
        self.runner.assert_not_called()
        self.assertEqual(endpoint('100.64.0.10','12345'),'100.64.0.10:12345')

    def test_success_cache_rate_limit_and_no_false_success(self):
        result=self.health.reconnect('100.64.0.10','12345')
        self.assertEqual(result['status'],'connected')
        with self.assertRaises(ValueError):self.health.reconnect('100.64.0.10','12345')
        self.assertEqual(self.runner.call_count,1)
        self.health.last_reconnect=0;self.runner.return_value=b'{"ok":false,"code":"connection_failed","raw":"SECRET"}'
        with self.assertRaises(ValueError) as error:self.health.reconnect('100.64.0.10','12345')
        self.assertNotIn('SECRET',str(error.exception))


class ControlTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        package=types.ModuleType('control_probe');package.__path__=[]
        phone_module=types.ModuleType('control_probe.phone');phone_module.PhoneError=type('PhoneError',(Exception,),{})
        sys.modules['control_probe']=package;sys.modules['control_probe.phone']=phone_module
        source=Path(__file__).parent/'connection_control.py'
        spec=importlib.util.spec_from_file_location('control_probe.connection_control',source)
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        self.error=phone_module.PhoneError
        self.phone=types.SimpleNamespace(root=Path(self.tmp.name),state={'serial':'100.64.0.10:11111','paused':True},
            lock=asyncio.Lock(),screen='old',_connect_once=AsyncMock(),_shell=AsyncMock(side_effect=['V2046A','whale-link-ok']))
        def validate(serial):
            ip,port=serial.rsplit(':',1);endpoint(ip,port)
        self.phone.validate_serial=validate
        self.state=self.phone.root/'state.json'
        self.phone._save=lambda:self.state.write_text(json.dumps(self.phone.state))
        self.phone._save();self.control=module.ConnectionControl(self.phone)

    async def test_live_state_and_disk_updated_together_without_unpausing_or_apps(self):
        result=await self.control.reconnect('100.64.0.10:22222')
        self.assertTrue(result['ok']);self.assertEqual(self.phone.state['serial'],'100.64.0.10:22222')
        self.assertEqual(json.loads(self.state.read_text())['serial'],self.phone.state['serial'])
        self.assertTrue(self.phone.state['paused']);self.assertIsNone(self.phone.screen)
        self.assertEqual([c.args for c in self.phone._shell.call_args_list],[('getprop','ro.product.model'),('echo','whale-link-ok')])

    async def test_bad_model_timeout_or_cancellation_keeps_original_settings(self):
        before=self.state.read_bytes()
        self.phone._shell.side_effect=['different-model']
        with self.assertRaises(self.error):await self.control.reconnect('100.64.0.10:22222')
        self.assertEqual(self.phone.state['serial'],'100.64.0.10:11111');self.assertEqual(self.state.read_bytes(),before)
        self.phone._connect_once.side_effect=asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):await self.control.reconnect('100.64.0.10:22222')
        self.assertEqual(self.phone.state['serial'],'100.64.0.10:11111');self.assertEqual(self.state.read_bytes(),before)

    async def test_busy_and_socket_protocol_do_not_expose_raw_errors(self):
        await self.phone.lock.acquire()
        self.assertEqual((await self.control.reconnect('100.64.0.10:22222'))['code'],'busy')
        self.phone.lock.release();self.phone._connect_once.assert_not_called()
        await self.control.start()
        try:
            self.assertEqual(self.control.path.stat().st_mode&0o777,0o600)
            self.phone._connect_once.side_effect=self.error('SECRET')
            reader,writer=await asyncio.open_unix_connection(str(self.control.path))
            writer.write(b'{"action":"connect","serial":"100.64.0.10:22222"}\n');await writer.drain()
            raw=await reader.readline();self.assertNotIn(b'SECRET',raw)
            self.assertEqual(json.loads(raw)['code'],'connection_failed')
            writer.close();await writer.wait_closed()
        finally:await self.control.close()
        self.assertFalse(self.control.path.exists())


class ReconnectHttpTests(GuestHttpTests):
    def test_private_settings_and_connect_require_admin(self):
        admin,guest=self.sessions()
        self.monitor.phone.settings.return_value={'ip':'100.64.0.10','port':12345}
        self.monitor.reconnect_phone.return_value={'status':'connected'}
        self.assertEqual(self.request('phone-settings',session=guest)[0],403)
        self.assertEqual(self.request('phone-connect',{},session=guest)[0],403)
        self.assertEqual(self.request('phone-connect',{})[0],401)
        self.monitor.reconnect_phone.assert_not_called();self.monitor.phone.settings.assert_not_called()
        self.assertEqual(self.request('phone-settings',session=admin)[0],200)
        self.assertEqual(self.request('phone-connect',{'ip':'100.64.0.10','port':12345},session=admin)[0],200)
        self.monitor.reconnect_phone.assert_called_once_with('100.64.0.10',12345)
