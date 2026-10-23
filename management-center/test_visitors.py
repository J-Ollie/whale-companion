"""Visitor identities, private inbox, HTTP role boundaries and duplicate submits."""
import json,tempfile,time,unittest
from pathlib import Path
from unittest.mock import patch
from visitors import Visitors
from test_guest import GuestHttpTests


class VisitorStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.v=Visitors(self.root)
        self.visitor,self.cookie,_=self.v.identity(None)

    def test_anonymous_visits_deduplicate_and_only_keep_public_columns(self):
        self.v.visit(self.visitor,'diary');self.v.visit(self.visitor,'diary');self.v.visit(self.visitor,'archive')
        d=self.v.snapshot();self.assertEqual(d['stats']['views'],1);self.assertEqual(d['stats']['sources'],1)
        self.assertEqual(d['visits'][0]['nickname'],'')
        self.assertNotIn(self.cookie,json.dumps(d));self.assertNotIn(self.visitor,json.dumps(d))
        self.assertNotIn('ip',json.dumps(d).lower())

    def test_nickname_is_optional_unverified_and_does_not_create_authority(self):
        self.v.profile(self.visitor,'F1nger');self.v.visit(self.visitor,'connection')
        row=self.v.snapshot()['visits'][0]
        self.assertEqual(row['nickname'],'F1nger');self.assertFalse(row['identity_verified'])
        self.assertNotIn('role',row)
        self.v.profile(self.visitor,'');self.assertEqual(self.v.snapshot()['visits'][0]['nickname'],'')

    def test_private_message_is_durable_and_retry_is_idempotent(self):
        first=self.v.note(self.visitor,'','你好，今天来看看你','a'*32)
        second=self.v.note(self.visitor,'','你好，今天来看看你','a'*32)
        self.assertEqual(first['id'],second['id']);self.assertTrue(second['duplicate'])
        other=Visitors(self.root)
        self.assertEqual(len(other.snapshot()['messages']),1)
        feed=json.loads(other.export.read_text(encoding='utf-8'))
        self.assertEqual(feed['messages'][0]['message'],'你好，今天来看看你')
        self.assertFalse(feed['messages'][0]['identity_verified'])

    def test_limits_and_invalid_values_do_not_create_messages(self):
        for name,msg,nonce in [('x'*25,'你好','a'*32),('张\n三','你好','a'*32),('', '', 'a'*32),('', 'x'*501,'a'*32),('', '你好','bad')]:
            with self.assertRaises(ValueError):self.v.note(self.visitor,name,msg,nonce)
        self.assertEqual(self.v.snapshot()['messages'],[])
        with self.assertRaises(ValueError):self.v.visit(self.visitor,'private?password=secret')

    def test_rate_limit_five_daily_and_dedup_even_after_limit(self):
        start=time.time()
        for i in range(5):
            with patch('visitors.time.time',return_value=start+i*61):self.v.note(self.visitor,'','留言'+str(i),str(i)*32)
        with patch('visitors.time.time',return_value=start+400):
            with self.assertRaises(ValueError):self.v.note(self.visitor,'','第六条','f'*32)
        self.assertTrue(self.v.note(self.visitor,'','重试','0'*32)['duplicate'])

    def test_too_soon_is_rejected_but_same_submission_is_safe(self):
        self.v.note(self.visitor,'','第一条','a'*32)
        with self.assertRaises(ValueError):self.v.note(self.visitor,'','第二条','b'*32)
        self.assertTrue(self.v.note(self.visitor,'','第一条','a'*32)['duplicate'])

    def test_history_paging_and_visit_retention_do_not_delete_notes(self):
        with self.v.connect() as db:
            for i in range(35):db.execute('INSERT INTO notes(visitor,nickname,message,at,submission) VALUES(?,?,?,?,?)',(self.visitor,'','旧留言',time.time()-200*86400,str(i)))
            db.execute('INSERT INTO visits(visitor,page,at) VALUES(?,?,?)',(self.visitor,'diary',time.time()-40*86400))
        self.v.visit(self.visitor,'connection')
        first=self.v.snapshot();second=self.v.snapshot(first['next_before'])
        self.assertEqual(len(first['messages']),30);self.assertEqual(len(second['messages']),5)
        self.assertEqual(first['stats']['notes'],35);self.assertEqual(len(first['visits']),1)


class VisitorDedupTests(VisitorStoreTests):
    def test_same_ip_across_browsers_and_distinct_ips(self):
        self.v.visit('a',address='192.0.2.1')
        self.v.visit('b',address='::ffff:192.0.2.1')
        self.assertEqual(self.v.snapshot()['stats']['views'],1)
        self.v.visit('a',address='192.0.2.2')
        self.assertEqual(self.v.snapshot()['stats']['views'],2)
        raw=self.v.path.read_bytes()+self.v.export.read_bytes()
        self.assertNotIn(b'192.0.2.',raw)
        self.assertNotIn('page',json.dumps(self.v.snapshot()))

    def test_window_boundary_and_persistent_salt(self):
        start=time.time()
        for offset in [0,1799,1800]:
            with patch('visitors.time.time',return_value=start+offset):self.v.visit('a',address='192.0.2.1')
        self.assertEqual(self.v.snapshot()['stats']['views'],2)
        self.assertEqual(Visitors(self.root).source_key('b','192.0.2.1'),self.v.source_key('a','192.0.2.1'))

    def test_migration_collapses_history_without_removing_notes(self):
        import sqlite3
        old=self.root/'legacy';(old/'recovery-data').mkdir(parents=True)
        with sqlite3.connect(old/'recovery-data/visitors.sqlite3') as db:
            db.execute('CREATE TABLE visits(id INTEGER PRIMARY KEY,visitor TEXT,page TEXT,at REAL)')
            for i,at in enumerate([100,101,102,1900]):db.execute('INSERT INTO visits VALUES(?,?,?,?)',(i+1,'a','diary',at))
        migrated=Visitors(old)
        with migrated.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM visits').fetchone()[0],2)
            self.assertEqual({r[0] for r in db.execute('SELECT page FROM visits')},{'site'})


class VisitorHttpTests(GuestHttpTests):
    def test_guest_can_submit_but_cannot_read_any_inbox_or_admin_data(self):
        admin,guest=self.sessions()
        self.assertEqual(self.request('visit',{'page':'diary'},session=guest)[0],200)
        self.assertEqual(self.request('visitor-profile',{'nickname':'访客小蓝','role':'admin'},session=guest)[0],200)
        self.assertEqual(self.request('guestbook',{'nickname':'访客小蓝','message':'<script>alert(1)</script>','submission_id':'a'*32},session=guest)[0],200)
        self.assertEqual(self.request('visitors',session=guest,claimed='admin')[0],403)
        self.assertEqual(self.request('visitors?role=admin',session=guest)[0],403)
        code,data=self.request('visitors',session=admin)
        self.assertEqual(code,200);self.assertEqual(len(data['messages']),1)
        self.assertEqual(data['messages'][0]['message'],'<script>alert(1)</script>')
        for path in ('chats','memories','diaries?book=private','qr'):
            self.assertEqual(self.request(path,session=guest)[0],403)

    def test_anonymous_cookie_does_not_replace_login_and_admin_is_not_counted(self):
        admin,guest=self.sessions()
        self.assertEqual(self.request('guestbook',{'nickname':'','message':'你好','submission_id':'b'*32})[0],401)
        self.assertEqual(self.request('visit',{'page':'diary'},session=guest,bootstrap=False)[0],403)
        self.assertEqual(self.request('visit',{'page':'diary'},session=guest,origin='http://evil.example')[0],403)
        self.assertEqual(self.request('visit',{'page':'diary'},session=admin)[0],403)

