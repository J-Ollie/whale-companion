"""Practice protocol, persistence and thinking integration with fake execution."""
import asyncio
import hashlib
import json
from pathlib import Path
import unittest
from unittest.mock import AsyncMock,patch

import test_thinking as fixture
import test_thinking_memory as private_fixture
from test_thinking import NOW,SCOPE
from astrbot_plugin_companion_phone.phone import PhoneError
from astrbot_plugin_companion_phone.thinking import ThoughtStore


class PracticeTests(unittest.IsolatedAsyncioTestCase):
    responses=fixture.ThinkingTests.responses
    thought=fixture.ThinkingTests.thought
    decision=fixture.ThinkingTests.decision

    def setUp(self):
        fixture.ThinkingTests.setUp(self)
        self.config.update(self_directed_enabled=True,practice_enabled=True)
        self.t.practice.socket.touch()
        self.t.practice.exchange=AsyncMock(side_effect=self.reply)

    async def reply(self,identifier,code):
        return {'id':identifier,'code_hash':hashlib.sha256(code.encode()).hexdigest(),'status':'succeeded',
                'stdout':'2\n','stderr':'','exit_code':0,'plots':[]}

    async def test_run_reserves_before_execution_and_saves_code_as_work(self):
        async def reply(identifier,code):
            self.assertEqual(self.t.practice.status()['used'],1)
            self.assertEqual(self.t.workspace.list()[0]['revision'],1)
            return await self.reply(identifier,code)
        self.t.practice.exchange.side_effect=reply
        result=await self.t.practice.request({'code':'print(1+1)','title':'加法'})
        self.assertEqual(result['status'],'succeeded')
        self.assertEqual(result['stdout'],'2\n')
        self.assertEqual(result['kind'],'program_execution_not_fact_verification')
        self.assertFalse(self.t.store.rows('receipts'))
        self.assertFalse(self.t.store.rows('trials'))

    async def test_code_fix_continues_from_real_error_in_same_budget(self):
        self.responses(self.thought(),self.decision(activity='practice',practice={'code':'print(missing)','title':'初次尝试'},**{'continue':True}),
                       self.decision(activity='practice',practice={'code':'print(1+1)','title':'修改尝试'}))
        async def reply(identifier,code):
            out=await self.reply(identifier,code)
            if 'missing' in code:out.update(status='execution_error',stdout='',stderr='NameError: missing',exit_code=1)
            return out
        self.t.practice.exchange.side_effect=reply
        await self.a.consider()
        self.assertEqual(self.t.practice.exchange.await_count,2)
        self.assertEqual(self.context.llm_generate.await_count,3)
        self.assertIn('NameError',self.context.llm_generate.call_args.kwargs['prompt'])
        self.assertEqual(self.t.resources.status()['used'],3)
        self.assertEqual(self.t.practice.status()['used'],2)

    async def test_daily_limit_is_independent_and_failed_runs_are_not_refunded(self):
        self.config['practice_daily_limit']=1
        self.t.practice.exchange.side_effect=OSError('offline')
        result=await self.t.practice.request({'code':'print(1)'})
        self.assertEqual(result['status'],'interrupted_unknown')
        with self.assertRaises(PhoneError):await self.t.practice.request({'code':'print(2)'})
        self.assertEqual(self.t.practice.exchange.await_count,1)
        self.t.store=ThoughtStore(self.root,SCOPE)
        self.assertEqual(self.t.practice.status()['used'],1)

    async def test_restart_does_not_replay_reserved_execution(self):
        with self.t.store.connect() as db:
            db.execute('INSERT INTO autonomy_practice_runs VALUES(?,?,?,?,?,?,?,?,?,?)',
                       (SCOPE,'a'*32,'run',NOW,'中断','print(1)','hash','reserved',None,''))
        self.t.store=ThoughtStore(self.root,SCOPE)
        with self.t.store.connect() as db:
            self.assertEqual(db.execute('SELECT status FROM autonomy_practice_runs').fetchone()[0],'interrupted_unknown')
        self.t.practice.exchange.assert_not_called()

    async def test_unknown_result_does_not_continue_automatically(self):
        self.t.practice.exchange.side_effect=OSError('offline')
        self.responses(self.thought(),self.decision(activity='practice',practice={'code':'print(1)'},**{'continue':True}))
        await self.a.consider()
        self.assertEqual(self.context.llm_generate.await_count,2)
        self.assertEqual(self.a.state['last_result'],'practice_unavailable')

    async def test_interruption_is_saved_as_unknown_and_propagated(self):
        self.t.practice.exchange.side_effect=asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):await self.t.practice.request({'code':'print(1)'})
        with self.t.store.connect() as db:
            self.assertEqual(db.execute('SELECT status FROM autonomy_practice_runs').fetchone()[0],'interrupted_unknown')

    async def test_old_code_revision_cannot_execute(self):
        first=await self.t.practice.request({'code':'print(1)'})
        with self.assertRaises(ValueError):
            await self.t.practice.request({'code':'print(2)','work_id':first['work_id'],'revision':0})
        self.assertEqual(self.t.practice.exchange.await_count,1)

    async def test_rerun_can_update_existing_work_with_revision(self):
        first=await self.t.practice.request({'code':'print(1)'})
        second=await self.t.practice.request({'code':'print(2)','work_id':first['work_id'],'revision':1})
        self.assertEqual(second['work_id'],first['work_id'])
        self.assertEqual(second['revision'],2)

    async def test_cross_scope_result_cannot_be_read(self):
        result=await self.t.practice.request({'code':'print(1)'})
        with self.t.store.connect() as db:db.execute('UPDATE autonomy_practice_runs SET scope=?',('other',))
        with self.assertRaises(ValueError):await self.t.practice.request({'action':'read','id':result['id']})

    async def test_disabled_sleep_and_private_branch_block_execution(self):
        self.config['practice_enabled']=False
        with self.assertRaises(PhoneError):await self.t.practice.request({'code':'print(1)'})
        self.config['practice_enabled']=True;self.t.private_run=True
        with self.assertRaises(PhoneError):await self.t.practice.request({'code':'print(1)'})
        self.t.private_run=False;self.a.rhythm.state.update(mode='resting',until=NOW+600)
        with self.assertRaises(PhoneError):await self.t.practice.request({'code':'print(1)'})
        self.t.practice.exchange.assert_not_called()

    async def test_code_cannot_choose_image_path_or_network_options(self):
        for extra in [{'image':'evil'},{'socket':'/var/run/docker.sock'},{'network':True},{'code':'x'*6001}]:
            with self.assertRaises(ValueError):await self.t.practice.request({'code':'print(1)',**extra})
        self.t.practice.exchange.assert_not_called()

    async def test_bad_plot_numbers_or_svg_injection_are_not_rendered(self):
        for plot in [{'x':[1,2],'y':[1,float('nan')]},{'x':[1,2],'y':[1,2],'title':{'svg':'bad'}}]:
            with self.assertRaises(ValueError):self.t.practice.render('b'*32,[plot])

    async def test_actual_plot_is_saved_without_publishing(self):
        from PIL import Image
        artifacts=self.t.practice.render('c'*32,[{'x':[0,1,2],'y':[0,1,4],'title':'A plot'}])
        target=self.root/artifacts[0]['relative_path']
        with Image.open(target) as image:self.assertEqual(image.size,(720,420))
        self.a.phone.key.assert_not_called()

    async def test_long_console_is_bounded_in_feedback_and_readback(self):
        async def reply(identifier,code):
            out=await self.reply(identifier,code);out['stdout']='x'*6000;return out
        self.t.practice.exchange.side_effect=reply
        result=await self.t.practice.request({'code':'print(1)'})
        self.assertEqual(len(result['stdout']),700)
        read=await self.t.practice.request({'action':'read','id':result['id']})
        self.assertEqual(len(read['result']['stdout']),700)


class PrivatePracticeTests(unittest.IsolatedAsyncioTestCase):
    responses=fixture.ThinkingTests.responses
    thought=fixture.ThinkingTests.thought
    decision=fixture.ThinkingTests.decision
    ask=private_fixture.MemoryThinkingTests.ask
    private_note=private_fixture.MemoryThinkingTests.private_note

    def setUp(self):
        private_fixture.MemoryThinkingTests.setUp(self)
        self.config.update(self_directed_enabled=True,practice_enabled=True)

    async def test_private_recollection_cannot_select_practice(self):
        self.t.practice.exchange=AsyncMock()
        self.responses(self.ask(),self.private_note(),{'activity':'practice','practice':{'code':'print(1)'}})
        await self.a.consider()
        self.t.practice.exchange.assert_not_called()
        self.assertEqual(self.t.store.rows('runs')[0]['status'],'invalid_output')


if __name__=='__main__':unittest.main()
