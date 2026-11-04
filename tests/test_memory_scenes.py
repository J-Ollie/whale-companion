"""Evidence, isolation and update semantics; synthetic data, no remote API calls."""
import json,os,sys,tempfile,time,unittest
from pathlib import Path
sys.path.insert(0,os.environ.get('COMPANION_TEST_PLUGINS',str(Path(__file__).parent)))
from astrbot_plugin_companion_memory.memory import MemoryStore
from astrbot_plugin_companion_memory.cognition import Cognition
from astrbot_plugin_companion_memory.fusion import rrf
from astrbot_plugin_companion_memory.vault import Vault
from astrbot_plugin_companion_memory.budget import size,CONTEXT_BYTES,TOOL_REPLY_BYTES

A='12345678';B='87654321'


class SceneTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.store=MemoryStore(self.tmp.name);self.c=Cognition(self.store);self.sc=self.c.control.scenes
    def tearDown(self):self.store.close();self.tmp.cleanup()
    def entry(self,text,topic,kind='fact',qq=A,role='user'):
        sid=self.store.observe(qq,role,text,at=time.time()-120)
        ref=self.store.put_entry(qq,kind,topic,text,[sid])
        return ref,sid
    def scenario(self):
        a,s1=self.entry('合成海岛旅行：上周讨论合成海岛旅行','event.1','episode')
        b,s2=self.entry('合成海岛旅行：本周继续讨论合成海岛旅行','event.2','episode')
        f,s3=self.entry('合成海岛旅行选海边住宿','合成海岛旅行.住宿')
        p,s4=self.entry('明天准备讨论合成海岛旅行','合成海岛旅行.计划')
        x,s5=self.entry('合成海岛旅行不要住高层','合成海岛旅行.限制')
        self.c.consolidate(A)
        scene=self.sc.search(A,'合成海岛旅行')[0]['id']
        return scene,f,p,x,s1,s4
    def test_scene_combines_background_facts_constraints_pending_and_timeline(self):
        scene,f,p,x,_,_=self.scenario();card=self.sc.get(A,scene)
        self.assertEqual(card['current_facts'][0]['id'],f)
        self.assertEqual(card['pending'][0]['id'],p);self.assertEqual(card['constraints'][0]['id'],x)
        self.assertGreaterEqual(len(card['timeline']),4);self.assertTrue(card['background'])
        self.assertIn('不证明',card['note'])
    def test_cross_person_scene_search_and_read_denied(self):
        scene,*_=self.scenario();self.assertFalse(self.sc.search(B,'合成海岛旅行'));self.assertIsNone(self.sc.get(B,scene))
        with self.assertRaises(ValueError):self.c.read(B,scene)
    def test_state_change_invalidates_before_refresh_and_preserves_history(self):
        scene,_,p,*_=self.scenario();version=self.sc.get(A,scene)['revision']
        with self.store.db:self.store.db.execute("UPDATE memory_state SET status='cancelled' WHERE qq=? AND id=?",(A,p))
        self.assertIsNone(self.sc.get(A,scene));self.sc.refresh(A);card=self.sc.get(A,scene)
        self.assertFalse(card['pending']);self.assertIn(p,[r['id'] for r in card['changes']])
        self.assertGreater(card['revision'],version)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM memory_scene_versions').fetchone()[0],1)
    def test_source_edit_blocks_scene_until_stale_member_removed(self):
        scene,_,_,_,source,_=self.scenario()
        with self.store.db:self.store.db.execute('UPDATE observations SET text=? WHERE id=?',('原始材料被纠正',source))
        self.assertIsNone(self.sc.get(A,scene));self.assertFalse(self.sc.search(A,'合成海岛旅行'))
        self.sc.refresh(A);self.assertNotIn(source,self.sc.get(A,scene)['source_ids'])
    def test_suppressed_fact_not_leaked_through_scene(self):
        scene,f,*_=self.scenario();self.store.forget(A,f)
        self.assertIsNone(self.sc.get(A,scene));self.sc.refresh(A)
        self.assertFalse(self.sc.get(A,scene)['current_facts'])
    def test_scene_read_pages_preserve_budget_and_expand_all_sections(self):
        scene,*_=self.scenario();offset=0;chunks=[]
        for _ in range(20):
            page=self.c.read(A,scene,offset);self.assertLessEqual(size(page),TOOL_REPLY_BYTES);chunks.append(page['text'])
            if not page['has_more']:break
            self.assertGreater(page['next_offset'],offset);offset=page['next_offset']
        self.assertFalse(page['has_more']);self.assertEqual(''.join(chunks),self.sc.render(self.sc.get(A,scene)))
    def test_automatic_related_scene_remains_within_existing_budget(self):
        self.scenario();self.assertEqual(self.c.related(A,'合成海岛旅行')[0]['kind'],'scenario_card')
        body=self.c.render(A,'合成海岛旅行');self.assertLessEqual(size(body),CONTEXT_BYTES)
        self.assertIn('scenario_card',body);self.assertFalse(self.sc.search(A,'南极冰川'))
    def test_catalog_only_projects_current_contact_sources(self):
        scene,*_=self.scenario();vault=Vault(self.c,Path(self.tmp.name)/'vault',owner=A);vault.export([A])
        path=vault.root/vault.scope(A)/'00-场景目录.md';self.assertTrue(path.exists())
        page=vault.root/vault.scope(A)/'scenes'/(scene.replace(':','-')+'.md')
        self.assertIn('待确认计划',page.read_text(encoding='utf-8'));self.assertFalse((vault.root/vault.scope(B)).exists())
    def test_link_proposals_require_current_batch_and_actual_owner(self):
        ref,sid=self.entry('合成出图能力已接入','生图事项','episode')
        source=dict(self.store.db.execute('SELECT * FROM observations WHERE id=?',(sid,)).fetchone())
        proposal=[{'title':'合成生图能力','source_ids':[sid]}]
        self.assertEqual(self.sc.accept_links(A,proposal,[source],sid),0)
        self.assertEqual(self.sc.accept_links(B,proposal,[source],0),0)
        self.assertEqual(self.sc.accept_links(A,proposal,[source],0),1)
        self.sc.refresh(A);self.assertTrue(self.sc.search(A,'合成生图'))
        self.assertEqual(self.sc.accept_links(A,[{'id':{},'source_ids':[sid]}],[source],0),0)
    def test_refresh_is_idempotent_and_does_not_rewrite_original_tables(self):
        self.scenario();tables=['observations','entries','entry_revisions','diary','suppressed_sources']
        snapshot=lambda:{t:[tuple(r) for r in self.store.db.execute('SELECT * FROM '+t)] for t in tables}
        before=snapshot();versions=self.store.db.execute('SELECT revision FROM memory_scenes').fetchall()
        self.sc.refresh(A);self.assertEqual(before,snapshot());self.assertEqual(versions,self.store.db.execute('SELECT revision FROM memory_scenes').fetchall())

    def test_fusion_cannot_bypass_person_type_date_suppression_or_exclusion(self):
        valid,_=self.entry('合成远征资料','合成远征')
        foreign,_=self.entry('合成远征资料','合成远征',qq=B)
        blocked,_=self.entry('合成远征屏蔽资料','合成远征.屏蔽');self.store.forget(A,blocked)
        plan,_=self.entry('明天准备合成远征','合成远征.计划')
        result=self.c.search(A,'合成远征',category='facts',semantic_refs=[foreign,blocked,plan,valid,valid])
        self.assertEqual([r['id'] for r in result['matches']],[valid]);self.assertEqual(result['matches'][0]['fusion'],'rrf')
        self.assertFalse(self.c.search(A,'合成远征',category='facts',semantic_refs=[valid],exclude=[valid])['matches'])
        self.assertFalse(self.c.search(A,'合成远征',start_day='2000-01-01',end_day='2000-01-02',category='facts',semantic_refs=[valid])['matches'])


class DecisionTests(unittest.TestCase):
    setUp=SceneTests.setUp
    tearDown=SceneTests.tearDown
    def submit(self,text,**fields):
        sid=self.store.observe(A,'user',text,at=time.time()-120)
        source=dict(self.store.db.execute('SELECT * FROM observations WHERE id=?',(sid,)).fetchone())
        result=self.store.apply_reflection(A,{'facts':[{'key':'合成饮品','quote':text,'source_id':sid,**fields}]},[source])
        return result,sid
    def test_duplicate_skips_without_refreshing_evidence_or_version(self):
        self.submit('我喜欢合成咖啡');old=self.store.entries(A)[0];result,_=self.submit('我喜欢合成咖啡')
        self.assertEqual(result['facts'],0);self.assertEqual(old,self.store.get_entry(A,old['id']))
        self.assertEqual(self.store.db.execute('SELECT action FROM memory_update_decisions ORDER BY id DESC').fetchone()[0],'skip')
    def test_explicit_skip_preserves_raw_source_without_fact_write(self):
        result,sid=self.submit('我喜欢合成咖啡',action='skip');self.assertEqual(result['facts'],0)
        self.assertTrue(self.store.db.execute('SELECT 1 FROM observations WHERE id=?',(sid,)).fetchone());self.assertFalse(self.store.entries(A))
    def test_explicit_correction_preserves_id_and_old_version(self):
        self.submit('我喜欢合成咖啡');old=self.store.entries(A)[0]
        result,sid=self.submit('现在我改为喜欢合成红茶',action='update',target_id=old['id'])
        self.assertEqual(result['facts'],1);self.assertIn('红茶',self.store.get_entry(A,old['id'])['text'])
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM entry_revisions WHERE entry_id=?',(old['id'],)).fetchone()[0],1)
    def test_merge_only_accepts_verbatim_restatement_plus_addition(self):
        self.submit('我喜欢合成咖啡');old=self.store.entries(A)[0]
        result,sid=self.submit('我喜欢合成咖啡，也喜欢合成红茶',action='merge',target_id=old['id'])
        row=self.store.get_entry(A,old['id']);self.assertEqual(result['facts'],1)
        self.assertEqual(json.loads(row['evidence']),json.loads(old['evidence'])+[sid]);self.assertIn('红茶',row['text'])
        self.assertTrue(self.store.lifecycle.provenance_valid(row))
    def test_merge_rejects_contradiction_foreign_fixed_and_unsupported_actions(self):
        self.submit('我喜欢合成咖啡');old=self.store.entries(A)[0]
        for text,fields in [('我也喜欢合成红茶',{'action':'merge','target_id':old['id']}),('我不喜欢合成咖啡，也喜欢合成红茶',{'action':'merge','target_id':old['id']}),('我喜欢合成咖啡',{'action':'delete','target_id':old['id']}),('我喜欢合成咖啡',{'action':'update','target_id':'ffffffff'})]:
            self.assertEqual(self.submit(text,**fields)[0]['facts'],0)
        self.assertEqual(old,self.store.get_entry(A,old['id']))
    def test_update_without_correction_cannot_overwrite(self):
        self.submit('我喜欢合成咖啡');old=self.store.entries(A)[0]
        self.assertEqual(self.submit('我喜欢合成红茶',action='update',target_id=old['id'])[0]['facts'],0)
        self.assertEqual(old,self.store.get_entry(A,old['id']))


class FusionTests(unittest.TestCase):
    def test_common_ranked_candidate_promoted(self):
        scores=rrf([['a','b'],['c','b']],{'a','b','c'});self.assertEqual(max(scores,key=scores.get),'b')
    def test_duplicates_and_filtered_references_cannot_shift_scores(self):
        self.assertEqual(rrf([['foreign','a','a','b'],['excluded','b']],{'a','b'}),rrf([['a','b'],['b']],{'a','b'}))

if __name__=='__main__':unittest.main()
