"""Grounded long-term recall, contact isolation and friend-only read tools."""
import asyncio,json,os,sys,tempfile,time,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock,Mock
sys.path.insert(0,os.environ.get('COMPANION_TEST_PLUGINS','/AstrBot/data/plugins'))
from astrbot_plugin_companion_memory.memory import MemoryStore
from astrbot_plugin_companion_memory.cognition import Cognition,TOOLS
from astrbot_plugin_companion_memory.main import CompanionMemory
from astrbot_plugin_companion_friends.main import CompanionFriends,VERIFIED
from astrbot_plugin_companion_friends.archive import ChatArchive
from astrbot.api.provider import ProviderRequest

OWNER='100000001';A='12345678';B='87654321'

class CognitiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.store=MemoryStore(self.tmp.name);self.c=Cognition(self.store)
    def tearDown(self):self.store.close();self.tmp.cleanup()
    def message(self,qq,text,role='user',at=None):
        i=self.store.observe(qq,role,text,at=at)
        return dict(self.store.db.execute('SELECT * FROM observations WHERE id=?',(i,)).fetchone())
    def fact(self,qq,text,topic='饮食.香菜'):
        row=self.message(qq,text)
        out=self.store.apply_reflection(qq,{'facts':[{'key':topic,'quote':text,'source_id':row['id']}]},[row],subject='对方')
        self.assertEqual(out['facts'],1)
        return self.store.entries(qq)[0]

    def test_personal_core_is_always_present_and_cross_contact_private(self):
        self.fact(A,'我不吃香菜')
        self.fact(B,'我家猫叫星星','宠物.名字')
        a=self.c.render(A,'随便聊聊');b=self.c.render(B,'今天怎么样')
        self.assertIn('不吃香菜',a);self.assertNotIn('星星',a)
        self.assertIn('星星',b);self.assertNotIn('不吃香菜',b)
        self.assertNotIn('不吃香菜',self.c.render(OWNER))

    def test_alias_recall_and_source_read(self):
        fact=self.fact(A,'我不吃香菜')
        data=self.c.search(A,'芫荽')
        self.assertEqual(data['matches'][0]['id'],fact['id'])
        self.assertIn('我不吃香菜',self.c.read(A,fact['id'])['evidence'][0]['text'])
        with self.assertRaises(ValueError):self.c.read(B,fact['id'])
        with self.assertRaises(ValueError):self.c.read(B,'obs:'+str(json.loads(fact['evidence'])[0]))

    def test_episode_and_continuity_use_quotes_not_model_inventions(self):
        a=self.message(A,'上次你说让我看海底两万里')
        b=self.message(A,'等周末再讨论这本书')
        output={'continuity':{'items':[{'source_id':b['id'],'quote':b['text']}]},
            'episodes':[{'topic':'读书.约定','items':[{'source_id':a['id'],'quote':a['text']},
                {'source_id':b['id'],'quote':'我们已经一起去过法国'}]}]}
        changed=self.c.digest(A,output,[a,b],0)
        self.assertEqual(changed,{'continuity':1,'episodes':1})
        self.assertIn('周末',self.c.render(A));self.assertNotIn('法国',self.c.render(A))
        self.assertTrue(self.c.search(A,'海底两万里')['matches'])
        self.assertFalse(self.c.search(B,'海底两万里')['matches'])

    def test_assistant_quotes_are_events_not_user_facts(self):
        row=self.message(A,'推荐你看海底两万里','assistant')
        output={'facts':[{'key':'推荐书','quote':row['text'],'source_id':row['id']}],
            'episodes':[{'topic':'推荐书','items':[{'source_id':row['id'],'quote':row['text']}]}]}
        self.assertEqual(self.store.apply_reflection(A,output,[row])['facts'],0)
        self.c.digest(A,output,[row],0)
        self.assertIn('你在对话中说过',self.store.entries(A,'episode')[0]['text'])

    def test_cross_person_supplied_source_cannot_become_fact_or_event(self):
        row=self.message(B,'B_PRIVATE_SENTINEL')
        out={'facts':[{'key':'偏好','quote':row['text'],'source_id':row['id']}],
             'episodes':[{'items':[{'source_id':row['id'],'quote':row['text']}]}]}
        self.assertEqual(self.store.apply_reflection(A,out,[row])['facts'],0)
        self.assertEqual(self.c.digest(A,out,[row],0)['episodes'],0)

    def test_forget_does_not_resurrect_through_raw_search_or_digest(self):
        fact=self.fact(A,'我不吃香菜');source_id=json.loads(fact['evidence'])[0]
        self.assertTrue(self.store.forget(A,fact['id']))
        self.assertFalse(self.c.search(A,'香菜')['matches'])
        with self.assertRaises(ValueError):self.c.read(A,'obs:'+str(source_id))
        row=dict(self.store.db.execute('SELECT * FROM observations WHERE id=?',(source_id,)).fetchone())
        self.assertEqual(self.c.digest(A,{'continuity':{'items':[{'source_id':source_id,'quote':row['text']}]}},[row],0)['continuity'],0)

    def test_date_scope_and_not_found_are_not_never_happened(self):
        from datetime import datetime
        from astrbot_plugin_companion_memory.memory import TZ
        self.message(A,'海底两万里',at=datetime(2026,10,20,12,tzinfo=TZ).timestamp())
        self.assertTrue(self.c.search(A,'海底两万里','2026-10-20','2026-10-20')['matches'])
        self.assertFalse(self.c.search(A,'海底两万里','2026-10-21','2026-10-21')['matches'])
        self.assertIn('不等于',self.c.search(A,'不存在的话题')['message'])
        for day in ['2026-13-01','DROP TABLE observations']:
            with self.assertRaises(ValueError):self.c.search(A,'书',day)

    def test_correction_preserves_revision_but_uses_latest_fact(self):
        self.store.put_entry(A,'fact','咖啡','咖啡加糖',[],now=time.time()-60)
        self.store.put_entry(A,'fact','咖啡','咖啡不加糖',[],now=time.time())
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM entry_revisions WHERE qq=?',(A,)).fetchone()[0],1)
        self.assertIn('不加糖',self.c.search(A,'咖啡')['matches'][0]['text'])

    def test_natural_correction_reuses_existing_fact_without_cross_person_replacement(self):
        old=self.fact(A,'我喜欢甜咖啡','饮食.咖啡')
        row=self.message(A,'现在咖啡只喝无糖的')
        output={'facts':[{'key':'偏好.咖啡','quote':row['text'],'source_id':row['id'],'replaces_id':old['id']}]}
        self.store.apply_reflection(A,output,[row],subject='对方')
        self.assertEqual(len(self.store.entries(A)),1)
        self.assertIn('无糖',self.store.get_entry(A,old['id'])['text'])
        other=self.message(B,'现在咖啡也只喝无糖的')
        output['facts'][0].update(quote=other['text'],source_id=other['id'])
        self.assertEqual(self.store.apply_reflection(B,output,[other])['facts'],0)

class CognitivePluginTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();root=Path(self.tmp.name)
        self.p=CompanionMemory.__new__(CompanionMemory);p=self.p
        p.owner=OWNER;p.umo='qq-companion:FriendMessage:'+OWNER;p.config={'enabled':True,'auto_extract':True}
        p.store=MemoryStore(root/'companion_memory');p.processing=asyncio.Lock()
        self.archive=ChatArchive(root/'companion_friends')
        self.friends=SimpleNamespace(archive=self.archive,sleeping=lambda:False,policy=SimpleNamespace(is_friend=AsyncMock(return_value=True)))
        p.context=SimpleNamespace(get_registered_star=lambda name:SimpleNamespace(activated=True,star_cls=self.friends),
            get_config=lambda **kw:{'provider_settings':{'enable':True}},llm_generate=AsyncMock())
    def tearDown(self):self.p.store.close();self.tmp.cleanup()
    def event(self,qq=A,verified=True):
        extra={VERIFIED:verified}
        return SimpleNamespace(get_sender_id=lambda:qq,unified_msg_origin='qq-companion:FriendMessage:'+qq,
            get_extra=lambda k,d=None:extra.get(k,d),set_extra=lambda k,v:extra.__setitem__(k,v),message_str='还记得我吗')

    async def test_archive_backfill_is_idempotent_and_friend_reflection_is_scoped(self):
        self.archive.record(A,'甲','in','我不吃香菜',101)
        self.archive.record(B,'乙','in','B_PRIVATE_SENTINEL',102)
        self.assertEqual(self.p.import_friend_archive(),2);self.assertEqual(self.p.import_friend_archive(),0)
        row=self.p.store.messages(A)[0]
        self.p.context.llm_generate.return_value=SimpleNamespace(completion_text=json.dumps({'facts':[{'key':'饮食.香菜','quote':row['text'],'source_id':row['id']}],
            'continuity':{'items':[{'source_id':row['id'],'quote':row['text']}]}}))
        await self.p.reflect(qq=A,force=True)
        self.assertIn('对方说',self.p.store.entries(A)[0]['text'])
        self.assertFalse(self.p.store.entries(B));self.assertFalse(self.p.store.entries(OWNER))
        payload=self.p.context.llm_generate.call_args.kwargs['prompt']
        self.assertNotIn('B_PRIVATE_SENTINEL',payload)
        self.assertNotIn('F1nger说',self.p.friend_context(self.event()))

    async def test_sleep_blocks_import_reflection_and_recall(self):
        self.friends.sleeping=lambda:True
        self.archive.record(A,'甲','in','我不吃香菜',101)
        self.assertEqual(self.p.import_friend_archive(),0)
        await self.p.reflect(force=True)
        result=await self.p.tool_recall(self.event(),'香菜')
        self.assertTrue(result.isError);self.p.context.llm_generate.assert_not_called()

    async def test_recall_validates_identity_and_revocation_and_budget(self):
        self.p.store.observe(A,'user','我不吃香菜')
        for event in [self.event(verified=False),self.event(qq='nobody')]:
            self.assertTrue((await self.p.tool_recall(event,'香菜')).isError)
        self.friends.policy.is_friend.return_value=False
        self.assertTrue((await self.p.tool_recall(self.event(),'香菜')).isError)
        self.friends.policy.is_friend.return_value=True;e=self.event()
        for _ in range(4):self.assertFalse((await self.p.tool_recall(e,'芫荽')).isError)
        self.assertTrue((await self.p.tool_recall(e,'芫荽')).isError)

    async def test_friend_tool_set_contains_only_read_only_memory_tools(self):
        friend=CompanionFriends.__new__(CompanionFriends)
        friend.context=SimpleNamespace(get_registered_star=lambda name:SimpleNamespace(activated=True,star_cls=self.p))
        tools=friend.friend_tools()
        self.assertEqual({t.name for t in tools.tools},TOOLS)
