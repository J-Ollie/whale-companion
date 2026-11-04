"""Quoted provisional knowledge and immutable revisions. Synthetic excerpts only."""
import copy
import hashlib
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0,os.environ.get('COMPANION_TEST_PLUGINS',str(Path(__file__).parent)))
from astrbot_plugin_companion_phone.knowledge import Knowledge
from astrbot_plugin_companion_phone import reading

NOW=1792476000.0
TOPIC='explore:'+'a'*12
QUESTION='合成植物在什么条件下会发光'
QUOTE_A='合成观测中，蓝灯开启时样品出现亮点。'
QUOTE_B='合成复核表明，亮点由反光造成而不是样品发光。'


def article(quote=QUOTE_A,url='https://example.org/source-a',prefix='合成试验记录，仅用于软件检查，不是真实知识。每个样品分别编号，记录日期、环境、光照和仪器设置，并保留不同批次的观测差异。'):
    text=prefix+quote+'记录未确定原因，还需要不同条件的观测。'*10
    return reading.extract(url,'text/plain; charset=utf-8',text.encode())


def update(**changes):
    row={'claim':'资料描述蓝灯下有亮点','quote':QUOTE_A,'relation':'unclear','action':'keep',
        'judgment':'亮点原因尚未确定','reason':'仅有一次合成观测','pending':'排除外部反光','cites_index':-1}
    row.update(changes);return row


class KnowledgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.clock=patch('time.time',return_value=NOW);self.clock.start();self.addCleanup(self.clock.stop)
        self.k=Knowledge(self.tmp.name)
        self.reader=reading.PublicReading(self.tmp.name,{'enabled':True,'public_reading_enabled':True})

    def record(self,page=None,data=None,version=0,topic=TOPIC):
        page=page or article();source=self.reader.receipt(page,'合成资料阅读')
        if source is None:
            source=next(r for r in self.reader.state['receipts'] if r['content_hash']==page['content_hash'])
        return self.k.record(topic,QUESTION,page,source,update() if data is None else data,version)

    def test_initial_provisional_judgment_and_exact_quote_survive_restart(self):
        self.assertFalse(self.k.path.exists());self.assertEqual(self.record(),'recorded')
        restored=Knowledge(self.tmp.name);self.assertTrue(restored.healthy)
        history=restored.review(TOPIC);row=history['versions'][0]
        self.assertEqual(row['status'],'provisional');self.assertEqual(row['version'],1)
        self.assertEqual(row['evidence']['quote'],QUOTE_A)
        with closing(sqlite3.connect(self.k.path)) as db:
            body=db.execute('SELECT body FROM sources').fetchone()[0]
        e=row['evidence'];self.assertEqual(body[e['quote_start']:e['quote_end']],QUOTE_A)
        self.assertEqual(history['verification'],'not_independently_verified')

    def test_fabricated_rewritten_or_missing_quotes_do_not_create_database(self):
        for data in [update(quote='网页没有说过的句子'),update(quote='蓝灯开着时样品有亮点'),update(quote='短句'),{}, {'confidence':1}]:
            self.assertEqual(self.record(data=data),'invalid_update')
        self.assertFalse(self.k.path.exists())

    def test_private_or_wrong_hash_timing_and_body_cannot_become_source(self):
        page=article();source=self.reader.receipt(page,'合成资料')
        for changes in ({'public_content':False},{'app':'QQ'},{'content_hash':'b'*64},{'timestamp':NOW-5}):
            self.assertEqual(self.k.record(TOPIC,QUESTION,page,{**source,**changes},update()),'invalid_source')
        changed={**page,'text':page['text']+'伪造新增内容'}
        self.assertEqual(self.k.record(TOPIC,QUESTION,changed,source,update()),'invalid_source')
        self.assertFalse(self.k.path.exists())

    def test_revision_keeps_exact_old_judgment_and_reason(self):
        self.record();before=self.k.review(TOPIC)['versions'][0]
        self.assertEqual(self.record(article(QUOTE_B,'https://another.example/new',prefix='另一份合成复核说明。'),
            update(quote=QUOTE_B,claim='复核认为是反光',relation='contradicts',action='revise',
                   judgment='这次亮点可能来自反光',reason='新资料提出了反光解释'),1),'recorded')
        history=self.k.review(TOPIC)['versions']
        self.assertEqual([v['version'] for v in history],[2,1]);self.assertEqual(history[1],before)
        self.assertEqual(history[0]['judgment'],'这次亮点可能来自反光')
        self.assertEqual(history[0]['status'],'provisional')

    def test_conflict_can_remain_unresolved_and_later_agreement_does_not_clear_it(self):
        self.record()
        self.record(article(QUOTE_B,'https://another.example/b'),update(quote=QUOTE_B,relation='contradicts'),1)
        self.assertEqual(self.k.latest(TOPIC)['status'],'contested')
        quote='合成的第三份记录同样观察到了一些亮点。'
        self.record(article(quote,'https://third.example/c'),update(quote=quote,relation='supports'),2)
        self.assertEqual(self.k.latest(TOPIC)['status'],'contested')

    def test_withdrawn_judgment_is_not_active_and_old_version_remains(self):
        self.record();old=self.k.latest(TOPIC)['judgment']
        self.record(article(QUOTE_B,'https://another.example/b'),update(quote=QUOTE_B,action='withdraw',
                    reason='旧解释缺少依据，先撤回',relation='contradicts'),1)
        latest=self.k.latest(TOPIC);self.assertEqual(latest['status'],'withdrawn');self.assertEqual(latest['judgment'],'')
        self.assertEqual(self.k.review(TOPIC)['versions'][1]['judgment'],old)
        self.assertIn('withdrawn已撤回',self.k.context([TOPIC]))

    def test_reasonless_or_unchanged_revision_rolls_back_claim_and_version(self):
        self.record();page=article(QUOTE_B,'https://another.example/b')
        before=self.k.review(TOPIC)
        for data in (update(quote=QUOTE_B,action='revise',reason=''),update(quote=QUOTE_B,action='revise')):
            self.assertEqual(self.record(page,data,1),'invalid_update')
            self.assertEqual(self.k.review(TOPIC),before)
        with closing(sqlite3.connect(self.k.path)) as db:self.assertEqual(db.execute('SELECT count(*) FROM claims').fetchone()[0],1)

    def test_stale_version_cannot_overwrite_a_newer_judgment(self):
        self.record();before=self.k.review(TOPIC)
        self.assertEqual(self.record(article(QUOTE_B,'https://another.example/b'),update(quote=QUOTE_B),0),'stale_revision')
        self.assertEqual(self.k.review(TOPIC),before)

    def test_repeated_content_cannot_add_version_even_under_new_url(self):
        self.record();page=article(url='https://copy.example/copied')
        source={**self.reader.state['receipts'][0],'id':'web:'+'b'*12,'url':page['url'],'title':page['title']}
        self.assertEqual(self.k.record(TOPIC,QUESTION,page,source,update(),1),'duplicate_material')
        self.assertEqual(self.k.latest(TOPIC)['version'],1)

    def test_similar_wording_and_link_to_old_source_are_only_dependency_hints(self):
        self.record()
        page=article(QUOTE_B,'https://copy.example/b')
        page['links']=['https://example.org/source-a']
        self.record(page,update(quote=QUOTE_B,cites_index=0),1)
        row=self.k.review(TOPIC)['versions'][0]
        self.assertEqual(row['evidence']['dependency'],'possible_linked_source')
        self.assertNotIn('confidence',row);self.assertNotEqual(row['status'],'verified')

    def test_similar_text_is_detected_but_different_domain_never_means_independent(self):
        self.record()
        quote='合成观测中，蓝灯开启后样品出现亮点。'
        self.record(article(quote,'https://copy.example/b'),update(quote=quote),1)
        self.assertEqual(self.k.review(TOPIC)['versions'][0]['evidence']['dependency'],'similar_text')
        distinct='不同的合成文本记录了一次测量失败，尚无可比结果。'
        self.record(article(distinct,'https://new.example/c',prefix='另一种方法的仪器日志。'*20),update(quote=distinct),2)
        self.assertEqual(self.k.review(TOPIC)['versions'][0]['evidence']['dependency'],'unknown')

    def test_same_host_is_not_independent_confirmation(self):
        self.record()
        quote='另一组日志在关闭设备后没有检测到任何亮点。'
        self.record(article(quote,'https://example.org/another',prefix='不同批次的合成测量。'*20),update(quote=quote),1)
        self.assertEqual(self.k.review(TOPIC)['versions'][0]['evidence']['dependency'],'same_host')

    def test_extra_verification_labels_and_user_approval_do_not_make_verified_knowledge(self):
        self.assertEqual(self.record(data=update(status='verified',confidence=1,likes=100)),'recorded')
        self.assertEqual(self.k.latest(TOPIC)['status'],'provisional')
        self.assertEqual(self.k.record(TOPIC,QUESTION,article(),{'id':'chat:owner'},update(),1),'invalid_source')

    def test_first_uncertainty_and_unavailable_archive_are_preserved(self):
        self.record(data=update(action='uncertain'));self.assertEqual(self.k.latest(TOPIC)['status'],'uncertain')
        with patch('astrbot_plugin_companion_phone.knowledge.MAX_BYTES',1):
            self.assertEqual(self.record(article(QUOTE_B,'https://other.example/b'),update(quote=QUOTE_B),1),'archive_full')
        self.assertEqual(self.k.latest(TOPIC)['version'],1)

    def test_corrupt_or_unknown_schema_is_not_recreated(self):
        self.k.path.write_bytes(b'broken database')
        old=self.k.path.read_bytes();restored=Knowledge(self.tmp.name)
        self.assertFalse(restored.healthy);self.assertEqual(restored.path.read_bytes(),old)
        self.assertEqual(restored.review(TOPIC),{'status':'not_available'})

    def test_tampered_excerpt_is_not_presented_as_matched_evidence(self):
        self.record()
        with closing(sqlite3.connect(self.k.path)) as db,db:db.execute('UPDATE sources SET body=?',('tampered',))
        self.assertEqual(self.k.review(TOPIC),{'status':'unavailable'})
        self.assertFalse(self.k.healthy)

    def test_context_and_review_are_bounded_and_topics_are_explicitly_scoped(self):
        self.record();other='explore:'+'b'*12
        quote='另一主题的合成材料说明这里不涉及原来的问题。'
        self.record(article(quote,'https://another.example/b'),update(quote=quote),0,other)
        context=self.k.context([TOPIC]);self.assertIn(TOPIC,context);self.assertNotIn(other,context)
        payload=context.split('：',1)[1]
        self.assertLessEqual(len(payload.encode()),2200)
        self.assertLessEqual(len(json.dumps(self.k.review(TOPIC),ensure_ascii=False).encode()),6000)

    def test_html_links_are_bounded_and_not_automatically_followed(self):
        raw=('<main>'+('用于合成解析检查的公开文字。'*20)+
             '<a href="/source">参考页</a><a href="https://127.0.0.1/">内网</a>'+
             '<a href="https://example.org/?token=secret">凭据链接</a></main>').encode()
        result=reading.extract('https://example.org/current','text/html',raw)
        self.assertEqual(result['links'],['https://example.org/source'])

    def test_local_keyword_lookup_finds_archived_topic_without_sql_interpretation(self):
        self.record()
        self.assertEqual(self.k.lookup('合成植物')[0]['id'],TOPIC)
        self.assertEqual(self.k.lookup("' OR 1=1 --"),[])
        self.assertEqual(self.k.lookup('不存在的问题'),[])


class KnowledgeFlowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        from test_public_reading import ReadingFlowTests
        self.harness=ReadingFlowTests();self.harness.setUp();self.addCleanup(self.harness.doCleanups)
        self.a=self.harness.a
        self.harness.fetch.side_effect=lambda url:article()
        self.a.reading_model.return_value=json.dumps({'useful':True,'note':'取得合成资料',
            'exploration':{'question':QUESTION,'progress':'先保留疑问','next_step':'反光的影响'},'knowledge':update()},ensure_ascii=False)

    async def test_real_read_flow_records_one_revision_with_same_two_model_calls(self):
        await self.a.consider()
        topic=self.a.exploration.rows[0]['id']
        self.assertEqual(self.a.state['last_knowledge_result'],'recorded')
        self.assertEqual(self.a.knowledge.latest(topic)['version'],1)
        self.assertEqual(self.harness.harness.model.await_count,1);self.assertEqual(self.a.reading_model.await_count,1)
        self.assertIn('暂时判断与版本',self.a.context())

    async def test_invalid_quote_keeps_real_reading_but_creates_no_knowledge(self):
        response=json.loads(self.a.reading_model.return_value);response['knowledge']['quote']='模型编造的引文并不在资料里'
        self.a.reading_model.return_value=json.dumps(response)
        await self.a.consider()
        self.assertEqual(self.a.state['last_result'],'web_read')
        self.assertEqual(self.a.state['last_knowledge_result'],'invalid_update')
        self.assertFalse(self.a.knowledge.path.exists())

    async def test_prior_version_reaches_next_analysis_and_revision_is_retained(self):
        await self.a.consider();topic=self.a.exploration.rows[0]['id']
        self.a.rhythm.state['next_decision']=self.harness.now-1
        self.harness.harness.model.return_value=json.dumps({'activity':'read','reading_id':topic})
        self.harness.fetch.side_effect=lambda url:article(QUOTE_B,'https://new.example/second')
        self.a.reading_model.return_value=json.dumps({'useful':True,'note':'取得合成复核材料',
            'exploration':{'progress':'原解释需要修订','next_step':'核对反光条件'},
            'knowledge':update(quote=QUOTE_B,action='revise',judgment='亮点可能是反光',reason='合成复核提供不同解释')})
        await self.a.consider()
        self.assertIn('亮点原因尚未确定',self.a.reading_model.await_args.args[0])
        self.assertEqual(self.a.knowledge.latest(topic)['version'],2)

    async def test_review_tool_is_local_read_only_and_owner_scoped(self):
        await self.a.consider();topic=self.a.exploration.rows[0]['id']
        plugin,event=self.harness.tool_plugin();self.harness.search.reset_mock();self.harness.fetch.reset_mock()
        before=copy.deepcopy(self.a.reading.state)
        response=await plugin.tool_public_read(event,'review',reference=topic)
        self.assertFalse(response.isError);self.assertIn(QUOTE_A,response.content[0].text)
        self.assertEqual(before,self.a.reading.state);self.harness.search.assert_not_called();self.harness.fetch.assert_not_called()
        event.unified_msg_origin='test:FriendMessage:other'
        self.assertTrue((await plugin.tool_public_read(event,'review',reference=topic)).isError)

    async def test_sleep_during_analysis_cannot_record_new_knowledge(self):
        async def asleep(*args):
            self.a.rhythm.state.update(mode='asleep',until=self.harness.now+1000)
            return json.dumps({'useful':True,'note':'未提交','exploration':{'question':QUESTION},'knowledge':update()})
        self.a.reading_model.side_effect=asleep
        await self.a.consider()
        self.assertFalse(self.a.knowledge.path.exists());self.assertEqual(self.a.exploration.rows,[])


if __name__=='__main__':unittest.main()
