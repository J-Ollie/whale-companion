"""Signed revision timelines and current-request media evidence; no model/network."""
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock
sys.path.insert(0,os.environ.get('COMPANION_TEST_PLUGINS',str(Path(__file__).parent)))
from astrbot_plugin_companion_memory.memory import MemoryStore
from astrbot_plugin_companion_memory.cognition import Cognition
from astrbot_plugin_companion_memory.scenes import hashof
from astrbot_plugin_companion_chat.perception import PerceptionObserver
from astrbot_plugin_companion_chat import grounding

A='12345678';B='87654321';SCOPE='qq-companion:FriendMessage:'+A

class SceneRevisionTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.store=MemoryStore(self.tmp.name);self.addCleanup(self.store.close)
        self.c=Cognition(self.store);self.scenes=self.c.control.scenes;self.now=time.time()
    def write(self,text,topic='饮食.茶',at=None):
        at=self.now if at is None else at
        sid=self.store.observe(A,'user',text,at=at)
        key=self.store.put_entry(A,'fact',topic,'对方说：'+text,[sid],now=at+1)
        return key,sid
    def chain(self):
        key,one=self.write('我喜欢红茶',at=self.now-300)
        _,two=self.write('你记错了，我一直喜欢绿茶',at=self.now-200)
        _,three=self.write('我现在改成喜欢乌龙茶了',at=self.now-100)
        return key,one,two,three
    def seed(self,key):
        with self.store.atomic():self.store.db.execute('INSERT INTO memory_scenes VALUES(?,?,?,?,?,?,?,?,?)',(A,'scene:0123456789ab','饮食茶偏好','[]','{}','',0,0,'pending'))
        with self.store.atomic():self.store.db.execute('INSERT INTO memory_scene_links VALUES(?,?,?)',(A,'scene:0123456789ab',key))
        self.scenes.refresh(A)
        return self.scenes.get(A,'scene:0123456789ab')
    def test_three_versions_link_to_actual_successors(self):
        key,a,b,c=self.chain();row=self.store.get_entry(A,key)
        history=self.store.continuity.describe(row,history=True)['history']
        self.assertEqual([(x['previous_source_ids'],x['current_source_ids']) for x in history],[([b],[c]),([a],[b])])
    def test_scene_preserves_all_verified_timeline_sources(self):
        key,a,b,c=self.chain();body,_=self.scenes.build(A,'饮食茶偏好',[key])
        self.assertEqual(body['source_ids'],[a,b,c]);self.assertEqual(len(body['source_relations']),2)
        self.assertEqual(body['current_facts'][0]['source_ids'],[c])
    def test_correction_not_rendered_as_previous_true_preference(self):
        key,a,b,c=self.chain();body,_=self.scenes.build(A,'饮食茶偏好',[key])
        self.assertEqual(body['background']['id'],'obs:'+str(c))
        old=next(x for x in body['timeline'] if x['id']=='obs:'+str(a))
        self.assertIn('不代表过去真实如此',old['text'])
        relation=next(x for x in body['source_relations'] if x['relation']=='correction')
        self.assertEqual(relation['status'],'corrected_assertion')
    def test_render_has_both_source_anchors_and_no_causal_inference(self):
        key,a,b,c=self.chain();body,_=self.scenes.build(A,'饮食茶偏好',[key]);text=self.scenes.render(body)
        self.assertIn('旧原话 obs:'+str(a)+' → 后续原话 obs:'+str(b),text)
        self.assertIn('不证明因果',text)
    def test_old_source_change_invalidates_cached_scene(self):
        key,a,b,c=self.chain();self.assertIsNotNone(self.seed(key))
        self.store.db.execute('UPDATE observations SET text=? WHERE id=?',('source changed',a))
        self.assertIsNone(self.scenes.get(A,'scene:0123456789ab'))
        body,_=self.scenes.build(A,'饮食茶偏好',[key]);self.assertNotIn(a,body['source_ids'])
    def test_changed_middle_source_invalidates_both_adjacent_links(self):
        key,a,b,c=self.chain();self.store.db.execute('UPDATE observations SET text=? WHERE id=?',('source changed',b))
        self.assertNotIn('history',self.store.continuity.describe(self.store.get_entry(A,key),history=True))
    def test_suppression_does_not_revive_old_revision(self):
        key,a,b,c=self.chain();self.store.db.execute('INSERT INTO suppressed_sources VALUES(?,?)',(A,a))
        body,_=self.scenes.build(A,'饮食茶偏好',[key]);self.assertNotIn(a,body['source_ids'])
    def test_cross_contact_scene_cannot_read_timeline(self):
        key,a,b,c=self.chain();self.seed(key)
        self.assertIsNone(self.scenes.get(B,'scene:0123456789ab'))
        body,refs=self.scenes.build(B,'饮食茶偏好',[key]);self.assertEqual(refs,[]);self.assertEqual(body['source_ids'],[])
    def test_legacy_signature_unchanged_when_no_transition(self):
        key,sid=self.write('我喜欢绿茶');row,state,sources=self.scenes.member(A,key)
        self.assertEqual(self.scenes.signature(A,[key]),hashof([[row,state,sources]]))
        body,_=self.scenes.build(A,'饮食茶偏好',[key]);self.assertNotIn('source_relations',body)
    def test_cancelled_plan_remains_user_report_not_execution_receipt(self):
        key,old=self.write('我计划明天去海棠会议','行程.海棠',self.now-100)
        new=self.store.observe(A,'user','海棠会议已经取消了，因为场地维修',at=self.now)
        self.store.apply_reflection(A,{'facts':[{'key':'行程.海棠','source_id':new,'quote':'海棠会议已经取消了，因为场地维修','action':'update','target_id':key,'status':'cancelled'}]},self.store.messages(A),now=self.now+1)
        body,_=self.scenes.build(A,'海棠会议',[key])
        self.assertEqual(body['pending'],[]);self.assertEqual(body['changes'][0]['status'],'cancelled')
        self.assertEqual(body['source_relations'][0]['relation'],'cancellation')
        self.assertIn('不证明因果、实际执行',body['source_relations'][0]['note'])
    def test_forget_removes_scene_evidence(self):
        key,a,b,c=self.chain();self.seed(key);self.store.forget(A,key)
        self.assertIsNone(self.scenes.get(A,'scene:0123456789ab'))

class Event:
    def __init__(self,qq=A,mid='1001'):
        self.qq=qq;self.unified_msg_origin='qq-companion:FriendMessage:'+qq
        self.message_obj=NS(message_id=mid);self.extra={};self.message_str='刚才有转写文字返回'
    def get_platform_id(self):return 'qq-companion'
    def get_sender_id(self):return self.qq
    def get_group_id(self):return ''
    def get_extra(self,k,default=None):return self.extra.get(k,default)
    def set_extra(self,k,value):self.extra[k]=value

class CurrentMediaTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.now=time.time()
        self.memory=MemoryStore(Path(self.tmp.name)/'memory');self.addCleanup(self.memory.close)
        self.stars={'astrbot_plugin_companion_memory':NS(activated=True,star_cls=NS(store=self.memory))}
        self.context=NS(get_registered_star=self.stars.get,llm_generate=AsyncMock(return_value=NS(completion_text='{"supported":true}')))
        self.active=NS(context=self.context,umo=SCOPE,config={},state={},commitments=NS(evidence=lambda:[]))
        self.chat=NS(context=self.context,active=self.active,ready=True,batch=NS(allowed=lambda e:e.qq==A),friends=NS(owner=A,policy=NS(root=Path(self.tmp.name)/'friends'),sleeping=lambda:False))
        self.observer=PerceptionObserver(self.chat);self.addCleanup(self.observer.close);self.chat.perceptions=self.observer
        self.stars['astrbot_plugin_companion_chat']=NS(activated=True,star_cls=self.chat)
        self.event=Event();self.event.set_extra('companion_perception_started_at',self.now-10)
    def record(self,kind='audio_transcript',status='returned',qq=A,mid='1001',at=None):
        sc='qq-companion:FriendMessage:'+qq;at=self.now-5 if at is None else at
        rid=self.observer.store.begin(sc,mid,'private-source',kind,now=at)
        if status!='attempted':self.observer.store.finish(rid,sc,status,'synthetic result',now=at+1)
        return rid
    def response(self,text):return NS(role='assistant',tools_call_name=None,completion_text=text,result_chain=None)
    async def test_current_records_reach_existing_check(self):
        rid=self.record();response=self.response(self.event.message_str)
        await grounding.audit(self.active,self.event,response)
        data=json.loads(self.context.llm_generate.await_args.kwargs['prompt'])
        self.assertEqual(data['perception_this_turn']['events'][0]['id'],rid)
        self.assertEqual(data['perception_this_turn']['returned_kinds'],['audio_transcript'])
        self.assertIn('不证明听过音色',data['perception_this_turn']['note'])
        self.context.llm_generate.assert_awaited_once()
    async def test_old_message_or_reused_id_cannot_supply_current_proof(self):
        self.record(mid='older');self.record(at=self.now-100)
        self.assertIsNone(self.observer.evidence(self.event))
    async def test_missing_observation_marker_does_not_search_history(self):
        self.record();self.event.extra={};self.assertIsNone(self.observer.evidence(self.event))
    async def test_failed_and_attempted_not_in_returned_kinds(self):
        self.record(status='failed');self.record(kind='image_caption',status='attempted')
        out=self.observer.evidence(self.event);self.assertEqual(out['returned_kinds'],[])
    async def test_scope_and_live_owner_identity_preserved(self):
        self.record(qq=B);self.assertIsNone(self.observer.evidence(self.event))
        self.assertIsNone(grounding.current_perception_evidence(self.active,Event(B)))
        fake=NS(context=self.context,umo=SCOPE)
        self.assertIsNone(grounding.current_perception_evidence(fake,self.event))
    async def test_suppressed_source_not_sent_to_checker(self):
        sid=self.memory.observe(A,'user','synthetic',origin=SCOPE,dedupe='qq:'+A+':1001')
        self.record();self.memory.db.execute('INSERT INTO suppressed_sources VALUES(?,?)',(A,sid))
        self.assertIsNone(grounding.current_perception_evidence(self.active,self.event))
    async def test_forged_event_receipts_not_accepted(self):
        self.event.set_extra('perception_this_turn',{'events':[{'id':'made-up','status':'returned'}]})
        self.assertIsNone(grounding.current_perception_evidence(self.active,self.event))
    async def test_empty_evidence_does_not_change_old_check_payload(self):
        await grounding.audit(self.active,self.event,self.response(self.event.message_str))
        data=json.loads(self.context.llm_generate.await_args.kwargs['prompt'])
        self.assertNotIn('perception_this_turn',data)
    async def test_current_feelings_do_not_add_a_check(self):
        self.record();response=self.response('我觉得这句话挺暖的')
        await grounding.audit(self.active,self.event,response);self.context.llm_generate.assert_not_awaited()
    async def test_payload_stays_bounded_and_has_no_raw_result(self):
        for _ in range(12):self.record()
        out=self.observer.evidence(self.event)
        self.assertLessEqual(len(json.dumps(out,ensure_ascii=False,separators=(',',':')).encode()),1600)
        self.assertTrue(out['more']);self.assertNotIn('synthetic result',json.dumps(out))

if __name__=='__main__':unittest.main()
