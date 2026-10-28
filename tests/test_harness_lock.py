"""Scenario 15 protocol regressions; no systemd commands or VM."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).with_name('systemd_harness')))
import guard
import scenarios
import synthetic


class ProtocolTests(unittest.TestCase):
    def exercise(self, elapsed=9, command=b'release\n', ready=True, interrupted=False):
        events, active = [], []
        with tempfile.TemporaryFile() as file:
            @contextmanager
            def lock(path):
                active.append(True)
                try:
                    yield file.fileno()
                finally:
                    active.pop()
            def emit(name, **fields):
                events.append((name, fields, bool(active)))
            def wait(*args):
                self.assertTrue(active)
                self.assertEqual(['lock_held'], [e[0] for e in events])
                if interrupted:
                    raise KeyboardInterrupt
                return ([0] if ready else [], [], [])
            with patch.object(synthetic.select, 'select', side_effect=wait), \
                    patch.object(synthetic.os, 'read', return_value=command), \
                    patch.object(synthetic.time, 'monotonic', side_effect=[0, elapsed]):
                try:
                    synthetic.controlled_lock(lock, Path('unused'), emit)
                except BaseException as error:
                    return events, error
        return events, None

    def test_more_than_four_seconds_remains_locked_until_explicit_release(self):
        events, error = self.exercise(elapsed=25)
        self.assertIsNone(error)
        self.assertEqual(['lock_held', 'lock_releasing', 'lock_released'], [e[0] for e in events])
        self.assertEqual([True, True, False], [e[2] for e in events])
        self.assertEqual('driver_release', events[-1][1]['reason'])
        self.assertIn('st_ino', events[0][1])

    def test_watchdog_fails_even_if_release_arrives_after_deadline(self):
        for ready in (False, True):
            events, error = self.exercise(elapsed=151, ready=ready)
            self.assertIsInstance(error, guard.Refused)
            self.assertIn('watchdog', str(error))
            self.assertEqual('watchdog', events[-1][1]['reason'])

    def test_driver_eof_invalid_command_and_interrupt_release_with_failure(self):
        for options in (dict(command=b''), dict(command=b'invalid'), dict(interrupted=True)):
            events, error = self.exercise(**options)
            self.assertIsNotNone(error)
            self.assertFalse(events[-1][2])
            self.assertNotEqual('driver_release', events[-1][1]['reason'])

    def test_cleanup_closes_channel_and_escalates_with_finite_waits(self):
        process = MagicMock(pid=12345)
        process.stdin.close.side_effect = BrokenPipeError
        process.wait.side_effect = [subprocess.TimeoutExpired('holder', 3), subprocess.TimeoutExpired('holder', 3), 0]
        with patch.object(scenarios.os, 'killpg', create=True) as kill, \
                patch.object(scenarios, 'signal', SimpleNamespace(SIGTERM=15, SIGKILL=9)):
            scenarios.finish_holder(process)
        self.assertEqual([(12345, 15), (12345, 9)], [c.args for c in kill.call_args_list])
        self.assertTrue(all(c.kwargs == dict(timeout=3) for c in process.wait.call_args_list))


@unittest.skipUnless(os.name == 'posix', 'real flock/pipe checks require POSIX')
class RealLockTests(unittest.TestCase):
    def test_pipe_permission_controls_real_lock_and_allows_work_after_release(self):
        import fcntl
        import select
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)/'deploy.lock'
            path.touch(mode=0o600)
            code = '''import sys,os,json,fcntl
from contextlib import contextmanager
sys.path.insert(0,sys.argv[1])
import synthetic
@contextmanager
def lock(path):
    with open(path,'r+b') as f:
        fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)
        yield f.fileno()
def emit(name,**fields): print(json.dumps(dict(event=name,**fields)),flush=True)
synthetic.controlled_lock(lock,sys.argv[2],emit,watchdog=5)
'''
            process = subprocess.Popen([sys.executable,'-c',code,str(Path(synthetic.__file__).parent),str(path)],
                stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,start_new_session=True)
            try:
                self.assertTrue(select.select([process.stdout],[],[],3)[0], 'holder did not acquire lock')
                held = json.loads(process.stdout.readline())
                lab = SimpleNamespace(case=Path(temp), seen=lambda name: [])
                # Probe the same inode at the scenario's fixed relative path.
                (Path(temp)/'locks').mkdir()
                os.link(path,Path(temp)/'locks/deploy.lock')
                with patch.object(synthetic,'event'):
                    scenarios.confirm_deployment_lock(lab,process,held)
                    process.stdin.write(b'release\n'); process.stdin.flush()
                    out, err = process.communicate(timeout=3)
                    self.assertEqual(0,process.returncode,err)
                    self.assertIn(b'lock_released',out)
                    with self.assertRaisesRegex(AssertionError,'prematurely'):
                        scenarios.confirm_deployment_lock(lab,process,held)
                with path.open('r+b') as file:
                    fcntl.flock(file,fcntl.LOCK_EX|fcntl.LOCK_NB)  # Work can proceed.
            finally:
                if process.stdin is not None and not process.stdin.closed:
                    scenarios.finish_holder(process)
                if process.poll() is None:
                    process.kill(); process.wait(timeout=3)

    def test_live_process_without_the_lock_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'locks/deploy.lock'
            path.parent.mkdir(); path.touch(mode=0o600)
            info=path.stat()
            lab=SimpleNamespace(case=Path(temp),seen=lambda name: [])
            process=MagicMock(); process.poll.return_value=None
            with self.assertRaisesRegex(AssertionError,'released lock prematurely'):
                scenarios.confirm_deployment_lock(lab,process,dict(st_dev=info.st_dev,st_ino=info.st_ino))
