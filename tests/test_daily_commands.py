"""Real bounded children/watchdog; no manager, credentials or production data."""
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from tools import daily_backup as daily


class CommandWatchdogTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).absolute()
        self.runtime = daily.Runtime(SimpleNamespace(state_dir=self.root, admin_dir=self.root))

    def exercise(self, cancel=False, group=False, inspect=None):
        errors, children, stops = [], [], []
        before = set(threading.enumerate())
        real_popen = subprocess.Popen
        real_stop = daily.offsite.External._stop
        def spawn(*args, **kwargs):
            child = real_popen(*args, **kwargs)
            children.append(child)
            return child
        def stop(child):
            if child.poll() is None:
                stops.append(child.pid)
                if inspect:
                    inspect(child)
            real_stop(child)
        # Self-bounded even with the original broken watchdog. Group child
        # ignores TERM, forcing the real two-second KILL escalation on POSIX.
        code = 'import time; time.sleep(6)'
        ready = self.root/'group-ready'
        if group:
            ready.unlink(missing_ok=True)
            code = ("import subprocess,sys,time,signal; signal.signal(signal.SIGTERM,signal.SIG_IGN); "
                    "p=subprocess.Popen([sys.executable,'-c',"
                    "'import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);print(1,flush=True);time.sleep(6)'],stdout=subprocess.PIPE); "
                    "p.stdout.readline(); "
                    f"open({str(ready)!r},'w').write(str(p.pid)); "
                    "p.wait()")
        def request_group_stop():
            end = time.monotonic()+15
            while not ready.exists() and time.monotonic()<end:
                time.sleep(.02)
            if cancel:
                self.runtime.cancel.set()
            else:
                self.runtime.commands.deadline = time.monotonic()+.15
        timer = (threading.Thread(target=request_group_stop) if group else
                 threading.Timer(.7, self.runtime.cancel.set) if cancel else None)
        start = time.monotonic()
        try:
            with patch.object(daily.offsite.subprocess, 'Popen', side_effect=spawn), \
                    patch.object(daily.offsite.External, '_stop', side_effect=stop), \
                    patch.object(threading, 'excepthook', side_effect=lambda arg: errors.append(arg.exc_type)):
                if timer:
                    timer.start()
                with self.assertRaises(daily.DailyError) as caught:
                    self.runtime.command([sys.executable, '-I', '-S', '-c', code],
                                         timeout=20 if group else 10 if cancel else .7)
            self.assertEqual('command_interrupted_or_timed_out', caught.exception.code)
            self.assertLess(time.monotonic()-start, 20 if group else 5)
            self.assertEqual([], errors)
            self.assertEqual(1, len(children))
            self.assertIn(children[0].pid, stops)  # watchdog saw a live child
            self.assertIsNotNone(children[0].returncode)  # wait/reap already completed
            self.assertFalse(set(threading.enumerate())-before-({timer} if timer else set()))
            if os.name == 'posix':
                with self.assertRaises(ChildProcessError):
                    os.waitpid(children[0].pid, os.WNOHANG)
            if group:
                self.assertTrue(ready.exists(), 'grandchild never initialized')
            return children[0]
        finally:
            if timer:
                if not group:
                    timer.cancel()
                timer.join(timeout=16)
            for child in children:
                real_stop(child)
                if child.stdout:
                    child.stdout.close()

    def test_real_deadline_terminates_and_reaps_child(self):
        self.exercise()

    def test_real_cancel_terminates_and_reaps_child(self):
        self.exercise(cancel=True)
        self.runtime.recovery_mode()
        self.assertEqual(b'recovered', self.runtime.command(
            [sys.executable, '-I', '-S', '-c', "print('recovered')"]).strip())

    def test_error_codes_redact_command_output_and_arguments(self):
        with self.assertRaises(daily.DailyError) as caught:
            self.runtime.command([sys.executable, '-c',
                                  "import sys;print('PRIVATE_SENTINEL');sys.exit(7)"])
        self.assertEqual('external_command_failed', caught.exception.code)
        self.assertNotIn('PRIVATE_SENTINEL', str(caught.exception))

    @unittest.skipUnless(os.name == 'posix', 'POSIX flock and process groups required')
    def test_group_kill_keeps_both_controller_locks_until_child_is_reaped(self):
        import fcntl
        deploy = self.root/'deploy.lock'
        deploy.touch(mode=0o600)
        config = SimpleNamespace(state_dir=self.root, deploy_lock=deploy)
        controller = daily.Controller(config, self.runtime)
        def busy(path):
            with path.open('r+b') as handle:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        def inspect(child):
            busy(deploy); busy(self.root/'offsite.lock')
            # Child inherited the very deployment inode, not a new lock file.
            fd = self.runtime.commands.lock_fd
            info = os.stat('/proc/%s/fd/%s' % (child.pid, fd))
            self.assertEqual((deploy.stat().st_dev, deploy.stat().st_ino), (info.st_dev, info.st_ino))
        for cancel in (False, True):
          with self.subTest(cancel=cancel), controller.locked():
            child = self.exercise(cancel=cancel, group=True, inspect=inspect)
            self.runtime.recovery_mode()
            busy(deploy); busy(self.root/'offsite.lock')
            # No live group member may survive. On Linux a reparented killed
            # grandchild can briefly be a zombie (PID1 owns its reaping).
            for status in Path('/proc').glob('[0-9]*/stat'):
                try:
                    fields = status.read_text().rsplit(')', 1)[1].split()
                except (FileNotFoundError, ProcessLookupError):
                    continue
                if int(fields[2]) == child.pid:
                    self.assertEqual('Z', fields[0])
        for path in (deploy, self.root/'offsite.lock'):
            with path.open('r+b') as handle:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)


if __name__ == '__main__':
    unittest.main()
