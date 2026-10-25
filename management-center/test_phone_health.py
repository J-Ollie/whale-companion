import contextlib
import io
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock,patch
import server
from phone_health import PhoneHealth, PROBE, project, rhythm_status
from test_guest import GuestHttpTests


class PhoneHealthTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.phone=self.root/'data/astrbot/plugin_data/companion_phone';self.phone.mkdir(parents=True)
        self.state=self.phone/'state.json';self.state.write_text(json.dumps({'serial':'100.64.0.10:38605','paused':True}))
        self.rhythm=self.phone/'rhythm.json'
        self.runner=Mock(return_value=b'{"status":"connected"}')
        self.health=PhoneHealth(self.root,self.runner,'qq-companion-astrbot')

    def test_sleeping_probe_does_not_wake_change_rhythm_or_use_phone_actions(self):
        self.rhythm.write_text(json.dumps({'updated_at':time.time(),'mode':'asleep','until':time.time()+3600}))
        before=self.state.read_bytes(),self.rhythm.read_bytes()
        value=self.health.query()
        self.assertEqual(value['status'],'connected');self.assertEqual(value['rhythm']['mode'],'asleep')
        args=self.runner.call_args.args[0]
        self.assertEqual(args[:4],['docker','exec','qq-companion-astrbot','python'])
        self.assertEqual(args[-1],'100.64.0.10:38605')
        for command in ('screencap','input keyevent',"'disconnect',",'am start'):
            self.assertNotIn(command,PROBE)
        self.assertEqual(before,(self.state.read_bytes(),self.rhythm.read_bytes()))

    def run_probe(self,replies):
        run=Mock(side_effect=[Mock(stdout=out,stderr=err,returncode=code) for out,err,code in replies])
        output=io.StringIO()
        with patch('subprocess.run',run),patch.object(sys,'argv',['probe','100.64.0.10:38605']),contextlib.redirect_stdout(output):
            exec(PROBE,{})
        return json.loads(output.getvalue())['status'],run

    def test_existing_connection_and_unauthorized_device_do_not_reconnect(self):
        status,run=self.run_probe([(b'device',b'',0),(b'whale-link-ok',b'',0)])
        self.assertEqual(status,'connected');self.assertEqual(run.call_count,2)
        self.assertEqual(run.call_args_list[-1].args[0][-3:],['shell','echo','whale-link-ok'])
        status,run=self.run_probe([(b'',b'device unauthorized',1)])
        self.assertEqual(status,'unauthorized');self.assertEqual(run.call_count,1)

    def test_missing_transport_reconnects_once_then_requires_state_and_echo(self):
        status,run=self.run_probe([(b'',b'device not found',1),(b'connected to phone',b'',0),
            (b'device',b'',0),(b'whale-link-ok',b'',0)])
        self.assertEqual(status,'connected');self.assertEqual(run.call_count,4)
        self.assertEqual(run.call_args_list[1].args[0][-2:],['connect','100.64.0.10:38605'])
        self.assertEqual(run.call_args_list[2].args[0][-1],'get-state')
        self.assertEqual(run.call_args_list[3].args[0][-3:],['shell','echo','whale-link-ok'])

    def test_failed_connect_and_failed_echo_never_claim_success(self):
        status,run=self.run_probe([(b'',b'device not found',1),(b'failed to connect',b'',0),
            (b'',b'device not found',1)])
        self.assertEqual(status,'disconnected');self.assertEqual(run.call_count,3)
        status,run=self.run_probe([(b'device',b'',0),(b'other output',b'',0)])
        self.assertEqual(status,'unresponsive')

    def test_no_injected_host_command_or_arbitrary_subprocess_output(self):
        for serial in ('example.com:5555','100.64.0.10:0','100.64.0.10:123; reboot',None):
            self.health.last_attempt=0
            self.state.write_text(json.dumps({'serial':serial}))
            self.assertEqual(self.health.query()['status'],'unavailable')
        self.runner.assert_not_called()
        self.state.write_text(json.dumps({'serial':'100.64.0.10:38605'}))
        self.runner.return_value=b'PRIVATE_SCREEN_OR_CREDENTIAL'
        self.health.last_attempt=0
        self.assertNotIn('PRIVATE',json.dumps(self.health.query()))

    def test_cache_and_unknown_rhythm_do_not_guess_awake_or_offline(self):
        self.health.query();self.health.query();self.assertEqual(self.runner.call_count,1)
        for value in ({'mode':'asleep','updated_at':time.time()-700,'until':time.time()+3600},
                      {'mode':'resting','updated_at':time.time(),'until':time.time()-1},{}):
            self.rhythm.write_text(json.dumps(value));self.assertEqual(rhythm_status(self.root)['mode'],'unknown')

    def test_guest_projection_omits_private_connection_and_character_fields(self):
        data=project({'status':'connected','serial':'SECRET','raw':'SECRET','checked_at':time.time(),
                      'rhythm':{'mode':'resting','label':'SECRET','energy':'SECRET'}})
        self.assertNotIn('SECRET',json.dumps(data));self.assertEqual(data['rhythm']['mode'],'resting')

    def test_sleep_receive_timeout_is_not_misreported_as_fake_online(self):
        with patch.object(server,'ROOT',self.root),patch.object(server,'DATA',self.root/'recovery-data'):
            monitor=server.Monitor()
        monitor.api.status=Mock(return_value={'isLogin':True,'isOffline':False})
        monitor.logs=Mock(return_value='');monitor.diagnose=Mock();monitor.event=Mock()
        monitor.challenge={'result':'not_received','napcat_received':True}
        monitor.phone.query=Mock(return_value={'status':'connected','label':'连通','rhythm':{'mode':'asleep'}})
        def command(args,**kw):
            if args[:2]==['docker','inspect']:
                return json.dumps([{'Name':'/'+n,'State':{'Running':True}} for n in (server.ASTRBOT,server.NAPCAT)]).encode()
            return b'1'
        response=Mock();response.__enter__=Mock(return_value=Mock(status=200));response.__exit__=Mock(return_value=False)
        with patch.object(server,'command',side_effect=command),patch('server.urllib.request.urlopen',return_value=response):
            monitor.probe();self.assertEqual(monitor.snapshot['level'],'unverified')
            self.assertIn('休息或睡觉',monitor.snapshot['summary'])
            monitor.phone.query.return_value['rhythm']['mode']='awake'
            monitor.probe();self.assertEqual(monitor.snapshot['level'],'error')


class PhoneHealthHttpTests(GuestHttpTests):
    def test_phone_check_admin_only_and_guest_reads_safe_status(self):
        admin,guest=self.sessions()
        self.monitor.check_phone.return_value={'status':'connected'}
        self.assertEqual(self.request('phone-check',{},session=guest)[0],403)
        self.assertEqual(self.request('phone-check',{})[0],401)
        self.monitor.check_phone.assert_not_called()
        self.assertEqual(self.request('phone-check',{},session=admin)[0],200)
        self.monitor.check_phone.assert_called_once()
        self.monitor.status.return_value['phone']={'status':'connected','serial':'SECRET','rhythm':{'mode':'asleep','label':'SECRET'}}
        value=self.request('status',session=guest)[1]
        self.assertEqual(value['phone']['status'],'connected');self.assertNotIn('SECRET',json.dumps(value))
