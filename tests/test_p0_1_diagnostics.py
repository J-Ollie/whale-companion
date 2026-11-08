import asyncio
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from contextlib import closing
from unittest.mock import patch
from astrbot_plugin_companion_phone.executor import Executor, ExecutorError
from astrbot_plugin_companion_phone import diagnostics
from test_executor import heartbeat, APP

class DiagnosticsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup); self.root=Path(self.tmp.name)
        with closing(sqlite3.connect(self.root/'executor-jobs.sqlite3')) as db, db:
            db.execute('CREATE TABLE jobs(id TEXT PRIMARY KEY,op TEXT NOT NULL,args TEXT NOT NULL,created REAL NOT NULL,expires REAL NOT NULL,status TEXT NOT NULL,lease TEXT,boot TEXT,result TEXT)')
            db.execute("INSERT INTO jobs VALUES('old','tap','{}',0,1,'done',NULL,NULL,NULL)")
        self.ex=Executor(self.root,lambda:True,lambda:[APP]); self.ex.poll(heartbeat())
    def rows(self):
        with closing(self.ex.db()) as db:return [dict(x) for x in db.execute('SELECT * FROM phone_diag_events ORDER BY at')]
    async def job(self):
        t=asyncio.create_task(self.ex.rpc('tap',{'x':1})); await asyncio.sleep(0)
        j=self.ex.poll(heartbeat())['job']; return t,j
    def result(self,j,**result):
        return self.ex.result({'id':j['id'],'lease':j['lease'],'boot':heartbeat()['boot'],'result':result})
    async def test_upgrade_and_repeat_and_old_row(self):
        Executor(self.root,lambda:True,lambda:[APP])
        with closing(self.ex.db()) as db:
            self.assertEqual(db.execute('select count(*) from jobs where id="old"').fetchone()[0],1)
            self.assertEqual(db.execute('pragma user_version').fetchone()[0],0)
            self.assertEqual(len(db.execute('pragma table_info(jobs)').fetchall()),9)
    async def test_correlation_multiple_steps_and_no_thinking(self):
        with diagnostics.scope('trace') as trace:
            with diagnostics.scope('step') as step:
                t,j=await self.job();self.result(j,ok=True);await t
                t2,j2=await self.job();self.result(j2,ok=True);await t2
            with diagnostics.scope('step'):
                t3,j3=await self.job();self.result(j3,ok=True);await t3
        binds=[x for x in self.rows() if x['kind']=='job_bound']
        self.assertEqual(len(binds),3);self.assertTrue(all(x['trace_id']==trace['trace_id'] for x in binds))
        self.assertEqual(binds[0]['operation_id'],binds[1]['operation_id']);self.assertNotEqual(binds[1]['step_id'],binds[2]['step_id'])
        self.assertTrue(all(x['thinking_run_id'] is None for x in binds))
    async def test_late_duplicate_restart_and_uncertain(self):
        t,j=await self.job(); self.result(j,ok=False,uncertain=True); 
        with self.assertRaises(ExecutorError):await t
        self.result(j,ok=False,uncertain=True)
        restarted=Executor(self.root,lambda:True,lambda:[APP]);self.ex=restarted
        self.result(j,ok=True);self.assertTrue(self.result(j,ok=True)['duplicate'])
        rows=self.rows();self.assertEqual(sum(x['kind']=='job_bound' for x in rows),1)
        self.assertEqual(sum(x['kind']=='late_receipt' and json.loads(x['metadata'])['status']=='done' for x in rows),1)
        self.assertEqual(len({x['event_id'] for x in rows}),len(rows))
    async def test_failed_diagnostic_does_not_change_job_or_receipt(self):
        with closing(self.ex.db()) as db,db:
            db.execute("CREATE TRIGGER diag_fail BEFORE INSERT ON phone_diag_events BEGIN SELECT RAISE(ABORT,'synthetic'); END")
        t,j=await self.job();self.result(j,ok=True);self.assertTrue((await t)['ok'])
        with closing(self.ex.db()) as db:
            self.assertEqual(db.execute('select status from jobs where id=?',(j['id'],)).fetchone()[0],'done')
            self.assertEqual(db.execute('select count(*) from phone_diag_events').fetchone()[0],0)
    async def test_same_transaction_rollback_and_savepoint(self):
        with closing(self.ex.db()) as db:
            db.execute('BEGIN');db.execute("insert into jobs values('tx','tap','{}',0,1,'queued',NULL,NULL,NULL)")
            diagnostics.write(db,'bind:tx',diagnostics.correlation(),'tx','job_bound')
            db.rollback()
            self.assertEqual(db.execute("select count(*) from jobs where id='tx'").fetchone()[0],0)
            self.assertEqual(db.execute("select count(*) from phone_diag_events where job_id='tx'").fetchone()[0],0)
            db.execute('BEGIN');db.execute("insert into jobs values('tx2','tap','{}',0,1,'queued',NULL,NULL,NULL)")
            db.execute("CREATE TEMP TRIGGER diag_fail BEFORE INSERT ON phone_diag_events BEGIN SELECT RAISE(ABORT,'synthetic'); END")
            self.assertFalse(diagnostics.write(db,'bind:tx2',diagnostics.correlation(),'tx2','job_bound'));db.commit()
            self.assertEqual(db.execute("select count(*) from jobs where id='tx2'").fetchone()[0],1)
    async def test_cancelled_leased_late_and_restart(self):
        t,j=await self.job();t.cancel()
        with self.assertRaises(asyncio.CancelledError):await t
        self.ex=Executor(self.root,lambda:True,lambda:[APP]);self.result(j,ok=True)
        self.assertEqual([r['kind'] for r in self.rows()].count('late_receipt'),1)
    async def test_real_timeout_marks_uncertain_without_retry(self):
        async def timeout(awaitable, seconds):
            awaitable.close()
            raise asyncio.TimeoutError()
        with patch('astrbot_plugin_companion_phone.executor.asyncio.wait_for',side_effect=timeout):
            with self.assertRaisesRegex(ExecutorError,'result_uncertain_do_not_repeat'):
                await self.ex.rpc('tap',{'x':1})
        with closing(self.ex.db()) as db:
            self.assertEqual(db.execute("select count(*) from jobs where id!='old'").fetchone()[0],1)
            self.assertEqual(db.execute("select status from jobs where id!='old'").fetchone()[0],'cancelled')
    async def test_conflicting_existing_table_leaves_jobs_usable(self):
        with closing(self.ex.db()) as db,db:
            db.execute('DROP TABLE phone_diag_events')
            db.execute('CREATE TABLE phone_diag_events(incompatible TEXT)')
        self.ex=Executor(self.root,lambda:True,lambda:[APP]);self.ex.poll(heartbeat())
        t,j=await self.job();self.result(j,ok=True);self.assertTrue((await t)['ok'])
        with closing(self.ex.db()) as db:
            self.assertEqual(db.execute('select status from jobs where id=?',(j['id'],)).fetchone()[0],'done')

    async def test_full_sql_rollback_never_reports_success(self):
        t,j=await self.job()
        with closing(self.ex.db()) as db,db:
            db.execute("CREATE TRIGGER diag_rollback BEFORE INSERT ON phone_diag_events BEGIN SELECT RAISE(ROLLBACK,'synthetic'); END")
        with self.assertRaises(sqlite3.Error): self.result(j,ok=True)
        with closing(self.ex.db()) as db:
            self.assertEqual(db.execute('select status from jobs where id=?',(j['id'],)).fetchone()[0],'leased')
        with closing(self.ex.db()) as db,db: db.execute('DROP TRIGGER diag_rollback')
        t.cancel()
        with self.assertRaises(asyncio.CancelledError): await t

    async def test_capacity_preserves_existing_bindings_and_business(self):
        with patch.object(diagnostics,'MAX_EVENTS',1):
            t,j=await self.job();self.result(j,ok=True);await t
            self.assertEqual(len(self.rows()),1)
            self.assertEqual(diagnostics.health(self.ex.path)['status'],'degraded')
        self.assertEqual(diagnostics.health(self.ex.path)['unlinked_jobs'],1)

    async def test_no_secrets_in_diagnostics(self):
        t=asyncio.create_task(self.ex.rpc('input',{'text':'SECRET-SYNTHETIC'}));await asyncio.sleep(0)
        j=self.ex.poll(heartbeat())['job'];self.result(j,ok=True,private='SECRET-SYNTHETIC');await t
        self.assertNotIn('SECRET-SYNTHETIC',json.dumps(self.rows()))

if __name__=='__main__': unittest.main()
