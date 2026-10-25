import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from library import Library


class MemoryViewTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.lib=Library(Path(self.tmp.name));folder=self.lib.data/'companion_memory';folder.mkdir(parents=True)
        self.path=folder/'memory.sqlite3';self.now=time.time();self.qq='12345678'
        with sqlite3.connect(self.path) as db:
            db.executescript('''CREATE TABLE entries(id TEXT,qq TEXT,kind TEXT,topic TEXT,text TEXT,evidence TEXT,created REAL,updated REAL,expires REAL,pinned INTEGER);
            CREATE TABLE memory_state(qq TEXT,id TEXT,type TEXT,status TEXT,valid_from REAL,valid_to REAL,review_after REAL,source_kind TEXT,version INTEGER);
            CREATE TABLE memory_conflicts(qq TEXT,entry_id TEXT,resolved INTEGER);
            ''')
            for i,typ,end,status in [('s','semantic',None,'active'),('p','plan',self.now-10,'active'),('c','plan',None,'cancelled')]:
                db.execute('INSERT INTO entries VALUES(?,?,?,?,?,?,?,?,?,?)',(i,self.qq,'fact',typ,'合成记忆','[]',self.now,self.now,None,0))
                db.execute('INSERT INTO memory_state VALUES(?,?,?,?,?,?,?,?,?)',(self.qq,i,typ,status,self.now-100,end,None,'self_report',1))
    def test_current_view_excludes_past_and_cancelled_plans(self):
        result=self.lib.memories(qq=self.qq)
        self.assertEqual([r['id'] for r in result['entries']],['s'])
        self.assertEqual(result['entries'][0]['memory_type'],'semantic')
    def test_temporary_filter_and_history_keep_rows(self):
        result=self.lib.memories(qq=self.qq,term='temporary',state='expired')
        self.assertEqual({r['id'] for r in result['entries']},{'p','c'})
        self.assertEqual(next(r for r in result['entries'] if r['id']=='p')['status'],'past_due')
        self.assertEqual(len(self.lib.memories(qq=self.qq,state='all')['entries']),3)
    def test_conflict_is_visible_as_uncertain(self):
        with sqlite3.connect(self.path) as db:db.execute('INSERT INTO memory_conflicts VALUES(?,?,0)',(self.qq,'s'))
        self.assertEqual(self.lib.memories(qq=self.qq)['entries'][0]['status'],'conflicted')
