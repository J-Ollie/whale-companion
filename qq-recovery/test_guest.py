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

PASSWORD='test-only-guest-owner-password'

class GuestRoleTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.auth=PanelAuth(Path(self.tmp.name))

    def test_guest_requires_existing_owner_and_never_authorizes_admin(self):
        self.assertFalse(self.auth.info()['guest_available'])
        with self.assertRaises(ValueError):self.auth.guest()
        admin=self.auth.setup(PASSWORD,PASSWORD)['session']
        saved=self.auth.path.read_bytes()
        value=self.auth.guest()
        self.assertEqual(value['role'],'guest')
        self.assertEqual(self.auth.role(value['session']),'guest')
        self.assertFalse(self.auth.authorized(value['session']))
        self.assertTrue(self.auth.authorized(admin))
        self.assertEqual(saved,self.auth.path.read_bytes())

    def test_guest_sessions_are_bounded_without_evicting_owner(self):
        admin=self.auth.setup(PASSWORD,PASSWORD)['session']
        for _ in range(25):self.auth.guest()
        self.assertEqual(len(self.auth.guest_sessions),20)
        self.assertTrue(self.auth.authorized(admin))

    def test_expiry_forgery_and_restart_invalidate_guest(self):
        self.auth.setup(PASSWORD,PASSWORD)
        guest=self.auth.guest()['session']
        self.assertIsNone(self.auth.role('x'*43))
        restarted=PanelAuth(self.auth.folder)
        self.assertIsNone(restarted.role(guest))
        self.auth.guest_sessions={k:0 for k in self.auth.guest_sessions}
        self.assertIsNone(self.auth.role(guest))

class GuestHttpTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.auth=PanelAuth(self.root/'recovery-data')
        self.monitor=Mock()
        self.monitor.status.return_value={
            'checked_at':1,'level':'unverified','summary':'test status','backend':'snowluma',
            'gateway_container':'qq-companion-snowluma',
            'containers':{'qq-companion-astrbot':{'running':True,'internal':'SECRET'},'qq-companion-snowluma':{'running':True}},
            'qq':{'isLogin':True,'internal':'SECRET'},'events':[{'at':1,'summary':'test event','raw':'SECRET'}],
            'challenge':{'text':'test challenge','result':'waiting','internal':'SECRET'},
            'operation':{'internal':'SECRET'},'internal':'SECRET'}
        self.monitor.new_challenge.return_value={'text':'test challenge','result':'waiting'}
        with patch.object(server,'ROOT',self.root),patch.object(server,'PanelAuth',return_value=self.auth):
            self.http=ThreadingHTTPServer(('127.0.0.1',0),server.make_handler(self.monitor,'t'*43))
        self.addCleanup(self.http.server_close)
        threading.Thread(target=self.http.serve_forever,daemon=True).start();self.addCleanup(self.http.shutdown)
        phone=self.root/'data/astrbot/plugin_data/companion_phone';phone.mkdir(parents=True)
        for visibility in ('public','private'):
            (phone/(visibility+'-diary.jsonl')).write_text(json.dumps({'visibility':visibility,'at':1,'text':visibility.upper()+'_DIARY'})+'\n',encoding='utf-8')

    def request(self,path,body=None,session='',bootstrap=True,origin=None,claimed=None):
        headers={'Host':'127.0.0.1:6190','X-Panel-Session':session}
        if bootstrap:headers['X-Recovery-Key']='t'*43
        if origin:headers['Origin']=origin
        if claimed:headers['X-Panel-Role']=claimed
        if body is not None:headers['Content-Type']='application/json'
        request=urllib.request.Request(f'http://127.0.0.1:{self.http.server_port}/api/'+path,
            data=json.dumps(body).encode() if body is not None else None,headers=headers)
        try:
            with urllib.request.urlopen(request) as response:return response.status,json.load(response)
        except urllib.error.HTTPError as exc:return exc.code,json.load(exc)

    def sessions(self):
        code,value=self.request('auth/setup',{'password':PASSWORD,'confirm':PASSWORD})
        self.assertEqual(code,200);admin=value['session']
        code,value=self.request('auth/guest',{'role':'admin'})
        self.assertEqual(code,200);self.assertEqual(value['role'],'guest')
        return admin,value['session']

    def test_guest_creation_still_requires_bootstrap_origin_and_owner_setup(self):
        self.assertEqual(self.request('auth/guest',{})[0],400)
        self.auth.setup(PASSWORD,PASSWORD)
        self.assertEqual(self.request('auth/guest',{},bootstrap=False)[0],403)
        self.assertEqual(self.request('auth/guest',{},origin='http://evil.example')[0],403)
        self.assertEqual(self.request('auth/guest',{})[0],200)

    def test_guest_status_is_projected_and_diary_is_public_only(self):
        admin,guest=self.sessions()
        code,data=self.request('status',session=guest)
        self.assertEqual(code,200);self.assertNotIn('SECRET',json.dumps(data))
        self.assertTrue(data['containers']['qq-companion-astrbot']['running'])
        self.assertEqual(data['challenge']['text'],'test challenge')
        code,data=self.request('diaries?book=public',session=guest)
        self.assertEqual(code,200);self.assertEqual(data['entries'][0]['text'],'PUBLIC_DIARY')
        self.assertEqual(self.request('diaries',session=guest)[0],200)
        self.assertEqual(self.request('diaries?book=private',session=admin)[1]['entries'][0]['text'],'PRIVATE_DIARY')
        self.assertEqual(self.request('chats',session=admin)[0],200)

    def test_guest_cannot_read_chat_private_history_media_or_qr(self):
        _,guest=self.sessions()
        for path in ('chats','chats?qq=12345678','diaries?book=private','diaries?book=%70rivate','diaries?book=legacy','chat-media/'+'a'*64+'.jpg','qr'):
            code,data=self.request(path,session=guest,claimed='admin')
            self.assertEqual(code,403,path);self.assertNotIn('PRIVATE_DIARY',json.dumps(data))
        self.monitor.qr.assert_not_called()

    def test_guest_cannot_restart_refresh_qr_or_invent_operations(self):
        _,guest=self.sessions()
        for path in ('recover','refresh-qr','arbitrary-command','check?op=recover'):
            self.assertEqual(self.request(path,{'role':'admin'},session=guest,claimed='admin')[0],403,path)
        self.monitor.recover.assert_not_called();self.monitor.refresh_qr.assert_not_called()

    def test_guest_can_check_and_generate_test_without_qq_restart(self):
        _,guest=self.sessions()
        checked=threading.Event();self.monitor.probe.side_effect=lambda:checked.set()
        self.assertEqual(self.request('check',{},session=guest)[0],200)
        self.assertTrue(checked.wait(1))
        code,data=self.request('test',{},session=guest)
        self.assertEqual(code,200);self.assertEqual(data['text'],'test challenge')
        self.monitor.new_challenge.assert_called_once();self.monitor.recover.assert_not_called()

    def test_guest_session_cannot_bypass_bootstrap_or_be_reused_after_expiry(self):
        _,guest=self.sessions()
        self.assertEqual(self.request('status',session=guest,bootstrap=False)[0],403)
        self.assertEqual(self.request('test',{},session=guest,origin='http://evil.example')[0],403)
        self.auth.guest_sessions={k:0 for k in self.auth.guest_sessions}
        self.assertEqual(self.request('status',session=guest)[0],401)
        self.assertEqual(self.request('test',{},session=guest)[0],401)

    def test_guest_never_becomes_owner_without_correct_password(self):
        admin,guest=self.sessions();saved=self.auth.path.read_bytes()
        self.assertEqual(self.request('auth/login',{'password':'wrong'},session=guest)[0],400)
        self.assertEqual(self.request('auth/setup',{'password':PASSWORD,'confirm':PASSWORD},session=guest)[0],400)
        self.assertEqual(saved,self.auth.path.read_bytes())
        self.assertEqual(self.auth.role(guest),'guest');self.assertTrue(self.auth.authorized(admin))

    def test_owner_recovery_still_available_after_guest_login(self):
        admin,_=self.sessions()
        self.assertEqual(self.request('recover',{},session=admin)[0],200)
        self.monitor.recover.assert_called_once()
