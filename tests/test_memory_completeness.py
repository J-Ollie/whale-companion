"""Synthetic regression checks. No real contacts, production DBs or model calls."""
import asyncio
import gc
import hashlib
import json
import os
import sqlite3
import sys
import tempfile
import unittest
import zipfile
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0,os.environ.get('COMPANION_TEST_PLUGINS',str(Path(__file__).parent)))
from astrbot_plugin_companion_memory.memory import MemoryStore,TZ
from astrbot_plugin_companion_memory.cognition import Cognition
from astrbot_plugin_companion_memory.budget import size,TOOL_REPLY_BYTES,REFLECTION_BYTES
from astrbot_plugin_companion_memory.reflection import prepare
from astrbot_plugin_companion_memory.portable import create_archive,verify_archive,restore_archive
from astrbot_plugin_companion_friends.archive import ChatArchive

A='12345678';B='87654321'
NOW=datetime(2026,10,25,15,tzinfo=TZ).timestamp()


class MemoryCompletenessTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.store=MemoryStore(self.tmp.name);self.c=Cognition(self.store)
    def tearDown(self):
        self.store.close();gc.collect();self.tmp.cleanup()

    def test_full_long_message_and_tail_search_keep_sanitization(self):
        text='鲸'*26000+'尾部约定周三看海 password=example-secret'
        i=self.store.observe(A,'user',text,at=NOW)
        row=self.store.db.execute('SELECT text FROM observations WHERE id=?',(i,)).fetchone()[0]
        self.assertIn('尾部约定周三看海',row);self.assertNotIn('example-secret',row)
        result=self.c.search(A,'尾部约定周三看海')
        self.assertEqual(result['matches'][0]['id'],'obs:'+str(i))
        self.assertIn('尾部约定',result['matches'][0]['text'])
        self.assertGreater(result['matches'][0]['offset'],24000)
        self.assertLessEqual(size(result),TOOL_REPLY_BYTES)

    def test_friend_archive_keeps_more_than_twenty_thousand_characters(self):
        archive=ChatArchive(Path(self.tmp.name)/'friends')
        text='鲸'*25000+'尾部证据'
        archive.record(A,'synthetic','in',text,'message-1',NOW)
        with archive.connect() as db:
            self.assertEqual(json.loads(db.execute('SELECT segments FROM chats').fetchone()[0])[0]['text'],text)

    def test_paging_reconstructs_complete_unicode_and_json_within_budget(self):
        text='开头'+('鲸\n"🙂'*1400)+'尾部证据'
        i=self.store.observe(A,'user',text,at=NOW)
        offset=0;parts=[]
        while True:
            page=self.c.read(A,'obs:'+str(i),offset)
            self.assertLessEqual(size(page),TOOL_REPLY_BYTES)
            self.assertEqual(page['offset'],offset);parts.append(page['text'])
            if not page['has_more']:break
            self.assertGreater(page['next_offset'],offset);offset=page['next_offset']
        self.assertEqual(''.join(parts),text)
        with self.assertRaises(ValueError):self.c.read(B,'obs:'+str(i))
        for value in (-1,True,1.1):
            with self.assertRaises(ValueError):self.c.read(A,'obs:'+str(i),value)

    def test_neighbors_and_forgotten_sources_never_cross_people(self):
        before=self.store.observe(A,'user','同一联系人的前文',at=NOW)
        self.store.observe(B,'user','PRIVATE_OTHER_CONTACT',at=NOW)
        center=self.store.observe(A,'user','鲸'*2000+'目标位置',at=NOW)
        blocked=self.store.observe(A,'user','SUPPRESSED_PRIVATE',at=NOW)
        self.store.db.execute('INSERT INTO suppressed_sources VALUES (?,?)',(A,blocked));self.store.db.commit()
        page=self.c.read(A,'obs:'+str(center),query='目标位置',context=1)
        self.assertIn('目标位置',page['text']);self.assertLessEqual(size(page),TOOL_REPLY_BYTES)
        self.assertNotIn('PRIVATE_OTHER_CONTACT',json.dumps(page));self.assertNotIn('SUPPRESSED_PRIVATE',json.dumps(page))
        self.assertEqual(page['neighbors'][0]['id'],'obs:'+str(before))

    def test_date_filter_happens_before_top_k_for_raw_and_fact(self):
        old=datetime(2026,9,1,12,tzinfo=TZ).timestamp()
        desired=self.store.observe(A,'user','研究项目 '+('无关背景材料 '*100)+' 当天约定',at=old)
        for i in range(140):self.store.observe(A,'user','研究项目',at=NOW,dedupe=str(i))
        result=self.c.search(A,'研究项目','2026-09-01','2026-09-01')
        self.assertIn('obs:'+str(desired),[r['id'] for r in result['matches']])
        # A recently extracted fact must retain the original evidence date.
        fact=self.store.put_entry(A,'fact','项目','研究项目当天约定',[desired],now=NOW)
        result=self.c.search(A,'研究项目','2026-09-01','2026-09-01')
        self.assertIn(fact,[r['id'] for r in result['matches']])
        self.assertFalse(self.c.search(B,'研究项目','2026-09-01','2026-09-01')['matches'])

    def test_extraction_cursor_covers_tail_without_exceeding_budget(self):
        text='鲸'*10000+'长消息最后的约定'
        i=self.store.observe(A,'user',text,at=NOW);parts=[];cursor=0
        while cursor<i:
            payload,supplied,cursor,offset=prepare(self.store,A,cursor,':'+A,NOW,'system')
            self.assertLessEqual(size(payload)+size('system'),REFLECTION_BYTES)
            parts.extend(r['text'] for r in supplied if r['id']==i)
            self.store.set_meta('reflection_offset:'+A,offset)
        self.assertEqual(''.join(parts),text)

    def test_daily_backup_refreshes_atomically_and_closes_connection(self):
        self.store.observe(A,'user','first',at=NOW);self.store.backup(NOW)
        self.store.observe(A,'user','second',at=NOW+1);self.store.backup(NOW+3601)
        path=Path(self.tmp.name)/'backups/2026-10-25.sqlite3'
        db=sqlite3.connect(path)
        try:self.assertEqual(db.execute('SELECT COUNT(*) FROM observations').fetchone()[0],2)
        finally:db.close()
        # Rename immediately (without relying on garbage collection) works on Windows too.
        renamed=path.with_suffix('.check');path.rename(renamed);renamed.rename(path)
        self.assertFalse(list(path.parent.glob('*.tmp')))
        with patch.object(self.store,'db') as broken:
            broken.backup.side_effect=RuntimeError('synthetic failure')
            before=path.read_bytes()
            with self.assertRaises(RuntimeError):self.store.backup(NOW+7202)
            self.assertEqual(path.read_bytes(),before)


class PortableTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.data=self.root/'source'
        self.memory=self.data/'plugin_data/companion_memory'
        store=MemoryStore(self.memory);i=store.observe(A,'user','记住周三看海',at=NOW)
        self.entry=store.put_entry(A,'fact','约定','记住周三看海',[i],now=NOW)
        store.set_meta('cursor:'+A,i);store.forget(A,self.entry);store.close()
        (self.memory/'public-profile.json').write_text('{"name":"岚"}',encoding='utf-8')
        friends=self.data/'plugin_data/companion_friends';friends.mkdir()
        db=sqlite3.connect(friends/'sleep-inbox.sqlite3');db.execute('CREATE TABLE inbox(id INTEGER,state TEXT)');db.execute("INSERT INTO inbox VALUES (1,'pending')");db.commit();db.close()
        (friends/'media').mkdir();(friends/'media/image.bin').write_bytes(b'private-attachment')
        native=sqlite3.connect(self.data/'data_v4.db')
        native.execute('CREATE TABLE personas(id INTEGER PRIMARY KEY,system_prompt TEXT)');native.execute("INSERT INTO personas VALUES(1,'synthetic persona')")
        native.execute('CREATE TABLE api_keys(secret TEXT)');native.execute("INSERT INTO api_keys VALUES('AUTH_MUST_NOT_BE_PACKAGED')")
        native.execute('CREATE TABLE attachments(id INTEGER PRIMARY KEY,path TEXT)')
        native.execute("INSERT INTO attachments VALUES(1,'/AstrBot/data/attachments/registered.bin')")
        native.commit();native.close()
        (self.data/'attachments').mkdir();(self.data/'attachments/registered.bin').write_bytes(b'native-attachment')
        (self.data/'cmd_config.json').write_text('PROVIDER_SECRET_NOT_PACKAGED')
        self.phone=self.data/'plugin_data/companion_phone';self.phone.mkdir()
        (self.phone/'private-diary.jsonl').write_text('private diary\n')
        (self.phone/'autonomy.json').write_text('{"used":3}')
        (self.phone/'credentials.json').write_text('OTHER_AUTH_NOT_PACKAGED')
    def tearDown(self):gc.collect();self.tmp.cleanup()

    def test_round_trip_preserves_identity_evidence_forgetting_cursors_and_pending(self):
        path=create_archive(self.data,self.root/'backups');report=verify_archive(path)
        self.assertGreater(report['files'],5)
        destination=self.root/'restored';restore_archive(path,destination)
        restored=destination/'data'
        self.assertEqual((restored/'plugin_data/companion_memory/public-profile.json').read_bytes(),(self.memory/'public-profile.json').read_bytes())
        store=MemoryStore(restored/'plugin_data/companion_memory')
        try:
            self.assertEqual(store.get_meta('cursor:'+A),1)
            self.assertTrue(store.db.execute('SELECT 1 FROM suppressed_sources').fetchone())
            self.assertFalse(Cognition(store).search(A,'周三看海')['matches'])
        finally:store.close()
        db=sqlite3.connect(restored/'data_v4.db')
        try:
            self.assertEqual(db.execute('SELECT system_prompt FROM personas').fetchone()[0],'synthetic persona')
            self.assertFalse(db.execute("SELECT 1 FROM sqlite_master WHERE name='api_keys'").fetchone())
        finally:db.close()
        self.assertFalse((restored/'cmd_config.json').exists());self.assertFalse((restored/'plugin_data/companion_phone/credentials.json').exists())
        self.assertEqual((restored/'plugin_data/companion_phone/autonomy.json').read_text(),'{"used":3}')
        self.assertEqual((restored/'plugin_data/companion_friends/media/image.bin').read_bytes(),b'private-attachment')
        self.assertEqual((restored/'attachments/registered.bin').read_bytes(),b'native-attachment')
        with self.assertRaises(ValueError):restore_archive(path,destination)

    def test_tampered_archive_never_creates_restore_destination(self):
        original=create_archive(self.data,self.root/'backups');tampered=self.root/'tampered.zip'
        with zipfile.ZipFile(original) as source,zipfile.ZipFile(tampered,'w') as target:
            for name in source.namelist():
                value=source.read(name)
                if name.endswith('public-profile.json'):value=b'corrupt'
                target.writestr(name,value)
        destination=self.root/'must-not-exist'
        with self.assertRaises(ValueError):restore_archive(tampered,destination)
        self.assertFalse(destination.exists())

    def test_traversal_and_extra_entries_are_rejected(self):
        path=create_archive(self.data,self.root/'backups')
        with zipfile.ZipFile(path,'a') as archive:archive.writestr('payload/../../escape',b'bad')
        with self.assertRaises(ValueError):verify_archive(path)

    def test_wal_data_included_and_live_files_unchanged(self):
        store=MemoryStore(self.memory);store.observe(A,'user','WAL pending content',at=NOW)
        path=create_archive(self.data,self.root/'backups')
        self.assertEqual(store.db.execute('SELECT COUNT(*) FROM observations').fetchone()[0],2)
        restored=self.root/'wal-restore';restore_archive(path,restored)
        other=MemoryStore(restored/'data/plugin_data/companion_memory')
        try:self.assertEqual(other.db.execute('SELECT COUNT(*) FROM observations').fetchone()[0],2)
        finally:other.close();store.close()


if __name__=='__main__':unittest.main()
