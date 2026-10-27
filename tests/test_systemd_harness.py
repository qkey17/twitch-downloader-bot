"""Host-safe checks ONLY. No systemd, sudo, signals or guest provisioning are run."""
import ast
import configparser
import hashlib
import importlib.util
import json
from pathlib import Path
import struct
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch, MagicMock
import uuid

HARNESS = Path(__file__).with_name('systemd_harness')


def load(name):
    spec = importlib.util.spec_from_file_location(name, HARNESS / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


guard = load('guard')
host = load('host')
guest = load('guest')
synthetic = load('synthetic')
scenarios = load('scenarios')


def evidence():
    return dict(manufacturer='TDB-Harness', product='TDB-Isolated-VM', machine='1' * 32,
                pid1='systemd', namespace=True, root_device=True, root_type='ext4',
                shared=False, container=False, network=['lo'], production=False,
                ubuntu='ID=ubuntu\nVERSION_ID="24.04"\n', python=(3, 12))


class IsolationTests(unittest.TestCase):
    def test_pure_valid_guest_predicate(self):
        guard.validate(evidence())

    def test_each_unsafe_boundary_is_refused(self):
        wrong = dict(manufacturer='Oracle', product='other', machine='bad', pid1='python',
                     namespace=False, root_device=False, root_type='overlay', shared=True,
                     container=True, network=['eth0', 'lo'], production=True,
                     ubuntu='ID=ubuntu\nVERSION_ID="22.04"', python=(3, 14))
        for key, value in wrong.items():
            with self.subTest(boundary=key), self.assertRaises(guard.Refused):
                guard.validate(evidence() | {key: value})

    def test_windows_refused_before_any_probe(self):
        with patch.object(guard.sys, 'platform', 'win32'), patch.object(guard.Path, 'read_text') as read:
            with self.assertRaisesRegex(guard.Refused, 'isolated Linux'):
                guard.check(provision=True)
            read.assert_not_called()

    def test_reject_host_bus_peer_even_with_vm_markers(self):
        peer = MagicMock()
        peer.__enter__.return_value = peer
        peer.getsockopt.return_value = struct.pack('3i', 987, 0, 0)
        with patch.object(guard, 'evidence', return_value=evidence()), \
                patch.object(guard, 'no_links'), \
                patch.object(guard.socket, 'AF_UNIX', 1, create=True), \
                patch.object(guard.socket, 'SO_PEERCRED', 17, create=True), \
                patch.object(guard.os, 'geteuid', return_value=0, create=True), \
                patch.object(guard.socket, 'socket', return_value=peer):
            with self.assertRaisesRegex(guard.Refused, 'guest PID 1'):
                guard.check(provision=True)

    def test_dangerous_entrypoints_do_nothing_when_guard_rejects(self):
        with patch.object(guard, 'check', side_effect=guard.Refused('outside VM')), \
                patch.object(guest, 'command') as command, patch.object(guest.Path, 'mkdir') as mkdir:
            for operation in (guest.provision, guest.cleanup, scenarios.run, scenarios.stage_powercut, scenarios.resume):
                with self.subTest(operation=operation.__name__), self.assertRaises(guard.Refused):
                    operation()
            command.assert_not_called()
            mkdir.assert_not_called()

    def test_owned_rejects_traversal_and_foreign_path(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(guard, 'ROOT', Path(directory)):
            self.assertEqual(guard.owned(Path(directory) / 'case'), Path(directory) / 'case')
            for path in (Path(directory), Path(directory) / '..' / 'foreign', Path(directory).parent / 'foreign'):
                with self.subTest(path=path), self.assertRaises(guard.Refused):
                    guard.owned(path)

    def test_symlink_boundary_refused_without_following_it(self):
        # Synthetic lstat result; no Windows symlink privilege needed.
        with patch.object(guard.Path, 'is_symlink', return_value=True):
            with self.assertRaisesRegex(guard.Refused, 'symlink'):
                guard.no_links(Path.cwd() / 'synthetic-link')

    def test_no_controller_source_modification_or_supervision_bypass_in_adapter(self):
        text = (HARNESS / 'synthetic.py').read_text()
        self.assertNotIn('ensure_supervised =', text)
        self.assertNotIn('Runtime.command =', text)
        self.assertNotIn('Runtime.start =', text)
        self.assertNotIn('Runtime.stop =', text)
        self.assertIn('module.Runtime.service = service', text)
        self.assertIn('return original(self)', text)


class BundleAndLauncherTests(unittest.TestCase):
    def test_bundle_exports_exact_allowlist_from_audited_commit(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'bundle'
            with patch.object(host.subprocess, 'run', return_value=SimpleNamespace(stdout=b'# synthetic source\n')) as run:
                host.bundle(target)
            manifest = json.loads((target / 'bundle.json').read_text())
            self.assertEqual(manifest['base'], host.BASE)
            self.assertEqual(set(manifest['sha256']), {*host.SOURCE, *host.HARNESS})
            for name, digest in manifest['sha256'].items():
                self.assertEqual(hashlib.sha256((target / name).read_bytes()).hexdigest(), digest)
            self.assertEqual([c.args[0] for c in run.call_args_list],
                             [['git', 'show', f'{host.BASE}:{name}'] for name in host.SOURCE])
            self.assertFalse((target / '.env').exists())
            self.assertFalse((target / '.git').exists())
            with self.assertRaises(FileExistsError):
                host.bundle(target)

    def test_qemu_plan_has_no_network_or_host_passthrough(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'guest.qcow2').write_bytes(b'synthetic disk, NOT a real VM image')
            (root / 'vm.json').write_text(json.dumps(dict(uuid=str(uuid.uuid4()))))
            iso = root / 'test.iso'
            iso.write_bytes(b'synthetic read-only ISO')
            args = host.qemu_argv(root, iso)
            self.assertEqual(args[args.index('-nic') + 1], 'none')
            self.assertEqual(args[args.index('-m') + 1], '954')
            self.assertIn('-nodefaults', args)
            self.assertIn('-no-reboot', args)
            self.assertTrue(any('readonly=on' in arg and str(iso) in arg for arg in args))
            self.assertNotIn('-boot', args)
            self.assertIn('-boot', host.qemu_argv(root, iso, install=True))
            for forbidden in ('-virtfs', '-fsdev', '-netdev', '-chardev', '-qmp', '-vnc', '-enable-kvm'):
                self.assertNotIn(forbidden, args)
            self.assertNotIn('/run/systemd', ' '.join(args))
            self.assertNotIn('-display', host.qemu_argv(root, gui=True))
            self.assertIn('VGA', host.qemu_argv(root, gui=True))

    def test_qemu_option_injection_paths_rejected(self):
        for name in ('disk,readonly=off', 'disk\n-virtfs', 'disk\r-serial'):
            with self.subTest(name=name), self.assertRaises(ValueError):
                host.safe_path(Path.cwd() / name)

    def test_missing_vm_is_not_launched(self):
        with tempfile.TemporaryDirectory() as directory, self.assertRaises(FileNotFoundError):
            host.qemu_argv(Path(directory))

    def test_vm_host_refuses_production_paths(self):
        with patch.object(host.Path, 'exists', return_value=True):
            with self.assertRaisesRegex(ValueError, 'production paths'):
                host.check_host()

    def test_vm_host_refuses_insufficient_linux_memory(self):
        with patch.object(host.sys, 'platform', 'linux'), patch.object(host.Path, 'exists', return_value=False), \
                patch.object(host.Path, 'read_text', return_value='MemAvailable: 954000 kB\n'):
            with self.assertRaisesRegex(ValueError, 'available RAM'):
                host.check_host()


class ConfigurationAndEvidenceTests(unittest.TestCase):
    def test_test_units_keep_real_supervision_and_graceful_stop(self):
        units = guest.unit_texts()
        self.assertEqual(set(units), set(guard.UNITS))
        parsed = {}
        for name, text in units.items():
            parser = configparser.ConfigParser(interpolation=None, strict=False)
            parser.optionxform = str
            parser.read_string(text)
            parsed[name] = parser
            self.assertNotIn('/home/ubuntu', text)
            if name.endswith('.service'):
                self.assertEqual(parser['Service']['User'], 'tdbh')
                self.assertEqual(parser['Service']['KillMode'], 'control-group')
                self.assertEqual(parser['Service']['MemoryMax'], '160M')
        bot = parsed['tdb.service']['Service']
        self.assertEqual(bot['TimeoutStopSec'], 'infinity')
        self.assertEqual(bot['SendSIGKILL'], 'no')
        self.assertEqual(bot['RuntimeDirectoryPreserve'], 'restart')
        self.assertEqual(bot['Restart'], 'on-failure')
        daily = parsed['tdb-daily-backup.service']['Service']
        recover = parsed['tdb-backup-recover.service']['Service']
        self.assertEqual(daily['Type'], 'oneshot')
        self.assertEqual(daily['SendSIGKILL'], 'yes')
        self.assertEqual(daily['ExecStopPost'], recover['ExecStart'])
        self.assertEqual(parsed['tdb-daily-backup.timer']['Timer']['Persistent'], 'true')

    def test_python312_grammar_for_all_new_harness_modules(self):
        for path in HARNESS.glob('*.py'):
            with self.subTest(path=path.name):
                ast.parse(path.read_text(encoding='utf-8'), feature_version=(3, 12))

    def test_all_scenario_groups_are_named_and_reboot_is_explicit(self):
        groups = {int(name[:2]) for name, _ in scenarios.CASES} | {14}
        self.assertEqual(groups, set(range(1, 17)))
        self.assertTrue(callable(scenarios.stage_powercut))
        self.assertTrue(callable(scenarios.resume))

    def test_event_order_rejects_early_offsite_and_wrong_process(self):
        lab = scenarios.Lab.__new__(scenarios.Lab)
        names = ('prepare', 'bot_stopping', 'bot_stopped', 'snapshot_begin', 'snapshot_done',
                 'verify_done', 'bot_ready', 'offsite_begin', 'offsite_done')
        events = [dict(event=n, mono=float(i), pid=42, main_pid='42') for i, n in enumerate(names)]
        lab.events = lambda: events
        lab.assert_order()
        events[-1]['main_pid'] = '43'
        with self.assertRaisesRegex(AssertionError, 'replacement process'):
            lab.assert_order()
        events[-1]['main_pid'] = '42'
        events[3], events[-2] = events[-2], events[3]
        with self.assertRaisesRegex(AssertionError, 'order'):
            lab.assert_order()

    def test_corrupt_synthetic_snapshot_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(synthetic.guard, 'owned', side_effect=lambda p: Path(p)):
                with synthetic.closing(synthetic.sqlite3.connect(root / 'vod_history.db')) as db:
                    db.execute('CREATE TABLE only_synthetic (id INTEGER)')
                (root / 'result.html').write_text('<p>synthetic</p>')
                synthetic.seal(root)
                synthetic.verify(root)
                (root / 'result.html').write_text('<p>changed</p>')
                with self.assertRaisesRegex(guard.Refused, 'hash mismatch'):
                    synthetic.verify(root)


if __name__ == '__main__':
    unittest.main()
