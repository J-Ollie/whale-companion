"""Public HTTPS ingress uses proxy-held bootstrap, with unchanged role checks."""
import json
import urllib.request
import urllib.error
from unittest.mock import patch
from test_guest import GuestHttpTests, PASSWORD
import server


class PublicHttpsTests(GuestHttpTests):
    def setUp(self):
        original = server.make_handler
        with patch.object(server, 'make_handler', side_effect=lambda monitor, token:
                          original(monitor, token, 'https://203.0.113.10', 'p'*43)):
            super().setUp()

    def public(self, path, body=None, session='', **overrides):
        headers = {'Host':'127.0.0.1:6190', 'X-Recovery-Key':'p'*43,
                   'X-Forwarded-Proto':'https', 'X-Forwarded-Host':'203.0.113.10',
                   'Origin':'https://203.0.113.10', 'X-Panel-Session':session}
        headers.update(overrides)
        if body is not None:headers['Content-Type']='application/json'
        request=urllib.request.Request(f'http://127.0.0.1:{self.http.server_port}/api/'+path,
            data=json.dumps(body).encode() if body is not None else None, headers=headers)
        try:
            with urllib.request.urlopen(request) as response:return response.status,json.load(response)
        except urllib.error.HTTPError as exc:return exc.code,json.load(exc)

    def test_visit_uses_authenticated_proxy_ip_and_never_exports_it(self):
        self.auth.setup(PASSWORD,PASSWORD)
        guest=self.public('auth/guest',{})[1]['session']
        for address in ['192.0.2.1','192.0.2.1','192.0.2.2']:
            self.assertEqual(self.public('visit',{},guest,**{'X-Real-IP':address})[0],200)
        admin=self.public('auth/login',{'password':PASSWORD})[1]['session']
        result=self.public('visitors',session=admin)[1]
        self.assertEqual(result['stats']['views'],2)
        self.assertNotIn('192.0.2.',json.dumps(result))
        self.assertNotIn('page',json.dumps(result))

    def test_public_info_and_existing_password(self):
        self.auth.setup(PASSWORD, PASSWORD)
        code,info=self.public('auth/info')
        self.assertEqual(code,200);self.assertTrue(info['public_access'])
        self.assertFalse(info['setup_required'])
        saved=self.auth.path.read_bytes()
        code,result=self.public('auth/login',{'password':PASSWORD})
        self.assertEqual(code,200);self.assertEqual(result['role'],'admin')
        self.assertEqual(self.public('status',session=result['session'])[0],200)
        self.assertEqual(saved,self.auth.path.read_bytes())

    def test_public_setup_and_anonymous_private_routes_denied(self):
        self.auth.setup(PASSWORD, PASSWORD)
        self.assertEqual(self.public('auth/setup',{'password':PASSWORD,'confirm':PASSWORD})[0],403)
        for route in ('status','chats','diaries?book=private'):
            self.assertEqual(self.public(route)[0],401)

    def test_proxy_requires_exact_origin_transport_and_bootstrap(self):
        for headers in ({'Origin':'https://evil.example'}, {'X-Forwarded-Host':'evil.example'},
                        {'X-Forwarded-Proto':'http'}, {'X-Recovery-Key':''},
                        {'Host':'203.0.113.10'}):
            self.assertEqual(self.public('auth/info',**headers)[0],403)

    def test_public_guest_cannot_escalate(self):
        self.auth.setup(PASSWORD, PASSWORD)
        code,result=self.public('auth/guest',{'role':'admin'})
        self.assertEqual(code,200);self.assertEqual(result['role'],'guest')
        session=result['session']
        self.assertEqual(self.public('status',session=session)[0],200)
        self.assertEqual(self.public('diaries?book=public',session=session)[0],200)
        for route in ('chats','diaries?book=private','diaries?book=legacy','qr'):
            self.assertEqual(self.public(route,session=session)[0],403)
        self.assertEqual(self.public('recover',{},session)[0],403)
        self.monitor.recover.assert_not_called()



if __name__=='__main__':
    import unittest
    unittest.main()
