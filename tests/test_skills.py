import importlib.util
import sqlite3
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

source=Path(__file__).parent/'astrbot_plugin_companion_phone/skills.py'
if not source.exists():source=Path('/AstrBot/data/plugins/astrbot_plugin_companion_phone/skills.py')
spec=importlib.util.spec_from_file_location('skills',source)
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
PK='com.coolapk.market'

def button():return {'resource_id':PK+':id/search','text':'搜索','enabled':True,'clickable':True,'node_id':'node:fresh:1'}
def before():return {'available':True,'fingerprint':'before','nodes':[button()]}
def after():return {'available':True,'fingerprint':'after','nodes':[dict(button(),editable=True,focused=True)]}

class SkillTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.s=m.Skills(Path(self.tmp.name))
 def learn(self,session,now=1793450000,observed=None):
  with patch.object(m.time,'time',return_value=now):
   a=self.s.attempt(PK,button(),before(),session)
   return self.s.observe(a,after() if observed is None else observed)
 def state(self):
  with closing(self.s.db()) as db:return db.execute('SELECT state FROM recipes').fetchone()[0]
 def test_lazy_no_candidates_no_database(self):
  self.assertEqual(self.s.hints(PK,before()),[]);self.s.observe(None,after());self.assertFalse(self.s.path.exists())
 def test_single_success_is_candidate_and_no_text_coordinates_saved(self):
  self.assertEqual(self.learn('one')['result'],'focused_search_editor');self.assertEqual(self.state(),'candidate')
  with closing(self.s.db()) as db:
   raw=' '.join(str(tuple(r)) for r in db.execute('SELECT * FROM recipes'))
   self.assertNotIn('node:fresh',raw);self.assertNotIn('搜索',raw);self.assertNotIn('bounds',raw)
 def test_three_sessions_on_two_days_activate_fresh_node_hint(self):
  for i in range(3):self.learn(str(i),1793450000+i*86400)
  self.assertEqual(self.state(),'active');h=self.s.hints(PK,before());self.assertEqual(h[0]['node_id'],'node:fresh:1')
 def test_same_session_or_day_cannot_inflate_learning(self):
  for i in range(4):self.learn('one',1793450000+i*86400)
  self.assertEqual(self.state(),'candidate')
  for i in range(4):self.learn(str(i),1793450000)
  # The earlier single session on multiple days was deduplicated, so all evidence remains on day one.
  self.assertEqual(self.state(),'candidate')
 def test_changed_picture_without_editor_is_failure(self):
  r=self.learn('one',observed={'available':True,'fingerprint':'changed','nodes':[]})
  self.assertEqual(r['result'],'postcondition_missing');self.assertEqual(self.state(),'suspended')
 def test_real_failed_reuse_suspends_even_same_session(self):
  for i in range(3):self.learn(str(i),1793450000+i*86400)
  self.learn('2',1793450001+2*86400,{'available':True,'fingerprint':'other','nodes':[]})
  self.assertEqual(self.state(),'suspended');self.assertEqual(self.s.hints(PK,before()),[])
 def test_no_observation_disconnect_or_timeout_not_skill_failure(self):
  a=self.s.attempt(PK,button(),before(),'one')
  self.assertIsNone(self.s.observe(a,{'available':False}));self.assertFalse(self.s.path.exists())
  a['at']=time.time()-26;self.assertIsNone(self.s.observe(a,after()))
 def test_ambiguous_or_changed_ui_has_no_reusable_hint(self):
  for i in range(3):self.learn(str(i),1793450000+i*86400)
  ui=before();ui['nodes']*=2;self.assertEqual(self.s.hints(PK,ui),[])
  ui=before();ui['nodes'][0]['resource_id']=PK+':id/new_search';self.assertEqual(self.s.hints(PK,ui),[])
  self.assertEqual(self.s.hints(PK,after()),[])
 def test_private_foreign_payment_disabled_or_no_stable_id_rejected(self):
  for package,node in [('com.tencent.mobileqq',button()),(PK,dict(button(),text='搜索并支付')),(PK,dict(button(),enabled=False)),(PK,dict(button(),resource_id='other:id/search')),(PK,dict(button(),resource_id=''))]:
   self.assertIsNone(self.s.attempt(package,node,before(),'one'))
 def test_size_limit_stops_append_without_deletion(self):
  self.s.path.write_bytes(b'x'*(8*1024*1024));self.assertIsNone(self.learn('one'));self.assertEqual(self.s.path.stat().st_size,8*1024*1024)

if __name__=='__main__':unittest.main()
