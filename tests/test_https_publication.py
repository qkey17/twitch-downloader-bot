"""HTTP -> HTTPS migration against disposable files/SQLite; no HTTP requests."""
import unittest
from pathlib import Path
from unittest.mock import patch

import test_result_registry as fixture
import test_html_cleanup as cleanup_fixture
from src import config, html_publisher, html_cleanup as cleanup
from src.database import database as db, db_core, db_results as results
from src.handlers import handlers_vod


class HttpsPublicationTests(unittest.TestCase):
    setUp = fixture.ResultRegistryTestCase.setUp
    plan = fixture.ResultRegistryTestCase.plan
    publish = fixture.ResultRegistryTestCase.publish
    rows = fixture.ResultRegistryTestCase.rows
    run_cleanup = cleanup_fixture.HtmlCleanupTests.run_cleanup
    intent = cleanup_fixture.HtmlCleanupTests.intent

    def snapshot(self, table):
        con = db_core._connect()
        try:
            return [tuple(row) for row in con.execute(f'SELECT * FROM {table} ORDER BY rowid')]
        finally:
            con.close()

    def old_publication(self, vod='123456789', complete=True):
        with patch.object(html_publisher, 'PUBLIC_BASE_URL', 'http://registry.invalid'):
            plan = self.plan(vod, mode='public')
            self.publish(plan)
            if complete:
                plan.complete()
            plan.close()
        return plan

    def test_old_http_and_new_https_with_historical_slashes(self):
        name = 'chat_'+'a'*32+'.html'
        for host in ('registry.invalid', '192.0.2.10'):
            for base_path in ('', '/base/'):
                base = 'https://'+host+base_path
                with self.subTest(base=base), patch.object(html_publisher, 'PUBLIC_BASE_URL', base):
                    canonical = html_publisher.public_html_url(name)
                    for scheme in ('http', 'https'):
                        ref = canonical.replace('https:', scheme+':', 1)
                        for saved in (ref, ref.replace('/public_html/', '//public_html//')):
                            self.assertEqual(name, html_publisher.public_html_filename(saved))
                            self.assertEqual((Path(html_publisher.PUBLIC_HTML_DIR).resolve(), name),
                                             results._legacy_location(saved))

    def test_foreign_authorities_ports_paths_and_invalid_names_are_rejected(self):
        name = 'chat_'+'a'*32+'.html'
        for scheme in ('http', 'https'):
            for host in ('evil.invalid', 'registry.invalid.evil', '192.0.2.11',
                         'registry.invalid:80', 'registry.invalid:443', 'registry.invalid:8443',
                         'registry.invalid:invalid', 'registry.invalid@evil.invalid',
                         'user@registry.invalid', 'registry.in\tvalid'):
                with self.subTest(scheme=scheme, host=host):
                    self.assertIsNone(html_publisher.public_html_filename(f'{scheme}://{host}/public_html/{name}'))
            for path in (f'/other/{name}', f'/public_html/../{name}',
                         f'/public_html/%2f{name}', f'/public_html/nested/{name}',
                         '/public_html/chat_short.html', '/public_html/chat_'+'A'*32+'.html',
                         f'/public_html/{name}.tmp', f'/public_html/{name}/'):
                with self.subTest(scheme=scheme, path=path):
                    self.assertIsNone(html_publisher.public_html_filename(f'{scheme}://registry.invalid{path}'))
            for suffix in ('?query=1', '#fragment', '?', '#', '?q=1#fragment', '\n'):
                with self.subTest(scheme=scheme, suffix=suffix):
                    self.assertIsNone(html_publisher.public_html_filename(
                        f'{scheme}://registry.invalid/public_html/{name}{suffix}'))
        for scheme in ('ftp', 'file', 'javascript'):
            self.assertIsNone(html_publisher.public_html_filename(f'{scheme}://registry.invalid/public_html/{name}'))

    def test_configured_explicit_port_is_not_inferred_or_changed(self):
        name = 'chat_'+'b'*32+'.html'
        with patch.object(html_publisher, 'PUBLIC_BASE_URL', 'https://registry.invalid:8443/base'):
            for scheme in ('http', 'https'):
                self.assertEqual(name, html_publisher.public_html_filename(
                    f'{scheme}://registry.invalid:8443/base/public_html/{name}'))
                for authority in ('registry.invalid', 'registry.invalid:443', 'registry.invalid:80'):
                    self.assertIsNone(html_publisher.public_html_filename(
                        f'{scheme}://{authority}/base/public_html/{name}'))

    def test_http_configuration_does_not_accept_unconfigured_scheme(self):
        name = 'chat_'+'c'*32+'.html'
        with patch.object(html_publisher, 'PUBLIC_BASE_URL', 'http://registry.invalid'):
            self.assertEqual(name, html_publisher.public_html_filename(f'http://registry.invalid/public_html/{name}'))
            self.assertIsNone(html_publisher.public_html_filename(f'https://registry.invalid/public_html/{name}'))

    def test_all_publishers_generate_canonical_https_urls(self):
        with patch.object(html_publisher, 'PUBLIC_BASE_URL', 'https://registry.invalid/base/'), \
             patch.object(html_publisher, 'PUBLIC_HTML_URL_PREFIX', '/public_html/'):
            refs = [html_publisher.publish_html('<html>ordinary</html>'),
                    html_publisher.publish_html_stream(lambda out: out.write('<html>stream</html>'))]
            plan = self.plan(mode='public')
            refs.append(self.publish(plan))
            plan.complete()
            plan.close()
            for ref in refs:
                self.assertTrue(ref.startswith('https://registry.invalid/base/public_html/chat_'))
                self.assertNotIn('//', ref.split('://', 1)[1])
                self.assertTrue(html_publisher.local_published_html_status(ref))
            self.assertEqual(plan.ref, results.get_ready_result(plan.vod_id)['html_ref'])

    def test_existing_published_generation_and_history_are_unchanged_after_switch(self):
        plan = self.old_publication()
        cache = db.get_cache(plan.vod_id, 'html_online')
        db.add_user_history(7, cache['id'])
        before = {table: self.snapshot(table) for table in ('html_results', 'html_current', 'vod_cache', 'user_history')}
        content = plan.path().read_bytes()
        for _ in range(2):
            db.init_db()
            results.recover_results()
            ready = results.get_ready_result(plan.vod_id)
            self.assertEqual(plan.id, ready['id'])
            self.assertEqual(plan.ref, ready['html_ref'])
            self.assertEqual(plan.ref, ready['meta']['html_url'])
            self.assertTrue(html_publisher.local_published_html_status(plan.ref))
            with patch.object(handlers_vod, 'HTML_PUBLISH_MODE', 'public'):
                self.assertTrue(handlers_vod._cache_entry_has_usable_output(cache, 'html_online'))
                self.assertEqual(plan.ref, handlers_vod._html_button_url(ready['html_ref']))
        self.assertEqual(content, plan.path().read_bytes())
        self.assertEqual(before, {table: self.snapshot(table) for table in before})

    def test_old_http_missing_file_is_still_detected(self):
        plan = self.old_publication()
        plan.path().unlink()  # Only this test's disposable output.
        self.assertFalse(html_publisher.local_published_html_status(plan.ref))
        self.assertIsNone(results.get_ready_result(plan.vod_id))
        with patch.object(handlers_vod, 'HTML_PUBLISH_MODE', 'public'):
            self.assertFalse(handlers_vod._cache_entry_has_usable_output(db.get_cache(plan.vod_id, 'html_online'), 'html_online'))
        self.assertEqual('ready', self.rows()[0]['state'])

    def test_legacy_http_cache_migrates_repeatably_without_rewriting_saved_url(self):
        with patch.object(html_publisher, 'PUBLIC_BASE_URL', 'http://registry.invalid'):
            ref = html_publisher.publish_html('<html>legacy</html>')
        saved = ref.replace('/public_html/', '/public_html//')
        cache_id = db.upsert_cache('123456789', 'html_online', 'vod', {'html_url': saved}, {}, [])
        db.add_user_history(7, cache_id)
        cache_before, history_before = self.snapshot('vod_cache'), self.snapshot('user_history')
        for _ in range(2):
            db.init_db()
            ready = results.get_ready_result('123456789')
            self.assertEqual('legacy', ready['origin'])
            self.assertEqual(saved, ready['html_ref'])
            self.assertEqual(cache_id, ready['cache_id'])
            self.assertEqual(1, len(self.rows()))
        self.assertEqual(cache_before, self.snapshot('vod_cache'))
        self.assertEqual(history_before, self.snapshot('user_history'))

    def test_unverified_legacy_http_cache_is_retried_after_switch(self):
        with patch.object(html_publisher, 'PUBLIC_BASE_URL', 'http://registry.invalid'):
            ref = html_publisher.publish_html('<html>legacy</html>')
        db.upsert_cache('123456789', 'html_online', 'vod', {'html_url': ref}, {}, [])
        # Simulate a prior startup where the configured origin did not match.
        with patch.object(html_publisher, 'PUBLIC_BASE_URL', 'https://other.invalid'):
            db.init_db()
        key = self.rows()[0]['id']
        self.assertEqual('unverified', self.rows()[0]['state'])
        db.init_db()
        self.assertEqual(key, results.get_ready_result('123456789')['id'])
        self.assertEqual(ref, self.rows()[0]['html_ref'])

    def test_interrupted_http_publication_recovers_after_switch(self):
        plan = self.old_publication(complete=False)
        content = plan.path().read_bytes()
        for _ in range(2):
            results.recover_results()
            ready = results.get_ready_result(plan.vod_id)
            self.assertEqual(plan.id, ready['id'])
            self.assertEqual(plan.ref, ready['html_ref'])
            self.assertEqual(plan.ref, db.get_cache(plan.vod_id, 'html_online')['html_url'])
            self.assertEqual(content, plan.path().read_bytes())

    def test_interrupted_regeneration_keeps_previous_http_head_after_switch(self):
        old = self.old_publication()
        new = self.old_publication(complete=False)
        for _ in range(2):
            results.recover_results()
            self.assertEqual(old.id, results.get_ready_result(old.vod_id)['id'])
            self.assertTrue(all(row['state'] == 'ready' for row in self.rows()))
            self.assertTrue(old.path().exists())
            self.assertTrue(new.path().exists())

    def test_https_regeneration_preserves_old_http_generation(self):
        old = self.old_publication()
        new = self.plan(mode='public')
        self.assertEqual(old.id, new.previous_id)
        self.publish(new)
        new.complete()
        new.close()
        self.assertEqual(new.id, results.get_ready_result(old.vod_id)['id'])
        self.assertTrue(new.ref.startswith('https://'))
        self.assertEqual(old.ref, self.rows()[0]['html_ref'])
        self.assertTrue(old.path().exists())
        self.assertTrue(html_publisher.local_published_html_status(old.ref))

    def test_cleanup_deletes_old_http_generations_and_preserves_new_https_head(self):
        old = self.old_publication()
        db.add_user_history(7, db.get_cache(old.vod_id, 'html_online')['id'])
        new = self.plan(mode='public')
        self.publish(new)
        new.complete()
        new.close()
        with patch.object(config, 'HTML_PUBLISH_MODE', 'public'):
            self.run_cleanup([4*cleanup_fixture.GIB, 8*cleanup_fixture.GIB])
        self.assertFalse(old.path().exists())
        self.assertTrue(new.path().exists())
        self.assertEqual('done', self.intent(old.id)['phase'])
        self.assertEqual(new.id, results.get_ready_result(new.vod_id)['id'])
        self.assertEqual(new.ref, db.get_cache(new.vod_id, 'html_online')['html_url'])
        self.assertEqual(1, db.get_history_count(7))

    def test_cleanup_deleted_http_head_is_not_resurrected(self):
        plan = self.old_publication()
        with patch.object(config, 'HTML_PUBLISH_MODE', 'public'):
            self.run_cleanup([4*cleanup_fixture.GIB, 8*cleanup_fixture.GIB])
        self.assertFalse(plan.path().exists())
        self.assertIsNone(db.get_cache(plan.vod_id, 'html_online')['html_url'])
        for _ in range(2):
            db.init_db()
            results.recover_results()
            self.assertIsNone(results.get_ready_result(plan.vod_id))
        self.assertEqual('deleted', self.rows()[0]['state'])

    def test_interrupted_http_deletion_resumes_after_https_switch(self):
        plan = self.old_publication()
        with patch.object(html_publisher, 'PUBLIC_BASE_URL', 'http://registry.invalid'), \
             patch.object(config, 'HTML_PUBLISH_MODE', 'public'), \
             patch.object(cleanup, '_unlink_owned', side_effect=cleanup_fixture.Crash()):
            with self.assertRaises(cleanup_fixture.Crash):
                self.run_cleanup()
        self.assertEqual('quarantined', self.intent(plan.id)['phase'])
        with patch.object(config, 'HTML_PUBLISH_MODE', 'public'):
            self.run_cleanup(4*cleanup_fixture.GIB)
            self.run_cleanup(40*cleanup_fixture.GIB)
        self.assertEqual('done', self.intent(plan.id)['phase'])
        self.assertEqual('deleted', self.rows()[0]['state'])
        self.assertIsNone(results.get_ready_result(plan.vod_id))
        self.assertFalse(plan.path().exists())


if __name__ == '__main__':
    unittest.main()
