"""Stable publication and download against disposable files and SQLite."""
import os
from concurrent.futures import ThreadPoolExecutor
import threading
import unittest
from unittest.mock import patch

import test_result_registry as fixture
from src import html_cleanup, html_publisher
from src.database import database as db, db_results as results
from src.ready_html import ReadyHtml


class CurrentVodHtmlTests(unittest.TestCase):
    setUp = fixture.ResultRegistryTestCase.setUp
    publish = fixture.ResultRegistryTestCase.publish
    ready = fixture.ResultRegistryTestCase.ready
    plan = fixture.ResultRegistryTestCase.plan
    rows = fixture.ResultRegistryTestCase.rows

    def staged(self, text, *, mode='public', plan=None):
        if plan is None:
            plan = results.Publication('123456789', mode=mode, stable=True)
            self.addCleanup(plan.close)
            plan.reserve()
        plan.meta = {'title': text, 'channel': 'channel'}
        plan.stats = {'messages': 1}
        self.publish(plan, text)
        return plan

    def commit(self, plan):
        cache_id = plan.complete()
        plan.close()
        return cache_id

    def test_first_publish_and_replacement_keep_random_name_history_and_old_callback(self):
        first = self.staged('first')
        self.assertRegex(first.filename, r'chat_[0-9a-f]{32}\.html')
        self.assertFalse(first.path().exists())
        self.assertIsNone(results.get_ready_result(first.vod_id))
        cache = self.commit(first)
        db.add_user_history(7, cache)
        second = self.staged('second version')
        self.assertEqual(first.ref, second.ref)
        self.assertEqual(first.id, second.result_id)
        self.assertEqual('<html>first</html>', first.path().read_text())
        self.assertEqual(cache, self.commit(second))
        db.init_db()
        self.assertEqual([first.path()], list(first.root.iterdir()))
        self.assertEqual(first.ref, db.get_history_for_user(7)[0]['html_url'])
        download = ReadyHtml(first.vod_id, first.id)  # ID stored on the first card
        try:
            download.open(threading.Event())
            self.assertEqual(b'<html>second version</html>', download.handle.read())
        finally:
            download.close()

    def test_adopts_only_current_verified_file_leaving_older_link_intact(self):
        historical = self.ready('history')
        old = self.ready('current')
        update = self.staged('new', mode='local')
        self.assertEqual(old.ref, update.ref)
        self.commit(update)
        self.assertEqual('<html>history</html>', historical.path().read_text())
        self.assertEqual('<html>new</html>', old.path().read_text())

    def test_cancel_or_failed_replace_preserves_working_version(self):
        first = self.staged('working')
        self.commit(first)
        for commit in (False, True):
            plan = self.staged('unfinished')
            if commit:
                with patch.object(results.os, 'replace', side_effect=OSError('disk failure')):
                    with self.assertRaises(OSError):
                        plan.complete()
            plan.fail(OSError('cancelled or failed'))
            plan.close()
            self.assertEqual('<html>working</html>', first.path().read_text())
            self.assertEqual(first.id, results.get_ready_result(first.vod_id)['id'])
            self.assertEqual([first.path()], list(first.root.iterdir()))

    def test_duplicate_writer_and_cleanup_cannot_displace_stable_file(self):
        first = self.staged('working')
        self.commit(first)
        plan = self.staged('new')
        second_writer = results.Publication(first.vod_id, mode='public', stable=True)
        with ThreadPoolExecutor(max_workers=1) as pool:
            with self.assertRaisesRegex(RuntimeError, 'already in progress'):
                pool.submit(second_writer.reserve).result(timeout=5)
        second_writer.close()
        row = results.get_ready_result(first.vod_id)
        registered = self.rows()[0]
        self.assertEqual(row['id'], registered['id'])
        self.assertFalse(html_cleanup._attempt(registered))
        self.commit(plan)
        self.assertFalse(html_cleanup._attempt(self.rows()[0]))
        self.assertEqual([first.path()], list(first.root.glob('*.html')))

    def test_first_publication_recovers_after_link_before_commit(self):
        plan = self.staged('first')
        with patch.object(html_publisher, 'sync_html_directory', side_effect=OSError('lost before SQLite commit')):
            with self.assertRaises(OSError):
                plan.complete()
        self.assertEqual('<html>first</html>', plan.path().read_text())
        plan.close()
        self.assertEqual(plan.id, results.get_ready_result(plan.vod_id)['id'])
        self.assertEqual([plan.path()], list(plan.root.iterdir()))

    def test_crash_after_replace_is_recovered_from_durable_proof(self):
        first = self.staged('working')
        self.commit(first)
        second = self.staged('new version')
        with patch.object(html_publisher, 'sync_html_directory', side_effect=OSError('lost before SQLite commit')):
            with self.assertRaises(OSError):
                second.complete()
        # Simulate startup without the publisher's normal close-time recovery.
        second._lease.close()
        second._lease = None
        second._vod_lease.close()
        second._vod_lease = None
        self.assertEqual('<html>new version</html>', first.path().read_text())
        results.recover_results()
        results.recover_results()
        row = results.get_ready_result(first.vod_id)
        self.assertEqual((first.id, first.ref, 'new version'), (row['id'], row['html_ref'], row['meta']['title']))
        self.assertEqual(1, len(self.rows()))

    def test_interrupted_staging_with_identical_bytes_does_not_replace_metadata(self):
        first = self.staged('same')
        self.commit(first)
        second = self.staged('same')
        second.close()
        results.recover_results()
        self.assertEqual(first.id, results.get_ready_result(first.vod_id)['id'])
        self.assertEqual('interrupted', self.rows()[-1]['state'])

    def test_stable_missing_or_unsafe_target_never_allocates_another_public_name(self):
        first = self.staged('working')
        self.commit(first)
        first.path().unlink()
        retry = results.Publication(first.vod_id, mode='public', stable=True)
        self.addCleanup(retry.close)
        with self.assertRaisesRegex(OSError, 'target is unavailable'):
            retry.reserve()
        self.assertEqual(1, len(self.rows()))

    @unittest.skipUnless(os.name == 'posix', 'open descriptors survive rename on POSIX')
    def test_active_upload_keeps_its_inode_while_new_upload_gets_latest(self):
        first = self.staged('first')
        self.commit(first)
        download = ReadyHtml(first.vod_id, first.id)
        latest = ReadyHtml(first.vod_id, first.id)
        try:
            download.open(threading.Event())
            self.commit(self.staged('second'))
            latest.open(threading.Event())
            self.assertEqual(b'<html>first</html>', download.handle.read())
            self.assertEqual(b'<html>second</html>', latest.handle.read())
        finally:
            download.close()
            latest.close()
