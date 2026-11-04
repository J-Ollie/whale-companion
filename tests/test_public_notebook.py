"""Source provenance, retrieval, versioning and private-source exclusion."""
import hashlib,json,os,sqlite3,sys,tempfile,unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock,patch
sys.path.insert(0,os.environ.get('COMPANION_TEST_PLUGINS',str(Path(__file__).parent)))
from astrbot_plugin_companion_phone.public_notebook import PublicNotebook
from astrbot_plugin_companion_phone.reading import extract


class PublicNotebookTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name);self.book=PublicNotebook(self.root)
 def article(self,text=None,url='https://example.org/public'):
  return extract(url,'text/plain',(text or '公开资料：鲸类声音交流研究仍需核实来源。音乐声学与旋律研究。'*15).encode())
 def test_empty_lookup_does_not_create_database_or_call_network(self):
  self.assertEqual(self.book.lookup('鲸类声音'),[]);self.assertFalse(self.book.path.exists())
 def test_read_source_retrieves_in_chinese_and_survives_reopen(self):
  source=self.article();ref=self.book.store(source);rows=PublicNotebook(self.root).lookup('鲸类声音')
  self.assertEqual(rows[0]['reference'],ref);self.assertEqual(rows[0]['url'],source['url']);self.assertIn('鲸类',rows[0]['excerpt'])
  self.assertEqual(rows[0]['verification'],'source_material_not_verified_fact')
 def test_duplicates_do_not_accumulate_and_old_versions_are_retained(self):
  a=self.article();ref=self.book.store(a);self.assertEqual(self.book.store(a),ref);self.assertEqual(self.book.count(),1)
  b=self.article('公开修订：海豚信号的解释需要更多样本。'*20);b['retrieved_at']=a['retrieved_at']+1
  self.book.store(b);self.assertEqual(self.book.count(),2);self.assertEqual(self.book.lookup('鲸类声音'),[])
  self.assertIn('海豚',self.book.lookup('海豚信号')[0]['excerpt'])
 def test_private_unknown_and_changed_content_rejected(self):
  a=self.article()
  for changed in [dict(a,material='chat'),dict(a,url='http://127.0.0.1/private'),dict(a,text=a['text']+'偷偷更改')]:
   with self.assertRaises(ValueError):self.book.store(changed)
 def test_query_syntax_cannot_inject_sql_or_fts_operators(self):
  self.book.store(self.article());self.book.lookup('鲸类 OR " DROP TABLE documents; --')
  self.assertEqual(self.book.count(),1)
 def test_unknown_database_is_preserved(self):
  with closing(sqlite3.connect(self.book.path)) as db,db:db.execute('CREATE TABLE private_data(secret TEXT)')
  with self.assertRaises(ValueError):self.book.store(self.article())
  with closing(sqlite3.connect(self.book.path)) as db,db:self.assertIsNotNone(db.execute("SELECT name FROM sqlite_master WHERE name='private_data'").fetchone())
 def test_portable_backup_includes_source_database(self):
  from astrbot_plugin_companion_memory.portable import selected
  data=self.root/'data';phone=data/'plugin_data/companion_phone';phone.mkdir(parents=True)
  PublicNotebook(phone).store(self.article());self.assertIn('public-notes.sqlite3',{p.name for p in selected(data)})


class NotebookToolTests(unittest.IsolatedAsyncioTestCase):
 def setUp(self):
  from test_public_reading import ReadingFlowTests
  self.h=ReadingFlowTests();self.h.setUp();self.addCleanup(self.h.doCleanups)
  self.plugin,self.event=self.h.tool_plugin()
 async def test_foreground_read_caches_source_without_creating_own_interest(self):
  result=await self.plugin.tool_public_read(self.event,'read',url='https://example.org/article')
  value=json.loads(result.content[0].text);self.assertTrue(value['saved_reference'].startswith('pub:'))
  self.assertEqual(self.plugin.autonomy.exploration.rows,[])
  lookup=await self.plugin.tool_public_read(self.event,'lookup',query='鲸类声音')
  self.assertTrue(json.loads(lookup.content[0].text)['sources'])
 async def test_knowledge_tool_keeps_real_contact_guard(self):
  self.event.get_sender_id=lambda:'untrusted'
  result=await self.plugin.tool_public_read(self.event,'lookup',query='鲸类声音');self.assertTrue(result.isError)
 async def test_retrieval_budget_not_expanded_and_tool_removed_at_limit(self):
  from astrbot.api.provider import ProviderRequest
  from astrbot.core.agent.tool import ToolSet,FunctionTool
  req=ProviderRequest();req.func_tool=ToolSet([FunctionTool(name='companion_public_read',description='',parameters={})]);self.event.set_extra('companion_reading_request',req)
  for _ in range(3):self.assertFalse((await self.plugin.tool_public_read(self.event,'lookup',query='鲸类声音')).isError)
  self.assertEqual(req.func_tool.names(),[])
  self.assertTrue((await self.plugin.tool_public_read(self.event,'lookup',query='鲸类声音')).isError)


if __name__=='__main__':unittest.main()
