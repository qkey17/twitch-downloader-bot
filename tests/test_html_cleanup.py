"""Space pressure and crash tests: only disposable DB/files, never a real disk fill."""
import asyncio
import os
import sqlite3
import stat
import subprocess
import sys
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import test_result_registry as fixture
from src import config, html_cleanup as cleanup, main
from src.database import database as db, db_core, db_results as results

GIB = 1024**3


class Crash(BaseException):
    """Bypass normal exception rollback, like an abrupt process termination."""


class HtmlCleanupTests(unittest.TestCase):
    plan = fixture.ResultRegistryTestCase.plan
    publish = fixture.ResultRegistryTestCase.publish
    ready = fixture.ResultRegistryTestCase.ready
    rows = fixture.ResultRegistryTestCase.rows
    change = fixture.ResultRegistryTestCase.change

    def setUp(self):
        fixture.ResultRegistryTestCase.setUp(self)
        self.stack.enter_context(patch.object(config,'HTML_CLEANUP_LOW_BYTES',5*GIB))
        self.stack.enter_context(patch.object(config,'HTML_CLEANUP_TARGET_BYTES',8*GIB))

    def run_cleanup(self, free=4*GIB):
        getter = Mock(side_effect=free) if isinstance(free,list) else Mock(return_value=free)
        with patch.object(cleanup,'_free',getter):
            cleanup.run_cleanup()
        return getter

    def intent(self, key):
        con = db_core._connect()
        try:
            row = con.execute('SELECT * FROM html_deletions WHERE result_id=?',(key,)).fetchone()
            return dict(row) if row else None
        finally:
            con.close()

    def test_sufficient_and_exact_boundary_space_are_quiet_and_delete_nothing(self):
        plan = self.ready()
        for free in (40*GIB,5*GIB):
            with self.subTest(free=free),patch.object(cleanup.log,'info') as info:
                self.run_cleanup(free)
                self.assertTrue(plan.path().exists())
                self.assertIsNone(self.intent(plan.id))
                info.assert_not_called()

    def test_oldest_first_and_stop_at_target_with_remeasurement(self):
        plans = [self.ready(str(i)) for i in range(3)]
        for i,plan in enumerate(plans):
            self.change(plan.id,completed_at=i+1)
        deleted = []
        original = cleanup._finalize
        def record(row):
            original(row)
            deleted.append(row['id'])
        with patch.object(cleanup,'_finalize',side_effect=record):
            getter = self.run_cleanup([4*GIB,6*GIB,8*GIB])
        self.assertEqual([p.id for p in plans[:2]],deleted)
        self.assertEqual(3,getter.call_count)
        self.assertEqual(plans[-1].id,results.get_ready_result(plans[-1].vod_id)['id'])
        self.assertTrue(plans[-1].path().exists())

    def test_deleted_current_does_not_fall_back_and_legacy_cannot_resurrect_it(self):
        old = self.ready('old')
        current = self.ready('current')
        db.add_user_history(7,results.get_ready_result(current.vod_id)['cache_id'])
        self.change(current.id,completed_at=1)
        self.change(old.id,completed_at=2)
        self.run_cleanup([4*GIB,8*GIB])
        self.assertIsNone(results.get_ready_result(current.vod_id))
        self.assertTrue(old.path().exists())
        self.assertEqual(1,db.get_history_count(7))
        self.assertIsNone(db.get_cache(current.vod_id,'html_online')['html_url'])
        # A stale compatibility writer/file must not re-import this tombstone.
        current.path().write_text('<html>foreign replacement</html>',encoding='utf-8')
        db.upsert_cache(current.vod_id,'html_online','vod',{'html_url':current.ref},{},[])
        db.init_db()
        db.init_db()
        self.assertIsNone(results.get_ready_result(current.vod_id))
        self.run_cleanup(40*GIB)
        self.assertTrue(current.path().exists())
        fresh = self.ready('fresh')
        self.assertEqual(fresh.id,results.get_ready_result(fresh.vod_id)['id'])

    def test_active_regeneration_protects_previous_in_both_prepublication_states(self):
        old = self.ready()
        plan = self.plan()
        temp = plan.root/plan.temp_name
        temp.write_text('unproven temp',encoding='utf-8')
        self.run_cleanup()
        self.assertEqual(old.id,results.get_ready_result(old.vod_id)['id'])
        self.assertTrue(temp.exists())
        temp.unlink()  # Only this test's disposable fixture, before actual writing.
        self.publish(plan)
        self.run_cleanup()
        self.assertTrue(old.path().exists())
        self.assertTrue(plan.path().exists())

    def test_ready_but_still_owned_publication_and_previous_are_protected(self):
        old = self.ready()
        plan = self.plan()
        self.publish(plan)
        plan.complete()
        self.run_cleanup()
        self.assertTrue(old.path().exists())
        self.assertTrue(plan.path().exists())

    def test_other_files_and_damaged_paths_are_retained(self):
        plan = self.ready()
        unknown = plan.root/'chat_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.html'
        unknown.write_text('foreign',encoding='utf-8')
        outside = self.root/'outside.html'
        outside.write_text('outside',encoding='utf-8')
        self.change(plan.id,root=str(self.root),filename='outside.html')
        self.run_cleanup()
        self.assertEqual('outside',outside.read_text(encoding='utf-8'))
        self.assertTrue(unknown.exists())
        self.assertEqual('ready',self.rows()[0]['state'])

    def test_symbolic_link_is_not_followed(self):
        plan = self.ready()
        path = plan.path()
        outside = self.root/'keep.html'
        outside.write_text('outside',encoding='utf-8')
        path.unlink()
        try:
            path.symlink_to(outside)
        except OSError:
            self.skipTest('OS does not allow creating a symlink')
        self.run_cleanup()
        self.assertTrue(path.is_symlink())
        self.assertEqual('outside',outside.read_text(encoding='utf-8'))

    def test_permission_error_keeps_record_and_other_candidates_can_be_deleted(self):
        first = self.ready(vod_id='101')
        second = self.ready(vod_id='102')
        self.change(first.id,completed_at=1)
        self.change(second.id,completed_at=2)
        original = cleanup.os.rename
        def denied(source,target):
            if source.name == first.filename:
                raise PermissionError('test permission error')
            return original(source,target)
        with patch.object(cleanup.os,'rename',side_effect=denied):
            self.run_cleanup([4*GIB,8*GIB])
        self.assertEqual(first.id,results.get_ready_result('101')['id'])
        self.assertEqual('cancelled',self.intent(first.id)['phase'])
        self.assertIsNone(results.get_ready_result('102'))

    def test_unlink_failure_restores_file_and_interrupted_rollback_is_recoverable(self):
        plan = self.ready()
        with patch.object(cleanup,'_unlink_owned',side_effect=PermissionError('test unlink error')):
            self.run_cleanup()
        self.assertTrue(plan.path().exists())
        self.assertEqual('ready',self.rows()[0]['state'])
        self.run_cleanup(40*GIB)
        self.assertEqual(plan.id,results.get_ready_result(plan.vod_id)['id'])

    def test_replaced_source_at_rename_is_retained_and_never_unlinked(self):
        plan = self.ready()
        original = cleanup.os.rename
        saved = self.root/'original.html'
        def replace(source,target):
            original(source,saved)
            source.write_text('foreign replacement',encoding='utf-8')
            original(source,target)
        with patch.object(cleanup.os,'rename',side_effect=replace):
            self.run_cleanup()
        intent = self.intent(plan.id)
        foreign = plan.root/intent['quarantine']/'page.html'
        self.assertEqual('foreign replacement',foreign.read_text(encoding='utf-8'))
        self.assertTrue(saved.exists())
        self.run_cleanup(40*GIB)
        self.assertTrue(foreign.exists())
        self.assertEqual('ready',self.rows()[0]['state'])

    def test_preexisting_quarantine_directory_is_never_claimed_or_removed(self):
        plan = self.ready()
        original = cleanup._claim
        def collide(row,info):
            intent = original(row,info)
            directory = plan.root/intent['quarantine']
            directory.mkdir()
            (directory/'foreign.txt').write_text('keep',encoding='utf-8')
            return intent
        with patch.object(cleanup,'_claim',side_effect=collide):
            self.run_cleanup()
        directory = plan.root/self.intent(plan.id)['quarantine']
        self.assertEqual('keep',(directory/'foreign.txt').read_text(encoding='utf-8'))
        self.assertEqual(plan.id,results.get_ready_result(plan.vod_id)['id'])

    def test_crashes_at_each_durable_boundary_are_recovered_idempotently(self):
        for stage in ('claim','rename','quarantined','unlink','commit'):
            with self.subTest(stage=stage):
                plan = self.ready(vod_id=str(200+len(self.rows())))
                self.change(plan.id,completed_at=-len(self.rows()))
                if stage == 'claim':
                    original = cleanup._claim
                    def crash(*args):
                        original(*args)
                        raise Crash()
                    target = patch.object(cleanup,'_claim',side_effect=crash)
                elif stage == 'rename':
                    original = cleanup.os.rename
                    def crash(*args):
                        original(*args)
                        raise Crash()
                    target = patch.object(cleanup.os,'rename',side_effect=crash)
                elif stage == 'quarantined':
                    original = cleanup._update
                    def crash(*args,**kwargs):
                        original(*args,**kwargs)
                        if kwargs.get('phase') == 'quarantined':
                            raise Crash()
                    target = patch.object(cleanup,'_update',side_effect=crash)
                elif stage == 'unlink':
                    original = cleanup._unlink_owned
                    def crash(*args):
                        original(*args)
                        raise Crash()
                    target = patch.object(cleanup,'_unlink_owned',side_effect=crash)
                else:
                    original = cleanup._finalize
                    def crash(*args):
                        original(*args)
                        raise Crash()
                    target = patch.object(cleanup,'_finalize',side_effect=crash)
                with target,self.assertRaises(Crash):
                    self.run_cleanup()
                self.run_cleanup(40*GIB)
                self.run_cleanup(40*GIB)
                if stage in ('unlink','commit'):
                    self.assertEqual('done',self.intent(plan.id)['phase'])
                    self.assertIsNone(results.get_ready_result(plan.vod_id))
                    self.assertEqual('deleted',next(r for r in self.rows() if r['id']==plan.id)['state'])
                else:
                    self.assertEqual('cancelled',self.intent(plan.id)['phase'])
                    self.assertEqual(plan.id,results.get_ready_result(plan.vod_id)['id'])

    def test_sqlite_failure_after_unlink_does_not_resurrect_missing_html(self):
        plan = self.ready()
        with patch.object(cleanup,'_finalize',side_effect=sqlite3.OperationalError('test db error')):
            with self.assertRaises(sqlite3.OperationalError):
                self.run_cleanup()
        self.assertIsNone(results.get_ready_result(plan.vod_id))
        db.init_db()
        self.run_cleanup(40*GIB)
        self.assertEqual('done',self.intent(plan.id)['phase'])

    def test_replacement_after_quarantined_commit_is_not_unlinked(self):
        plan = self.ready()
        update = cleanup._update
        saved = self.root/'owned-before-substitution.html'
        def replace(key,**values):
            update(key,**values)
            if values.get('phase') == 'quarantined':
                target = plan.root/self.intent(key)['quarantine']/'page.html'
                target.rename(saved)
                target.write_bytes(saved.read_bytes())
        with patch.object(cleanup,'_update',side_effect=replace):
            self.run_cleanup()
        foreign = plan.root/self.intent(plan.id)['quarantine']/'page.html'
        self.assertTrue(foreign.exists())
        self.assertTrue(saved.exists())
        self.run_cleanup(40*GIB)
        self.assertTrue(foreign.exists())

    def test_real_process_exit_after_unlink_is_recovered(self):
        plan = self.ready()
        code = """
import os,sys
from src import config,html_publisher,html_cleanup
from src.database import db_core
db_core.DB_PATH=sys.argv[1]
config.SPOOL_DIR=sys.argv[2]
config.HTML_PUBLISH_MODE='local'
html_publisher.OUT_DIR=sys.argv[3]
html_cleanup._free=lambda root:0
html_cleanup._finalize=lambda row:os._exit(0)
html_cleanup.run_cleanup()
os._exit(2)
"""
        subprocess.run([sys.executable,'-B','-c',code,str(self.root/'app.sqlite3'),
                        str(self.root/'work'),str(self.root/'html')],
                       cwd=Path(__file__).resolve().parents[1],check=True,timeout=10,capture_output=True)
        self.assertEqual('quarantined',self.intent(plan.id)['phase'])
        self.assertIsNone(results.get_ready_result(plan.vod_id))
        self.run_cleanup(40*GIB)
        self.run_cleanup(40*GIB)
        self.assertEqual('done',self.intent(plan.id)['phase'])

    def test_directory_created_before_identity_commit_is_preserved(self):
        plan = self.ready()
        update = cleanup._update
        def crash(*args,**values):
            if 'dir_inode' in values:
                raise Crash()
            update(*args,**values)
        with patch.object(cleanup,'_update',side_effect=crash),self.assertRaises(Crash):
            self.run_cleanup()
        directory = plan.root/self.intent(plan.id)['quarantine']
        self.assertTrue(directory.exists())

        self.run_cleanup(40*GIB)
        self.assertEqual(plan.id,results.get_ready_result(plan.vod_id)['id'])
        self.assertTrue(directory.exists())

    def test_healthy_boundary_restores_quarantined_html_instead_of_deleting(self):
        plan = self.ready()
        update = cleanup._update
        def crash(*args,**values):
            update(*args,**values)
            if values.get('phase')=='quarantined':
                raise Crash()
        with patch.object(cleanup,'_update',side_effect=crash),self.assertRaises(Crash):
            self.run_cleanup()
        self.run_cleanup(5*GIB)
        self.assertTrue(plan.path().exists())
        self.assertEqual('cancelled',self.intent(plan.id)['phase'])

    def test_posix_unlink_uses_verified_directory_descriptor_and_no_symlinks(self):
        intent = dict(device='7',inode='9',dir_device='1',dir_inode='2',size_bytes=10,mtime_ns='3')
        for mode in (stat.S_IFREG,stat.S_IFLNK):
            with self.subTest(mode=mode):
                fake = SimpleNamespace(name='posix',O_RDONLY=0,O_DIRECTORY=1,O_NOFOLLOW=2,
                    open=Mock(return_value=11),fstat=Mock(return_value=SimpleNamespace(st_dev=1,st_ino=2)),
                    stat=Mock(return_value=SimpleNamespace(st_mode=mode,st_dev=7,st_ino=9,st_size=10,st_mtime_ns=3)),
                    unlink=Mock(),fsync=Mock(),close=Mock())
                with patch.object(cleanup,'os',fake),patch.object(cleanup,'_owned_directory',return_value=True):
                    if mode==stat.S_IFREG:
                        cleanup._unlink_owned(self.root,intent)
                        fake.unlink.assert_called_once_with('page.html',dir_fd=11)
                    else:
                        with self.assertRaises(cleanup.UnsafeDeletion):
                            cleanup._unlink_owned(self.root,intent)
                        fake.unlink.assert_not_called()
                fake.stat.assert_called_once_with('page.html',dir_fd=11,follow_symlinks=False)
                fake.close.assert_called_once_with(11)

    def test_replaced_quarantine_directory_is_never_unlinked(self):
        plan = self.ready()
        update = cleanup._update
        saved = self.root/'saved-quarantine'
        def replace(*args,**values):
            update(*args,**values)
            if values.get('phase')=='quarantined':
                directory = plan.root/self.intent(plan.id)['quarantine']
                directory.rename(saved)
                directory.mkdir()
                (directory/'page.html').write_bytes((saved/'page.html').read_bytes())
        with patch.object(cleanup,'_update',side_effect=replace):
            self.run_cleanup()
        directory = plan.root/self.intent(plan.id)['quarantine']
        self.assertTrue((directory/'page.html').exists())
        self.assertTrue((saved/'page.html').exists())
        self.run_cleanup(40*GIB)
        self.assertTrue((directory/'page.html').exists())
        self.assertTrue((saved/'page.html').exists())

    def test_cleanup_configuration_defaults_and_environment_validation(self):
        names=('TCD_HTML_CLEANUP_LOW_BYTES','TCD_HTML_CLEANUP_TARGET_BYTES','TCD_HTML_CLEANUP_INTERVAL_SECONDS')
        env = dict(os.environ)
        for name in names:
            env.pop(name,None)
        code='from src import config; print(config.HTML_CLEANUP_LOW_BYTES,config.HTML_CLEANUP_TARGET_BYTES,config.HTML_CLEANUP_INTERVAL_SECONDS)'
        result = subprocess.run([sys.executable,'-B','-c',code],env=env,check=True,capture_output=True,text=True)
        self.assertEqual([5*GIB,8*GIB,600],[int(n) for n in result.stdout.split()])
        env.update(dict(zip(names,('100','200','3'))))
        result = subprocess.run([sys.executable,'-B','-c',code],env=env,check=True,capture_output=True,text=True)
        self.assertEqual('100 200 3',result.stdout.strip())
        env[names[1]]='50'
        result = subprocess.run([sys.executable,'-B','-c',code],env=env,capture_output=True,text=True)
        self.assertNotEqual(0,result.returncode)
        self.assertIn('ConfigurationError',result.stderr)

    def test_restored_unverified_legacy_generation_does_not_replace_removed_head(self):
        from src import html_publisher
        ref = html_publisher.publish_html_local('<html>incomplete')
        db.upsert_cache('123456789','html_online','vod',{'html_url':ref},{},[])
        db.init_db()
        current = self.ready('current')
        legacy = current.root/ref[6:]
        legacy.write_text('<html>legacy restored</html>',encoding='utf-8')
        self.run_cleanup([4*GIB,8*GIB])
        self.assertIsNone(results.get_ready_result(current.vod_id))
        self.assertTrue(legacy.exists())
        db.init_db()
        self.assertIsNone(results.get_ready_result(current.vod_id))

    def test_deleted_newer_head_does_not_let_stale_operation_switch_back(self):
        old = self.ready()
        original = cleanup.os.rename
        started = []
        def reserve(*args):
            started.append(self.plan())
            started.append(self.plan())
            return original(*args)
        with patch.object(cleanup.os,'rename',side_effect=reserve):
            self.run_cleanup([4*GIB,8*GIB])
        first,stale = started
        self.publish(first)
        first.complete()
        first.close()
        self.run_cleanup([4*GIB,8*GIB])
        self.publish(stale)
        with self.assertRaisesRegex(ValueError,'changed'):
            stale.complete()
        self.assertIsNone(results.get_ready_result(old.vod_id))

    def test_publication_starting_after_claim_can_finish_after_predecessor_deleted(self):
        old = self.ready()
        original = cleanup.os.rename
        created = []
        def reserve(source,target):
            created.append(self.plan())
            return original(source,target)
        with patch.object(cleanup.os,'rename',side_effect=reserve):
            self.run_cleanup([4*GIB,8*GIB])
        new = created[0]
        self.assertEqual(old.id,new.previous_id)
        self.publish(new)
        new.complete()
        new.close()
        self.assertEqual(new.id,results.get_ready_result(new.vod_id)['id'])

    def test_publication_committing_during_deletion_keeps_new_head_and_cache(self):
        self.ready()
        original = cleanup._finalize
        created = []
        def finish(row):
            new = self.ready('new')
            created.append(new)
            original(row)
        with patch.object(cleanup,'_finalize',side_effect=finish):
            self.run_cleanup([4*GIB,8*GIB])
        new = created[0]
        self.assertEqual(new.id,results.get_ready_result(new.vod_id)['id'])
        self.assertEqual(new.ref,db.get_cache(new.vod_id,'html_online')['html_url'])

    def test_two_concurrent_cleanup_passes_do_not_double_delete(self):
        self.ready()
        entered,release = threading.Event(),threading.Event()
        original = cleanup.os.rename
        def pause(*args):
            entered.set()
            if not release.wait(3):
                raise TimeoutError('test release missing')
            return original(*args)
        with ThreadPoolExecutor(max_workers=2) as pool, \
             patch.object(cleanup,'_free',side_effect=[4*GIB,8*GIB]), \
             patch.object(cleanup.os,'rename',side_effect=pause) as rename:
            first = pool.submit(cleanup.run_cleanup)
            try:
                self.assertTrue(entered.wait(3))
                with patch.object(config,'SPOOL_DIR',str(self.root/'other-work')):
                    pool.submit(cleanup.run_cleanup).result(timeout=3)
            finally:
                release.set()
            first.result(timeout=3)
        self.assertEqual(1,rename.call_count)

    def test_stop_during_quarantine_restores_previous_result(self):
        plan = self.ready()
        stop = threading.Event()
        original = cleanup.os.rename
        def stop_after_move(*args):
            original(*args)
            stop.set()
        with patch.object(cleanup,'_free',return_value=4*GIB),patch.object(cleanup.os,'rename',side_effect=stop_after_move):
            cleanup.run_cleanup(stop)
        self.assertEqual(plan.id,results.get_ready_result(plan.vod_id)['id'])
        self.assertEqual('cancelled',self.intent(plan.id)['phase'])

    def test_existing_stage_d_schema_migrates_without_data_loss(self):
        plan = self.ready()
        con = db_core._connect()
        try:
            con.execute('DROP TABLE html_deletions')
            con.commit()
        finally:
            con.close()
        db.init_db()
        db.init_db()
        self.assertEqual(plan.id,results.get_ready_result(plan.vod_id)['id'])
        self.assertTrue(plan.path().exists())


class CleanupServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_initial_check_deduplication_periodic_run_and_shutdown_drain(self):
        entered,release = threading.Event(),threading.Event()
        calls = []
        def check(stop):
            calls.append('check')
            if len(calls) > 1:
                entered.set()
                if not release.wait(3):
                    raise TimeoutError('test release missing')
            calls.append('finished')
        app = SimpleNamespace(bot_data={},bot=SimpleNamespace())
        session = SimpleNamespace(close=AsyncMock())
        app.bot_data['aiohttp'] = session
        with patch.object(cleanup,'run_cleanup',side_effect=check), \
             patch.object(config,'HTML_CLEANUP_INTERVAL_SECONDS',0.01):
            try:
                await cleanup.start_cleanup(app)
                task = app.bot_data['html_cleanup'].task
                await cleanup.start_cleanup(app)
                self.assertIs(task,app.bot_data['html_cleanup'].task)
                self.assertTrue(await asyncio.to_thread(entered.wait,3))
                stopping = asyncio.create_task(main.post_stop(app))
                await asyncio.sleep(0)
                self.assertFalse(stopping.done())
                session.close.assert_not_awaited()
                release.set()
                await asyncio.wait_for(stopping,3)
                await main.post_shutdown(app)
                session.close.assert_awaited_once()
                self.assertTrue(task.done())
            finally:
                release.set()
                await cleanup.stop_cleanup(app)

    async def test_startup_order_is_recovery_then_cleanup_before_http_session(self):
        order = []
        app = SimpleNamespace(bot_data={},bot=SimpleNamespace(set_my_commands=AsyncMock()))
        session = SimpleNamespace(close=AsyncMock())
        with patch.object(main.db,'init_db',side_effect=lambda:order.append('db')), \
             patch.object(main,'recover_results',side_effect=lambda:order.append('recover')), \
             patch.object(cleanup,'run_cleanup',side_effect=lambda stop:order.append('cleanup')), \
             patch.object(main,'cleanup_orphaned_chat_spools',return_value=0), \
             patch.object(main,'cleanup_stale_html_temp_files',return_value=0), \
             patch.object(main,'protected_temp_files',return_value=set()), \
             patch.object(main.aiohttp,'ClientSession',side_effect=lambda:order.append('http') or session):
            try:
                await main.post_init(app)
                self.assertEqual(['db','recover','cleanup','http'],order)
            finally:
                await main.post_shutdown(app)
