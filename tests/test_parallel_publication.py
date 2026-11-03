import concurrent.futures
import threading
import unittest
import test_result_registry as fixture
from src.database import database as db, db_results as results
from src import html_publisher
from src.chat_spool import ChatSpool,encode_row,cleanup_orphaned_chat_spools


class ParallelPublication(unittest.TestCase):
    setUp=fixture.ResultRegistryTestCase.setUp

    def test_two_spools_keep_rows_and_cleanup_isolated(self):
        root=self.root/'work';barrier=threading.Barrier(2)
        def spool(label):
            s=ChatSpool(root);s.open()
            s.append_batch([encode_row({'user':label,'text':label})]);barrier.wait(timeout=3)
            return s
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            a=pool.submit(spool,'a');b=pool.submit(spool,'b');a,b=a.result(),b.result()
        try:
            self.assertNotEqual(a.directory,b.directory)
            self.assertEqual(0,cleanup_orphaned_chat_spools(root))
            a.close()
            self.assertEqual([{'user':'b','text':'b'}],list(b.iter_rows()))
        finally:a.close();b.close()
        self.assertEqual([],list(root.iterdir()))

    def test_two_real_publications_preserve_history_files_and_old_generation(self):
        old=results.Publication('123456789',mode='local');old.reserve()
        html_publisher.publish_html_stream(lambda out:out.write('old'),local=True,publication=old)
        old_cache=old.complete();db.add_user_history(1,old_cache);old.close()
        barrier=threading.Barrier(2)
        def write(vod,text):
            p=results.Publication(vod,mode='local');p.reserve()
            try:
                def output(out):
                    out.write(text[:1]);barrier.wait(timeout=3);out.write(text[1:])
                html_publisher.publish_html_stream(output,local=True,publication=p)
                cache=p.complete();db.add_user_history(1,cache)
                return p
            finally:p.close()
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            a=pool.submit(write,'123456789','first');b=pool.submit(write,'234567890','second')
            a,b=a.result(),b.result()
        self.assertEqual('first',a.path().read_text());self.assertEqual('second',b.path().read_text())
        self.assertEqual('old',old.path().read_text());self.assertNotEqual(a.id,b.id)
        self.assertEqual(a.id,results.get_ready_result(a.vod_id)['id'])
        self.assertEqual(b.id,results.get_ready_result(b.vod_id)['id'])
        self.assertEqual(2,db.get_history_count(1))  # History remains one entry per VOD/format.

    def test_failed_parallel_write_cleans_only_its_temp_and_preserves_other_result(self):
        barrier=threading.Barrier(2)
        def write(vod,fail):
            p=results.Publication(vod,mode='local');p.reserve()
            try:
                def output(out):
                    out.write('safe');barrier.wait(timeout=3)
                    if fail:raise OSError('synthetic write failure')
                try:
                    html_publisher.publish_html_stream(output,local=True,publication=p)
                    p.complete()
                except OSError as exc:p.fail(exc)
                return p
            finally:p.close()
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            bad=pool.submit(write,'123456789',True);good=pool.submit(write,'234567890',False)
            bad,good=bad.result(),good.result()
        self.assertEqual('safe',good.path().read_text())
        self.assertEqual(good.id,results.get_ready_result(good.vod_id)['id'])
        self.assertIsNone(results.get_ready_result(bad.vod_id))
        self.assertFalse((bad.root/bad.temp_name).exists());self.assertFalse(bad.path().exists())
