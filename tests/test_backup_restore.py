"""Administrative backup tests: synthetic SQLite/HTML only, no service control."""
import errno
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import test_result_registry as fixture
from src import html_cleanup as cleanup
from src.database import database as db, db_core, db_results as results
from tools import backup_restore as backup


class BackupRestoreTests(unittest.TestCase):
    setUp_fixture = fixture.ResultRegistryTestCase.setUp
    plan = fixture.ResultRegistryTestCase.plan
    publish = fixture.ResultRegistryTestCase.publish
    ready = fixture.ResultRegistryTestCase.ready
    rows = fixture.ResultRegistryTestCase.rows
    change = fixture.ResultRegistryTestCase.change

    def setUp(self):
        self.setUp_fixture()
        self.guard = self.stack.enter_context(patch.object(backup, '_require_stopped'))
        self.html = self.root/'html'
        self.html.mkdir(exist_ok=True)
        self.database = self.root/'app.sqlite3'

    def create(self, name='snapshot'):
        destination = self.root/name
        backup.create_backup(self.database, [self.html], destination,
                             maintenance_stopped=True, reserve_bytes=0)
        return destination

    def rewrite_manifest(self, directory, modify):
        path = directory/'manifest.json'
        manifest = json.loads(path.read_text(encoding='utf-8'))
        modify(manifest)
        content = json.dumps(manifest).encode('utf-8')
        path.write_bytes(content)
        (directory/'COMPLETE').write_bytes((hashlib.sha256(content).hexdigest()+'\n').encode('ascii'))

    def refresh_db_hash(self, directory):
        content = (directory/'database.sqlite3').read_bytes()
        def update(manifest):
            entry = next(e for e in manifest['files'] if e['path'] == 'database.sqlite3')
            entry.update(size=len(content), sha256=hashlib.sha256(content).hexdigest())
        self.rewrite_manifest(directory, update)

    def symlink(self, target, link, directory=False):
        try:
            link.symlink_to(target, target_is_directory=directory)
        except OSError:
            self.skipTest('Creating symlinks requires OS privileges')

    def test_all_generations_history_unknown_html_and_temp_restore_without_secrets(self):
        plans = [self.ready(str(index)) for index in range(3)]
        db.add_user_history(7, db.get_cache(plans[0].vod_id, 'html_online')['id'])
        unknown = self.html/('chat_'+'f'*32+'.html')
        unknown.write_text('<html>unknown generation</html>', encoding='utf-8')
        temp = self.html/('.chat_'+'e'*32+'.html.'+'d'*32+'.tmp')
        temp.write_text('unproven partial output', encoding='utf-8')
        (self.html/'.env').write_text('PRIVATE_TEST_TOKEN=do-not-copy', encoding='utf-8')
        (self.html/'private.key').write_text('do-not-copy', encoding='utf-8')
        before = self.database.read_bytes()
        directory = self.create()
        self.assertEqual(before, self.database.read_bytes())
        self.assertFalse((directory/'_sqlite_stage').exists())
        self.assertFalse(list(directory.rglob('.env')))
        self.assertFalse(list(directory.rglob('private.key')))
        manifest = backup.verify_backup(directory)
        self.assertEqual(6, len(manifest['files']))
        restored = self.root/'restored'
        backup.restore_backup(directory, restored, reserve_bytes=0)
        self.assertEqual(manifest, backup.verify_backup(restored))
        for entry in manifest['files']:
            self.assertEqual((directory/entry['path']).read_bytes(), (restored/entry['path']).read_bytes())
        with closing(sqlite3.connect(restored/'database.sqlite3')) as con:
            self.assertEqual('ok', con.execute('PRAGMA integrity_check').fetchone()[0])
            self.assertEqual([], list(con.execute('PRAGMA foreign_key_check')))
            self.assertEqual(3, con.execute('SELECT COUNT(*) FROM html_results').fetchone()[0])
            self.assertEqual(plans[-1].id, con.execute('SELECT result_id FROM html_current').fetchone()[0])
            self.assertEqual(1, con.execute('SELECT COUNT(*) FROM user_history').fetchone()[0])
            # Absolute roots and recovery metadata are retained verbatim, never rebound.
            self.assertEqual(str(self.html), con.execute('SELECT root FROM html_results LIMIT 1').fetchone()[0])
        if os.name == 'posix':
            self.assertEqual(0o700, directory.stat().st_mode & 0o777)
            self.assertEqual(0o600, (directory/'database.sqlite3').stat().st_mode & 0o777)

    def test_wal_snapshot_contains_committed_rows_and_never_touches_source_sidecars(self):
        self.ready()
        con = sqlite3.connect(self.database)
        self.addCleanup(con.close)
        con.execute('PRAGMA journal_mode=WAL')
        con.execute('PRAGMA wal_autocheckpoint=0')
        con.execute("UPDATE vod_cache SET title='committed in WAL'")
        con.commit()
        paths = [Path(str(self.database)+s) for s in ('','-wal','-shm')]
        self.assertTrue(paths[1].exists())
        before = [(p.read_bytes(), backup._stamp(p.stat())) for p in paths]
        directory = self.create()
        self.assertEqual(before, [(p.read_bytes(), backup._stamp(p.stat())) for p in paths])
        with closing(sqlite3.connect(directory/'database.sqlite3')) as copied:
            self.assertEqual('committed in WAL', copied.execute('SELECT title FROM vod_cache').fetchone()[0])

    def test_hot_rollback_journal_is_recovered_only_in_the_private_clone(self):
        self.ready()
        code = """
import os,sqlite3,sys
con=sqlite3.connect(sys.argv[1])
con.execute('PRAGMA cache_size=1')
con.execute('PRAGMA synchronous=FULL')
con.execute('BEGIN IMMEDIATE')
con.execute('UPDATE vod_cache SET title=?', ('uncommitted'*20000,))
os._exit(0)
"""
        subprocess.run([sys.executable,'-I','-S','-B','-c',code,str(self.database)], check=True, timeout=10)
        journal = Path(str(self.database)+'-journal')
        self.assertGreater(journal.stat().st_size, 0)
        before = [(p.read_bytes(), backup._stamp(p.stat())) for p in (self.database,journal)]
        directory = self.create()
        self.assertEqual(before, [(p.read_bytes(), backup._stamp(p.stat())) for p in (self.database,journal)])
        with closing(sqlite3.connect(directory/'database.sqlite3')) as con:
            self.assertEqual('VOD', con.execute('SELECT title FROM vod_cache').fetchone()[0])

    def test_multiple_explicit_roots_are_mapped_without_merging_or_rewriting_paths(self):
        self.ready()
        public = self.plan('555', mode='public')
        self.publish(public, 'public')
        public.complete()
        public.close()
        roots = [self.html, self.root/'public']
        directory = self.root/'snapshot'
        manifest = backup.create_backup(self.database, roots, directory, maintenance_stopped=True, reserve_bytes=0)
        self.assertEqual(list(map(str,roots)), manifest['roots'])
        backup.restore_backup(directory, self.root/'restored', reserve_bytes=0)
        self.assertTrue((self.root/'restored/html/1'/public.filename).is_file())

    def deletion(self, plan, boundary):
        row = next(row for row in self.rows() if row['id'] == plan.id)
        intent = cleanup._claim(row, plan.path().lstat())
        if boundary == 'claim':
            return None
        directory = self.html/intent['quarantine']
        directory.mkdir()
        info = directory.stat()
        cleanup._update(plan.id, dir_device=str(info.st_dev), dir_inode=str(info.st_ino))
        plan.path().rename(directory/'page.html')
        if boundary in ('quarantined','unlink'):
            cleanup._update(plan.id, phase='quarantined')
        if boundary == 'unlink':
            (directory/'page.html').unlink()
        return directory

    def test_deletion_crash_boundaries_preserve_journal_and_quarantine_without_recovery(self):
        for boundary in ('claim','rename','quarantined','unlink'):
            with self.subTest(boundary=boundary):
                plan = self.ready(vod_id=str(100+len(self.rows())))
                quarantine = self.deletion(plan, boundary)
                directory = self.create('snapshot-'+boundary)
                restored = self.root/('restore-'+boundary)
                backup.restore_backup(directory, restored, reserve_bytes=0)
                if quarantine:
                    copied = restored/'html/0'/quarantine.name
                    self.assertTrue(copied.is_dir())
                    self.assertEqual(boundary != 'unlink', (copied/'page.html').exists())
                with closing(sqlite3.connect(restored/'database.sqlite3')) as con:
                    self.assertEqual('ready', con.execute('SELECT state FROM html_results WHERE id=?', (plan.id,)).fetchone()[0])
                    self.assertEqual('quarantined' if boundary in ('quarantined','unlink') else 'pending',
                                     con.execute('SELECT phase FROM html_deletions WHERE result_id=?', (plan.id,)).fetchone()[0])
                self.assertEqual('ready', next(r for r in self.rows() if r['id'] == plan.id)['state'])

    def test_completed_deletion_tombstones_and_history_survive_restore(self):
        plan = self.ready()
        db.add_user_history(7, db.get_cache(plan.vod_id, 'html_online')['id'])
        with patch.object(cleanup, '_free', return_value=0):
            cleanup.run_cleanup()
        directory = self.create()
        backup.restore_backup(directory, self.root/'restored', reserve_bytes=0)
        with closing(sqlite3.connect(self.root/'restored/database.sqlite3')) as con:
            self.assertEqual('deleted', con.execute('SELECT state FROM html_results').fetchone()[0])
            self.assertEqual('done', con.execute('SELECT phase FROM html_deletions').fetchone()[0])
            self.assertEqual(1, con.execute('SELECT COUNT(*) FROM html_removed_heads').fetchone()[0])
            self.assertEqual(1, con.execute('SELECT COUNT(*) FROM user_history').fetchone()[0])
            self.assertIsNone(con.execute('SELECT html_url FROM vod_cache').fetchone()[0])

    def test_missing_html_is_not_excused_by_an_unproven_quarantine_directory(self):
        plan = self.ready()
        self.deletion(plan, 'unlink')
        with closing(db_core._connect()) as con, con:
            con.execute("UPDATE html_deletions SET dir_inode='0'")
        with self.assertRaisesRegex(backup.BackupError, 'Quarantine identity'):
            self.create()
        self.assertFalse((self.root/'snapshot/COMPLETE').exists())

    def test_missing_old_ready_html_refuses_but_unfinished_generation_is_preserved(self):
        old = self.ready('old')
        self.ready('new')
        old.path().unlink()
        with self.assertRaisesRegex(backup.BackupError, 'missing'):
            self.create('missing-ready')
        self.assertFalse((self.root/'missing-ready/COMPLETE').exists())
        # A non-ready reservation has no promised final file and remains metadata.
        old.path().write_text('<html>old</html>', encoding='utf-8')
        pending = self.plan('555')
        pending.close()
        directory = self.create()
        with closing(sqlite3.connect(directory/'database.sqlite3')) as con:
            self.assertEqual('building', con.execute('SELECT state FROM html_results WHERE id=?', (pending.id,)).fetchone()[0])

    def test_source_db_and_html_changes_abort_without_completion(self):
        plan = self.ready()
        original = backup._sqlite_copy
        def change_database(*args):
            original(*args)
            with closing(sqlite3.connect(self.database)) as con, con:
                con.execute("UPDATE vod_cache SET title='changed by another writer'")
        with patch.object(backup, '_sqlite_copy', side_effect=change_database):
            with self.assertRaises(backup.BackupError):
                self.create('db-change')
        self.assertFalse((self.root/'db-change/COMPLETE').exists())
        copy = backup._copy
        def change_html(source, target, *args):
            result = copy(source, target, *args)
            if source == plan.path():
                source.write_text('<html>changed concurrently</html>', encoding='utf-8')
            return result
        with patch.object(backup, '_copy', side_effect=change_html):
            with self.assertRaises(backup.BackupError):
                self.create('html-change')
        self.assertFalse((self.root/'html-change/COMPLETE').exists())

    def test_manifest_checksum_missing_members_and_html_corruption_are_rejected(self):
        plan = self.ready()
        for failure in ('manifest','missing','html','extra'):
            with self.subTest(failure=failure):
                directory = self.create('snapshot-'+failure)
                html = directory/'html/0'/plan.filename
                if failure == 'manifest':
                    with (directory/'manifest.json').open('ab') as output:
                        output.write(b' ')
                elif failure == 'missing':
                    html.unlink()
                elif failure == 'html':
                    html.write_bytes(b'corrupted')
                else:
                    (directory/'.env').write_text('must-not-be-accepted', encoding='utf-8')
                with self.assertRaises((backup.BackupError, OSError)):
                    backup.verify_backup(directory)
                restored = self.root/('restore-'+failure)
                with self.assertRaises((backup.BackupError, OSError)):
                    backup.restore_backup(directory, restored, reserve_bytes=0)
                self.assertFalse(restored.exists())

    def test_sqlite_integrity_and_foreign_keys_are_checked_beyond_file_hashes(self):
        self.ready()
        for failure in ('sqlite','foreign-key','cache-mirror'):
            with self.subTest(failure=failure):
                directory = self.create('snapshot-'+failure)
                path = directory/'database.sqlite3'
                if failure == 'sqlite':
                    path.write_bytes(b'not sqlite')
                else:
                    with closing(sqlite3.connect(path)) as con, con:
                        if failure == 'foreign-key':
                            con.execute('INSERT INTO user_history(user_id,cache_id,processed_at) VALUES(1,999,0)')
                        else:
                            con.execute("UPDATE vod_cache SET html_url='different reference'")
                self.refresh_db_hash(directory)
                with self.assertRaises((backup.BackupError, sqlite3.Error)):
                    backup.verify_backup(directory)

    def test_manifest_traversal_duplicate_and_absolute_paths_never_write_outside_restore(self):
        self.ready()
        outside = self.root/'outside.html'
        outside.write_text('keep', encoding='utf-8')
        values = ['../outside.html', '/outside.html', 'C:/outside.html', r'html\0\outside.html',
                  'html/0/../outside.html', 'html/00/chat_'+'a'*32+'.html']
        for index, value in enumerate(values + ['duplicate']):
            with self.subTest(value=value):
                directory = self.create('unsafe-'+str(index))
                def modify(manifest):
                    if value == 'duplicate':
                        manifest['files'].append(dict(manifest['files'][0]))
                    else:
                        manifest['files'][1]['path'] = value
                self.rewrite_manifest(directory, modify)
                with self.assertRaises(backup.BackupError):
                    backup.restore_backup(directory, self.root/('restored-'+str(index)), reserve_bytes=0)
                self.assertFalse((self.root/('restored-'+str(index))).exists())
                self.assertEqual('keep', outside.read_text(encoding='utf-8'))

    def test_unsafe_registry_roots_and_journal_paths_are_not_followed(self):
        plan = self.ready()
        self.change(plan.id, root=str(self.root))
        with self.assertRaises(backup.BackupError):
            self.create('outside-root')
        self.change(plan.id, root=str(self.html))
        self.deletion(plan, 'claim')
        with closing(db_core._connect()) as con, con:
            con.execute("UPDATE html_deletions SET quarantine='../outside'")
        with self.assertRaises(backup.BackupError):
            self.create('unsafe-journal')

    def test_source_and_backup_symlink_files_are_rejected(self):
        plan = self.ready()
        directory = self.create()
        original = self.root/'saved.html'
        plan.path().rename(original)
        self.symlink(original, plan.path())
        with self.assertRaises((backup.BackupError, OSError)):
            self.create('source-link')
        copied = directory/'html/0'/plan.filename
        copied.unlink()
        self.symlink(original, copied)
        with self.assertRaises((backup.BackupError, OSError)):
            backup.verify_backup(directory)

    def test_symlink_roots_and_destination_parents_are_rejected(self):
        self.ready()
        directory = self.create()
        link = self.root/'linked'
        self.symlink(self.root, link, directory=True)
        with self.assertRaises((backup.BackupError, OSError)):
            backup.create_backup(self.database, [link/'html'], self.root/'bad-root', maintenance_stopped=True, reserve_bytes=0)
        with self.assertRaises((backup.BackupError, OSError)):
            backup.restore_backup(directory, link/'bad-restore', reserve_bytes=0)
        self.assertFalse((self.root/'bad-restore').exists())

    def test_existing_destinations_and_source_overlap_never_overwrite(self):
        plan = self.ready()
        directory = self.create()
        with self.assertRaises(FileExistsError):
            self.create()
        for name, is_file in (('existing-dir',False), ('existing-file',True)):
            target = self.root/name
            if is_file:
                target.write_text('keep', encoding='utf-8')
            else:
                target.mkdir()
                (target/'keep').write_text('keep', encoding='utf-8')
            with self.assertRaises(FileExistsError):
                backup.restore_backup(directory, target, reserve_bytes=0)
            self.assertEqual('keep', (target if is_file else target/'keep').read_text(encoding='utf-8'))
        for target in (directory/'nested', self.html/'nested'):
            with self.assertRaises(backup.BackupError):
                backup.restore_backup(directory, target, reserve_bytes=0)
        self.assertTrue(plan.path().is_file())

    def test_low_space_timeout_and_enospc_leave_sources_unchanged(self):
        plan = self.ready()
        before = (self.database.read_bytes(), plan.path().read_bytes())
        with patch.object(backup.shutil, 'disk_usage', return_value=SimpleNamespace(free=0)):
            with self.assertRaises(backup.BackupError):
                self.create('no-space')
        self.assertFalse((self.root/'no-space').exists())
        with patch.object(backup.time, 'monotonic', side_effect=[0, 1000]):
            with self.assertRaises(backup.BackupError):
                self.create('timeout')
        with patch.object(backup, '_copy', side_effect=OSError(errno.ENOSPC, 'synthetic disk full')):
            with self.assertRaises(OSError):
                self.create('disk-full')
        self.assertFalse((self.root/'disk-full/COMPLETE').exists())
        self.assertEqual(before, (self.database.read_bytes(), plan.path().read_bytes()))

    def test_maintenance_ack_and_restart_detection_are_mandatory(self):
        self.ready()
        with self.assertRaises(backup.BackupError):
            backup.create_backup(self.database, [self.html], self.root/'no-ack', reserve_bytes=0)
        self.guard.assert_not_called()
        self.guard.side_effect = [None, None, backup.BackupError('service restarted')]
        with self.assertRaisesRegex(backup.BackupError, 'restarted'):
            self.create('restarted')
        self.assertFalse((self.root/'restarted/COMPLETE').exists())

    def test_manifest_change_during_restore_never_completes_the_output(self):
        self.ready()
        directory = self.create()
        copy = backup._copy
        changed = False
        def tamper(*args):
            nonlocal changed
            entry = copy(*args)
            if not changed:
                changed = True
                self.rewrite_manifest(directory, lambda m: m.update(source_database='/another/database.sqlite3'))
            return entry
        with patch.object(backup, '_copy', side_effect=tamper):
            with self.assertRaisesRegex(backup.BackupError, 'Manifest changed'):
                backup.restore_backup(directory, self.root/'restored', reserve_bytes=0)
        self.assertFalse((self.root/'restored/COMPLETE').exists())

    def test_restore_deadline_is_not_reset_after_verifying_the_input(self):
        self.ready()
        directory = self.create()
        now = [0.0]
        verify = backup.verify_backup
        def slow_verification(*args, **kwargs):
            manifest = verify(*args, **kwargs)
            now[0] = 2.0
            return manifest
        with patch.object(backup.time, 'monotonic', side_effect=lambda: now[0]), \
             patch.object(backup, 'verify_backup', side_effect=slow_verification):
            with self.assertRaisesRegex(backup.BackupError, 'timed out'):
                backup.restore_backup(directory, self.root/'restored', reserve_bytes=0, timeout=1)
        self.assertFalse((self.root/'restored').exists())

    def test_bounded_reads_and_cli_verify_restore_need_no_application_environment(self):
        text = '<html>'+'x'*(backup.BLOCK*3)+'</html>'
        self.ready(text)
        read = backup.os.read
        def bounded_read(fd, count):
            self.assertLessEqual(count, backup.BLOCK)
            return read(fd, count)
        with patch.object(backup.os, 'read', side_effect=bounded_read):
            directory = self.create()
            backup.restore_backup(directory, self.root/'restored', reserve_bytes=0)
        script = Path(backup.__file__).resolve()
        env = dict(os.environ, TCD_DB_PATH='must-not-be-opened', TCD_HTML_PUBLISH_MODE='invalid',
                   TELEGRAM_BOT_TOKEN_TWITCH='PRIVATE_TEST_SENTINEL')
        for arguments in (['verify','--backup',str(directory)],
                          ['restore','--backup',str(directory),'--destination',str(self.root/'cli-restore'),'--reserve-bytes','0']):
            run = subprocess.run([sys.executable,'-I','-S','-B',str(script),*arguments], env=env,
                                 capture_output=True,text=True,check=True,timeout=20)
            self.assertEqual('ok', json.loads(run.stdout)['status'])
            self.assertNotIn('PRIVATE_TEST_SENTINEL', run.stdout+run.stderr)


class StoppedServiceGuardTests(unittest.TestCase):
    def test_guard_is_read_only_and_rejects_active_failed_missing_or_queued_service(self):
        base = dict(LoadState='loaded',ActiveState='inactive',SubState='dead',MainPID='0',Job='0')
        for change in ({}, {'ActiveState':'active'}, {'ActiveState':'failed'}, {'MainPID':'123'},
                       {'LoadState':'not-found'}, {'Job':'42'}, {'SubState':'running'}):
            fields = dict(base, **change)
            response = SimpleNamespace(returncode=0, stdout='\n'.join(f'{k}={v}' for k,v in fields.items()))
            with self.subTest(change=change), patch.object(backup.subprocess, 'run', return_value=response) as run:
                if change:
                    with self.assertRaises(backup.BackupError):
                        backup._require_stopped('tdb.service')
                else:
                    backup._require_stopped('tdb.service')
                self.assertEqual('show', run.call_args.args[0][1])
                self.assertNotIn('Environment', ' '.join(run.call_args.args[0]))
        with self.assertRaises(backup.BackupError):
            backup._require_stopped('--bad.service')
        with patch.object(backup.subprocess, 'run', side_effect=subprocess.TimeoutExpired('systemctl', 5)):
            with self.assertRaises(backup.BackupError):
                backup._require_stopped('tdb.service')


if __name__ == '__main__':
    unittest.main()
