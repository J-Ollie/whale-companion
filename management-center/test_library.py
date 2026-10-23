import json
import sys
import tempfile
import sqlite3
import threading
import unittest
import urllib.request
from pathlib import Path
from http.server import ThreadingHTTPServer
from unittest.mock import Mock,patch
from library import Library
import server


class LibraryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.lib=Library(self.root)
        self.phone=self.lib.data/'companion_phone';self.phone.mkdir(parents=True)

    def test_private_only_on_explicit_book(self):
        (self.phone/'private-diary.jsonl').write_text(json.dumps({'visibility':'private','at':1,'text':'PRIVATE_SENTINEL'})+'\n',encoding='utf-8')
        self.assertEqual(self.lib.diaries('public')['entries'],[])
        self.assertEqual(self.lib.diaries('private')['entries'][0]['text'],'PRIVATE_SENTINEL')

    def test_malformed_and_traversal_parameters_rejected(self):
        with self.assertRaises(ValueError):self.lib.diaries('../private')
        with self.assertRaises(ValueError):self.lib.media('../access-token')

    def test_diary_pagination_and_untrusted_text_preserved_as_data(self):
        rows=[{'visibility':'public','at':i,'text':'<script>alert(1)</script>'} for i in range(60)]
        (self.phone/'public-diary.jsonl').write_text('\n'.join(json.dumps(r) for r in rows),encoding='utf-8')
        d=self.lib.diaries('public');self.assertEqual(len(d['entries']),50);self.assertTrue(d['has_more'])
        self.assertEqual(len(self.lib.diaries('public',50)['entries']),10)
        self.assertIn('<script>',d['entries'][0]['text'])

    def test_public_combines_history_without_private_or_writes(self):
        folder=self.lib.data/'companion_memory';folder.mkdir()
        path=folder/'memory.sqlite3'
        with sqlite3.connect(path) as db:
            db.execute('CREATE TABLE diary(id TEXT,qq TEXT,at REAL,text TEXT,mood TEXT)')
            db.execute('INSERT INTO diary VALUES(?,?,?,?,?)',('old','100000001',1,'old memory',''))
            db.execute('INSERT INTO diary VALUES(?,?,?,?,?)',('other','12345678',2,'OTHER_ACCOUNT',''))
        public=self.phone/'public-diary.jsonl';life=self.phone/'life-diary.jsonl'
        public.write_text(json.dumps({'visibility':'public','day':'2026-10-21','at':3,'text':'new day'})+'\n',encoding='utf-8')
        life.write_text('\n'.join(json.dumps(r) for r in [
            {'at':2,'text':'old life'}, {'visibility':'private','at':4,'text':'PRIVATE_LIFE'},
            {'visibility':'public','at':5,'text':'older public life'}]),encoding='utf-8')
        (self.phone/'private-diary.jsonl').write_text(json.dumps({'visibility':'private','at':6,'text':'PRIVATE_BOOK'})+'\n',encoding='utf-8')
        snapshots={p:p.read_bytes() for p in [path,public,life]}
        result=self.lib.diaries('public')
        self.assertEqual({r['text'] for r in result['entries']},{'old memory','old life','older public life','new day'})
        self.assertNotIn('PRIVATE',json.dumps(result))
        self.assertEqual(result['entries'][0]['day'],'2026-10-21')
        for p,raw in snapshots.items():self.assertEqual(p.read_bytes(),raw)
        self.assertEqual({r['text'] for r in self.lib.diaries('legacy')['entries']},{'old memory','old life','older public life'})

    def test_combined_pagination_keeps_every_record(self):
        public=[{'id':'p'+str(i),'visibility':'public','day':'2026-10-21','at':i*2,'text':'p'+str(i)} for i in range(30)]
        history=[{'id':'l'+str(i),'day':'2026-10-21','at':i*2+1,'text':'l'+str(i)} for i in range(30)]
        (self.phone/'public-diary.jsonl').write_text('\n'.join(map(json.dumps,public)),encoding='utf-8')
        (self.phone/'life-diary.jsonl').write_text('\n'.join(map(json.dumps,history)),encoding='utf-8')
        first=self.lib.diaries('public');last=self.lib.diaries('public',first['next_before'])
        self.assertEqual(len(first['entries']),50);self.assertEqual(len(last['entries']),10)
        self.assertTrue(first['has_more']);self.assertFalse(last['has_more'])
        all_rows=first['entries']+last['entries']
        self.assertEqual(len({r['id'] for r in all_rows}),60)
        self.assertEqual([r['at'] for r in all_rows],list(range(59,-1,-1)))

    def test_history_day_uses_beijing_midnight(self):
        # UTC Jan 1 15:59 and 16:01 are opposite sides of Beijing midnight.
        rows=[{'at':57540,'text':'before midnight'},{'at':57660,'text':'after midnight'}]
        (self.phone/'life-diary.jsonl').write_text('\n'.join(map(json.dumps,rows)),encoding='utf-8')
        self.assertEqual([(r['text'],r['day']) for r in self.lib.diaries('public')['entries']],
                         [('after midnight','1970-01-02'),('before midnight','1970-01-01')])

    def test_chat_pagination_and_sources_never_exported(self):
        folder=self.lib.data/'companion_friends';folder.mkdir()
        with sqlite3.connect(folder/'chats.sqlite3') as db:
            db.execute('CREATE TABLE chats(id INTEGER PRIMARY KEY,qq TEXT,name TEXT,direction TEXT,at REAL,message_id TEXT,segments TEXT)')
            for i in range(60):
                db.execute('INSERT INTO chats VALUES(?,?,?,?,?,?,?)',(i+1,'12345678','好友','in',i,str(i),json.dumps([{'type':'image','source':'SECRET_INTERNAL_URL','label':'图片'}])))
            db.execute('INSERT INTO chats VALUES(?,?,?,?,?,?,?)',(99,'87654321','另一个人','in',99,'99','[]'))
        d=self.lib.chats('12345678')
        self.assertEqual(len(d['messages']),50);self.assertTrue(d['has_more'])
        self.assertNotIn('SECRET_INTERNAL_URL',json.dumps(d));self.assertEqual(len(d['contacts']),2)
        old=self.lib.chats('12345678',d['next_before']);self.assertEqual(len(old['messages']),10)
        self.assertFalse(old['has_more']);self.assertEqual(old['messages'][0]['id'],1)

    def test_all_library_routes_require_authentication(self):
        with patch.object(server,'ROOT',self.root):
            http=ThreadingHTTPServer(('127.0.0.1',0),server.make_handler(Mock(),'secret'*8))
        self.addCleanup(http.server_close)
        threading.Thread(target=http.serve_forever,daemon=True).start();self.addCleanup(http.shutdown)
        for path in ('chats','diaries?book=private','chat-media/'+'a'*64+'.jpg'):
            req=urllib.request.Request(f'http://127.0.0.1:{http.server_port}/api/'+path,headers={'Host':'127.0.0.1:6190'})
            with self.assertRaises(urllib.error.HTTPError) as error:urllib.request.urlopen(req)
            self.assertEqual(error.exception.code,403)
        req=urllib.request.Request(f'http://127.0.0.1:{http.server_port}/api/diaries?book=public',headers={'Host':'127.0.0.1:6190','X-Recovery-Key':'secret'*8})
        with self.assertRaises(urllib.error.HTTPError) as error:urllib.request.urlopen(req)
        self.assertEqual(error.exception.code,401)
