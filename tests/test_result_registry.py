import errno
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from src import config, html_publisher
from src.database import database as db, db_core, db_results as results
from src.html_renderer import render_viewer_html, write_viewer_html


class ResultRegistryTestCase(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory(prefix='tdb-registry-',dir=Path(__file__).parent)
        self.addCleanup(temp.cleanup)
        self.root=Path(temp.name)
        self.stack=ExitStack()
        self.addCleanup(self.stack.close)
        for module,name,value in (
            (db_core,'DB_PATH',str(self.root/'app.sqlite3')),
            (config,'SPOOL_DIR',str(self.root/'work')),
            (config,'HTML_PUBLISH_MODE','local'),
            (html_publisher,'OUT_DIR',str(self.root/'html')),
            (html_publisher,'PUBLIC_HTML_DIR',str(self.root/'public')),
            (html_publisher,'PUBLIC_BASE_URL','https://registry.invalid'),
            (html_publisher,'PUBLIC_HTML_URL_PREFIX','/public_html'),
        ):
            self.stack.enter_context(patch.object(module,name,value))
        db.init_db()

    def plan(self,vod_id='123456789',mode='local'):
        plan=results.Publication(vod_id,mode=mode)
        self.addCleanup(plan.close)
        plan.reserve()
        plan.meta={'title':'VOD','channel':'channel','created_at':None,'length_seconds':30}
        plan.stats={'messages':1,'unique_users':1,'parts':0}
        return plan

    def publish(self,plan,text='viewer'):
        return html_publisher.publish_html_stream(lambda out:out.write(f'<html>{text}</html>'),
                                                 publication=plan,local=plan.mode=='local')

    def ready(self,text='old',vod_id='123456789'):
        plan=self.plan(vod_id)
        self.publish(plan,text)
        plan.complete()
        plan.close()
        return plan

    def rows(self):
        con=db_core._connect()
        try:
            return [dict(row) for row in con.execute('SELECT * FROM html_results ORDER BY created_at,id')]
        finally:
            con.close()

    def change(self,key,**fields):
        con=db_core._connect()
        try:
            with con:
                con.execute('UPDATE html_results SET '+','.join(f'{name}=?' for name in fields)+' WHERE id=?',(*fields.values(),key))
        finally:
            con.close()

    def test_three_generations_keep_old_files_and_one_current_pointer(self):
        plans=[self.ready(str(i)) for i in range(3)]
        self.assertEqual(3,len(self.rows()))
        self.assertEqual(plans[-1].id,results.get_ready_result('123456789')['id'])
        self.assertTrue(all(plan.path().is_file() for plan in plans))
        self.assertEqual(3,len(tuple((self.root/'html').glob('*.html'))))
        self.assertEqual(0,db.get_history_count(1))
        self.assertEqual(plans[-1].ref,db.get_cache('123456789','html_online')['html_url'])

    def test_building_and_publishing_are_not_current_until_commit(self):
        old=self.ready()
        new=self.plan()
        self.assertEqual(old.id,results.get_ready_result('123456789')['id'])
        self.publish(new,'new')
        self.assertEqual(old.id,results.get_ready_result('123456789')['id'])
        self.assertEqual('publishing',self.rows()[-1]['state'])
        new.complete()
        self.assertEqual(new.id,results.get_ready_result('123456789')['id'])

    def test_download_error_and_cancellation_keep_old_result(self):
        old=self.ready()
        for error in (OSError('download'),RuntimeError('Загрузка была отменена.')):
            new=self.plan()
            new.fail(error)
            new.close()
            self.assertEqual(old.id,results.get_ready_result('123456789')['id'])
            self.assertTrue(old.path().exists())

    def test_partial_write_enospc_and_serialization_failure_keep_old_file(self):
        old=self.ready()
        for error in (OSError(errno.ENOSPC,'disk full'),TypeError('serialization')):
            new=self.plan()

            def fail(output):
                output.write('partial')
                raise error

            with self.assertRaises(type(error)):
                html_publisher.publish_html_stream(fail,publication=new)
            new.fail(error)
            new.close()
            self.assertFalse(new.path().exists())
            self.assertFalse((new.root/new.temp_name).exists())
            self.assertEqual(old.id,results.get_ready_result('123456789')['id'])

    def test_filename_collision_never_overwrites_existing_html(self):
        new=self.plan()
        self.publish(new)
        original=new.path().read_bytes()
        with self.assertRaises(FileExistsError):
            self.publish(new,'overwrite')
        self.assertEqual(original,new.path().read_bytes())

    def test_link_race_is_no_clobber_even_when_file_appears_after_precheck(self):
        new=self.plan()
        original_link=html_publisher.os.link

        def collide(source,target):
            Path(target).write_text('foreign',encoding='utf-8')
            return original_link(source,target)

        with patch.object(html_publisher.os,'link',side_effect=collide):
            with self.assertRaises(FileExistsError) as caught:
                self.publish(new)
        new.fail(caught.exception)
        new.close()
        results.recover_results()
        self.assertEqual('foreign',new.path().read_text(encoding='utf-8'))
        self.assertIsNone(results.get_ready_result('123456789'))

    def test_database_finalize_failure_rolls_back_cache_and_head(self):
        old=self.ready()
        new=self.plan()
        self.publish(new,'new')
        real=results._upsert_cache

        def fail_after_cache_write(*args,**kwargs):
            real(*args,**kwargs)
            raise sqlite3.OperationalError('injected commit failure')

        with patch.object(results,'_upsert_cache',side_effect=fail_after_cache_write):
            with self.assertRaises(sqlite3.OperationalError):
                new.complete()
        self.assertEqual(old.id,results.get_ready_result('123456789')['id'])
        self.assertEqual(old.ref,db.get_cache('123456789','html_online')['html_url'])
        self.assertTrue(new.path().exists())

    def test_file_sync_failure_never_stages_or_replaces_previous_generation(self):
        old = self.ready()
        old_bytes = old.path().read_bytes()
        cache_id = db.get_cache(old.vod_id, 'html_online')['id']
        db.add_user_history(7, cache_id)
        new = self.plan()

        with patch.object(html_publisher.os, 'fsync', side_effect=OSError(errno.EIO, 'sync failed')):
            with self.assertRaises(OSError) as caught:
                self.publish(new, 'new')
        row = next(row for row in self.rows() if row['id'] == new.id)
        self.assertEqual('building', row['state'])
        self.assertIsNone(row['sha256'])
        new.fail(caught.exception)
        new.close()

        for _ in range(2):
            results.recover_results()
            self.assertEqual(old.id, results.get_ready_result(old.vod_id)['id'])
            self.assertEqual(old.ref, db.get_cache(old.vod_id, 'html_online')['html_url'])
            self.assertEqual(cache_id, db.get_history_for_user(7)[0]['id'])
            self.assertEqual(old_bytes, old.path().read_bytes())
            self.assertFalse(new.path().exists())
            self.assertFalse((new.root / new.temp_name).exists())

    def test_cancel_after_durable_stage_does_not_publish_or_recover_partial_result(self):
        # Exercise both first publication and regeneration after proof was committed.
        for existing in (False, True):
            with self.subTest(existing=existing):
                vod_id = '222' if existing else '111'
                old = self.ready(vod_id=vod_id) if existing else None
                new = self.plan(vod_id)
                cancelled = False
                stage = new.stage

                def stage_then_cancel(*args):
                    nonlocal cancelled
                    stage(*args)
                    cancelled = True

                with patch.object(new, 'stage', side_effect=stage_then_cancel):
                    with self.assertRaisesRegex(RuntimeError, 'отменена') as caught:
                        html_publisher.publish_html_stream(
                            lambda out: out.write('<html>complete bytes</html>'),
                            publication=new, cancel_check=lambda: cancelled,
                        )
                row = next(row for row in self.rows() if row['id'] == new.id)
                self.assertEqual('publishing', row['state'])
                self.assertIsNotNone(row['sha256'])
                new.fail(caught.exception)
                new.close()

                for _ in range(2):
                    results.recover_results()
                    ready = results.get_ready_result(vod_id)
                    self.assertEqual(old.id if old else None, ready['id'] if ready else None)
                    self.assertFalse(new.path().exists())
                    self.assertFalse((new.root / new.temp_name).exists())
                self.assertEqual('cancelled', next(row for row in self.rows() if row['id'] == new.id)['state'])

    def test_link_disk_full_after_proof_keeps_cache_history_and_previous_bytes(self):
        old = self.ready()
        old_bytes = old.path().read_bytes()
        cache_id = db.get_cache(old.vod_id, 'html_online')['id']
        db.add_user_history(7, cache_id)
        new = self.plan()

        with patch.object(html_publisher.os, 'link', side_effect=OSError(errno.ENOSPC, 'disk full')):
            with self.assertRaises(OSError) as caught:
                self.publish(new, 'new')
        row = next(row for row in self.rows() if row['id'] == new.id)
        self.assertEqual('publishing', row['state'])
        self.assertIsNotNone(row['sha256'])
        new.fail(caught.exception)
        new.close()

        for _ in range(2):
            results.recover_results()
            self.assertEqual(old.id, results.get_ready_result(old.vod_id)['id'])
            self.assertEqual(old.ref, db.get_cache(old.vod_id, 'html_online')['html_url'])
            self.assertEqual(cache_id, db.get_history_for_user(7)[0]['id'])
            self.assertEqual(old_bytes, old.path().read_bytes())
            self.assertFalse(new.path().exists())
            self.assertFalse((new.root / new.temp_name).exists())

    def test_recovery_retains_unproven_partial_temp_and_is_idempotent(self):
        old=self.ready()
        new=self.plan()
        (new.root/new.temp_name).write_text('partial',encoding='utf-8')
        new.close()  # Process-exit equivalent: release lease without changing DB state.
        results.recover_results()
        results.recover_results()
        self.assertEqual('interrupted',self.rows()[-1]['state'])
        self.assertEqual('partial',(new.root/new.temp_name).read_text(encoding='utf-8'))
        self.assertEqual(old.id,results.get_ready_result('123456789')['id'])

    def test_recovery_adopts_first_fully_published_result_only_with_proof(self):
        new=self.plan()
        self.publish(new)
        new.close()
        self.assertIsNone(results.get_ready_result('123456789'))
        results.recover_results()
        results.recover_results()
        self.assertEqual(new.id,results.get_ready_result('123456789')['id'])
        self.assertEqual(new.ref,db.get_cache('123456789','html_online')['html_url'])

    def test_recovery_does_not_replace_previous_head_after_crashed_regeneration(self):
        old=self.ready()
        new=self.plan()
        self.publish(new,'new')
        new.close()
        results.recover_results()
        self.assertEqual(old.id,results.get_ready_result('123456789')['id'])
        self.assertEqual('ready',self.rows()[-1]['state'])
        self.assertTrue(new.path().exists())

    def test_recovery_failure_can_be_retried_without_partial_activation(self):
        new=self.plan()
        self.publish(new)
        new.close()
        with patch.object(results,'_activate',side_effect=sqlite3.OperationalError('recovery failure')):
            with self.assertRaises(sqlite3.OperationalError):
                results.recover_results()
        self.assertIsNone(results.get_ready_result('123456789'))
        results.recover_results()
        self.assertEqual(new.id,results.get_ready_result('123456789')['id'])

    def test_active_generation_and_its_temp_are_skipped_by_recovery(self):
        new=self.plan()
        (new.root/new.temp_name).write_text('active',encoding='utf-8')
        results.recover_results()
        self.assertEqual('building',self.rows()[0]['state'])
        self.assertTrue((new.root/new.temp_name).exists())

    def test_corrupt_proof_or_unknown_final_is_never_adopted_or_deleted(self):
        for published in (True,False):
            new=self.plan('111' if published else '222')
            if published:
                self.publish(new)
                new.path().write_bytes(b'tampered')
            else:
                new.path().write_text('unproven file',encoding='utf-8')
            new.close()
            results.recover_results()
            self.assertIsNone(results.get_ready_result(new.vod_id))
            self.assertTrue(new.path().exists())

    def test_damaged_path_reference_and_metadata_never_escape_managed_roots(self):
        old=self.ready()
        outside=self.root/'user-file.html'
        outside.write_text('keep',encoding='utf-8')
        for fields in ({'root':str(self.root)},{'filename':'../user-file.html'},
                       {'html_ref':'https://evil.invalid/other.html'},{'meta_json':'[]'}):
            snapshot=self.rows()[0]
            self.change(old.id,**fields)
            self.assertIsNone(results.get_ready_result(old.vod_id))
            self.change(old.id,**{key:snapshot[key] for key in fields})
        self.assertEqual('keep',outside.read_text(encoding='utf-8'))

    def test_missing_file_and_temporary_access_error_do_not_delete_registry(self):
        old=self.ready()
        with patch.object(Path,'open',side_effect=PermissionError('temporary')):
            self.assertIsNone(results.get_ready_result(old.vod_id))
        self.assertEqual(old.id,results.get_ready_result(old.vod_id)['id'])
        old.path().unlink()
        self.assertIsNone(results.get_ready_result(old.vod_id))
        self.assertEqual('ready',self.rows()[0]['state'])

    def test_old_cache_migration_preserves_history_and_is_repeatable(self):
        ref=html_publisher.publish_html_local('<html>legacy</html>')
        cache_id=db.upsert_cache('123456789','html_online','vod',{'html_url':ref,'title':'legacy'},{'messages':2},[])
        db.add_user_history(7,cache_id)
        # Remove only new schema in this test DB to simulate the installed old schema.
        con=db_core._connect()
        try:
            con.execute('DROP TABLE html_current')
            con.execute('DROP TABLE html_results')
            con.commit()
        finally:
            con.close()
        db.init_db()
        db.init_db()
        self.assertEqual(1,len(self.rows()))
        self.assertEqual('legacy',self.rows()[0]['origin'])
        self.assertEqual(cache_id,results.get_ready_result('123456789')['cache_id'])
        self.assertEqual(1,db.get_history_count(7))
        self.assertTrue((self.root/'html'/ref[6:]).exists())

    def test_unknown_missing_and_ambiguous_legacy_records_remain_explicit(self):
        ref=html_publisher.publish_html_local('<html>legacy</html>')
        for vod,value in (('1',ref),('2',ref),('3','https://unknown.invalid/file.html'),
                          ('4','local:chat_'+'a'*32+'.html')):
            db.upsert_cache(vod,'html_online','vod',{'html_url':value},{},[])
        db.init_db()
        self.assertEqual(4,len(self.rows()))
        self.assertIsNotNone(results.get_ready_result('1'))
        self.assertTrue(all(results.get_ready_result(vod) is None for vod in ('2','3','4')))
        self.assertEqual(3,sum(row['state']=='unverified' for row in self.rows()))

    def test_html_has_no_ttl_and_old_ready_result_remains_reusable(self):
        old=self.ready()
        con=db_core._connect()
        try:
            con.execute('UPDATE vod_cache SET cached_at=1')
            con.commit()
        finally:
            con.close()
        self.assertFalse(db.cache_is_expired(db.get_cache(old.vod_id,'html_online')))
        self.assertEqual(old.id,results.get_ready_result(old.vod_id)['id'])

    def test_database_constraints_forbid_unready_or_cross_vod_current_pointer(self):
        building=self.plan()
        con=db_core._connect()
        try:
            with self.assertRaises(sqlite3.IntegrityError):
                con.execute('INSERT INTO html_current VALUES(?,?,?)',('123456789','html_online',building.id))
            con.rollback()
        finally:
            con.close()

    def test_registered_stream_is_byte_identical_and_hashes_actual_file_bytes(self):
        plan=self.plan()
        rows=[{'t':'00:00:01','user':'Юзер','text':'</SCRIPT> & 🎮','fragments':[{'text':'x'}],'badges':[]}]
        options={'title':'title','channel':'channel','vod_url':'vod'}
        html_publisher.publish_html_stream(lambda output:write_viewer_html(output,rows,**options),publication=plan)
        self.assertEqual(render_viewer_html(rows,**options),plan.path().read_text(encoding='utf-8'))
        row=self.rows()[0]
        self.assertEqual(hashlib.sha256(plan.path().read_bytes()).hexdigest(),row['sha256'])
        self.assertEqual(plan.path().stat().st_size,row['size_bytes'])

    def test_public_generations_keep_existing_public_url_layout(self):
        plan=self.plan(mode='public')
        self.publish(plan)
        plan.complete()
        plan.close()
        ready=results.get_ready_result(plan.vod_id)
        self.assertEqual(f'https://registry.invalid/public_html/{plan.filename}',ready['html_ref'])
        self.assertTrue(plan.path().is_file())

    def test_legacy_temporary_access_failure_is_rechecked_without_losing_history(self):
        ref=html_publisher.publish_html_local('<html>legacy</html>')
        cache=db.upsert_cache('123456789','html_online','vod',{'html_url':ref},{},[])
        db.add_user_history(7,cache)
        with patch.object(Path,'open',side_effect=PermissionError('temporary access error')):
            db.init_db()
        self.assertEqual('unverified',self.rows()[0]['state'])
        self.assertIsNotNone(results.get_ready_result('123456789'))
        self.assertEqual(1,db.get_history_count(7))

    def test_changed_head_prevents_old_operation_overwriting_new_generation(self):
        old=self.ready()
        first=self.plan()
        self.publish(first,'first')
        second=self.ready('second')
        with self.assertRaisesRegex(ValueError,'changed'):
            first.complete()
        self.assertEqual(second.id,results.get_ready_result(old.vod_id)['id'])
        self.assertTrue(old.path().exists())

    def test_unknown_files_and_invalid_temporary_path_are_never_deleted(self):
        plan=self.plan()
        sentinel=self.root/'keep.txt'
        sentinel.write_text('user data',encoding='utf-8')
        unknown=plan.root/'unknown.html'
        unknown.write_text('unknown',encoding='utf-8')
        self.change(plan.id,temp_name='../keep.txt')
        plan.close()
        results.recover_results()
        self.assertEqual('user data',sentinel.read_text(encoding='utf-8'))
        self.assertTrue(unknown.exists())

    def test_preexisting_temp_collision_is_not_deleted_by_recovery(self):
        plan=self.plan()
        temp=plan.root/plan.temp_name
        temp.write_text('foreign temp',encoding='utf-8')
        with self.assertRaises(FileExistsError) as caught:
            self.publish(plan)
        plan.fail(caught.exception)
        plan.close()
        results.recover_results()
        self.assertEqual('foreign temp',temp.read_text(encoding='utf-8'))
        os.utime(temp,(1,1))
        self.assertEqual(0,html_publisher.cleanup_stale_html_temp_files(protected=results.protected_temp_files()))
        self.assertTrue(temp.exists())

    def test_multiple_interrupted_first_publications_are_recovered_once(self):
        plans=[]
        for vod_id in ('101','102','103'):
            plan=self.plan(vod_id)
            self.publish(plan)
            plan.close()
            plans.append(plan)
        results.recover_results()
        results.recover_results()
        self.assertTrue(all(results.get_ready_result(plan.vod_id)['id']==plan.id for plan in plans))
        self.assertEqual(3,len(self.rows()))

    def test_incomplete_legacy_html_is_retained_but_not_offered_until_repaired(self):
        ref=html_publisher.publish_html_local('<html>partial')
        db.upsert_cache('123456789','html_online','vod',{'html_url':ref},{},[])
        db.init_db()
        self.assertIsNone(results.get_ready_result('123456789'))
        path=self.root/'html'/ref[6:]
        self.assertEqual('<html>partial',path.read_text(encoding='utf-8'))
        path.write_text('<html>complete</html>',encoding='utf-8')
        self.assertIsNotNone(results.get_ready_result('123456789'))

    def test_failure_before_generation_insert_creates_no_ready_artifact(self):
        old=self.ready()
        plan=results.Publication('123456789',mode='local')
        self.addCleanup(plan.close)
        with patch.object(results,'_connect',side_effect=sqlite3.OperationalError('insert unavailable')):
            with self.assertRaises(sqlite3.OperationalError):
                plan.reserve()
        plan.close()
        self.assertEqual(1,len(self.rows()))
        self.assertEqual(old.id,results.get_ready_result(old.vod_id)['id'])
        self.assertFalse(plan.path().exists())

    def test_crash_after_registration_before_any_file_is_recoverable(self):
        old=self.ready()
        plan=self.plan()
        plan.close()
        results.recover_results()
        results.recover_results()
        self.assertEqual('interrupted',self.rows()[-1]['state'])
        self.assertEqual(old.id,results.get_ready_result(old.vod_id)['id'])

    def test_unreserved_or_closed_publication_cannot_write_files(self):
        plan=results.Publication('123456789',mode='local')
        with self.assertRaisesRegex(ValueError,'reserved'):
            self.publish(plan)
        self.assertFalse(plan.path().exists())

    def test_real_process_exit_after_publication_recovers_without_replacing_old_head(self):
        old=self.ready()
        code="""
import os,sys
from src import config,html_publisher
from src.database import db_core,database,db_results
db_core.DB_PATH=sys.argv[1]
config.SPOOL_DIR=sys.argv[2]
html_publisher.OUT_DIR=sys.argv[3]
database.init_db()
p=db_results.Publication('123456789',mode='local')
p.reserve()
p.meta={'title':'interrupted'}
p.stats={'messages':1,'unique_users':1,'parts':0}
html_publisher.publish_html_stream(lambda out:out.write('<html>complete</html>'),publication=p)
os._exit(0)
"""
        subprocess.run([sys.executable,'-B','-c',code,str(self.root/'app.sqlite3'),str(self.root/'work'),str(self.root/'html')],
                       cwd=Path(__file__).resolve().parents[1],check=True,timeout=10)
        results.recover_results()
        results.recover_results()
        self.assertEqual(old.id,results.get_ready_result(old.vod_id)['id'])
        self.assertTrue(all(row['state']=='ready' for row in self.rows()))
        self.assertEqual(2,len(self.rows()))


if __name__=='__main__':
    unittest.main()
