"""Isolated round-trip backup of exploration, reading and quoted knowledge history."""
import hashlib
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0,os.environ.get('COMPANION_TEST_PLUGINS',str(Path(__file__).parent)))
from astrbot_plugin_companion_memory.portable import create_archive,verify_archive,restore_archive
from astrbot_plugin_companion_phone.exploration import Exploration
from astrbot_plugin_companion_phone.reading import PublicReading
from astrbot_plugin_companion_phone.knowledge import Knowledge
from test_knowledge import article,update,QUOTE_B,NOW


class LearningBackupTests(unittest.TestCase):
    def test_learning_chain_and_versions_restore_only_to_new_directory_without_credentials(self):
        with tempfile.TemporaryDirectory() as directory,patch('time.time',return_value=NOW):
            root=Path(directory);data=root/'data';memory=data/'plugin_data/companion_memory';memory.mkdir(parents=True)
            with closing(sqlite3.connect(memory/'memory.sqlite3')) as db,db:
                db.execute('CREATE TABLE observations(id INTEGER PRIMARY KEY,text TEXT)')
                db.execute('INSERT INTO observations(text) VALUES(?)',('synthetic private record',))
            phone=data/'plugin_data/companion_phone';phone.mkdir()
            book=Exploration(phone);reader=PublicReading(phone,{'enabled':True,'public_reading_enabled':True});knowledge=Knowledge(phone)
            first=article();source=reader.receipt(first,'合成材料一')
            book.observe(source,{'question':'合成样品的亮点来自哪里','progress':'原因待查'})
            topic=book.rows[0]['id']
            self.assertEqual(knowledge.record(topic,book.rows[0]['question'],first,source,update()),'recorded')
            second=article(QUOTE_B,'https://second.example/b');next_source=reader.receipt(second,'合成材料二')
            book.observe(next_source,{'id':topic,'progress':'旧解释需要修订'},selected=topic)
            self.assertEqual(knowledge.record(topic,book.rows[0]['question'],second,next_source,
                update(quote=QUOTE_B,action='revise',judgment='亮点可能是反光',reason='合成复核指出另一解释'),1),'recorded')
            active=data/'plugin_data/companion_active';active.mkdir()
            (active/'exploration_outreach.json').write_text(json.dumps({'version':1,'owner':'synthetic-owner','rows':{}}))
            for name in ('adbkey','credentials.json','connection-maintenance.json'):(phone/name).write_text('AUTH_NOT_PACKAGED')
            (active/'credentials.json').write_text('AUTH_NOT_PACKAGED')
            names=['exploration.json','public-reading.json','knowledge.sqlite3']
            before={name:hashlib.sha256((phone/name).read_bytes()).hexdigest() for name in names}
            archive=create_archive(data,root/'archives');self.assertGreaterEqual(verify_archive(archive)['files'],5)
            target=root/'restored';restore_archive(archive,target)
            restored=target/'data/plugin_data/companion_phone'
            self.assertEqual(Knowledge(restored).review(topic),knowledge.review(topic))
            self.assertEqual((restored/'exploration.json').read_bytes(),(phone/'exploration.json').read_bytes())
            self.assertEqual((restored/'public-reading.json').read_bytes(),(phone/'public-reading.json').read_bytes())
            self.assertTrue((target/'data/plugin_data/companion_active/exploration_outreach.json').exists())
            for name in ('adbkey','credentials.json','connection-maintenance.json'):self.assertFalse((restored/name).exists())
            self.assertFalse((target/'data/plugin_data/companion_active/credentials.json').exists())
            self.assertEqual(before,{name:hashlib.sha256((phone/name).read_bytes()).hexdigest() for name in names})
            with self.assertRaises(ValueError):restore_archive(archive,target)


if __name__=='__main__':unittest.main()
