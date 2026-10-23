import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch, Mock
import urllib.request
from http.server import ThreadingHTTPServer

import server


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        work = Path(__file__).parent / '.test-tmp'
        work.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=work)
        self.patch = patch.object(server, 'DATA', Path(self.temp.name))
        self.patch.start()
        self.root_patch=patch.object(server,'ROOT',Path(self.temp.name))
        self.root_patch.start()
        self.monitor = server.Monitor()

    def tearDown(self):
        self.root_patch.stop()
        self.patch.stop()
        self.temp.cleanup()

    def test_snowluma_checks_account_and_online(self):
        api=server.SnowLuma()
        for account,expected in [(server.BOT,True),('12345',False)]:
            with patch.object(api,'request',side_effect=[{'online':True},{'user_id':account}]):
                self.assertEqual(api.status()['isLogin'],expected)
        with patch.object(api,'request',side_effect=RuntimeError()):
            self.assertEqual(api.status()['loginPhase'],'waiting_login_or_hook')

    def test_snowluma_manual_recovery_never_restarts_napcat(self):
        self.monitor.backend='snowluma'
        self.monitor.gateway='qq-companion-snowluma'
        self.monitor.action_lock.acquire()
        with patch.object(server,'command') as command,patch.object(self.monitor,'probe'):
            self.monitor.recover_worker()
        command.assert_called_once_with(['docker','restart','qq-companion-snowluma'],timeout=45)
        self.assertFalse(self.monitor.action_lock.locked())

    def test_recovery_cooldown_survives_restart(self):
        with patch.object(server.threading, 'Thread'):
            self.monitor.recover()
        other = server.Monitor()
        with self.assertRaisesRegex(ValueError, '冷却'):
            other.recover()

    def test_no_mutations_in_probe(self):
        self.monitor.api = Mock()
        self.monitor.api.status.return_value = {'isLogin': True, 'isOffline': False}
        with patch.object(server, 'command', side_effect=RuntimeError()), patch.object(server.urllib.request, 'urlopen', side_effect=RuntimeError()), patch.object(self.monitor, 'logs', return_value=''):
            self.monitor.probe()
        self.monitor.api.request.assert_not_called()
        self.assertNotIn('recovery_requested', (server.DATA/'events.jsonl').read_text())

    def test_outgoing_quote_is_not_incoming(self):
        text = self.monitor.new_challenge()['text']
        self.monitor.diagnose(f'发送 -> 私聊 ({server.OWNER}) {text}', f'Prepare to send - F1nger/{server.OWNER}: {text}')
        self.assertFalse(self.monitor.challenge['napcat_received'])
        self.assertFalse(self.monitor.challenge['astrbot_received'])

    def test_owner_incoming_and_model_bus(self):
        text = self.monitor.new_challenge()['text']
        self.monitor.diagnose(f'接收 <- 私聊 ({server.OWNER}) {text}', f'[core.event_bus:116]: [qq-companion] F1nger/{server.OWNER}: {text}')
        self.assertEqual(self.monitor.challenge['result'], 'received')
        self.assertTrue(self.monitor.challenge['napcat_received'])

    def test_other_sender_cannot_verify(self):
        text = self.monitor.new_challenge()['text']
        self.monitor.diagnose(f'接收 <- 私聊 (123) {text}', f'[core.event_bus:116]: stranger/123: {text}')
        self.assertEqual(self.monitor.challenge['result'], 'waiting')

    def test_timeout_not_claiming_offline_from_silence(self):
        self.monitor.new_challenge()
        self.monitor.challenge['expires'] = 0
        self.monitor.diagnose('', '')
        self.assertEqual(self.monitor.challenge['result'], 'not_received')
        self.assertIsNone(self.monitor.snapshot.get('qq'))

    def test_qr_does_not_show_stale_logged_in_image(self):
        self.monitor.api = Mock()
        self.monitor.api.status.return_value = {'isLogin': True}
        with patch.object(server, 'command') as cmd:
            with self.assertRaises(ValueError):
                self.monitor.qr()
            cmd.assert_not_called()

    def test_failed_saved_login_called_only_once(self):
        self.monitor.api = Mock()
        self.monitor.api.status.return_value = {'isLogin': False}
        self.monitor.api.request.return_value = {'code': -1}
        self.monitor.action_lock.acquire()
        with patch.object(server, 'command'), patch.object(self.monitor, 'probe'):
            self.monitor.recover_worker()
        calls = self.monitor.api.request.call_args_list
        self.assertEqual(sum(c.args[0] == '/QQLogin/SetQuickLogin' for c in calls), 1)
        self.assertEqual(sum(c.args[0] == '/QQLogin/RefreshQRcode' for c in calls), 1)
        self.assertFalse(self.monitor.action_lock.locked())

    def test_no_login_when_already_online_after_restart(self):
        self.monitor.api = Mock()
        self.monitor.api.status.return_value = {'isLogin': True}
        self.monitor.action_lock.acquire()
        with patch.object(server, 'command'), patch.object(self.monitor, 'probe'):
            self.monitor.recover_worker()
        self.monitor.api.request.assert_not_called()

    def test_corrupt_rate_limit_state_is_not_reset(self):
        (server.DATA/'state.json').write_text('broken')
        with self.assertRaises(ValueError):
            server.Monitor()

    def test_http_auth_origin_and_unknown_action(self):
        token = 'a'*43
        auth=server.PanelAuth(server.ROOT/'recovery-data')
        password='isolated-test-password'
        session=auth.setup(password,password)['session']
        with patch.object(server,'PanelAuth',return_value=auth):
            httpd = ThreadingHTTPServer(('127.0.0.1', 0), server.make_handler(self.monitor, token))
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        url = f'http://127.0.0.1:{httpd.server_port}'
        try:
            def request(path, key=None, origin=None, method='GET'):
                headers = {'Host': '127.0.0.1:16190', 'Content-Type': 'application/json'}
                if key: headers['X-Recovery-Key'] = key
                if key: headers['X-Panel-Session'] = session
                if origin: headers['Origin'] = origin
                req = urllib.request.Request(url+path, headers=headers, method=method, data=b'{}' if method=='POST' else None)
                try:
                    with urllib.request.urlopen(req) as response:
                        return response.status, response.read()
                except urllib.error.HTTPError as exc:
                    return exc.code, exc.read()
            self.assertEqual(request('/api/status')[0],403)
            self.assertEqual(request('/api/status',token)[0],200)
            self.assertEqual(request('/api/recover',token,'https://evil.example','POST')[0],403)
            self.assertEqual(request('/api/arbitrary-command',token,method='POST')[0],404)
            self.assertEqual(request('/../deployment.env')[0],403)
            code, data = request('/api/test',token,method='POST')
            self.assertEqual(code,200)
            self.assertIn('连通检查',json.loads(data)['text'])
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join()


if __name__ == '__main__':
    unittest.main()
