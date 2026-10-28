"""Exact readiness boundaries of the SHA-verified production commit, virtual time.

Reuse synthetic files/service/gate fixtures. Execute the entire original module;
no AST rewrite, altered controller methods, VM, real service or network access.
"""
import hashlib
from pathlib import Path
import subprocess
import sys
import types
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).with_name('systemd_harness')))
import test_daily_backup as fixtures
import release


class PinnedReadinessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        root=Path(__file__).resolve().parents[1]
        data=subprocess.run(['git','show',release.BASE+':tools/daily_backup.py'],cwd=root,
                            capture_output=True,check=True,timeout=10).stdout
        assert hashlib.sha256(data).hexdigest()==release.SOURCE_HASHES['tools/daily_backup.py']
        cls.module=types.ModuleType('tools.harness_boundary_controller')
        cls.module.__package__='tools'
        cls.module.__file__=str(root/'tools/daily_backup.py')
        exec(compile(data,release.BASE+':tools/daily_backup.py','exec'),cls.module.__dict__)

    def setUp(self):
        binding=patch.object(fixtures,'daily',self.module)
        binding.start(); self.addCleanup(binding.stop)
        fixtures.DailyBackupTests.setUp(self)
        self.config=self.module.Config(self.values|dict(start_timeout_seconds=8,stability_seconds=1,
                                                       stop_timeout_seconds=7))

    controller=fixtures.DailyBackupTests.controller
    assert_originals_unchanged=fixtures.DailyBackupTests.assert_originals_unchanged

    def own(self):
        owner=dict(token='e'*32,boot_id=self.runtime.boot,invocation='a'*32,pid=401,
                   attempts=0,snapshot=str(self.config.backup_dir/'unopened-snapshot'),binding=self.config.binding)
        self.controller().store.write('owner',owner)
        return owner

    def first_probe_completes(self, at):
        original=self.runtime.service
        calls=[]
        def read():
            calls.append(1)
            if len(calls)==3:  # Outer state, before-socket state, after-socket identity fence.
                self.assertIn('gate_status',self.runtime.events)
                self.runtime.sleep(at)
            return original()
        return patch.object(self.runtime,'service',side_effect=read)

    def test_first_full_positive_probe_before_deadline(self):
        self.own()
        with self.first_probe_completes(7):
            self.assertEqual('bot_restored',self.controller()._recover_locked()['code'])
        self.assertEqual(8,self.runtime.elapsed)
        self.assertIsNone(self.controller().store.read('owner'))

    def test_first_full_positive_probe_exactly_at_deadline_is_accepted(self):
        self.own()
        with self.first_probe_completes(8):
            self.assertEqual('bot_restored',self.controller()._recover_locked()['code'])
        self.assertEqual(9,self.runtime.elapsed)  # Stability/final probe may finish after first-probe deadline.
        self.assertIsNone(self.controller().store.read('owner'))

    def test_positive_reply_with_late_final_identity_fence_is_rejected(self):
        owner=self.own()
        with self.first_probe_completes(8.001), self.assertRaises(self.module.DailyError) as caught:
            self.controller()._recover_locked()
        self.assertEqual('critical_application_readiness_timeout',caught.exception.code)
        self.assertEqual(owner,self.controller().store.read('owner'))
        self.assertEqual(0,self.runtime.start_attempts)

    def test_application_never_ready_times_out_at_equality(self):
        owner=self.own(); self.runtime.application_running=False
        with self.assertRaises(self.module.DailyError) as caught:
            self.controller()._recover_locked()
        self.assertEqual('critical_application_readiness_timeout',caught.exception.code)
        self.assertEqual(8,self.runtime.elapsed)
        self.assertEqual(owner,self.controller().store.read('owner'))
        self.assertEqual(0,self.runtime.start_attempts)

    def test_identity_change_inside_probe_requires_a_new_positive_probe_and_stability(self):
        for changes in (dict(MainPID='402'),dict(InvocationID='b'*32),dict(NRestarts='1')):
            with self.subTest(changes=changes):
                self.runtime=fixtures.FakeRuntime(); self.own()
                original=self.runtime.service; calls=[]
                def read():
                    calls.append(1)
                    if len(calls)==3:
                        self.assertIsNotNone(self.controller().store.read('owner'))
                        self.runtime.sleep(7)
                        self.runtime.properties.update(changes)
                    return original()
                with patch.object(self.runtime,'service',side_effect=read):
                    self.assertEqual('bot_restored',self.controller()._recover_locked()['code'])
                self.assertEqual(9,self.runtime.elapsed)
                self.assertEqual(3,self.runtime.events.count('gate_status'))
                self.assertEqual(0,self.runtime.start_attempts)

    def test_retry_recovers_but_run_preserves_initial_timeout_and_failed_ledger(self):
        controller=self.controller(); outcomes=[]; flags=[]
        original_status=fixtures.FakeGate.status
        original_read=self.runtime.service
        original_recover=controller._recover_locked
        def status(gate):
            flags.append('positive')
            return original_status(gate)
        def read():
            if flags and 'delayed' not in flags:
                flags.append('delayed'); self.runtime.sleep(8.001)
            return original_read()
        def recover():
            try:
                result=original_recover()
            except self.module.DailyError as error:
                outcomes.append(error.code)
                raise
            outcomes.append(result['code'])
            return result
        with patch.object(fixtures.FakeGate,'status',new=status), \
                patch.object(self.runtime,'service',side_effect=read), \
                patch.object(controller,'_recover_locked',side_effect=recover), \
                self.assertRaises(self.module.DailyError) as caught:
            controller.run()
        self.assertEqual('critical_application_readiness_timeout',caught.exception.code)
        self.assertEqual(['critical_application_readiness_timeout','bot_restored'],outcomes)
        self.assertIsNone(controller.store.read('owner'))
        self.assertEqual('failed',controller.store.read('day')['state'])
        self.assertEqual('daily_backup_failed',controller.store.read('status')['code'])
        self.assertNotIn('offsite',self.runtime.events)
        self.assert_originals_unchanged()
