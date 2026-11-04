"""Anonymous reading and source integration. All networks and model calls are simulated."""
import asyncio
import copy
import io
import json
import os
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, os.environ.get('COMPANION_TEST_PLUGINS', str(Path(__file__).parent)))
from astrbot_plugin_companion_phone import reading
from astrbot_plugin_companion_phone.exploration import Exploration
from astrbot_plugin_companion_active.goals import GoalLedger

NOW = 1792476000.0
TEXT = '鲸类通过声音交流，研究者仍需区分观察和解释。'*30
URL = 'https://example.org/article'


def article():
    return reading.extract(URL, 'text/plain; charset=utf-8', TEXT.encode())


class Response:
    def __init__(self, body=b'', status=200, **headers):
        self.status, self.headers, self.body = status, headers, io.BytesIO(body)
    def getheader(self, key, default=''):
        return self.headers.get(key, default)
    def read1(self, size):
        return self.body.read(size)


class ReadingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.clock = patch('time.time', return_value=NOW)
        self.clock.start(); self.addCleanup(self.clock.stop)
        self.config = {'enabled': True, 'public_reading_enabled': True, 'public_reading_daily_limit': 2}
        self.reader = reading.PublicReading(self.tmp.name, self.config)

    def test_private_credentials_non_https_and_local_hosts_are_rejected(self):
        for url in ['http://example.org/', 'https://127.0.0.1/', 'https://[::1]/', 'https://100.64.0.10/',
                    'https://169.254.169.254/', 'https://host.local/', 'https://localhost/', 'https://host:8443/',
                    'https://user:pass@example.org/', 'https://example.org/?token=private',
                    'https://example.org/?sessionid=private', 'https://example.org/\r\nCookie:x']:
            with self.subTest(url=url), self.assertRaises(reading.ReadingError):
                reading.public_url(url)

    def test_dns_with_any_private_address_is_rejected_before_connection(self):
        with patch('astrbot_plugin_companion_phone.public_images.socket.getaddrinfo', return_value=[
            (socket.AF_INET,socket.SOCK_STREAM,6,'',('93.184.216.34',443)),
            (socket.AF_INET,socket.SOCK_STREAM,6,'',('127.0.0.1',443))]), patch('astrbot_plugin_companion_phone.public_images.socket.socket') as dial:
            with self.assertRaises(ValueError):reading.PublicHTTPS('example.org').connect()
            dial.assert_not_called()

    def test_dns_is_pinned_but_tls_uses_original_hostname(self):
        context=Mock();connection=reading.PublicHTTPS('example.org',context=context)
        with patch('astrbot_plugin_companion_phone.public_images.socket.getaddrinfo',return_value=[
            (socket.AF_INET,socket.SOCK_STREAM,6,'',('93.184.216.34',443))]), patch('astrbot_plugin_companion_phone.public_images.socket.socket') as dial:
            connection.connect()
            dial.return_value.connect.assert_called_once_with(('93.184.216.34',443))
            self.assertEqual(context.wrap_socket.call_args.kwargs['server_hostname'],'example.org')

    def test_redirect_is_checked_and_never_sends_cookies_or_auth(self):
        connection=Mock();connection.getresponse.return_value=Response(status=302,Location='https://127.0.0.1/private',**{'Set-Cookie':'session=x'})
        with patch.object(reading,'PublicHTTPS',return_value=connection) as cls:
            with self.assertRaises(reading.ReadingError):reading.fetch(URL,('text/plain',))
            cls.assert_called_once()
            headers=connection.request.call_args.kwargs['headers']
            self.assertNotIn('Cookie',headers);self.assertNotIn('Authorization',headers)
            connection.close.assert_called_once()

    def test_limits_status_encoding_and_nontext_content_fail_closed(self):
        cases=[Response(b'x'*(reading.MAX_BODY+1),**{'Content-Type':'text/plain'}),
               Response(status=403),Response(status=206),Response(**{'Content-Type':'application/pdf'}),
               Response(**{'Content-Type':'text/html','Content-Encoding':'gzip'})]
        for response in cases:
            connection=Mock();connection.getresponse.return_value=response
            with self.subTest(status=response.status),patch.object(reading,'PublicHTTPS',return_value=connection):
                with self.assertRaises(reading.ReadingError):reading.fetch(URL,('text/plain','text/html'))
                connection.close.assert_called_once()

    def test_html_removes_scripts_navigation_and_hidden_text(self):
        raw=('<title>鲸类资料</title><nav>账户导航</nav><main><p>'+TEXT+'</p>'
             '<script>删除数据</script><p hidden>隐藏凭据</p><aside>广告</aside></main>').encode()
        result=reading.extract(URL,'text/html',raw)
        self.assertIn('鲸类通过声音',result['text'])
        for word in ['账户导航','删除数据','隐藏凭据','广告']:self.assertNotIn(word,result['text'])
        self.assertEqual(result['scope'],'extracted_text')
        self.assertEqual(result['title'],'鲸类资料')

    def test_login_empty_invalid_charset_and_challenges_do_not_become_reading(self):
        for body in ['<input type=password>'+TEXT,'<title>登录</title>'+TEXT,'<script>'+TEXT+'</script>','loading']:
            with self.assertRaises(reading.ReadingError):reading.extract(URL,'text/html',body.encode())
        with self.assertRaises(reading.ReadingError):reading.extract(URL,'text/plain; charset=utf-7',b'x'*200)

    def test_utf8_excerpt_and_truncation_are_explicit(self):
        result=reading.extract(URL,'text/plain',(TEXT*100).encode())
        self.assertLessEqual(len(result['text'].encode()),reading.MAX_TEXT)
        self.assertTrue(result['truncated']);self.assertEqual(result['material'],'web_text')
        gbk=reading.extract(URL,'text/html',('<meta charset=gbk><article>'+TEXT+'</article>').encode('gbk'))
        self.assertIn('鲸类',gbk['text'])

    def test_search_results_are_links_not_completed_reading(self):
        xml='<rss><channel><item><title>鲸类资料</title><link>'+URL+'</link><description>搜索摘要</description></item><item><link>http://example.org/</link></item></channel></rss>'
        with patch.object(reading,'fetch',return_value=(URL,'text/xml',xml.encode())):
            results=reading.search_pages('鲸类交流')
        self.assertEqual(len(results),1);self.assertEqual(results[0]['scope'],'search_result_only')
        self.assertFalse(self.reader.path.exists())

    def test_invalid_queries_and_entity_xml_do_not_execute(self):
        for query in ['x','账号123456','密码是什么','https://example.org/']:
            with self.assertRaises(reading.ReadingError):reading.search_pages(query)
        with patch.object(reading,'fetch',return_value=(URL,'text/xml',b'<!DOCTYPE rss><rss/>')):
            with self.assertRaises(reading.ReadingError):reading.search_pages('鲸类交流')

    def test_daily_reservation_is_persistent_and_independent_of_requested_reading(self):
        self.reader.reserve();self.reader.reserve()
        restored=reading.PublicReading(self.tmp.name,self.config)
        self.assertFalse(restored.available())
        with self.assertRaises(reading.ReadingError):restored.reserve()
        restored.reserve(background=False)
        self.assertEqual(restored.state['used'],2);self.assertEqual(restored.state['requested'],1)
        with patch('time.time',return_value=NOW+86400):
            self.assertTrue(restored.available());restored.reserve();self.assertEqual(restored.state['used'],1)

    def test_same_material_cannot_create_fresh_learning_even_under_another_url(self):
        row=self.reader.receipt(article(),'读到鲸类声音研究')
        other={**article(),'url':'https://example.net/copied-article'}
        self.assertIsNone(self.reader.receipt(other,'换个说法再次学会了'))
        self.assertEqual(len(self.reader.state['receipts']),1)
        self.assertEqual(reading.web_source(row),row)

    def test_web_source_continues_existing_phone_goal_and_survives_restart(self):
        book=Exploration(self.tmp.name)
        phone={'id':'phone:one','timestamp':NOW-100,'app':'酷安','note':'公开电池讨论','views':1,'public_content':True}
        book.observe(phone,{'question':'怎样区分鲸类声音','progress':'需要查证'})
        identifier=book.rows[0]['id'];source=self.reader.receipt(article(),'取得一份公开资料')
        self.assertTrue(book.observe(source,{'id':identifier,'progress':'网页提到一个新问题'},selected=identifier))
        restored=Exploration(self.tmp.name)
        self.assertTrue(restored.healthy);self.assertEqual(len(restored.rows),1)
        self.assertEqual([s['id'] for s in restored.rows[0]['sources']],[phone['id'],source['id']])
        self.assertEqual(restored.candidates(['酷安']),[])
        goals=restored.candidates(['酷安','公开网页'])
        self.assertIn(identifier,GoalLedger(self.tmp.name,'owner').offers(goals,NOW))

    def test_corrupt_reading_receipts_are_preserved_and_reader_stops(self):
        self.reader.path.write_text('{broken')
        bad=reading.PublicReading(self.tmp.name,self.config)
        self.assertFalse(bad.available());self.assertEqual(bad.path.read_text(),'{broken')
        self.reader.path.write_text(json.dumps({'version':1,'day':'','used':0,'requested':0,'receipts':[None]}))
        self.assertFalse(reading.PublicReading(self.tmp.name,self.config).healthy)


class ReadingFlowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        from test_rhythm import RhythmFlowTests, NOW as FLOW_NOW
        self.harness=RhythmFlowTests();self.harness.setUp()
        self.addCleanup(self.harness.doCleanups)
        self.a=self.harness.a;self.phone=self.harness.phone;self.config=self.harness.config
        self.now=FLOW_NOW
        self.config.update(public_reading_enabled=True,public_reading_daily_limit=2)
        self.phone.ready=Mock(side_effect=RuntimeError('phone unavailable'))
        self.harness.model.return_value=json.dumps({'activity':'read','reading_id':'topic:animals'})
        self.a.reading_model=AsyncMock(return_value=json.dumps({'useful':True,'note':'读到了动物交流资料',
            'exploration':{'question':'鲸类怎样交流','progress':'文章提出一项解释，尚未核实','next_step':'寻找不同资料'}}))
        self.search=patch.object(reading,'search_pages',return_value=[{'url':URL}]).start()
        self.fetch=patch.object(reading,'read_page',side_effect=lambda url:article()).start()
        self.addCleanup(patch.stopall)

    async def test_offline_phone_can_read_and_create_source_bound_goal(self):
        self.phone.status.side_effect=RuntimeError('offline')
        await self.a.consider()
        self.assertEqual(self.a.state['last_result'],'web_read')
        self.assertEqual(self.a.state['used'],0);self.assertEqual(self.a.reading.state['used'],1)
        self.phone.status.assert_not_called();self.phone.key.assert_not_called();self.phone.screenshot.assert_not_called();self.phone.ready.assert_not_called()
        self.assertEqual(self.harness.model.await_count,1);self.assertEqual(self.a.reading_model.await_count,1)
        self.assertEqual(self.a.exploration.rows[0]['sources'][0]['url'],URL)

    async def test_last_daily_slot_can_complete(self):
        from datetime import datetime
        self.a.reading.state.update(day=datetime.fromtimestamp(self.now,reading.TZ).date().isoformat(),used=1)
        await self.a.consider()
        self.assertEqual(self.a.state['last_result'],'web_read');self.assertEqual(self.a.reading.state['used'],2)

    async def test_search_failure_no_results_and_irrelevance_do_not_create_goals(self):
        self.search.side_effect=reading.ReadingError('unavailable')
        await self.a.consider();self.assertEqual(self.a.exploration.rows,[])
        self.assertEqual(self.a.state['last_result'],'web_unavailable')
        self.a.reading_model.assert_not_called()

    async def test_irrelevant_material_is_not_learning(self):
        self.a.reading_model.return_value='{"useful":false}'
        await self.a.consider()
        self.assertEqual(self.a.state['last_result'],'web_no_progress')
        self.assertEqual(self.a.exploration.rows,[]);self.assertEqual(self.a.reading.recent(),[])

    async def test_unknown_reading_id_cannot_export_arbitrary_query(self):
        self.harness.model.return_value=json.dumps({'activity':'read','reading_id':'unknown','query':'private message'})
        await self.a.consider();self.search.assert_not_called();self.assertEqual(self.a.reading.state['used'],0)

    async def test_query_is_program_bound_not_model_supplied(self):
        self.harness.model.return_value=json.dumps({'activity':'read','reading_id':'topic:animals','query':'private message'})
        await self.a.consider();self.search.assert_called_once_with('动物行为')

    async def test_new_message_during_fetch_stops_before_analysis_or_progress(self):
        def interrupted(url):
            self.a.state['last_user_at']=self.now
            return article()
        self.fetch.side_effect=interrupted
        with self.assertRaises(asyncio.CancelledError):await self.a.consider()
        self.a.reading_model.assert_not_called();self.assertEqual(self.a.exploration.rows,[])
        self.assertEqual(self.a.reading.state['last_result'],'interrupted')

    async def test_sleep_or_revoke_during_analysis_cannot_commit(self):
        async def model(*args):
            self.a.rhythm.state.update(mode='asleep',until=self.now+1000)
            return '{"useful":true,"note":"不能写进度","exploration":{"question":"伪进度"}}'
        self.a.reading_model.side_effect=model
        await self.a.consider()
        self.assertEqual(self.a.exploration.rows,[]);self.assertEqual(self.a.reading.recent(),[])
        self.assertEqual(self.a.state['last_result'],'web_limited')

    async def test_same_existing_problem_can_continue_via_web(self):
        source={'id':'phone:prior','timestamp':self.now-60,'app':'酷安','note':'公开鲸类讨论','views':1,'public_content':True}
        self.a.exploration.observe(source,{'question':'鲸类怎样交流','progress':'还需要找资料'})
        identifier=self.a.exploration.rows[0]['id']
        self.harness.model.return_value=json.dumps({'activity':'read','reading_id':identifier})
        await self.a.consider()
        self.assertEqual(len(self.a.exploration.rows),1);self.assertEqual(self.a.exploration.rows[0]['id'],identifier)
        self.assertEqual(len(self.a.exploration.rows[0]['sources']),2)

    async def test_next_step_refines_search_instead_of_repeating_original_question(self):
        source={'id':'phone:prior','timestamp':self.now-60,'app':'酷安','note':'公开鲸类讨论','views':1,'public_content':True}
        self.a.exploration.observe(source,{'question':'鲸类怎样交流','progress':'还需要查证','next_step':'群体差异与声音学习'})
        identifier=self.a.exploration.rows[0]['id']
        self.harness.model.return_value=json.dumps({'activity':'read','reading_id':identifier})
        await self.a.consider()
        self.search.assert_called_once_with('鲸类怎样交流 群体差异与声音学习')

    async def test_paused_asleep_and_disabled_reader_do_not_search(self):
        self.a.state['paused']=True;await self.a.consider()
        self.a.state['paused']=False;self.a.rhythm.state.update(mode='asleep',until=self.now+1000);await self.a.consider()
        self.search.assert_not_called();self.harness.model.assert_not_called()

    def tool_plugin(self):
        from astrbot_plugin_companion_phone.main import CompanionPhone
        plugin=CompanionPhone.__new__(CompanionPhone);plugin.autonomy=self.a;plugin.phone=self.phone
        plugin.config=self.config;plugin.owner='owner';plugin.umo='test:FriendMessage:owner'
        extra={}
        event=SimpleNamespace(unified_msg_origin=plugin.umo,get_sender_id=lambda:'owner',
            get_extra=lambda k,d=None:extra.get(k,d),set_extra=lambda k,v:extra.__setitem__(k,v))
        return plugin,event

    async def test_explicit_read_tool_does_not_create_own_interest_or_use_phone_budget(self):
        plugin,event=self.tool_plugin()
        response=await plugin.tool_public_read(event,'read',url=URL)
        self.assertFalse(response.isError)
        self.assertEqual(self.a.exploration.rows,[]);self.assertEqual(self.a.reading.state['used'],0)
        self.assertEqual(self.a.reading.state['requested'],1);self.phone.ready.assert_not_called()

    async def test_tool_rejects_other_contact_and_caps_one_request(self):
        plugin,event=self.tool_plugin()
        event.unified_msg_origin='test:FriendMessage:other'
        self.assertTrue((await plugin.tool_public_read(event,'read',url=URL)).isError)
        self.fetch.assert_not_called()
        event.unified_msg_origin=plugin.umo
        for _ in range(3):self.assertFalse((await plugin.tool_public_read(event,'read',url=URL)).isError)
        self.assertTrue((await plugin.tool_public_read(event,'read',url=URL)).isError)
        self.assertEqual(self.fetch.call_count,3)


if __name__=='__main__':unittest.main()
