"""Daily orchestration with a fake clock, fake service and synthetic files only."""
from contextlib import contextmanager
import configparser
from datetime import datetime, timedelta, timezone
import json
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from tools import daily_backup as daily


class FakeGate:
    def __init__(self, runtime, expected_pid):
        self.runtime = runtime
        self.expected_pid = int(expected_pid)
        self.token = None

    def response(self, status):
        return dict(version=1, status=status, pid=int(self.runtime.properties['MainPID']),
                    running=self.runtime.application_running and self.runtime.properties['ActiveState']=='active',
                    committed=self.runtime.committed, token=self.runtime.token)

    def prepare(self, token):
        self.runtime.events.append('prepare')
        if not self.runtime.application_running:
            return self.response('unavailable')
        if self.expected_pid != int(self.runtime.properties['MainPID']):
            raise daily.DailyError('peer_changed')
        if self.runtime.busy_until and self.runtime.clock < self.runtime.busy_until:
            return self.response('busy')
        if self.runtime.busy_attempts:
            self.runtime.busy_attempts -= 1
            return self.response('busy')
        if self.runtime.token is not None:
            return self.response('busy')
        self.token = self.runtime.token = token
        return self.response('ready')

    def commit(self, token):
        self.runtime.events.append('commit')
        if token != self.token or token != self.runtime.token:
            raise daily.DailyError('wrong_token')
        self.runtime.committed = True
        return self.response('committed')

    def status(self):
        self.runtime.events.append('gate_status')
        return self.response('running')

    def release(self, token):
        self.runtime.events.append('release')
        if self.runtime.token is not None and token != self.runtime.token:
            raise daily.DailyError('wrong_token')
        self.runtime.token = None
        self.runtime.committed = False
        return self.response('released')

    def __exit__(self,*_):
        self.close()

    def close(self):
        if not self.runtime.committed:
            self.runtime.token = None


class FakeRuntime:
    """No systemctl, network, credentials or production files are accessed."""
    def __init__(self):
        self.clock = datetime(2026, 10, 28, 10, tzinfo=timezone.utc)
        self.elapsed = 0
        self.events = []
        self.sleeps = []
        self.lock_fds = []
        self.busy_attempts = 0
        self.busy_until = None
        self.application_running = True
        self.system_stopping = False
        self.fail = None
        self.crash_snapshot = False
        self.stop_fails_after_effect = False
        self.start_attempts = 0
        self.token = None
        self.committed = False
        self.boot = '11111111-1111-1111-1111-111111111111'
        self.pending_resume = None
        self.jobs = {}
        self.offsite_calls = []
        self.properties = dict(LoadState='loaded', ActiveState='active', SubState='running',
            MainPID='401', InvocationID='a'*32, Job='0', TimeoutStopUSec='infinity',
            KillSignal='15', KillMode='control-group', SendSIGKILL='no',
            RuntimeDirectoryPreserve='restart',
            Result='success', ExecMainStatus='0', NRestarts='0')

    def now(self):
        return self.clock

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.clock += timedelta(seconds=seconds)
        self.elapsed += seconds

    def monotonic(self):
        return self.elapsed

    def boot_id(self):
        return self.boot

    def ensure_supervised(self):
        self.events.append('supervised')

    def recovery_mode(self):
        pass

    def manager_stopping(self):
        return self.system_stopping

    def service(self):
        self.events.append('service')
        return dict(self.properties)

    @contextmanager
    def gate(self, expected_pid):
        client = FakeGate(self, expected_pid)
        try:
            yield client
        finally:
            if not self.committed:
                self.token = None

    def stop(self):
        self.events.append('stop')
        if self.fail == 'stop' and not self.stop_fails_after_effect:
            raise daily.DailyError('synthetic_stop_failure')
        self.properties.update(ActiveState='inactive', SubState='dead', MainPID='0', Job='0')
        if self.fail == 'stop':
            raise daily.DailyError('synthetic_lost_stop_response')

    def start(self):
        self.events.append('start')
        self.start_attempts += 1
        if self.properties['ActiveState'] in ('active','activating') or self.properties['Job'] not in ('','0'):
            return  # systemd start does not create another active process.
        if self.fail == 'start':
            self.properties.update(ActiveState='failed', SubState='failed', MainPID='0')
            raise daily.DailyError('synthetic_start_failure')
        self.token = None
        self.committed = False
        self.properties.update(ActiveState='active', SubState='running', MainPID=str(500+self.start_attempts),
                               InvocationID=f'{self.start_attempts:032x}', Job='0')

    def snapshot(self, destination):
        self.events.append('snapshot')
        if self.crash_snapshot:
            os._exit(73)  # Intentional local child death; parent runs recovery independently.
        if self.fail == 'snapshot':
            raise daily.DailyError('synthetic_snapshot_failure')
        destination = Path(destination)
        destination.mkdir(mode=0o700)
        (destination/'COMPLETE').write_text('b'*64+'\n',encoding='ascii')
        (destination/'database.sqlite3').write_bytes(b'SYNTHETIC-UNVALIDATED-BY-THIS-FAKE')

    def verify(self, path):
        self.events.append('verify')
        if self.fail == 'verify':
            raise daily.DailyError('synthetic_verify_failure')
        if not (Path(path)/'COMPLETE').is_file():
            raise daily.DailyError('missing_synthetic_snapshot')
        return dict(format='tdb-backup-v1')

    def resume_for(self, path):
        self.events.append('resume_for')
        return self.pending_resume

    def job_ids(self):
        return list(self.jobs)

    def resolve_job(self,path,baseline):
        matches = [identifier for identifier,source in self.jobs.items()
                   if identifier not in baseline and source==str(path)]
        if len(matches)>1:
            raise daily.DailyError('ambiguous_synthetic_jobs')
        return matches[0] if matches else None

    def offsite(self, path, resume=None):
        self.events.append('offsite')
        self.offsite_calls.append((str(path),resume))
        if self.properties['ActiveState'] != 'active':
            raise AssertionError('Dropbox ran before TDB was restored')
        if self.fail == 'offsite':
            raise daily.DailyError('synthetic_dropbox_unavailable')
        identifier = resume or f'{len(self.offsite_calls):032x}'
        self.jobs[identifier] = str(path)
        return dict(status='ok', cloud_verified=True, id=identifier,
                    archive='synthetic-test.tar.gz.age', sha256='d'*64, bytes=128)


class CostlyRecoveryRuntime(FakeRuntime):
    """Deterministic query durations and process events, without real waiting."""
    def __init__(self,service_cost=.15,manager_cost=.15,socket_cost=.05):
        super().__init__()
        self.service_cost, self.manager_cost, self.socket_cost = service_cost,manager_cost,socket_cost
        self.observe = lambda: None
        self.after_probe = lambda: None
        self.service_calls = self.manager_calls = 0
        self.probes = []

    def service(self):
        self.service_calls+=1
        self.sleep(self.service_cost)
        self.observe()
        return super().service()

    def manager_stopping(self):
        self.manager_calls+=1
        self.sleep(self.manager_cost)
        return super().manager_stopping()

    @contextmanager
    def gate(self,pid):
        with super().gate(pid) as gate:
            original = gate.status
            def status():
                self.sleep(self.socket_cost)
                self.observe()
                result = original()
                self.probes.append((self.elapsed,daily._process_identity(self.properties),result['running']))
                self.after_probe()
                return result
            gate.status = status
            yield gate


class DailyBackupTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='tdb-daily-', dir=Path(__file__).parent)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).absolute()
        self.values = dict(
            database=str(self.root/'production.sqlite3'), html_roots=[str(self.root/'html')],
            admin_dir=str(self.root/'admin'), backup_dir=str(self.root/'backups'),
            offsite_state=str(self.root/'offsite'), state_dir=str(self.root/'daily-state'),
            socket=str(self.root/'runtime'/'control.sock'), deploy_lock=str(self.root/'deployment'/'deploy.lock'),
            stability_seconds=1, start_timeout_seconds=3, stop_timeout_seconds=3,
            restart_retry_seconds=1,
        )
        for key in ('admin_dir','backup_dir','offsite_state','state_dir'):
            Path(self.values[key]).mkdir(mode=0o700)
        for name in ('backup_restore.py','offsite_backup.py'):
            path = Path(self.values['admin_dir'])/name
            path.write_text('# synthetic, never executed\n',encoding='ascii')
            path.chmod(0o600)
        (self.root/'html').mkdir(mode=0o700)
        (self.root/'runtime').mkdir(mode=0o700)
        (self.root/'deployment').mkdir(mode=0o700)
        self.database = Path(self.values['database'])
        self.database.write_bytes(b'SYNTHETIC-ORIGINAL-DATABASE')
        Path(self.values['deploy_lock']).touch(mode=0o600)
        self.previous = Path(self.values['backup_dir'])/'previous-snapshot'
        self.previous.mkdir(mode=0o700)
        (self.previous/'keep.txt').write_bytes(b'PREVIOUS-GOOD-COPY')
        self.offsite_proof = Path(self.values['offsite_state'])/'foreign-state.json'
        self.offsite_proof.write_bytes(b'EXISTING-PRIVATE-OFFSITE-STATE')
        self.runtime = FakeRuntime()
        self.config = daily.Config(self.values)
        space = patch.object(daily.shutil,'disk_usage',return_value=SimpleNamespace(free=100*1024**3))
        space.start()
        self.addCleanup(space.stop)

    def controller(self, runtime=None):
        return daily.Controller(self.config, runtime=runtime or self.runtime)

    def assert_originals_unchanged(self):
        self.assertEqual(b'SYNTHETIC-ORIGINAL-DATABASE',self.database.read_bytes())
        self.assertEqual(b'PREVIOUS-GOOD-COPY',(self.previous/'keep.txt').read_bytes())
        self.assertEqual(b'EXISTING-PRIVATE-OFFSITE-STATE',self.offsite_proof.read_bytes())

    def test_success_restores_service_before_offsite_and_preserves_previous_data(self):
        result = self.controller().run()
        self.assertEqual('ok',result['status'])
        sequence = [event for event in self.runtime.events
                    if event in ('prepare','commit','stop','snapshot','verify','start','offsite')]
        self.assertEqual(['prepare','commit','stop','snapshot','verify','start','verify','offsite'],sequence)
        self.assertEqual('active',self.runtime.properties['ActiveState'])
        self.assert_originals_unchanged()

    def test_busy_retries_after_fifteen_minutes_without_stopping_active_work(self):
        self.runtime.busy_attempts = 1
        result = self.controller().run()
        self.assertEqual('ok',result['status'])
        self.assertIn(900,self.runtime.sleeps)
        self.assertEqual(2,self.runtime.events.count('prepare'))
        self.assertEqual(1,self.runtime.events.count('stop'))
        self.assert_originals_unchanged()

    def test_entire_busy_window_is_skipped_without_queue_or_service_stop(self):
        self.runtime.busy_until = self.runtime.clock.replace(hour=14)
        result = self.controller().run()
        self.assertEqual('skipped',result['status'])
        self.assertNotIn('stop',self.runtime.events)
        self.assertNotIn('snapshot',self.runtime.events)
        self.assertLessEqual(self.runtime.events.count('prepare'),13)
        self.assertGreaterEqual(self.runtime.clock.hour,13)
        self.runtime.clock += timedelta(days=1)
        self.runtime.clock = self.runtime.clock.replace(hour=10,minute=0,second=0)
        self.runtime.busy_until = None
        self.assertEqual('ok',self.controller().run()['status'])

    def test_timer_catchup_outside_daily_window_does_not_stop_service(self):
        self.runtime.clock = self.runtime.clock.replace(hour=16)
        self.assertEqual('skipped',self.controller().run()['status'])
        self.assertNotIn('stop',self.runtime.events)

    def test_snapshot_verify_and_stop_failures_restore_owned_service(self):
        for index,stage in enumerate(('snapshot','verify','stop')):
            with self.subTest(stage=stage):
                runtime = FakeRuntime()
                runtime.clock += timedelta(days=index+1)
                runtime.fail = stage
                runtime.stop_fails_after_effect = stage == 'stop'
                with self.assertRaises(daily.DailyError):
                    self.controller(runtime).run()
                self.assertEqual('active',runtime.properties['ActiveState'])
                self.assertNotIn('offsite',runtime.events)
                self.assertLessEqual(runtime.start_attempts,3)
                self.assert_originals_unchanged()

    def test_failed_recovery_is_bounded_across_repeated_invocations(self):
        self.runtime.fail = 'start'
        with self.assertRaises(daily.DailyError): self.controller().run()
        attempts = self.runtime.start_attempts
        self.assertGreater(attempts,0)
        self.assertLessEqual(attempts,3)
        for _ in range(3):
            with self.assertRaises(daily.DailyError): self.controller().recover()
        self.assertEqual(attempts,self.runtime.start_attempts)
        self.assertNotIn('offsite',self.runtime.events)
        self.assert_originals_unchanged()

    def test_inactive_service_never_creates_restart_ownership(self):
        self.runtime.properties.update(ActiveState='inactive',SubState='dead',MainPID='0')
        try:
            result = self.controller().run()
            self.assertEqual('skipped',result['status'])
        except daily.DailyError:
            pass  # Both safe refusal classifications leave administrative intent intact.
        self.controller().recover()
        self.assertNotIn('start',self.runtime.events)
        self.assertNotIn('stop',self.runtime.events)

    def test_manual_hold_excludes_daily_run_and_recovery_from_starting_tdb(self):
        self.controller().hold()
        self.runtime.properties.update(ActiveState='inactive',SubState='dead',MainPID='0')
        result = self.controller().run()
        self.assertEqual('skipped',result['status'])
        self.controller().recover()
        self.assertNotIn('start',self.runtime.events)
        self.assertNotIn('stop',self.runtime.events)
        self.controller().unhold()

    def test_cloud_outage_happens_after_restore_and_does_not_touch_existing_copies(self):
        self.runtime.fail = 'offsite'
        with self.assertRaises(daily.DailyError): self.controller().run()
        self.assertEqual('active',self.runtime.properties['ActiveState'])
        self.assertIn('snapshot',self.runtime.events)
        self.assertLess(self.runtime.events.index('start'),self.runtime.events.index('offsite'))
        self.assert_originals_unchanged()

    def test_same_day_repeat_does_not_stop_or_create_another_snapshot(self):
        self.controller().run()
        before = list(self.runtime.events)
        result = self.controller().run()
        self.assertIn(result['status'],('ok','skipped'))
        self.assertEqual(before.count('stop'),self.runtime.events.count('stop'))
        self.assertEqual(before.count('snapshot'),self.runtime.events.count('snapshot'))
        self.assert_originals_unchanged()

    def test_parallel_controller_and_existing_deploy_lock_do_not_stop_service(self):
        with daily.offsite._lease(self.config.state_dir):
            with self.assertRaises((daily.DailyError,daily.offsite.OffsiteError)):
                self.controller().run()
        self.assertNotIn('stop',self.runtime.events)
        with daily.file_lock(self.config.deploy_lock):
            self.assertEqual('skipped',self.controller().run()['status'])
        self.assertNotIn('stop',self.runtime.events)
        self.assert_originals_unchanged()

    def test_recovery_releases_committed_fence_if_stop_was_never_issued(self):
        token = 'e'*32
        owner = dict(token=token,boot_id=self.runtime.boot,invocation='a'*32,pid=401,
                     attempts=0,snapshot=str(Path(self.values['backup_dir'])/'not-created'),
                     binding=self.config.binding)
        self.runtime.token = token
        self.runtime.committed = True
        self.controller().store.write('owner',owner)
        result = self.controller().recover()
        self.assertEqual('ok',result['status'])
        self.assertIn('release',self.runtime.events)
        self.assertNotIn('start',self.runtime.events)
        self.assertIsNone(self.runtime.token)
        self.assertFalse(self.runtime.committed)
        self.assertIsNone(self.controller().store.read('owner'))

    def test_forced_controller_exit_keeps_durable_ownership_for_independent_boot_recovery(self):
        config_path = self.root/'synthetic-config.json'
        config_path.write_text(json.dumps(self.values),encoding='utf-8')
        child_path = self.root/'synthetic-controller.py'
        search = [str(Path(__file__).resolve().parents[1]),str(Path(__file__).resolve().parent)]
        child_path.write_text(
            "import json,sys\nfrom pathlib import Path\nfrom types import SimpleNamespace\n"
            "from unittest.mock import patch\nsys.path[:0]="+repr(search)+"\n"
            "from test_daily_backup import FakeRuntime\nfrom tools import daily_backup as daily\n"
            "runtime=FakeRuntime()\nruntime.crash_snapshot=True\n"
            "config=daily.Config(json.loads(Path(sys.argv[1]).read_text(encoding='utf-8')))\n"
            "with patch.object(daily.shutil,'disk_usage',return_value=SimpleNamespace(free=100*1024**3)):\n"
            "    daily.Controller(config,runtime=runtime).run()\n",encoding='utf-8')
        process = subprocess.run([sys.executable,'-I','-S','-B',str(child_path),str(config_path)],
                                 capture_output=True,timeout=15,check=False)
        self.assertEqual(73,process.returncode,process.stderr.decode(errors='replace'))
        owner = self.controller().store.read('owner')
        self.assertIsNotNone(owner)
        self.assertEqual(0,owner['attempts'])
        self.assertFalse(Path(owner['snapshot']).exists())
        self.runtime.boot = '22222222-2222-2222-2222-222222222222'
        self.runtime.properties.update(ActiveState='inactive',SubState='dead',MainPID='0',Job='0')
        self.assertEqual('ok',self.controller().recover()['status'])
        self.assertEqual(1,self.runtime.start_attempts)
        self.assertIsNone(self.controller().store.read('owner'))
        self.assert_originals_unchanged()

    def test_unsafe_shutdown_configuration_and_low_space_refuse_before_stop(self):
        self.runtime.properties['SendSIGKILL'] = 'yes'
        with self.assertRaises(daily.DailyError): self.controller().run()
        self.assertNotIn('stop',self.runtime.events)
        self.runtime.clock += timedelta(days=1)
        self.runtime.properties['SendSIGKILL'] = 'no'
        with patch.object(daily.shutil,'disk_usage',return_value=SimpleNamespace(free=10*1024**3-1)):
            with self.assertRaises(daily.DailyError): self.controller().run()
        self.assertNotIn('stop',self.runtime.events)
        self.assert_originals_unchanged()

    def test_invalid_paths_limits_or_corrupt_ownership_fail_closed(self):
        for changes in ({'reserve_bytes':10*1024**3-1}, {'backup_dir':self.values['state_dir']},
                        {'database':'../production.sqlite3'}, {'unknown_secret':'PRIVATE_TEST_SENTINEL'},
                        {'window_start_utc':'13:00','window_end_utc':'10:00'}):
            with self.subTest(changes=changes):
                with self.assertRaises((daily.DailyError,daily.snapshots.BackupError)):
                    daily.Config(self.values|changes)
        self.controller().store.write('owner',{'token':'foreign','snapshot':'../outside'})
        with self.assertRaises(daily.DailyError): self.controller().recover()
        self.assertNotIn('start',self.runtime.events)
        self.assertNotIn('stop',self.runtime.events)
        self.assert_originals_unchanged()

    def test_stop_job_that_never_settles_cannot_authorize_snapshot_or_forced_restart(self):
        def hung_stop():
            self.runtime.events.append('stop')
            self.runtime.properties.update(ActiveState='deactivating',SubState='stop-sigterm',Job='71')
        with patch.object(self.runtime,'stop',side_effect=hung_stop):
            with self.assertRaises(daily.DailyError): self.controller().run()
        self.assertNotIn('snapshot',self.runtime.events)
        self.assertNotIn('start',self.runtime.events)
        self.assertIsNotNone(self.controller().store.read('owner'))
        self.assertLessEqual(sum(self.runtime.sleeps),15)
        self.assert_originals_unchanged()

    def test_lost_start_response_is_reconciled_by_application_readiness(self):
        original = self.runtime.start
        def lost_response():
            original()
            raise daily.DailyError('synthetic_lost_start_response')
        with patch.object(self.runtime,'start',side_effect=lost_response):
            self.assertEqual('ok',self.controller().run()['status'])
        self.assertEqual(1,self.runtime.start_attempts)
        self.assertEqual('active',self.runtime.properties['ActiveState'])
        self.assertIsNone(self.controller().store.read('owner'))
        self.assert_originals_unchanged()

    def test_same_day_retry_after_cloud_failure_reuses_snapshot_without_stopping_again(self):
        self.runtime.fail = 'offsite'
        with self.assertRaises(daily.DailyError): self.controller().run()
        first_path = self.runtime.offsite_calls[0][0]
        stop_count = self.runtime.events.count('stop')
        snapshot_count = self.runtime.events.count('snapshot')
        self.runtime.fail = None
        result = self.controller().run()
        self.assertEqual('ok',result['status'])
        self.assertEqual(stop_count,self.runtime.events.count('stop'))
        self.assertEqual(snapshot_count,self.runtime.events.count('snapshot'))
        self.assertEqual(first_path,self.runtime.offsite_calls[-1][0])
        self.assertIsNone(self.controller().store.read('pending'))
        self.assert_originals_unchanged()

    def test_lost_successful_offsite_response_resumes_known_new_job(self):
        original = self.runtime.offsite
        completed = []
        def lose_response(path,resume=None):
            result = original(path,resume)
            completed.append(result)
            raise daily.DailyError('synthetic_lost_successful_cloud_response')
        with patch.object(self.runtime,'offsite',side_effect=lose_response):
            with self.assertRaises(daily.DailyError): self.controller().run()
        stop_count = self.runtime.events.count('stop')
        identifier = completed[0]['id']
        self.assertEqual([identifier],self.runtime.job_ids())
        self.assertEqual('ok',self.controller().run()['status'])
        self.assertEqual(identifier,self.runtime.offsite_calls[-1][1])
        self.assertEqual([identifier],self.runtime.job_ids())
        self.assertEqual(stop_count,self.runtime.events.count('stop'))
        self.assert_originals_unchanged()

    def test_changed_service_configuration_cannot_claim_previous_restart_ownership(self):
        owner = dict(token='e'*32,boot_id=self.runtime.boot,invocation='a'*32,pid=401,
                     attempts=0,snapshot=str(Path(self.values['backup_dir'])/'not-created'),
                     binding=self.config.binding)
        self.controller().store.write('owner',owner)
        altered = daily.Config(self.values|{'service':'intentionally-stopped.service'})
        self.runtime.properties.update(ActiveState='inactive',SubState='dead',MainPID='0')
        with self.assertRaises(daily.DailyError):
            daily.Controller(altered,runtime=self.runtime).recover()
        self.assertNotIn('start',self.runtime.events)
        self.assertEqual(owner,self.controller().store.read('owner'))

    def test_stop_wait_remains_bounded_if_wall_clock_moves_backwards(self):
        original_sleep = self.runtime.sleep
        def clock_rollback(seconds):
            original_sleep(seconds)
            self.runtime.clock -= timedelta(days=1)
        def hung_stop():
            self.runtime.events.append('stop')
            self.runtime.properties.update(ActiveState='deactivating',SubState='stop-sigterm',Job='71')
        with patch.object(self.runtime,'sleep',side_effect=clock_rollback), \
                patch.object(self.runtime,'stop',side_effect=hung_stop):
            with self.assertRaises(daily.DailyError): self.controller().run()
        self.assertNotIn('snapshot',self.runtime.events)
        self.assertNotIn('start',self.runtime.events)
        self.assertLessEqual(sum(self.runtime.sleeps),15)
        self.assert_originals_unchanged()

    def test_systemd_active_without_application_readiness_never_authorizes_stop(self):
        self.runtime.application_running = False
        self.assertEqual('skipped',self.controller().run()['status'])
        self.assertNotIn('stop',self.runtime.events)
        self.assertNotIn('snapshot',self.runtime.events)
        self.assert_originals_unchanged()

    def test_service_invocation_change_during_prepare_refuses_stop(self):
        original = self.runtime.service
        def changed():
            if 'prepare' in self.runtime.events:
                self.runtime.properties.update(MainPID='402',InvocationID='f'*32)
            return original()
        with patch.object(self.runtime,'service',side_effect=changed):
            with self.assertRaises(daily.DailyError): self.controller().run()
        self.assertNotIn('stop',self.runtime.events)
        self.assertIsNone(self.controller().store.read('owner'))
        self.assert_originals_unchanged()

    def test_runtime_rejects_unsupervised_or_missing_manager_recovery_hooks(self):
        runtime = daily.Runtime(self.config)
        invocation = 'f'*32
        command = (f'/usr/bin/python3 -I -S -B {self.config.admin_dir}/daily_backup.py '
                   f'--config {runtime.config_path} recover')
        main = dict(MainPID=str(os.getpid()),InvocationID=invocation,KillMode='control-group',
                    SendSIGKILL='yes',ExecStopPost='{ argv[]='+command+' ; }',Type='oneshot',User='ubuntu')
        recovery = dict(LoadState='loaded',UnitFileState='enabled',ExecStart='{ argv[]='+command+' ; }',User='ubuntu')
        def output(data): return '\n'.join(key+'='+value for key,value in data.items()).encode('ascii')
        with patch.dict(os.environ,{'INVOCATION_ID':''}), patch.object(runtime,'command') as execute:
            with self.assertRaises(daily.DailyError): runtime.ensure_supervised()
            execute.assert_not_called()
        with patch.dict(os.environ,{'INVOCATION_ID':invocation}):
            with patch.object(runtime,'command',side_effect=[output(main),output(recovery)]) as execute:
                runtime.ensure_supervised()
                self.assertEqual(2,execute.call_count)
            for changes in ({'ExecStopPost':''},{'KillMode':'process'},{'SendSIGKILL':'no'},
                            {'MainPID':'1'},{'InvocationID':'a'*32},{'Type':'simple'}):
                with self.subTest(main=changes):
                    with patch.object(runtime,'command',return_value=output(main|changes)):
                        with self.assertRaises(daily.DailyError): runtime.ensure_supervised()
            for changes in ({'UnitFileState':'disabled'},{'ExecStart':''},{'User':'root'}):
                with self.subTest(recovery=changes):
                    with patch.object(runtime,'command',side_effect=[output(main),output(recovery|changes)]):
                        with self.assertRaises(daily.DailyError): runtime.ensure_supervised()

    def test_runtime_tool_uses_isolated_sibling_cli_and_rejects_hardlink_or_raw_output(self):
        runtime = daily.Runtime(self.config)
        with patch.object(runtime,'command',return_value=b'{"status":"ok"}') as execute:
            runtime.verify(self.previous)
        argv = execute.call_args.args[0]
        self.assertEqual([sys.executable,'-I','-S','-B'],argv[:4])
        self.assertEqual(str(self.config.admin_dir/'backup_restore.py'),argv[4])
        self.assertEqual('verify',argv[5])
        self.assertNotIn('src.main',argv)
        with patch.object(runtime,'command',return_value=b'PRIVATE_TEST_SENTINEL'):
            with self.assertRaises(daily.DailyError) as error: runtime.verify(self.previous)
        self.assertNotIn('PRIVATE_TEST_SENTINEL',str(error.exception))
        os.link(self.config.admin_dir/'backup_restore.py',self.root/'tool-hardlink.py')
        with patch.object(runtime,'command') as execute:
            with self.assertRaises(daily.offsite.OffsiteError): runtime.verify(self.previous)
            execute.assert_not_called()

    def test_recovery_commands_ignore_disk_reserve_and_inherit_administration_lease(self):
        runtime = daily.Runtime(self.config)
        runtime.lock_fds = [10,11]
        with patch.object(runtime.commands,'run',return_value=b'') as execute, \
                patch.object(daily.shutil,'disk_usage',return_value=SimpleNamespace(free=0)):
            runtime.commands.check()
            runtime.command(['/usr/bin/systemctl','show','tdb.service'])
        self.assertEqual(11,runtime.commands.lock_fd)
        self.assertEqual(['/usr/bin/systemctl','show','tdb.service'],execute.call_args.args[0])

    def test_manager_shutdown_defers_owned_recovery_without_consuming_start_budget(self):
        owner = dict(token='e'*32,boot_id=self.runtime.boot,invocation='a'*32,pid=401,
                     attempts=1,snapshot=str(Path(self.values['backup_dir'])/'not-created'),
                     binding=self.config.binding)
        self.controller().store.write('owner',owner)
        self.runtime.properties.update(ActiveState='inactive',SubState='dead',MainPID='0')
        self.runtime.system_stopping = True
        result = self.controller().recover()
        self.assertEqual('deferred',result['status'])
        self.assertEqual('recovery_deferred_until_boot',result['code'])
        self.assertEqual(owner,self.controller().store.read('owner'))
        self.assertNotIn('start',self.runtime.events)
        self.runtime.system_stopping = False
        self.runtime.boot = '22222222-2222-2222-2222-222222222222'
        self.assertEqual('ok',self.controller().recover()['status'])
        self.assertEqual(1,self.runtime.start_attempts)
        self.assertIsNone(self.controller().store.read('owner'))
        self.assert_originals_unchanged()


class RecoveryRegressionTests(unittest.TestCase):
    setUp = DailyBackupTests.setUp
    controller = DailyBackupTests.controller
    assert_originals_unchanged = DailyBackupTests.assert_originals_unchanged

    def configure(self,timeout=180,stability=10,stop=600):
        self.config = daily.Config(self.values|dict(start_timeout_seconds=timeout,
            stability_seconds=stability,stop_timeout_seconds=stop))

    def own(self,attempts=0):
        owner = dict(token='e'*32,boot_id=self.runtime.boot,invocation='a'*32,pid=401,
            attempts=attempts,snapshot=str(self.config.backup_dir/'unopened-snapshot'),binding=self.config.binding)
        self.controller().store.write('owner',owner)
        return owner

    def ready_at(self,seconds):
        original = self.runtime.service
        def read():
            self.runtime.application_running = self.runtime.elapsed>=seconds
            return original()
        return patch.object(self.runtime,'service',side_effect=read)

    def test_active_readiness_30_180_500_800_seconds_never_spends_start_attempts(self):
        self.configure(timeout=900)
        for delay in (30,180,500,800):
            with self.subTest(delay=delay):
                self.runtime = FakeRuntime()
                self.own()
                with self.ready_at(delay): result = self.controller()._recover_locked()
                self.assertEqual('bot_restored',result['code'])
                self.assertEqual(delay+10,self.runtime.elapsed)
                self.assertEqual(0,self.runtime.start_attempts)
                self.assertNotIn('stop',self.runtime.events)
                self.assertIsNone(self.controller().store.read('owner'))
        self.assert_originals_unchanged()

    def test_default_readiness_deadline_accepts_boundary_and_refuses_longer_delays_without_start(self):
        self.configure()
        self.own()
        with self.ready_at(180): self.controller()._recover_locked()
        self.assertEqual(190,self.runtime.elapsed)
        for delay in (500,800):
            with self.subTest(delay=delay):
                self.runtime = FakeRuntime()
                owner = self.own()
                with self.ready_at(delay), self.assertRaises(daily.DailyError) as error:
                    self.controller()._recover_locked()
                self.assertEqual('critical_application_readiness_timeout',error.exception.code)
                self.assertEqual(180,self.runtime.elapsed)
                self.assertEqual(0,self.runtime.start_attempts)
                self.assertEqual(owner,self.controller().store.read('owner'))

    def test_exhausted_budget_still_waits_for_an_already_active_application(self):
        self.configure()
        self.own(attempts=3)
        with self.ready_at(30): self.controller()._recover_locked()
        self.assertEqual(40,self.runtime.elapsed)
        self.assertEqual(0,self.runtime.start_attempts)
        self.assertIsNone(self.controller().store.read('owner'))

    def test_never_ready_active_application_is_bounded_and_keeps_budget_and_diagnostic(self):
        self.configure(timeout=7,stability=2)
        owner = self.own(attempts=2)
        self.runtime.application_running = False
        with patch('sys.stderr',new=io.StringIO()), self.assertRaises(daily.DailyError) as error:
            self.controller().recover()
        self.assertEqual('critical_application_readiness_timeout',error.exception.code)
        self.assertEqual(7,self.runtime.elapsed)
        self.assertEqual(0,self.runtime.start_attempts)
        self.assertEqual(owner,self.controller().store.read('owner'))
        status = self.controller().store.read('status')
        self.assertEqual('critical',status['status'])
        self.assertEqual(error.exception.code,status['code'])

    def test_fake_systemd_start_is_a_noop_for_an_active_process(self):
        before = dict(self.runtime.properties)
        self.runtime.application_running = False
        self.runtime.start()
        self.assertEqual(before,self.runtime.properties)
        self.assertFalse(self.runtime.application_running)

    def test_only_settled_inactive_or_failed_service_gets_a_durable_start_attempt(self):
        self.configure(timeout=20,stability=2)
        for state,substate in (('inactive','dead'),('failed','failed')):
            with self.subTest(state=state):
                self.runtime = FakeRuntime()
                self.runtime.properties.update(ActiveState=state,SubState=substate,MainPID='0')
                self.own(attempts=1)
                original_start = self.runtime.start
                def start():
                    self.assertEqual(2,self.controller().store.read('owner')['attempts'])
                    original_start()
                with patch.object(self.runtime,'start',side_effect=start): self.controller()._recover_locked()
                self.assertEqual(1,self.runtime.start_attempts)
                self.assertNotIn('stop',self.runtime.events)
                self.assertIsNone(self.controller().store.read('owner'))

    def test_exhausted_budget_for_stopped_service_never_dispatches_a_start(self):
        owner = self.own(attempts=3)
        self.runtime.properties.update(ActiveState='inactive',SubState='dead',MainPID='0')
        with self.assertRaises(daily.DailyError) as error: self.controller()._recover_locked()
        self.assertEqual('critical_restart_attempts_exhausted',error.exception.code)
        self.assertEqual(owner,self.controller().store.read('owner'))
        self.assertEqual(0,self.runtime.start_attempts)

    def test_transitioning_service_and_pending_jobs_are_waited_without_spending_attempts(self):
        self.configure(timeout=30,stability=2,stop=20)
        for state,substate,pid,job in (('activating','start','0','9'),('deactivating','stop-sigterm','401','9'),
                                     ('active','running','401','9'),('inactive','dead','0','9')):
            with self.subTest(state=state):
                self.runtime = FakeRuntime()
                self.own(attempts=3)
                original = self.runtime.service
                def read():
                    if self.runtime.elapsed<5:
                        self.runtime.properties.update(ActiveState=state,SubState=substate,MainPID=pid,Job=job)
                    else:
                        self.runtime.properties.update(ActiveState='active',SubState='running',MainPID='402',Job='0')
                    return original()
                with patch.object(self.runtime,'service',side_effect=read): self.controller()._recover_locked()
                self.assertEqual(7,self.runtime.elapsed)
                self.assertEqual(0,self.runtime.start_attempts)

    def test_permanent_transition_or_unknown_state_is_a_bounded_refusal(self):
        self.configure(timeout=4,stability=1,stop=5)
        cases = [dict(ActiveState='activating',SubState='start',Job='0'),
                 dict(ActiveState='deactivating',SubState='stop-sigterm',Job='0'),
                 dict(Job='19'),dict(LoadState='not-found'),dict(ActiveState='unknown')]
        for changes in cases:
            with self.subTest(changes=changes):
                self.runtime = FakeRuntime()
                self.runtime.properties.update(changes)
                owner = self.own()
                with self.assertRaises(daily.DailyError): self.controller()._recover_locked()
                self.assertLessEqual(self.runtime.elapsed,5)
                self.assertEqual(owner,self.controller().store.read('owner'))
                self.assertEqual(0,self.runtime.start_attempts)
                self.assertNotIn('stop',self.runtime.events)

    def test_automatic_restart_during_readiness_is_observed_without_manual_start(self):
        self.configure(timeout=120,stability=10)
        self.own()
        original = self.runtime.service
        def read():
            t = self.runtime.elapsed
            self.runtime.application_running = t>=60
            if 30<=t<40:
                self.runtime.properties.update(ActiveState='activating',SubState='auto-restart',Job='7')
            elif t>=40:
                self.runtime.properties.update(ActiveState='active',SubState='running',Job='0',
                    MainPID='402',InvocationID='b'*32,NRestarts='1')
            return original()
        with patch.object(self.runtime,'service',side_effect=read): self.controller()._recover_locked()
        self.assertEqual(70,self.runtime.elapsed)
        self.assertEqual(0,self.runtime.start_attempts)
        self.assertIsNone(self.controller().store.read('owner'))

    def test_each_identity_component_changing_before_final_probe_requires_a_new_full_interval(self):
        self.configure(timeout=50,stability=10)
        for changes in ({'MainPID':'402'},{'InvocationID':'b'*32},{'NRestarts':'1'}):
            with self.subTest(changes=changes):
                self.runtime = FakeRuntime()
                self.own()
                original = self.runtime.service
                replacement = []
                def read():
                    data = original()
                    if self.runtime.elapsed>=10 and not replacement:
                        self.assertIsNotNone(self.controller().store.read('owner'))
                        self.runtime.properties.update(changes)
                        replacement.append(self.runtime.elapsed)
                    return data
                with patch.object(self.runtime,'service',side_effect=read): self.controller()._recover_locked()
                self.assertGreaterEqual(self.runtime.elapsed,replacement[0]+10)
                self.assertEqual(0,self.runtime.start_attempts)
                self.assertIsNone(self.controller().store.read('owner'))

    def test_repeated_process_changes_cannot_extend_recovery_indefinitely(self):
        self.configure(timeout=12,stability=4,stop=6)
        owner = self.own(attempts=3)
        original = self.runtime.service
        def read():
            generation = int(self.runtime.elapsed)//2
            self.runtime.properties.update(MainPID=str(401+generation),InvocationID=f'{generation:032x}',NRestarts=str(generation))
            return original()
        with patch.object(self.runtime,'service',side_effect=read), self.assertRaises(daily.DailyError) as error:
            self.controller()._recover_locked()
        # Each new invocation now has its own window, but the original total
        # bound remains stop(6) + one readiness/stability interval(12+4).
        self.assertEqual('critical_recovery_timed_out',error.exception.code)
        self.assertEqual(22,self.runtime.elapsed)
        self.assertEqual(owner,self.controller().store.read('owner'))
        self.assertEqual(0,self.runtime.start_attempts)

    def test_start_revalidation_observes_a_concurrent_boot_instead_of_spending_an_attempt(self):
        self.configure(timeout=10,stability=2)
        for attempts in (0,3):
            with self.subTest(attempts=attempts):
                self.runtime = FakeRuntime()
                self.own(attempts=attempts)
                original = self.runtime.service
                calls = []
                def read():
                    calls.append(1)
                    if len(calls)==1:
                        return original()|dict(ActiveState='inactive',SubState='dead',MainPID='0')
                    return original()
                with patch.object(self.runtime,'service',side_effect=read): self.controller()._recover_locked()
                self.assertEqual(0,self.runtime.start_attempts)
                self.assertIsNone(self.controller().store.read('owner'))

    def test_revalidation_flapping_has_a_global_deadline_and_never_starts(self):
        self.configure(timeout=3,stability=1,stop=3)
        owner = self.own()
        original = self.runtime.service
        calls = []
        def read():
            calls.append(1)
            data = original()
            return data|dict(ActiveState='inactive',SubState='dead',MainPID='0') if len(calls)%2 else data
        with patch.object(self.runtime,'service',side_effect=read), self.assertRaises(daily.DailyError) as error:
            self.controller()._recover_locked()
        self.assertEqual('critical_recovery_timed_out',error.exception.code)
        self.assertLessEqual(self.runtime.elapsed,22)
        self.assertEqual(owner,self.controller().store.read('owner'))
        self.assertEqual(0,self.runtime.start_attempts)

    def test_one_transient_read_failure_at_each_probe_stage_recovers_without_commands(self):
        self.configure(timeout=10,stability=2)
        for fail_on in (1,2,3):
            with self.subTest(fail_on=fail_on):
                self.runtime = FakeRuntime()
                self.own(attempts=3)
                original = self.runtime.service
                calls = []
                def read():
                    calls.append(1)
                    if len(calls)==fail_on: raise daily.offsite.OffsiteError('synthetic unavailable read')
                    return original()
                with patch.object(self.runtime,'service',side_effect=read): self.controller()._recover_locked()
                self.assertEqual(0,self.runtime.start_attempts)
                self.assertGreaterEqual(self.runtime.elapsed,3)
                self.assertIsNone(self.controller().store.read('owner'))

    def test_persistent_read_failure_keeps_owner_budget_and_specific_diagnostic(self):
        self.configure(timeout=4,stability=1)
        owner = self.own(attempts=2)
        with patch.object(self.runtime,'service',side_effect=OSError('PRIVATE_TEST_SENTINEL')), \
                patch('sys.stderr',new=io.StringIO()) as log, self.assertRaises(daily.DailyError) as error:
            self.controller().recover()
        self.assertEqual('critical_service_state_unavailable',error.exception.code)
        self.assertEqual(4,self.runtime.elapsed)
        self.assertEqual(owner,self.controller().store.read('owner'))
        self.assertEqual(error.exception.code,self.controller().store.read('status')['code'])
        self.assertNotIn('PRIVATE_TEST_SENTINEL',log.getvalue())
        self.assertEqual(0,self.runtime.start_attempts)
        self.assertNotIn('stop',self.runtime.events)

    def test_query_failure_during_start_revalidation_never_spends_or_dispatches_a_start(self):
        self.configure(timeout=4,stability=1)
        owner = self.own(attempts=1)
        original = self.runtime.service
        calls = []
        def read():
            calls.append(1)
            if len(calls)==1:
                return original()|dict(ActiveState='inactive',SubState='dead',MainPID='0')
            raise daily.offsite.OffsiteError('synthetic unavailable revalidation')
        with patch.object(self.runtime,'service',side_effect=read), self.assertRaises(daily.DailyError) as error:
            self.controller()._recover_locked()
        self.assertEqual('critical_service_state_unavailable',error.exception.code)
        self.assertEqual(4,self.runtime.elapsed)
        self.assertEqual(owner,self.controller().store.read('owner'))
        self.assertEqual(0,self.runtime.start_attempts)

    def test_slow_revalidation_cannot_dispatch_start_after_the_global_deadline(self):
        self.configure(timeout=3,stability=1,stop=3)
        owner = self.own()
        self.runtime.properties.update(ActiveState='inactive',SubState='dead',MainPID='0')
        original = self.runtime.service
        calls = []
        def read():
            calls.append(1)
            if len(calls)==2: self.runtime.sleep(30)
            return original()
        with patch.object(self.runtime,'service',side_effect=read), self.assertRaises(daily.DailyError) as error:
            self.controller()._recover_locked()
        self.assertEqual('critical_recovery_timed_out',error.exception.code)
        self.assertEqual(owner,self.controller().store.read('owner'))
        self.assertEqual(0,self.runtime.start_attempts)

    def test_system_manager_queries_also_retry_transient_errors_and_bound_persistent_errors(self):
        self.configure(timeout=4,stability=1)
        for permanent in (False,True):
            with self.subTest(permanent=permanent):
                self.runtime = FakeRuntime()
                owner = self.own(attempts=3)
                calls = []
                def manager():
                    calls.append(1)
                    if permanent or len(calls)==1: raise OSError('synthetic manager unavailable')
                    return False
                with patch.object(self.runtime,'manager_stopping',side_effect=manager):
                    if permanent:
                        with self.assertRaises(daily.DailyError) as error: self.controller()._recover_locked()
                        self.assertEqual('critical_system_manager_unavailable',error.exception.code)
                        self.assertEqual(4,self.runtime.elapsed)
                        self.assertEqual(owner,self.controller().store.read('owner'))
                    else:
                        self.controller()._recover_locked()
                        self.assertIsNone(self.controller().store.read('owner'))
                self.assertEqual(0,self.runtime.start_attempts)
                self.assertNotIn('stop',self.runtime.events)


class RecoveryDeadlineTests(unittest.TestCase):
    setUp = DailyBackupTests.setUp
    controller = DailyBackupTests.controller
    configure = RecoveryRegressionTests.configure
    own = RecoveryRegressionTests.own

    def assert_no_extra_commands(self):
        self.assertEqual(0,self.runtime.start_attempts)
        self.assertNotIn('stop',self.runtime.events)

    def test_timely_readiness_allows_costly_final_probe_past_old_stability_cutoff(self):
        self.configure(timeout=8,stability=1,stop=7)
        for replacement in (False,True):
            with self.subTest(replacement=replacement):
                self.runtime = CostlyRecoveryRuntime()
                self.own(attempts=3)
                def observe():
                    if replacement and self.runtime.elapsed>=2.605:
                        self.runtime.properties.update(MainPID='402',InvocationID='b'*32,NRestarts='1')
                    self.runtime.application_running = self.runtime.elapsed>=6.8
                self.runtime.observe = observe
                result = self.controller()._recover_locked()
                first = next(t for t,_,ready in self.runtime.probes if ready)+self.runtime.service_cost
                self.assertAlmostEqual(8.15,first)
                self.assertLess(first,8.3)  # Initial window: first state at .3 + 8.
                self.assertGreater(self.runtime.elapsed,8.3+1)
                self.assertGreaterEqual(self.runtime.elapsed-first,1)
                self.assertIsNone(self.controller().store.read('owner'))
                self.assertEqual('bot_restored',result['code'])
                self.assert_no_extra_commands()

    def test_first_complete_probe_after_readiness_deadline_is_not_accepted(self):
        self.configure(timeout=8,stability=1,stop=7)
        self.runtime = CostlyRecoveryRuntime(socket_cost=7.8)
        owner = self.own(attempts=3)
        # Socket response arrives at 8.25, but final state validation ends at
        # 8.4, after the 8.3 deadline. A partial early probe is insufficient.
        with self.assertRaises(daily.DailyError) as error: self.controller()._recover_locked()
        self.assertEqual('critical_application_readiness_timeout',error.exception.code)
        self.assertAlmostEqual(8.4,self.runtime.elapsed)
        self.assertEqual(1,len(self.runtime.probes))
        self.assertEqual(owner,self.controller().store.read('owner'))
        self.assert_no_extra_commands()

    def test_automatic_restart_near_old_deadline_grants_each_identity_a_new_window(self):
        self.configure(timeout=8,stability=1,stop=20)
        for changes in ({'MainPID':'402'},{'InvocationID':'b'*32},{'NRestarts':'1'}):
            with self.subTest(changes=changes):
                self.runtime = CostlyRecoveryRuntime(0,0,0)
                self.own(attempts=3)
                def observe():
                    t = self.runtime.elapsed
                    self.runtime.application_running = t>=12.2
                    if 7<=t<8:
                        self.runtime.properties.update(ActiveState='activating',SubState='auto-restart',Job='7')
                    elif t>=8:
                        self.runtime.properties.update(ActiveState='active',SubState='running',Job='0',**changes)
                self.runtime.observe = observe
                self.controller()._recover_locked()
                self.assertEqual(14,self.runtime.elapsed)  # First ready at 13, stable at 14.
                self.assertIsNone(self.controller().store.read('owner'))
                self.assert_no_extra_commands()

    def test_identity_change_during_final_socket_probe_restarts_both_phases(self):
        self.configure(timeout=1,stability=1,stop=7)
        for changes in ({'MainPID':'402'},{'InvocationID':'b'*32},{'NRestarts':'1'}):
            with self.subTest(changes=changes):
                self.runtime = CostlyRecoveryRuntime(0,0,0)
                self.own(attempts=3)
                changed = []
                def after_probe():
                    if self.runtime.elapsed>=1 and not changed:
                        self.assertIsNotNone(self.controller().store.read('owner'))
                        self.runtime.properties.update(changes)
                        changed.append(self.runtime.elapsed)
                def observe():
                    self.runtime.application_running = not changed or self.runtime.elapsed>=2
                self.runtime.after_probe, self.runtime.observe = after_probe,observe
                self.controller()._recover_locked()
                self.assertEqual([1],changed)
                self.assertEqual(3,self.runtime.elapsed)  # New process ready at its deadline 2.
                self.assertIsNone(self.controller().store.read('owner'))
                self.assert_no_extra_commands()

    def test_several_automatic_restarts_with_costly_queries_recover_without_start(self):
        self.configure(timeout=8,stability=1,stop=20)
        self.runtime = CostlyRecoveryRuntime()
        self.own(attempts=3)
        def observe():
            generation = min(3,int(self.runtime.elapsed)//2)
            self.runtime.properties.update(MainPID=str(401+generation),
                InvocationID=f'{generation:032x}',NRestarts=str(generation))
            self.runtime.application_running = self.runtime.elapsed>=10
        self.runtime.observe = observe
        self.controller()._recover_locked()
        self.assertGreater(self.runtime.elapsed,10+1)
        self.assertLess(self.runtime.elapsed,20+8+1)
        self.assertEqual('3',self.runtime.probes[-1][1][2])
        self.assertIsNone(self.controller().store.read('owner'))
        self.assert_no_extra_commands()

    def test_restarting_forever_cannot_extend_the_original_global_deadline(self):
        self.configure(timeout=12,stability=4,stop=6)
        self.runtime = CostlyRecoveryRuntime()
        owner = self.own(attempts=3)
        def observe():
            generation = int(self.runtime.elapsed)
            self.runtime.properties.update(MainPID=str(401+generation),
                InvocationID=f'{generation:032x}',NRestarts=str(generation))
        self.runtime.observe = observe
        with self.assertRaises(daily.DailyError) as error: self.controller()._recover_locked()
        self.assertEqual('critical_recovery_timed_out',error.exception.code)
        self.assertGreaterEqual(self.runtime.elapsed,22)
        self.assertLess(self.runtime.elapsed,23)
        self.assertEqual(owner,self.controller().store.read('owner'))
        self.assert_no_extra_commands()

    def test_same_unready_process_and_transient_query_errors_do_not_renew_window(self):
        self.configure(timeout=3,stability=1,stop=10)
        for unavailable in (False,True):
            with self.subTest(unavailable=unavailable):
                self.runtime = CostlyRecoveryRuntime()
                self.runtime.application_running = False
                owner = self.own(attempts=3)
                def observe():
                    if unavailable and self.runtime.service_calls==3:
                        raise OSError('synthetic read failure')
                self.runtime.observe = observe
                with self.assertRaises(daily.DailyError) as error: self.controller()._recover_locked()
                self.assertEqual('critical_application_readiness_timeout',error.exception.code)
                self.assertGreaterEqual(self.runtime.elapsed,3.3)
                self.assertLess(self.runtime.elapsed,6)
                self.assertEqual(owner,self.controller().store.read('owner'))
                self.assert_no_extra_commands()

    def test_pending_job_or_unknown_state_does_not_grant_a_new_process_window(self):
        self.configure(timeout=3,stability=1,stop=10)
        for changes in ({'Job':'9','MainPID':'999','InvocationID':'b'*32,'NRestarts':'1'},
                        {'LoadState':'not-found'}):
            with self.subTest(changes=changes):
                self.runtime = CostlyRecoveryRuntime(0,0,0)
                self.runtime.application_running = False
                owner = self.own(attempts=3)
                initial = dict(self.runtime.properties)
                def observe():
                    self.runtime.properties = initial | (changes if self.runtime.elapsed==2 else {})
                self.runtime.observe = observe
                with self.assertRaises(daily.DailyError) as error: self.controller()._recover_locked()
                self.assertEqual('critical_application_readiness_timeout',error.exception.code)
                self.assertEqual(3,self.runtime.elapsed)
                self.assertEqual(owner,self.controller().store.read('owner'))
                self.assert_no_extra_commands()

    def test_successful_final_probe_cannot_clear_owner_after_global_deadline(self):
        self.configure(timeout=3,stability=1,stop=1)
        self.runtime = CostlyRecoveryRuntime(0,0,0)
        owner = self.own(attempts=3)
        self.runtime.after_probe = lambda: setattr(self.runtime,'socket_cost',10)
        with self.assertRaises(daily.DailyError) as error: self.controller()._recover_locked()
        self.assertEqual('critical_recovery_timed_out',error.exception.code)
        self.assertEqual(11,self.runtime.elapsed)
        self.assertEqual(owner,self.controller().store.read('owner'))
        self.assert_no_extra_commands()


class DailyUnitSafetyTests(unittest.TestCase):
    def read_unit(self,name):
        path = Path(__file__).resolve().parents[1]/'deploy'/'systemd'/name
        parser = configparser.ConfigParser(interpolation=None,strict=False)
        parser.optionxform = str
        parser.read(path,encoding='utf-8')
        return parser

    def test_controller_has_manager_owned_recovery_and_kills_its_entire_own_group(self):
        unit = self.read_unit('tdb-daily-backup.service')
        service = unit['Service']
        self.assertEqual('oneshot',service['Type'])
        self.assertEqual('ubuntu',service['User'])
        self.assertEqual('no',service['Restart'])
        self.assertEqual('control-group',service['KillMode'])
        self.assertEqual('yes',service['SendSIGKILL'])
        self.assertTrue(service['ExecStart'].endswith(' run'))
        self.assertTrue(service['ExecStopPost'].endswith(' recover'))
        self.assertEqual('tdb-backup-recover.service',unit['Unit']['OnFailure'])
        self.assertNotIn('EnvironmentFile',service)

    def test_boot_recovery_is_enabled_separately_and_has_no_start_ordering_deadlock(self):
        unit = self.read_unit('tdb-backup-recover.service')
        self.assertEqual('multi-user.target',unit['Install']['WantedBy'])
        self.assertEqual('no',unit['Service']['Restart'])
        self.assertTrue(unit['Service']['ExecStart'].endswith(' recover'))
        self.assertNotIn('tdb.service',unit['Unit'].get('Before','').split())
        self.assertNotIn('OnFailure',unit['Unit'])

    def test_timer_uses_explicit_utc_and_single_service_without_random_delay(self):
        unit = self.read_unit('tdb-daily-backup.timer')
        timer = unit['Timer']
        self.assertEqual('*-*-* 10:00:00 UTC',timer['OnCalendar'])
        self.assertEqual('0',timer['RandomizedDelaySec'])
        self.assertEqual('true',timer['Persistent'])
        self.assertEqual('tdb-daily-backup.service',timer['Unit'])


if __name__ == '__main__':
    unittest.main()
