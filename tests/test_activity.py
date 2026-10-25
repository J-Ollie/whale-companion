import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0,os.environ.get('COMPANION_TEST_PLUGINS','/AstrBot/data/plugins'))
from astrbot_plugin_companion_phone.activity import ActivityLedger,public_source
from astrbot_plugin_companion_phone.site_visitors import VisitorInbox


class ActivityTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)/'companion_phone';self.ledger=ActivityLedger(self.root)

    def count(self):
        with sqlite3.connect(self.ledger.path) as db:return db.execute('SELECT SUM(count) FROM receipts').fetchone()[0] or 0

    def test_fixed_metadata_and_grouping_no_content_or_invented_duration(self):
        now=time.time();self.ledger.record('space_posts',now=now,key='same')
        self.ledger.record('space_posts',now=now+1,key='same')
        self.assertEqual(self.count(),2)
        sources=self.ledger.sources();self.assertEqual(len(sources),1)
        self.assertTrue(public_source(sources[0]))
        self.assertNotIn('duration',json.dumps(sources));self.assertNotIn('same',json.dumps(sources))
        self.assertIsNone(self.ledger.record('PRIVATE_SENTINEL'))

    def test_automatic_context_is_not_an_active_website_visit(self):
        feed=self.root.parent/'companion_visitors/feed.json';feed.parent.mkdir()
        feed.write_text(json.dumps({'generated_at':time.time(),'messages':[{'id':1,'message':'PRIVATE_SENTINEL'}],'visits':[],'stats':{}}))
        inbox=VisitorInbox(self.root);inbox.context()
        self.assertEqual(self.count(),0)
        inbox.read();self.assertEqual(self.count(),1)
        self.assertNotIn('PRIVATE_SENTINEL',json.dumps(self.ledger.sources()))

    def test_unknown_page_or_private_content_not_promoted_to_public_diary(self):
        self.assertFalse(public_source({'app':'Via','note':'PRIVATE_SENTINEL','public_content':True}))
        self.ledger.record('phone_screen')
        self.assertEqual(self.ledger.sources(),[])
        self.ledger.record('space_posts');source=self.ledger.sources()[0]
        self.assertFalse(public_source({**source,'note':'PRIVATE_SENTINEL'}))


if __name__=='__main__':unittest.main()
