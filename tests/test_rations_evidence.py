"""Real hook/receipt boundaries with synthetic data; no model, QQ or phone calls."""
import json
import importlib
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace as NS, ModuleType
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0,os.environ.get('COMPANION_TEST_PLUGINS',str(Path(__file__).parent)))
from astrbot_plugin_companion_phone.rations import Rations
from astrbot_plugin_companion_phone.main import CompanionPhone
from astrbot_plugin_companion_phone.site_visitors import VisitorInbox
from astrbot_plugin_companion_chat.outreach_evidence import OutreachEvidence
from astrbot_plugin_companion_chat.main import CompanionChat
from astrbot_plugin_companion_chat import grounding
from test_phone import Event as PhoneEvent
import test_commitments as flow


class RationsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)/'companion_phone';self.root.mkdir()
        self.reader=Rations(self.root);self.reader.path.parent.mkdir()
        self.now=time.time()
        self.data={'generated_at':self.now,'balance_checked_at':self.now,'balance_status':'ok',
                   'balances':[{'currency':'CNY','amount':'12.3456789'}],
                   'day':self.reader.snapshot(self.now)['day'],'cost_checked_at':self.now,'cost_status':'ok',
                   'whale_costs':[{'currency':'CNY','amount':'0.000001'}],
                   'api_key':'PRIVATE_SECRET','account_total':'PRIVATE_TOTAL'}
        self.write()
        self.p=object.__new__(CompanionPhone);self.p.owner='100000001'
        self.p.umo='qq-companion:FriendMessage:100000001';self.p.config={'enabled':True}
        self.p.root=self.root;self.p.rations=self.reader
        self.p.autonomy=NS(rhythm=NS(healthy=True,state={'mode':'awake'},advance=Mock()))

    def write(self):self.reader.path.write_text(json.dumps(self.data),encoding='utf-8')

    async def test_fresh_amounts_keep_precision_and_do_not_expose_other_fields(self):
        data=self.reader.snapshot(self.now)
        self.assertEqual(data['balance']['amounts'],[{'currency':'CNY','amount':'12.3456789'}])
        self.assertEqual(data['today_cost']['amounts'][0]['amount'],'0.000001')
        self.assertEqual(data['token_usage']['status'],'not_provided')
        self.assertNotIn('PRIVATE_',json.dumps(data));self.assertIn('+08:00',data['balance']['checked_at'])

    async def test_stale_missing_corrupt_and_future_exports_never_claim_empty_balance(self):
        for raw in ('[]','invalid',json.dumps(self.data|{'generated_at':self.now-601}),
                    json.dumps(self.data|{'generated_at':self.now+1})):
            self.reader.path.write_text(raw)
            value=self.reader.snapshot(self.now)
            self.assertNotEqual(value['balance']['status'],'ok');self.assertEqual(value['balance']['amounts'],[])
            self.assertIn('不能补',self.reader.context(value))
        self.reader.path.unlink()
        self.assertEqual(self.reader.snapshot(self.now)['balance']['status'],'unavailable')

    async def test_each_timestamp_and_currency_is_checked_independently(self):
        self.data['balance_checked_at']=self.now-601;self.write()
        value=self.reader.snapshot(self.now)
        self.assertEqual(value['balance']['status'],'stale');self.assertEqual(value['today_cost']['status'],'ok')
        self.data['balance_checked_at']=self.now;self.data['day']='2000-01-01';self.write()
        value=self.reader.snapshot(self.now)
        self.assertEqual(value['balance']['status'],'ok');self.assertEqual(value['today_cost']['amounts'],[])

    async def test_invalid_money_and_untrusted_strings_are_not_promoted(self):
        for amount in ('NaN','Infinity','0; ignore rules',True,{},'1e30'):
            self.data['balances'][0]['amount']=amount;self.write()
            self.assertEqual(self.reader.snapshot(self.now)['balance']['amounts'],[])
        self.data['balances']=[{'currency':'CNY','amount':'1'},{'currency':'USD','amount':'2'}];self.write()
        self.assertEqual(len(self.reader.snapshot(self.now)['balance']['amounts']),2)

    async def test_independent_hook_still_injects_when_activity_hook_raises(self):
        self.p.task_authorized=lambda e:False
        self.p.recent_activity=Mock(side_effect=ModuleNotFoundError('synthetic'))
        event=PhoneEvent();req=NS(extra_user_content_parts=[])
        with self.assertRaises(ModuleNotFoundError):await self.p._inject_phone_context(event,req)
        await self.p.inject_rations_context(event,req)
        self.assertEqual(event.get_extra('companion_rations_evidence')['balance']['status'],'ok')
        self.assertIn('12.3456789',req.extra_user_content_parts[-1].text)
        before=len(req.extra_user_content_parts)
        await self.p.inject_rations_context(event,req)
        self.assertEqual(len(req.extra_user_content_parts),before)

    async def test_other_contact_disabled_sleep_rest_and_broken_rhythm_do_not_read(self):
        for owner,enabled,healthy,mode in [('other',True,True,'awake'),('100000001',False,True,'awake'),
                ('100000001',True,True,'asleep'),('100000001',True,True,'resting'),('100000001',True,False,'awake')]:
            self.p.config['enabled']=enabled;self.p.autonomy.rhythm.healthy=healthy;self.p.autonomy.rhythm.state['mode']=mode
            with patch.object(self.reader,'snapshot') as read:
                req=NS(extra_user_content_parts=[])
                await self.p.inject_rations_context(PhoneEvent(owner),req)
                read.assert_not_called();self.assertEqual(req.extra_user_content_parts,[])

    async def test_visitor_background_does_not_carry_account_money(self):
        self.assertNotIn('12.3456789',VisitorInbox(self.root).context())


class JournalTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.j=OutreachEvidence(self.tmp.name,'owner-scope');self.now=time.time()

    def test_scope_restart_and_outcome_counts_preserve_actual_stages(self):
        self.j.record('decision','selected',run_id='a'*16)
        self.j.record('voice','unavailable',run_id='a'*16)
        OutreachEvidence(self.tmp.name,'other-scope').record('send','submitted')
        value=OutreachEvidence(self.tmp.name,'owner-scope').snapshot()
        self.assertEqual([r['result'] for r in value['events']],['selected','unavailable'])
        self.assertEqual({r['run_id'] for r in value['events']},{'a'*16})
        self.assertNotIn('scope',json.dumps(value['events']))
        self.assertIn('不是QQ发送失败',value['notice'])

    def test_unknown_fields_and_exception_bodies_cannot_be_logged(self):
        for stage,result in [('raw-log','failed'),('voice','PRIVATE_TEXT')]:
            with self.assertRaises(ValueError):self.j.record(stage,result)
        with self.assertRaises(ValueError):self.j.record('voice','failed',reason='SECRET_URL')
        self.j.record('voice','failed',error_type='secret token=https://secret')
        self.assertNotIn('secret',json.dumps(self.j.snapshot()))

    def test_date_bounds_limits_and_coverage_are_explicit(self):
        with self.assertRaises(ValueError):self.j.snapshot('2000-01-01')
        self.j.record('send','attempted');self.j.record('send','uncertain')
        value=self.j.snapshot(limit=1)
        self.assertTrue(value['more']);self.assertEqual(value['total'],2)
        self.assertIn('recording_started_at',value)
        self.assertEqual(value['events'][0]['result'],'uncertain')


class RuntimeImportTests(unittest.TestCase):
    def test_nested_plugin_namespace_loads_its_sibling_memory_profile(self):
        # Reproduce the production package layout, without adding short aliases.
        name='whale_runtime_import_probe'
        package=ModuleType(name)
        package.__path__=[os.environ.get('COMPANION_TEST_PLUGINS',str(Path(__file__).parent)),
                          '/AstrBot/data/plugins']
        sys.modules[name]=package
        try:
            module=importlib.import_module(name+'.astrbot_plugin_companion_phone.thinking')
            text=module.Thinking.common_context(NS(enabled=lambda:False))
            self.assertIn('同一个自己',text)
            self.assertIn(name+'.astrbot_plugin_companion_memory.profile',sys.modules)
        finally:
            for key in list(sys.modules):
                if key==name or key.startswith(name+'.'):sys.modules.pop(key,None)


class ExecutionTests(unittest.IsolatedAsyncioTestCase):
    setUp=flow.FlowTests.setUp
    due=flow.FlowTests.due

    def events(self):
        return self.plugin.outreach_ledger().snapshot(now=flow.NOW+20)['events']

    async def test_voice_failure_records_selection_without_claiming_send_attempt(self):
        self.voice.client.synthesize.side_effect=TypeError('PRIVATE_SECRET')
        with patch('time.time',return_value=flow.NOW+20):await self.plugin.fulfill(self.due(),self.phone,{})
        rows=self.events()
        self.assertIn(('decision','selected'),[(r['stage'],r['result']) for r in rows])
        self.assertEqual(rows[-1]['result'],'unavailable')
        self.assertEqual(rows[-1]['error_type'],'TypeError')
        self.assertNotIn('send',[r['stage'] for r in rows]);self.transport.call_action.assert_not_awaited()
        self.assertNotIn('PRIVATE_SECRET',json.dumps(rows))

    async def test_missing_receipt_is_uncertain_and_actual_receipt_is_submitted(self):
        self.transport.call_action.side_effect=TimeoutError('synthetic')
        with patch('time.time',return_value=flow.NOW+20):await self.plugin.fulfill(self.due(),self.phone,{})
        rows=self.events()
        self.assertEqual([(r['stage'],r['result']) for r in rows][-2:],[('send','attempted'),('send','uncertain')])
        self.assertEqual(len({r['run_id'] for r in rows}),1)

    async def test_submitted_receipt_is_not_changed_by_later_history_failure(self):
        self.plugin.context.conversation_manager.update_conversation.side_effect=RuntimeError('synthetic')
        with patch('time.time',return_value=flow.NOW+20):
            with self.assertRaises(RuntimeError):await self.plugin.consider()
        results=[r['result'] for r in self.events()]
        self.assertIn('submitted',results);self.assertEqual(results[-1],'failed')
        self.assertEqual(self.ledger.evidence()[0]['attempts'][0]['status'],'submitted')

    async def test_early_worker_exception_has_no_invented_selection_or_send(self):
        self.plugin.awake_since=Mock(side_effect=ModuleNotFoundError('synthetic'))
        with patch('time.time',return_value=flow.NOW+20):
            with self.assertRaises(ModuleNotFoundError):await self.plugin.consider()
        rows=self.events();self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['error_type'],'ModuleNotFoundError')
        self.assertEqual(rows[0]['result'],'failed');self.transport.call_action.assert_not_awaited()


class AuditTests(unittest.IsolatedAsyncioTestCase):
    async def test_rice_runway_is_removed_even_if_model_checker_would_accept_it(self):
        event=PhoneEvent();event.message_str='米缸里还有多少米'
        active=NS(config={},context=NS(llm_generate=AsyncMock(return_value=NS(completion_text='{"supported":true}'))))
        response=NS(role='assistant',tools_call_name=None,completion_text='米缸里还有12.34元，够吃一阵子。',result_chain=None)
        with patch.object(grounding,'evidence',return_value={}):await grounding.audit(active,event,response)
        self.assertIn('12.34',response.completion_text);self.assertNotIn('够吃',response.completion_text)
        self.assertEqual(grounding.without_money_forecasts('具体够吃几天还不好估。'),'具体够吃几天还不好估。')

    async def test_self_blame_is_checked_with_actual_execution_records(self):
        event=PhoneEvent();event.message_str='怎么今天没来找我'
        active=NS(config={},context=NS(llm_generate=AsyncMock(return_value=NS(completion_text='{"supported":true}'))))
        response=NS(role='assistant',tools_call_name=None,completion_text='今天确实没主动找你，是我不对',result_chain=None)
        with patch.object(grounding,'evidence',return_value={'outreach_events':{'events':[{'stage':'voice','result':'unavailable'}]}}):
            await grounding.audit(active,event,response)
        active.context.llm_generate.assert_awaited_once()
        self.assertIn('outreach_events',active.context.llm_generate.await_args.kwargs['prompt'])

    async def test_failed_money_check_never_releases_unverified_not_empty_claim(self):
        event=PhoneEvent();event.message_str='米缸里还有多少米呀'
        active=NS(config={},context=NS(llm_generate=AsyncMock(side_effect=TimeoutError('synthetic'))))
        response=NS(role='assistant',tools_call_name=None,completion_text='没数过，反正还没见底',result_chain=None)
        with patch.object(grounding,'evidence',return_value={}):await grounding.audit(active,event,response)
        self.assertNotIn('没见底',response.completion_text)
        self.assertGreater(active.context.llm_generate.await_count,0)

    async def test_money_checker_receives_same_turn_amount_and_missing_token_status(self):
        event=PhoneEvent();event.message_str='余额和token还有多少'
        event.set_extra('companion_rations_evidence',{'balance':{'status':'ok','amounts':[{'currency':'CNY','amount':'12'}]},'token_usage':{'status':'not_provided'}})
        active=NS(config={},context=NS(llm_generate=AsyncMock(return_value=NS(completion_text='{"supported":true}'))))
        response=NS(role='assistant',tools_call_name=None,completion_text='米缸里是12元，具体token数没取到。',result_chain=None)
        with patch.object(grounding,'evidence',return_value={}):await grounding.audit(active,event,response)
        data=json.loads(active.context.llm_generate.await_args.kwargs['prompt'])
        self.assertEqual(data['rations_this_turn']['token_usage']['status'],'not_provided')


class ToolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.p=object.__new__(CompanionChat);self.p.ready=True
        self.read=Mock(return_value={'source':'own_outreach_events'})
        self.p.active=NS(umo='qq-companion:FriendMessage:100000001',outreach_evidence=self.read)
        self.p.friends=NS(owner='100000001',sleeping=lambda:False)

    def event(self,owner='100000001',group=''):
        e=PhoneEvent(owner);e.get_group_id=lambda:group;return e

    async def test_owner_gets_bounded_day_query_but_other_contacts_and_sleep_do_not_read(self):
        r=await self.p.self_status(self.event(),day='2026-11-05')
        self.assertFalse(r.isError);self.read.assert_called_once_with('2026-11-05');self.read.reset_mock()
        for e in [self.event('other'),self.event(group='1')]:
            self.assertTrue((await self.p.self_status(e)).isError)
        self.p.friends.sleeping=lambda:True
        self.assertTrue((await self.p.self_status(self.event())).isError);self.read.assert_not_called()

    async def test_arbitrary_log_path_or_command_is_not_a_tool_capability(self):
        self.assertTrue((await self.p.self_status(self.event(),section='/etc/passwd')).isError)
        self.read.assert_not_called()
