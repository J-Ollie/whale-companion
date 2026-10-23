import hashlib
import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from library import Library
from test_guest import GuestHttpTests


def seed(root):
    path=Path(root)/'data/astrbot/plugin_data/companion_memory/memory.sqlite3'
    path.parent.mkdir(parents=True,exist_ok=True)
    now=time.time()
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE entries(id TEXT,qq TEXT,kind TEXT,topic TEXT,text TEXT,created REAL,updated REAL,expires REAL,pinned INT,evidence TEXT)')
        for i in range(65):
            db.execute('INSERT INTO entries VALUES(?,?,?,?,?,?,?,?,?,?)',
                (str(i),'100000001','fact','喜好'+str(i),'喜欢蓝色 '+str(i),now,now+i,None,int(i==0),'[1,2]'))
        db.execute('INSERT INTO entries VALUES(?,?,?,?,?,?,?,?,?,?)',('expired','100000001','impression','旧印象','曾经的理解',now,now,now-10,0,'[]'))
        db.execute('INSERT INTO entries VALUES(?,?,?,?,?,?,?,?,?,?)',('growth','12345678','growth','表达','更自然',now,now,now+10000,0,'[]'))
        db.execute('INSERT INTO entries VALUES(?,?,?,?,?,?,?,?,?,?)',('special','100000001','fact','100%_literal','password=should-never-be-disclosed',now,now,None,0,'[]'))
        db.execute('CREATE TABLE observations(text TEXT)');db.execute('INSERT INTO observations VALUES(?)',('RAW_PRIVATE_CHAT',))
    return path


class MemoryQueryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.path=seed(self.root);self.library=Library(self.root)

    def test_long_term_paging_pins_and_read_only(self):
        before=hashlib.sha256(self.path.read_bytes()).digest()
        seen=[]
        for offset in (0,30,60):
            d=self.library.memories(term='long',before=offset)
            self.assertEqual(d['total'],66);seen.extend(r['id'] for r in d['entries'])
        self.assertEqual(len(set(seen)),66)
        self.assertTrue(self.library.memories()['entries'][0]['pinned'])
        self.assertEqual(hashlib.sha256(self.path.read_bytes()).digest(),before)

    def test_search_is_literal_parameterized_and_private_material_not_read(self):
        d=self.library.memories(q='100%_literal')
        self.assertEqual(d['total'],1);self.assertNotIn('should-never-be-disclosed',json.dumps(d))
        self.assertEqual(d['entries'][0]['text'],'[凭据已省略]')
        self.assertNotIn('RAW_PRIVATE_CHAT',json.dumps(self.library.memories()))
        self.assertEqual(self.library.memories(q="' OR 1=1 --")['total'],0)

    def test_person_kind_expiration_and_validation(self):
        self.assertEqual(self.library.memories(kind='impression')['total'],0)
        self.assertEqual(self.library.memories(state='expired')['total'],1)
        self.assertEqual(self.library.memories(qq='12345678',kind='growth',term='temporary')['total'],1)
        for values in ({'qq':'../../path'},{'kind':'private'},{'term':'never'},{'state':'unknown'},{'before':-1},{'q':'x'*101}):
            with self.assertRaises(ValueError):self.library.memories(**values)

    def test_episodes_continuity_audit_and_scoped_evidence(self):
        now=time.time()
        with sqlite3.connect(self.path) as db:
            db.execute('DROP TABLE observations')
            db.execute('CREATE TABLE observations(id INT,qq TEXT,role TEXT,text TEXT,at REAL)')
            db.execute('INSERT INTO observations VALUES(1,?,?,?,?)',('100000001','user','我不吃香菜',now))
            db.execute('INSERT INTO observations VALUES(2,?,?,?,?)',('12345678','user','OTHER_PERSON_PRIVATE',now))
            db.execute('INSERT INTO entries VALUES(?,?,?,?,?,?,?,?,?,?)',('episode','100000001','episode','读书','讨论过一本书',now,now,None,0,'[1,2]'))
            db.execute('CREATE TABLE recall_audit(id INT,qq TEXT,at REAL,operation TEXT,refs TEXT)')
            db.execute('INSERT INTO recall_audit VALUES(1,?,?,?,?)',('100000001',now,'search','["episode"]'))
        d=self.library.memories(kind='episode')
        self.assertEqual(d['total'],1);self.assertEqual(d['recall_audit'][0]['matched_count'],1)
        proof=self.library.memory_evidence('100000001','episode')
        self.assertEqual(len(proof['sources']),1)
        self.assertNotIn('OTHER_PERSON_PRIVATE',json.dumps(proof))
        with self.assertRaises(ValueError):self.library.memory_evidence('12345678','episode')


class MemoryQueryHttpTests(GuestHttpTests):
    def test_evidence_requires_admin_and_bootstrap(self):
        seed(self.root);admin,guest=self.sessions()
        with patch.object(Library,'memory_evidence',side_effect=AssertionError('guest read')):
            self.assertEqual(self.request('memory-evidence?qq=100000001&id=0',session=guest)[0],403)
            self.assertEqual(self.request('memory-evidence?qq=100000001&id=0')[0],401)
        self.assertEqual(self.request('memory-evidence?qq=100000001&id=0',session=admin)[0],200)
        self.assertEqual(self.request('memory-evidence?qq=100000001&id=0',session=admin,bootstrap=False)[0],403)

    def test_admin_only_memory_query_and_guest_denial_before_reading(self):
        seed(self.root);admin,guest=self.sessions()
        self.assertEqual(self.request('memories')[0],401)
        with patch.object(Library,'memories',side_effect=AssertionError('guest read')):
            self.assertEqual(self.request('memories?role=admin',session=guest,claimed='admin')[0],403)
        code,d=self.request('memories?term=long',session=admin)
        self.assertEqual(code,200);self.assertEqual(d['total'],66)
        self.assertNotIn('evidence',d['entries'][0]);self.assertEqual(d['entries'][0]['evidence_count'],2)
        self.assertEqual(self.request('memories',session=admin,bootstrap=False)[0],403)


if __name__=='__main__':unittest.main()
