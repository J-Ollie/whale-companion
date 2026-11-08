"""Revision, actual image and linked outcome checks in isolated fixtures."""
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock
import test_thinking as fixtures
from test_thinking import NOW
from astrbot_plugin_companion_phone.self_experience import SelfExperience
from astrbot_plugin_companion_phone.thinking import SESSION,charge_model
from astrbot_plugin_companion_phone.main import CompanionPhone
from astrbot_plugin_companion_chat.main import CompanionChat

class WorkTests(unittest.IsolatedAsyncioTestCase):
    setUp=fixtures.ThinkingTests.setUp

    def work(self,body='print(1)',identifier='',revision=0):
        self.config['self_directed_enabled']=True
        return self.t.workspace.request({'action':'write','id':identifier,'revision':revision,'title':'自己的作品','body':body})

    def artifact(self):
        work=self.work();code='print(1)';digest=hashlib.sha256(code.encode()).hexdigest();identifier='a'*32
        directory=self.root/'practice-artifacts'/hashlib.sha256(self.t.store.scope.encode()).hexdigest()[:16]/identifier
        directory.mkdir(parents=True);path=directory/'0.png'
        from PIL import Image
        Image.new('RGB',(5,5),'blue').save(path)
        image_hash=hashlib.sha256(path.read_bytes()).hexdigest()
        result={'kind':'program_execution_not_fact_verification','status':'succeeded','id':'practice:'+identifier,'revision':1,
            'code_hash':digest,'artifacts':[{'id':'practice:'+identifier+':plot:0','format':'png','image_hash':image_hash,'relative_path':'../../untrusted'}]}
        with self.t.store.connect() as db:
            db.execute('INSERT INTO autonomy_practice_runs VALUES(?,?,?,?,?,?,?,?,?,?)',
                (self.t.store.scope,identifier,'run',NOW,'作品',code,digest,'succeeded',json.dumps(result),work['id']))
        return work,path,result

    def test_exact_old_revision_and_page_have_actual_read_receipt(self):
        w=self.work('旧内容'*400);self.work('新内容',w['id'],1)
        old=self.t.workspace.request({'action':'read','id':w['id'],'revision':1,'offset':800})
        self.assertEqual(old['body'],('旧内容'*400)[800:]);self.assertEqual(old['revision'],1)
        with self.t.store.connect() as db:self.assertEqual(db.execute('SELECT count(*) FROM work_reads').fetchone()[0],1)
        self.assertEqual(self.t.work_memory.history(w['id'])['revisions'][0]['revision'],2)
        with self.t.store.connect() as db:self.assertEqual(db.execute('SELECT count(*) FROM work_reads').fetchone()[0],1)

    def test_chain_joins_old_read_new_work_and_real_result(self):
        a=self.work();b=self.work('print(2)');self.t.run_id='run-one'
        first=self.t.work_memory.remember({'focus':'重新看看旧作品'},'workspace',self.t.work_memory.inspect(a['id']))
        second=self.t.work_memory.remember({'focus':'想比较另一种办法'},'workspace',b)
        links=self.t.work_memory.chain(b['id'])['links']
        self.assertEqual(links[0]['prior'],first);self.assertEqual({r['id'] for r in links},{first,second})
        with self.t.store.connect() as db:db.execute("UPDATE autonomy_works SET visibility='private' WHERE id=?",(a['id'],))
        self.assertEqual(len(self.t.work_memory.chain(b['id'])['links']),1)

    def test_source_forgetting_invalidates_private_work(self):
        w=self.work()
        with self.t.store.connect() as db:
            db.execute("UPDATE autonomy_works SET visibility='private',refs=? WHERE id=?",(json.dumps({'source':'old'}),w['id']))
        private=NS(t=self.t,validate=lambda:None,refs={'source':'new'},snapshot=lambda k:'new')
        with self.assertRaises(ValueError):self.t.work_memory.inspect(w['id'],private=private)
        with self.assertRaises(ValueError):self.t.work_memory.inspect(w['id'])

    def chat_event(self,sender='owner',group=''):
        extras={}
        return NS(unified_msg_origin=self.p.umo,get_sender_id=lambda:sender,get_group_id=lambda:group,
            get_extra=lambda k,d=None:extras.get(k,d),set_extra=lambda k,v:extras.__setitem__(k,v))
    def chat(self):
        self.p.thinking=self.t
        return NS(ready=True,config={},friends=NS(owner='owner',sleeping=lambda:False),active=NS(umo=self.p.umo),
            context=NS(get_registered_star=lambda n:NS(activated=True,star_cls=self.p)))
    async def test_chat_work_tool_rejects_group_other_sender_and_sleep(self):
        chat=self.chat();w=self.work()
        for e in (self.chat_event('other'),self.chat_event(group='group')):
            result=await CompanionChat.work_read(chat,e,'read',w['id'])
            self.assertTrue(result.isError)
        chat.friends.sleeping=lambda:True
        self.assertTrue((await CompanionChat.work_read(chat,self.chat_event(),'read',w['id'])).isError)
    async def test_chat_work_tool_reads_original_without_executing_and_limits_each_turn(self):
        chat=self.chat();w=self.work();event=self.chat_event()
        for _ in range(3):
            result=await CompanionChat.work_read(chat,event,'read',w['id'])
            self.assertFalse(result.isError);self.assertIn('print(1)',result.content[0].text)
        self.assertTrue((await CompanionChat.work_read(chat,event,'read',w['id'])).isError)
        self.context.llm_generate.assert_not_awaited()

    def test_artifact_uses_canonical_path_and_creation_hash(self):
        w,path,r=self.artifact();self.assertEqual(self.t.work_memory.artifact(r['id'])[2],path.read_bytes())
        path.write_bytes(b'\x89PNG\r\n\x1a\nchanged')
        with self.assertRaises(ValueError):self.t.work_memory.artifact(r['id'])
        with self.assertRaises(ValueError):self.t.work_memory.artifact('../../private')

    def test_reading_code_or_execution_is_not_viewing_image(self):
        w,path,r=self.artifact();self.t.work_memory.inspect(w['id'])
        view=SelfExperience(self.root,self.t.store.scope).snapshot()
        self.assertEqual(view['image_review_evidence'],'not_provided_by_program_receipts')
        self.assertFalse(any(x['kind']=='work_image_review' for x in view['work_reviews']))

    async def test_actual_image_review_consumes_shared_budget_and_keeps_observation(self):
        w,path,r=self.artifact()
        async def model(prompt,data):
            self.assertEqual(data,path.read_bytes());self.assertIsNotNone(SESSION.get());charge_model()
            return json.dumps({'seen':True,'observation':'蓝色方块','next_idea':'想换一种颜色'})
        self.p.public_model=AsyncMock(side_effect=model)
        result=await self.t.work_memory.review_image(r['id'],guard=lambda:None)
        self.assertEqual(result['kind'],'image_reviewed');self.assertIsNone(SESSION.get())
        self.assertEqual(self.t.resources.status()['used'],1)
        self.assertEqual(SelfExperience(self.root,self.t.store.scope).snapshot()['image_review_evidence'],'separate_receipts_only')
        self.t.work_memory.remember({'focus':'回看后有新想法'},'practice',result)
        self.assertIn('想换一种颜色',self.t.work_memory.chain(w['id'])['links'][0]['result'])

    async def test_png_input_reaches_native_provider_with_correct_suffix_and_is_removed(self):
        w,path,r=self.artifact();data=path.read_bytes();seen=[]
        self.p.context.persona_manager=NS(get_persona=AsyncMock(return_value=NS(system_prompt='小鲸鱼')))
        async def generate(**kwargs):
            image=Path(kwargs['image_urls'][0]);seen.append(image)
            self.assertEqual(image.suffix,'.png');self.assertEqual(image.read_bytes(),data)
            return NS(completion_text='{"seen":true,"observation":"蓝色","next_idea":""}')
        self.context.llm_generate.side_effect=generate
        self.p.public_model=lambda prompt,image:CompanionPhone.public_model(self.p,prompt,image)
        result=await self.t.work_memory.review_image(r['id'],guard=lambda:None)
        self.assertEqual(result['kind'],'image_reviewed');self.assertTrue(seen);self.assertFalse(seen[0].exists())

    async def test_mid_review_file_change_and_unknown_never_become_viewed(self):
        w,path,r=self.artifact()
        async def model(*a):
            path.write_bytes(b'changed')
            return '{"seen":true,"observation":"假观感","next_idea":""}'
        self.p.public_model=AsyncMock(side_effect=model)
        with self.assertRaises(ValueError):await self.t.work_memory.review_image(r['id'],guard=lambda:None)
        with self.t.store.connect() as db:self.assertEqual(db.execute('SELECT status FROM work_reviews').fetchone()[0],'interrupted_unknown')

    async def test_image_review_has_six_attempt_ceiling_even_when_model_unavailable(self):
        w,path,r=self.artifact();self.p.public_model=AsyncMock(side_effect=OSError('offline'))
        for _ in range(6):
            with self.assertRaises(OSError):await self.t.work_memory.review_image(r['id'],guard=lambda:None)
        with self.assertRaisesRegex(ValueError,'daily_limit'):await self.t.work_memory.review_image(r['id'],guard=lambda:None)
        self.assertEqual(self.p.public_model.await_count,6)

if __name__=='__main__':unittest.main()
