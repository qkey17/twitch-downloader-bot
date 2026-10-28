"""Host-side state machine/protocol tests; no QEMU, systemctl or network calls."""
import base64
import hashlib
import json
from pathlib import Path
import socket
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).with_name('systemd_harness')))
import guard
import iso
import release
import runner
import update
import wire


class ProtocolTests(unittest.TestCase):
    def test_reject_traversal_absolute_paths_and_unknown_artifacts(self):
        for name in ('/etc/passwd', 'suite/../etc/passwd', 'suite\\reports\\x.json',
                     'suite/reports/.hidden', 'suite/environment.env', 'suite/cases/not-uuid/x.json'):
            with self.subTest(name=name), self.assertRaises(ValueError):
                wire.valid_name(name)

    def test_duplicate_fields_and_large_frames_refused(self):
        for line in (b'{"type":"x","type":"y"}\n', b'[]\n', b'{}', b'a'*(wire.MAX_LINE+1)):
            with self.subTest(size=len(line)), self.assertRaises(ValueError):
                wire.decode(line)

    def test_checksum_size_and_no_overwrite(self):
        with tempfile.TemporaryDirectory() as temp:
            receive = wire.Receiver(Path(temp))
            data = b'synthetic report'
            manifest = dict(type='file_begin', name='suite/reports/result.json', size=len(data),
                            sha256=hashlib.sha256(data).hexdigest())
            receive.accept(manifest)
            receive.accept(dict(type='file_chunk', data=base64.b64encode(data).decode()))
            receive.accept(dict(type='file_end'))
            self.assertEqual(data, (Path(temp)/manifest['name']).read_bytes())
            with self.assertRaises(ValueError): receive.accept(manifest)
            manifest['name'] = 'suite/reports/corrupt.json'
            receive.accept(manifest)
            receive.accept(dict(type='file_chunk', data=base64.b64encode(b'x'*len(data)).decode()))
            with self.assertRaises(ValueError): receive.accept(dict(type='file_end'))
            receive.close()

    def test_artifact_budget_and_nested_files_refused(self):
        with tempfile.TemporaryDirectory() as temp:
            receive = wire.Receiver(Path(temp))
            message = dict(type='file_begin', name='suite/reports/x.json', size=wire.MAX_FILE+1, sha256='a'*64)
            with self.assertRaises(ValueError): receive.accept(message)
            message['size'] = 0
            receive.accept(message)
            with self.assertRaises(ValueError): receive.accept(message)
            receive.close()


class RunnerTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.config = dict(run_id='11111111-1111-1111-1111-111111111111', base=release.BASE, harness='a'*40)
        self.session = runner.Session(self.root, self.config, 1000)
        self.addCleanup(self.session.receiver.close)

    def test_qemu_is_offline_and_second_boot_uses_identical_disk(self):
        args = [runner.qemu_args('qemu', self.root, self.root/'payload.iso', self.root/'seed.iso', 1234,
                                 self.config['run_id'], 'tcg', index) for index in (1, 2)]
        for command in args:
            self.assertEqual('none', command[command.index('-nic')+1])
            self.assertEqual('none', command[command.index('-display')+1])
            self.assertIn('host=127.0.0.1', ' '.join(command))
            for forbidden in ('-virtfs', '-fsdev', '-netdev', '-enable-kvm', '-qmp'):
                self.assertNotIn(forbidden, command)
        disks = [[arg for arg in command if 'guest.qcow2' in arg] for command in args]
        self.assertEqual(disks[0], disks[1])

    def test_only_owned_qemu_process_is_killed_for_power_loss(self):
        process = MagicMock()
        process.poll.return_value = None
        process.returncode = -9
        self.session.process = process
        self.session.stop(powercut=True)
        process.kill.assert_called_once_with()
        process.wait.assert_called_once_with(timeout=15)
        self.assertEqual(-9, self.session.summary['powercut']['returncode'])

    def test_dead_qemu_is_not_reported_as_controlled_power_loss(self):
        process = MagicMock()
        process.poll.return_value = 1
        self.session.process = process
        self.session.stop(powercut=True)
        process.kill.assert_not_called()
        self.assertFalse(self.session.summary['powercut'])

    def test_global_timeout_and_wrong_session_are_failures(self):
        self.session.connection = MagicMock()
        with patch.object(runner.time, 'monotonic', return_value=1001), self.assertRaises(TimeoutError):
            self.session.message()
        self.session.buffer = bytearray(wire.encode(dict(type='hello', run_id='foreign')))
        with self.assertRaises(ValueError): self.session.message()

    def test_suite_requires_all_cases_cleanup_and_exact_version(self):
        report = dict(base=release.BASE, harness='a'*40, cleanup='PASS',
                      kind='REAL_SYSTEMD_WITH_SYNTHETIC_TOOLS',
                      cases=[dict(name=name, status='PASS') for name in wire.EXPECTED])
        runner.validate_suite(report, self.config)
        for key, bad in (('base', 'wrong'), ('harness', 'wrong'), ('cleanup', 'FAIL'), ('cases', [])):
            with self.subTest(key=key), self.assertRaises(ValueError):
                runner.validate_suite(report | {key: bad}, self.config)

    def test_no_success_without_real_new_boot_report(self):
        self.session.message = lambda: dict(type='complete', success=True)
        with self.assertRaises(ValueError): self.session.consume(2)
        self.assertEqual('FAIL', self.session.summary['status'])

    def test_error_always_stops_qemu_and_writes_failed_report(self):
        self.session.process = MagicMock()
        self.session.process.poll.side_effect = [None, 1]
        self.session.finish()
        self.assertEqual('FAIL', json.loads((self.root/'summary.json').read_text())['status'])
        self.session.process.kill.assert_called_once()

    def test_seed_is_offline_and_has_no_credentials_or_package_download(self):
        seed = runner.seed_config(self.config)
        self.assertEqual([], seed['users'])
        self.assertFalse(seed['package_update'])
        self.assertFalse(seed['package_upgrade'])
        self.assertEqual('disabled', seed['network']['config'])
        self.assertNotIn('ssh_authorized_keys', json.dumps(seed))
        self.assertNotIn('Oracle', json.dumps(seed))


class SystemdRegressionTests(unittest.TestCase):
    def test_idle_cold_units_with_empty_job_and_timer_without_pids_are_accepted(self):
        def state(unit):
            value = dict(LoadState='loaded', ActiveState='inactive', Job='', UnitFileState='disabled')
            if unit.endswith('.service'):
                value.update(MainPID='0', ControlPID='0')
            if unit == 'tdb-daily-backup.service': value['UnitFileState'] = 'static'
            return value
        with patch.object(update, 'service_state', side_effect=state), \
                patch.object(update, 'account', return_value=(999,999)), patch.object(Path, 'iterdir', return_value=iter([])):
            update.check_units()

    def test_query_failures_have_specific_diagnostics(self):
        for error in (subprocess.TimeoutExpired('systemctl',15), subprocess.CalledProcessError(1,'systemctl',stderr='bus unavailable')):
            with self.subTest(error=type(error).__name__), patch.object(update.subprocess, 'run', side_effect=error), \
                    self.assertRaisesRegex(guard.Refused, 'tdb.service: systemctl show'):
                update.service_state('tdb.service')

    def test_service_query_explicitly_requests_empty_job(self):
        completed = MagicMock(stdout='LoadState=loaded\nActiveState=inactive\nJob=\nMainPID=0\nControlPID=0\nUnitFileState=disabled\n')
        with patch.object(update.subprocess, 'run', return_value=completed) as run:
            state = update.service_state('tdb.service')
        self.assertEqual('', state['Job'])
        args = run.call_args.args[0]
        self.assertIn('--all', args)
        self.assertTrue(any(arg.startswith('--property=') and 'Job' in arg for arg in args))

    def test_missing_job_does_not_get_silently_treated_as_idle(self):
        state = dict(LoadState='loaded', ActiveState='inactive', MainPID='0', ControlPID='0', UnitFileState='disabled')
        with patch.object(update, 'service_state', return_value=state), self.assertRaisesRegex(guard.Refused, 'Job=None'):
            update.check_units()

    def test_nocloud_iso_has_joliet_descriptor_and_required_names(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)
            for name in ('user-data', 'meta-data'): (path/name).write_text('{}')
            iso.create(path, path/'seed.iso', ['user-data', 'meta-data'], volume='CIDATA')
            content = (path/'seed.iso').read_bytes()
            self.assertEqual(b'CIDATA', content[16*2048+40:16*2048+46])
            self.assertEqual(b'\x02CD001\x01', content[17*2048:17*2048+7])
            self.assertEqual(b'%/E', content[17*2048+88:17*2048+91])
            self.assertIn('user-data;1'.encode('utf-16-be'), content)


if __name__ == '__main__': unittest.main()
