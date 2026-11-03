from contextlib import ExitStack
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from src.vod_start_guard import start_guard
from src import config


class StartGuardTests(unittest.TestCase):
    def setUp(self):
        self.stack=ExitStack();self.addCleanup(self.stack.close)
        self.root=Path(self.stack.enter_context(tempfile.TemporaryDirectory(dir=Path(__file__).parent)))
        self.stack.enter_context(patch.object(Path,'home',return_value=self.root))
        for name in ('OUT_DIR','PUBLIC_HTML_DIR','SPOOL_DIR'):
            self.stack.enter_context(patch.object(config,name,str(self.root)))
        self.stack.enter_context(patch.dict(os.environ,{'TCD_MAINTENANCE_SOCKET':'/run/tdb-maintenance/control.sock'}))
        self.stack.enter_context(patch('src.vod_start_guard.shutil.disk_usage',return_value=SimpleNamespace(free=20*1024**3)))

    def configured(self):
        state=self.root/'tdb-daily-state';state.mkdir(mode=0o700)
        lock=self.root/'.local/state/tdb-deploy/deploy.lock';lock.parent.mkdir(parents=True)
        for p in (state/'offsite.lock',lock):p.touch(mode=0o600)
        return state

    def record(self,state,value):
        p=state/'hold.json';p.write_text(json.dumps(dict(format='tdb-daily-state-v1',value=value)))
        p.chmod(0o600)

    def test_existing_resource_floor_blocks_without_any_admin_files(self):
        with patch('src.vod_start_guard.shutil.disk_usage',return_value=SimpleNamespace(free=1)):
            with start_guard() as allowed:self.assertFalse(allowed)
        self.assertEqual([],list(self.root.iterdir()))

    def test_installed_administration_with_missing_state_fails_closed(self):
        admin=self.root/'tdb-admin';admin.mkdir()
        (admin/'daily-backup.json').write_text('{}')
        with start_guard() as allowed:self.assertFalse(allowed)

    @unittest.skipUnless(os.name=='posix','Existing Oracle administration uses POSIX leases')
    def test_manual_hold_malformed_state_and_live_leases_never_admit(self):
        import fcntl
        state=self.configured();self.record(state,False)
        before={p:p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        with start_guard() as allowed:self.assertTrue(allowed)
        self.assertEqual(before,{p:p.read_bytes() for p in before})
        self.record(state,True)
        with start_guard() as allowed:self.assertFalse(allowed)
        from src.vod_scheduler import Scheduler
        manager=Scheduler(SimpleNamespace(bot_data={}))
        job,error=manager.reserve(1,'123')
        self.assertIsNone(job);self.assertIn('обслуживание',error)
        self.assertEqual({},manager.jobs)
        self.record(state,False)
        with (state/'offsite.lock').open('r+b') as f:
            fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)
            with start_guard() as allowed:self.assertFalse(allowed)
        (state/'hold.json').write_text('{"format":"tdb-daily-state-v1","value":true,"value":false}')
        with start_guard() as allowed:self.assertFalse(allowed)

    @unittest.skipUnless(os.name=='posix','POSIX state file safety')
    def test_unknown_owner_and_symlink_hold_fail_closed(self):
        state=self.configured();self.record(state,False)
        owner=state/'owner.json';owner.write_text(json.dumps(dict(format='tdb-daily-state-v1',value={'unknown':True})));owner.chmod(0o600)
        with start_guard() as allowed:self.assertFalse(allowed)
        owner.unlink();(state/'hold.json').unlink()
        target=state/'other';target.write_text(json.dumps(dict(format='tdb-daily-state-v1',value=False)));target.chmod(0o600)
        (state/'hold.json').symlink_to(target)
        with start_guard() as allowed:self.assertFalse(allowed)
