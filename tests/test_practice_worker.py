"""Worker profile and protocol tests; no Docker commands in this suite."""
import hashlib
import asyncio
import importlib.util
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock,Mock,patch

spec=importlib.util.spec_from_file_location('whale_practice_worker',Path(__file__).parent/'practice-worker/worker.py')
worker=importlib.util.module_from_spec(spec);spec.loader.exec_module(worker)


class WorkerTests(unittest.TestCase):
    def test_profile_is_fixed_no_mounts_no_network_nonroot_and_bounded(self):
        command=worker.command('sha256:'+'a'*64,'b'*32)
        for flag in ['--network=none','--read-only','--cap-drop=ALL','--security-opt=no-new-privileges',
                     '--pids-limit=32','--memory=128m','--cpus=0.5','--user=65534:65534','--log-driver=none']:
            self.assertIn(flag,command)
        self.assertFalse(any(x in command for x in ['--volume','-v','--mount','--privileged','--device']))

    def test_bad_image_and_job_identifiers_are_rejected(self):
        for image,job in [('python:latest','a'*32),('sha256:'+'a'*64,'../other')]:
            with self.assertRaises(ValueError):worker.command(image,job)

    def test_request_does_not_accept_execution_flags(self):
        for data in [{'id':'a'*32,'code':'print(1)','image':'evil'}, {'id':'bad','code':'print(1)'},
                     {'id':'a'*32,'code':'x'*6001}]:
            with self.assertRaises(ValueError):worker.validate_request(data)

    def test_plot_protocol_rejects_nonfinite_points_and_limits_artifacts(self):
        good=worker.MARKER+'{"x":[0,1],"y":[0,1],"title":"test"}'
        bad=worker.MARKER+'{"x":[0,1],"y":[0,NaN]}'
        self.assertEqual(len(worker.plots_from_output('\n'.join([bad,good,good,good]))),2)

    def test_persisted_job_is_not_reexecuted_and_hash_conflicts_fail(self):
        with tempfile.TemporaryDirectory() as root:
            first=worker.Worker(root,'sha256:'+'a'*64)
            with first.db() as db:db.execute('INSERT INTO jobs VALUES(?,?,?,?,?)',('b'*32,'hash','running',None,1))
            restored=worker.Worker(root,'sha256:'+'a'*64)
            self.assertEqual(restored.result('b'*32,'hash')['status'],'interrupted_unknown')
            with self.assertRaises(ValueError):restored.result('b'*32,'other')


class WorkerDrainTests(unittest.IsolatedAsyncioTestCase):
    async def test_timeout_drains_pipes_instead_of_hanging_on_wait(self):
        with tempfile.TemporaryDirectory() as root:
            instance=worker.Worker(root,'sha256:'+'a'*64,timeout=1)
            instance.remove=AsyncMock()
            async def pending():await asyncio.Event().wait()
            proc=SimpleNamespace(stdin=SimpleNamespace(write=Mock(),drain=AsyncMock(),close=Mock()),
                                 stdout=SimpleNamespace(read=AsyncMock(return_value=b'')),
                                 stderr=SimpleNamespace(read=AsyncMock(return_value=b'')),returncode=None,
                                 wait=AsyncMock(side_effect=pending),kill=Mock(),communicate=AsyncMock(return_value=(b'',b'')))
            with patch.object(worker.asyncio,'create_subprocess_exec',AsyncMock(return_value=proc)):
                result=await asyncio.wait_for(instance.run({'id':'b'*32,'code':'print(1)'}),timeout=2)
            self.assertEqual(result['status'],'timeout')
            proc.communicate.assert_awaited_once()


if __name__=='__main__':unittest.main()
