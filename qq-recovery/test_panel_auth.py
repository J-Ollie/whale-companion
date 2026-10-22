import json
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path
from http.server import ThreadingHTTPServer
from unittest.mock import Mock,patch
from panel_auth import PanelAuth
import server

PASSWORD='test-only-password-123'


class AuthTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.auth=PanelAuth(Path(self.tmp.name))

    def test_first_setup_own_password_hash_only(self):
        self.assertTrue(self.auth.info()['setup_required'])
        session=self.auth.setup(PASSWORD,PASSWORD)['session']
        raw=self.auth.path.read_text()
        self.assertNotIn(PASSWORD,raw);self.assertNotIn(session,raw)
        self.assertTrue(self.auth.authorized(session));self.assertFalse(self.auth.info()['setup_required'])

    def test_eight_character_password_accepted(self):
        result=self.auth.setup("test1234","test1234")
        self.assertTrue(self.auth.authorized(result["session"]))
        self.assertTrue(self.auth.authorized(self.auth.login("test1234")["session"]))

    def test_first_setup_cannot_replace_password(self):
        self.auth.setup(PASSWORD,PASSWORD)
        original=self.auth.path.read_bytes()
        with self.assertRaises(ValueError):self.auth.setup('another-password','another-password')
        self.assertEqual(original,self.auth.path.read_bytes())

    def test_password_mismatch_and_length(self):
        for password,confirm in [('1234567','1234567'),(PASSWORD,'different'),('a'*129,'a'*129),(None,None)]:
            with self.assertRaises(ValueError):self.auth.setup(password,confirm)
        self.assertFalse(self.auth.path.exists())

    def test_restart_requires_new_login_and_correct_password(self):
        session=self.auth.setup(PASSWORD,PASSWORD)['session']
        restarted=PanelAuth(self.auth.folder)
        self.assertFalse(restarted.authorized(session));self.assertFalse(restarted.info()['setup_required'])
        with self.assertRaises(ValueError):restarted.login('incorrect')
        self.assertTrue(restarted.authorized(restarted.login(PASSWORD)['session']))

    def test_failed_attempts_are_bounded(self):
        self.auth.setup(PASSWORD,PASSWORD)
        for _ in range(5):
            with self.assertRaisesRegex(ValueError,'不正确'):self.auth.login('wrong')
        with self.assertRaisesRegex(ValueError,'5分钟'):self.auth.login(PASSWORD)
        self.auth.failures=[0]*5
        self.assertTrue(self.auth.authorized(self.auth.login(PASSWORD)['session']))

    def test_corrupted_file_not_treated_as_first_setup(self):
        self.auth.path.write_text('{bad')
        with self.assertRaises(ValueError):PanelAuth(self.auth.folder)

    def test_session_expiry_and_forgery(self):
        session=self.auth.setup(PASSWORD,PASSWORD)['session']
        self.assertFalse(self.auth.authorized('x'*43))
        self.auth.sessions={k:0 for k in self.auth.sessions}
        self.assertFalse(self.auth.authorized(session))


class AuthHttpTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        with patch.object(server,'ROOT',Path(self.tmp.name)):
            self.monitor=Mock();self.monitor.status.return_value={'ok':True}
            self.http=ThreadingHTTPServer(('127.0.0.1',0),server.make_handler(self.monitor,'t'*43))
        self.addCleanup(self.http.server_close)
        threading.Thread(target=self.http.serve_forever,daemon=True).start();self.addCleanup(self.http.shutdown)

    def request(self,path,body=None,session='',bootstrap=True,origin=None):
        headers={'Host':'127.0.0.1:6190','X-Panel-Session':session}
        if bootstrap:headers['X-Recovery-Key']='t'*43
        if origin:headers['Origin']=origin
        if body is not None:headers['Content-Type']='application/json'
        request=urllib.request.Request(f'http://127.0.0.1:{self.http.server_port}/api/'+path,
            data=json.dumps(body).encode() if body is not None else None,headers=headers)
        try:
            with urllib.request.urlopen(request) as response:return response.status,json.load(response)
        except urllib.error.HTTPError as exc:return exc.code,json.load(exc)

    def test_key_alone_cannot_read_or_operate_even_before_setup(self):
        self.assertEqual(self.request('auth/info')[0],200)
        for route in ('status','chats','diaries?book=private','qr','chat-media/'+'a'*64+'.jpg'):
            self.assertEqual(self.request(route)[0],401)
        self.assertEqual(self.request('recover',{})[0],401)
        self.monitor.recover.assert_not_called()

    def test_setup_login_and_all_routes_still_require_bootstrap(self):
        code,value=self.request('auth/setup',{'password':PASSWORD,'confirm':PASSWORD})
        self.assertEqual(code,200);session=value['session']
        self.assertEqual(self.request('status',session=session)[0],200)
        self.assertEqual(self.request('status',session=session,bootstrap=False)[0],403)
        self.assertEqual(self.request('status',session=session,origin='http://evil.example')[0],403)
        self.assertEqual(self.request('auth/setup',{'password':PASSWORD,'confirm':PASSWORD})[0],400)
        self.assertEqual(self.request('auth/login',{'password':'wrong'})[0],400)
        code,value=self.request('auth/login',{'password':PASSWORD})
        self.assertEqual(code,200)
        self.assertEqual(self.request('chats',session=value['session'])[0],200)


    def test_public_illustration_is_exact_asset_without_api_access(self):
        art=b'RIFF-test-public-illustration'
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)
            asset_names=('whale-girl.webp','companion-winter-awake.webp','companion-winter-sleep.webp','companion-maid-awake.webp','companion-maid-sleep.webp')
            for name in asset_names:(path/name).write_bytes(art)
            with patch.object(server,'WEB',path):
                for name in asset_names:
                    request=urllib.request.Request(f'http://127.0.0.1:{self.http.server_port}/'+name)
                    with urllib.request.urlopen(request) as response:
                        self.assertEqual(response.status,200)
                        self.assertEqual(response.headers['Content-Type'],'image/webp')
                        self.assertEqual(response.read(),art)
                        self.assertIn("img-src 'self' blob:",response.headers['Content-Security-Policy'])
                for target in ('/whale-girl.webp/../panel-password.json','/other.webp','/api/diaries?book=private'):
                    with self.assertRaises(urllib.error.HTTPError) as error:
                        urllib.request.urlopen(f'http://127.0.0.1:{self.http.server_port}'+target)
                    self.assertEqual(error.exception.code,403)
