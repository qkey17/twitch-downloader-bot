"""Synthetic files and manager responses only; never accesses a VM or real units."""
from contextlib import ExitStack
import copy
import hashlib
import importlib.util
import io
import json
import os
import stat
from pathlib import Path
import struct
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).with_name('systemd_harness')))
import guard
import guest
import host
import iso
import release
import scenarios
import update


class UpdateTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.installed = self.root / 'installed'
        self.installed.mkdir()
        self.payload = self.root / 'payload'
        self.payload.mkdir()
        self.stack.enter_context(patch.object(update, 'ROOT', self.installed))
        self.stack.enter_context(patch.object(guard, 'check', return_value={}))
        self.stack.enter_context(patch.object(guard.os, 'geteuid', return_value=0, create=True))
        self.stack.enter_context(patch.object(update, 'check_units'))
        self.stack.enter_context(patch.object(update, 'check_directories'))
        # Windows cannot enact POSIX chown/mode. Simulate only this boundary;
        # real bytes, checksums, fsync, atomic replaces, journals and rollback run.
        meta = update.attributes(self.root.stat()) | dict(mode=0o666)
        self.stack.enter_context(patch.object(update, 'expected_attributes', return_value=meta))
        self.stack.enter_context(patch.object(update, 'set_attributes'))
        if os.name == 'nt':
            # Windows stat/fstat can report different legacy ctime semantics.
            # The Linux implementation retains the ctime mutation fence.
            real_stamp = update.stamp
            self.stack.enter_context(patch.object(update, 'stamp', side_effect=lambda info: real_stamp(info)[:-1]))
        self.old_bytes = {name: ('# SYNTHETIC OLD ' + name + '\n').encode() for name in release.OLD_HASHES}
        self.new_bytes = {name: ('# SYNTHETIC NEW ' + name + '\n').encode() for name in (*release.SOURCE, *release.HARNESS)}
        self.stack.enter_context(patch.dict(release.OLD_HASHES,
            {p: [update.digest(b)] for p, b in self.old_bytes.items()}, clear=True))
        self.stack.enter_context(patch.dict(release.SOURCE_HASHES,
            {p: update.digest(self.new_bytes[p]) for p in release.SOURCE}, clear=True))
        for name, data in self.old_bytes.items():
            path = self.installed / 'code' / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        (self.installed / 'admin').mkdir()
        (self.installed / 'admin/daily_backup.py').write_bytes(self.old_bytes['tools/daily_backup.py'])
        for name, data in update.wrappers().items():
            (self.installed / name).write_bytes(data)
        (self.installed / 'guest.json').write_bytes(b'{"base":"original-test-attestation"}')
        self.attested = (self.installed / 'guest.json').read_bytes()
        self.stack.enter_context(patch.object(update, 'attestation', side_effect=lambda:
            (update.digest((self.installed / 'guest.json').read_bytes()), dict(base=release.OLD_BASE))))
        self.manifest = dict(base=release.BASE, old_base=release.OLD_BASE, old_harness=release.OLD_HARNESS,
                             harness='a'*40, sha256={p: update.digest(b) for p, b in self.new_bytes.items()})
        for name, data in self.new_bytes.items():
            path = self.payload / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        self.save_manifest()
        self.evidence = {}
        for name in ('cases/old/vod.db', 'cases/old/result.html', 'report-previous.json', 'current.json'):
            path = self.installed / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(('SYNTHETIC EVIDENCE ' + name).encode())
            self.evidence[name] = path.read_bytes()

    def save_manifest(self):
        (self.payload / 'bundle.json').write_text(json.dumps(self.manifest))

    def unchanged_evidence(self):
        self.assertEqual(self.attested, (self.installed / 'guest.json').read_bytes())
        for name, data in self.evidence.items():
            self.assertEqual(data, (self.installed / name).read_bytes())

    def old_restored(self):
        for name, data in self.old_bytes.items():
            self.assertEqual(data, (self.installed / 'code' / name).read_bytes())
        self.assertEqual(self.old_bytes['tools/daily_backup.py'], (self.installed / 'admin/daily_backup.py').read_bytes())
        for name in ('code/release.py', 'code/update.py', 'version.json'):
            self.assertFalse((self.installed / name).exists())
        self.unchanged_evidence()

    def test_update_both_controllers_and_verify_version_without_touching_evidence(self):
        version = update.apply(self.payload)
        self.assertEqual(release.BASE, version['base'])
        self.assertEqual('upgrade', version['origin'])
        for name in ('code/tools/daily_backup.py', 'admin/daily_backup.py'):
            self.assertEqual(self.new_bytes['tools/daily_backup.py'], (self.installed / name).read_bytes())
        self.assertEqual(version, update.verify())
        self.assertEqual('committed', update.journal()[1]['state'])
        self.assertEqual('committed', json.loads((self.installed / 'update-status.json').read_text())['state'])
        self.unchanged_evidence()

    def test_wrong_source_sha_refused_before_replacement(self):
        self.manifest['base'] = release.OLD_BASE
        self.save_manifest()
        with self.assertRaises(guard.Refused):
            update.apply(self.payload)
        self.old_restored()

    def test_corrupt_transfer_and_self_consistent_wrong_source_are_both_refused(self):
        path = self.payload / 'tools/daily_backup.py'
        path.write_bytes(b'# CORRUPTED SOURCE')
        for consistent_manifest in (False, True):
            with self.subTest(consistent_manifest=consistent_manifest):
                if consistent_manifest:
                    self.manifest['sha256']['tools/daily_backup.py'] = update.digest(path.read_bytes())
                    self.save_manifest()
                with self.assertRaises(guard.Refused):
                    update.apply(self.payload)
                self.old_restored()

    def test_changed_original_source_or_unknown_file_refused(self):
        original = self.installed / 'code/tools/daily_backup.py'
        original.write_bytes(b'# MODIFIED ORIGINAL')
        with self.assertRaises(guard.Refused):
            update.apply(self.payload)
        original.write_bytes(self.old_bytes['tools/daily_backup.py'])
        (self.installed / 'code/unknown.py').write_text('# unknown')
        with self.assertRaises(guard.Refused):
            update.apply(self.payload)
        self.assertFalse((self.installed / 'updates').exists())
        self.unchanged_evidence()

    def test_modified_admin_copy_is_refused_independently(self):
        (self.installed / 'admin/daily_backup.py').write_text('# changed')
        with self.assertRaises(guard.Refused):
            update.apply(self.payload)
        self.assertFalse((self.installed / 'updates').exists())

    def test_payload_traversal_and_symlinks_are_refused(self):
        self.manifest['sha256']['../outside.py'] = 'f'*64
        self.save_manifest()
        with self.assertRaises(guard.Refused):
            update.apply(self.payload)
        self.manifest['sha256'].pop('../outside.py')
        self.save_manifest()
        real_links = Path.is_symlink
        with patch.object(Path, 'is_symlink', lambda p: p.name == 'scenarios.py' or real_links(p)):
            with self.assertRaises(guard.Refused):
                update.apply(self.payload)
        self.old_restored()

    def test_copy_error_rolls_back_all_files(self):
        original = update.atomic
        failed = []
        def copy_file(path, data, meta):
            if path == self.installed / 'admin/daily_backup.py' and not failed:
                failed.append(True)
                raise OSError('synthetic ENOSPC')
            return original(path, data, meta)
        with patch.object(update, 'atomic', side_effect=copy_file):
            with self.assertRaises(OSError):
                update.apply(self.payload)
        self.assertEqual('rolled_back', update.journal()[1]['state'])
        self.old_restored()

    def interrupt_update(self):
        class PowerCut(BaseException):
            pass
        original = update.atomic
        def copy_file(path, data, meta):
            if path == self.installed / 'admin/daily_backup.py':
                raise OSError('synthetic interrupted copy')
            return original(path, data, meta)
        with patch.object(update, 'atomic', side_effect=copy_file), \
                patch.object(update, 'recover', side_effect=PowerCut):
            with self.assertRaises(PowerCut):
                update.apply(self.payload)

    def test_power_loss_blocks_new_reports_and_durable_recovery_is_repeatable(self):
        self.interrupt_update()
        self.assertEqual('applying', update.journal()[1]['state'])
        with self.assertRaises(guard.Refused):
            update.verify()
        with patch.object(guest, 'command') as command:
            with self.assertRaises(guard.Refused):
                scenarios.run()
            command.assert_not_called()
        update.recover()
        update.recover()
        self.old_restored()
        self.assertTrue((update.journal()[0] / 'update.py').exists())

    def test_damaged_recovery_copy_or_unknown_target_stays_blocked(self):
        self.interrupt_update()
        tx, journal = update.journal()
        item = journal['files']['code/tools/daily_backup.py']
        (tx / f'{item["index"]}.old').write_bytes(b'DAMAGED')
        with self.assertRaises(guard.Refused):
            update.recover()
        self.assertEqual('applying', update.journal()[1]['state'])
        with self.assertRaises(guard.Refused):
            update.verify()
        self.unchanged_evidence()

    def test_interrupted_rollback_keeps_new_guard_until_every_old_file_is_restored(self):
        self.interrupt_update()
        original = update.atomic
        def restore(path, data, meta):
            if path == self.installed / 'code/guest.py':
                raise OSError('synthetic second power loss')
            return original(path, data, meta)
        with patch.object(update, 'atomic', side_effect=restore), self.assertRaises(OSError):
            update.recover()
        self.assertEqual(self.new_bytes['guard.py'], (self.installed / 'code/guard.py').read_bytes())
        self.assertEqual('applying', update.journal()[1]['state'])
        update.recover()
        self.old_restored()

    def test_report_uses_verified_installation_metadata(self):
        version = update.apply(self.payload)
        reports = []
        with patch.object(scenarios, 'CASES', ()), patch.object(scenarios, 'datetime') as clock, \
                patch.object(guest, 'command', return_value=SimpleNamespace(stdout='systemd 255\n')), \
                patch.object(guest, 'cleanup'), patch.object(guest, 'private_write',
                    side_effect=lambda path, value, **kw: reports.append(copy.deepcopy(value))):
            clock.now.return_value.hour = 10
            scenarios.run()
        self.assertEqual(version['base'], reports[-1]['base'])
        self.assertEqual(version['harness'], reports[-1]['harness'])
        self.assertEqual(version['attestation_sha256'], reports[-1]['attestation_sha256'])

    def test_tampered_installed_copy_or_version_receipt_cannot_claim_success(self):
        update.apply(self.payload)
        (self.installed / 'admin/daily_backup.py').write_text('# changed')
        with self.assertRaises(guard.Refused):
            update.verify()
        self.unchanged_evidence()


class BoundaryTests(unittest.TestCase):
    def test_upgrading_never_bypasses_isolation(self):
        with patch.object(guard, 'evidence', return_value=dict(manufacturer='Oracle', product='production')):
            with self.assertRaises(guard.Refused):
                guard.check(upgrading=True)

    def test_incomplete_journal_fences_normal_commands(self):
        for state in ('applying', 'committed', 'rolled_back'):
            with self.subTest(state=state), patch.object(guard, 'no_links'), \
                    patch.object(Path, 'exists', return_value=True), \
                    patch.object(Path, 'stat', return_value=SimpleNamespace(st_mode=stat.S_IFREG | 0o644,
                        st_uid=0, st_nlink=1, st_size=100)), \
                    patch.object(Path, 'read_text', return_value=json.dumps(dict(id='a'*32, state=state))) as read:
                if state == 'applying':
                    with self.assertRaisesRegex(guard.Refused, 'upgrade incomplete'):
                        guard.upgrade_fence()
                else:
                    guard.upgrade_fence()
                self.assertEqual(1, read.call_count)  # No private transaction reads by tdbh.

    def test_unprivileged_process_outside_test_units_is_also_refused(self):
        state = dict(LoadState='loaded', ActiveState='inactive', Job='0', MainPID='0', ControlPID='0', UnitFileState='disabled')
        process = MagicMock()
        process.name = '424242'
        process.__truediv__.return_value.read_text.return_value = 'Uid:\t999\t999\t999\t999\n'
        process.__truediv__.return_value.read_bytes.return_value = b'python3\0synthetic.py\0bot\0'
        with patch.object(update, 'service_state', return_value=state), \
                patch.object(update, 'account', return_value=(999,999)), \
                patch.object(Path, 'iterdir', return_value=iter([process])), self.assertRaisesRegex(guard.Refused, 'process'):
            update.check_units()

    def test_unknown_directory_permissions_refused(self):
        with patch.object(update, 'account', return_value=(123,456)), patch.object(guard, 'no_links'), \
                patch.object(Path, 'stat', return_value=SimpleNamespace(st_mode=stat.S_IFDIR | 0o777,
                    st_uid=0, st_gid=0)), self.assertRaises(guard.Refused):
            update.check_directories()

    def test_refusal_outside_vm_precedes_payload_or_manager_access(self):
        with patch.object(guard, 'check', side_effect=guard.Refused('outside VM')), \
                patch.object(update, 'read_file') as read, patch.object(guest, 'show') as show:
            for operation in (lambda: update.apply(Path.cwd()), update.verify, update.recover):
                with self.assertRaises(guard.Refused):
                    operation()
            read.assert_not_called()
            show.assert_not_called()

    def test_active_unknown_and_pending_jobs_refused(self):
        base = dict(LoadState='loaded', ActiveState='inactive', Job='0', MainPID='0', ControlPID='0', UnitFileState='disabled')
        for change in (dict(ActiveState='active'), dict(ActiveState='activating'), dict(Job='17'),
                       dict(MainPID='42'), dict(ControlPID='43'), dict(LoadState='not-found'), dict(UnitFileState='enabled')):
            with self.subTest(change=change), patch.object(update, 'service_state', return_value=base | change), \
                    self.assertRaises(guard.Refused):
                update.check_units()

    def test_expected_posix_owners_and_modes(self):
        with patch.object(update, 'account', return_value=(123, 456)):
            self.assertEqual(dict(mode=0o600, uid=123, gid=456), update.expected_attributes('admin/daily_backup.py'))
            self.assertEqual(dict(mode=0o644, uid=0, gid=0), update.expected_attributes('code/tools/daily_backup.py'))

    def test_dirty_harness_bundle_is_refused(self):
        with patch.object(host, 'git_bytes', side_effect=[b'a'*40, b'modified']), \
                patch.object(Path, 'mkdir') as mkdir:
            with self.assertRaises(ValueError):
                host.bundle(Path.cwd() / 'not-created')
            mkdir.assert_not_called()

    def test_iso_directory_extents_round_trip_and_no_overwrite(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            names = ['bundle.json', 'scenarios.py', 'tools/daily_backup.py', 'src/maintenance.py']
            content = {name: ('SYNTHETIC ISO ' + name).encode() * 200 for name in names}
            for name, data in content.items():
                path = root / name
                path.parent.mkdir(exist_ok=True)
                path.write_bytes(data)
            target = root / 'update.iso'
            iso.create(root, target, names)
            image = target.read_bytes()
            self.assertEqual(b'\x01CD001\x01', image[16*2048:16*2048+7])
            def entries(extent):
                result = {}
                offset = extent*2048
                end = offset+2048
                while offset < end and image[offset]:
                    length = image[offset]
                    name_len = image[offset+32]
                    name = image[offset+33:offset+33+name_len]
                    result[name] = struct.unpack_from('<I',image,offset+2)[0], struct.unpack_from('<I',image,offset+10)[0]
                    offset += length
                return result
            root_entries = entries(20)
            for name, data in content.items():
                path = Path(name)
                table = root_entries if path.parent == Path('.') else entries(root_entries[str(path.parent).upper().encode()][0])
                extent, length = table[(path.name.upper()+';1').encode()]
                self.assertEqual(data, image[extent*2048:extent*2048+length])
            with self.assertRaises(FileExistsError):
                iso.create(root, target, names)


if __name__ == '__main__':
    unittest.main()
