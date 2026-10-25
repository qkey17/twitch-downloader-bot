"""Final Stage D audit regressions, using temporary SQLite and mocked APIs."""
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import test_result_registry as registry_fixture
import test_result_callbacks as callback_fixture
from src import config, html_publisher
from src.database import database as db, db_results as results
from src.handlers import handlers_vod
from src.ui import ui_history
from src.ui.ui_formatters import _fmt_len
from src.ui.ui_labels import CHAT_GENERIC


class PublicationAuditRegressionTests(unittest.TestCase):
    setUp = registry_fixture.ResultRegistryTestCase.setUp
    plan = registry_fixture.ResultRegistryTestCase.plan
    publish = registry_fixture.ResultRegistryTestCase.publish
    ready = registry_fixture.ResultRegistryTestCase.ready
    rows = registry_fixture.ResultRegistryTestCase.rows
    change = registry_fixture.ResultRegistryTestCase.change

    def test_collision_process_exit_before_fail_retains_foreign_temp(self):
        old = self.ready()
        code = """
import os,sys
from src import config,html_publisher
from src.database import db_core,db_results
db_core.DB_PATH=sys.argv[1]
config.SPOOL_DIR=sys.argv[2]
html_publisher.OUT_DIR=sys.argv[3]
p=db_results.Publication('123456789',mode='local')
p.reserve()
(p.root/p.temp_name).write_text('foreign temp',encoding='utf-8')
try:
    html_publisher.publish_html_stream(lambda out:out.write('<html>new</html>'),publication=p)
except FileExistsError:
    os._exit(0)  # No fail(), close(), or Python cleanup.
os._exit(2)
"""
        subprocess.run([sys.executable, '-B', '-c', code, str(self.root/'app.sqlite3'),
                        str(self.root/'work'), str(self.root/'html')],
                       cwd=Path(__file__).resolve().parents[1], check=True, timeout=10)
        row = self.rows()[-1]
        temp = Path(row['root'])/row['temp_name']
        for _ in range(2):
            results.recover_results()
            self.assertEqual('foreign temp', temp.read_text(encoding='utf-8'))
            os.utime(temp, (1, 1))
            html_publisher.cleanup_stale_html_temp_files(protected=results.protected_temp_files())
            self.assertTrue(temp.exists())
            self.assertEqual(old.id, results.get_ready_result(old.vod_id)['id'])
        self.assertTrue(old.path().exists())

    def test_recovery_retains_substituted_temp_even_with_matching_contents(self):
        old = self.ready()
        for hardlink in (False, True):
            with self.subTest(hardlink=hardlink):
                plan = self.plan()
                self.publish(plan)
                content = plan.path().read_bytes()
                temp = plan.root/plan.temp_name
                other = self.root/f'foreign-{plan.id}.html'
                other.write_bytes(content)
                if hardlink:
                    os.link(other, temp)
                else:
                    temp.write_bytes(content)
                plan.close()
                for _ in range(2):
                    results.recover_results()
                    self.assertEqual(content, temp.read_bytes())
                    self.assertEqual(content, other.read_bytes())
                    self.assertEqual(old.id, results.get_ready_result(old.vod_id)['id'])
                self.assertEqual('ready', self.rows()[-1]['state'])

    def test_ready_generation_temp_name_is_not_age_cleanup_authority(self):
        plan = self.ready()
        temp = plan.root/plan.temp_name
        temp.write_text('unproven file', encoding='utf-8')
        os.utime(temp, (1, 1))
        html_publisher.cleanup_stale_html_temp_files(protected=results.protected_temp_files())
        self.assertTrue(temp.exists())

    def test_mismatched_registered_temp_name_is_retained_by_both_cleanups(self):
        plan = self.plan()
        name = '.chat_'+'b'*32+'.html.'+'c'*32+'.tmp'
        temp = plan.root/name
        temp.write_text('foreign name', encoding='utf-8')
        os.utime(temp, (1, 1))
        self.change(plan.id, temp_name=name)
        plan.close()
        for _ in range(2):
            results.recover_results()
            html_publisher.cleanup_stale_html_temp_files(protected=results.protected_temp_files())
            self.assertEqual('foreign name', temp.read_text(encoding='utf-8'))

    def test_publisher_does_not_link_or_delete_replaced_temp(self):
        old = self.ready()
        plan = self.plan()
        stage = plan.stage

        def substitute(*args):
            stage(*args)
            temp = plan.root/plan.temp_name
            # Keep the old inode alive; identical bytes do not prove ownership.
            saved = self.root/'original.tmp'
            temp.rename(saved)
            temp.write_bytes(saved.read_bytes())

        with patch.object(plan, 'stage', side_effect=substitute):
            with self.assertRaises(ValueError):
                self.publish(plan)
        self.assertTrue((plan.root/plan.temp_name).exists())
        self.assertFalse(plan.path().exists())
        self.assertEqual(old.id, results.get_ready_result(old.vod_id)['id'])

    def test_public_urls_are_canonical_for_generations_and_existing_publishers(self):
        for prefix in ('/public_html', '/public_html/'):
            for base in ('https://registry.invalid', 'https://registry.invalid/base/'):
                with self.subTest(prefix=prefix, base=base), \
                     patch.object(html_publisher, 'PUBLIC_HTML_URL_PREFIX', prefix), \
                     patch.object(html_publisher, 'PUBLIC_BASE_URL', base):
                    plan = self.plan(mode='public')
                    self.publish(plan)
                    plan.complete()
                    plan.close()
                    expected = base.rstrip('/')+'/public_html/'+plan.filename
                    self.assertEqual(expected, plan.ref)
                    self.assertEqual(plan.id, results.get_ready_result(plan.vod_id)['id'])
                    ref = html_publisher.publish_html('<html>legacy API</html>')
                    self.assertTrue(ref.startswith(base.rstrip('/')+'/public_html/'))
                    self.assertNotIn('//', ref.split('://', 1)[1])
                    ref = html_publisher.publish_html_stream(lambda out:out.write('<html>stream</html>'))
                    self.assertTrue(html_publisher.local_published_html_status(ref))

    def test_saved_double_slash_urls_work_with_history_on_and_off(self):
        for prefix in ('/public_html', '/public_html/'):
            for enabled in (False, True):
                with self.subTest(prefix=prefix, history=enabled), \
                     patch.object(html_publisher, 'PUBLIC_HTML_URL_PREFIX', prefix), \
                     patch.object(config, 'HISTORY_ENABLED', enabled), \
                     patch.object(ui_history, 'HTML_PUBLISH_MODE', 'public'):
                    ref = html_publisher.publish_html('<html>legacy</html>')
                    saved = 'https://registry.invalid/public_html//'+ref.rsplit('/', 1)[1]
                    vod = str(100+len(self.rows()))
                    cache_id = db.upsert_cache(vod, 'html_online', f'https://www.twitch.tv/videos/{vod}',
                                               {'title':'legacy','html_url':saved}, {}, [])
                    db.init_db()
                    self.assertTrue(html_publisher.local_published_html_status(saved))
                    self.assertEqual(saved, results.get_ready_result(vod)['html_ref'])
                    if enabled:
                        db.add_user_history(7, cache_id)
                        cards, _ = ui_history.build_history_page(db.get_history_for_user(7), 0, 1)
                        buttons = [b for row in cards[0][1].inline_keyboard for b in row]
                        self.assertTrue(any(b.text == CHAT_GENERIC and b.url == saved for b in buttons))

    def test_url_mapping_rejects_external_domains_and_path_escapes(self):
        name = 'chat_'+'a'*32+'.html'
        for url in (f'https://registry.invalid.evil/public_html/{name}',
                    f'https://evil.invalid/public_html/{name}',
                    f'https://registry.invalid@evil.invalid/public_html/{name}',
                    f'https://registry.invalid/other/{name}',
                    f'https://registry.invalid/public_html/../{name}',
                    f'https://registry.invalid/public_html/%2f{name}',
                    f'https://registry.invalid/public_html/{name}?query=1',
                    f'https://registry.invalid/public_html/{name}#fragment'):
            with self.subTest(url=url):
                self.assertIsNone(html_publisher.local_published_html_status(url))
                self.assertIsNone(results._legacy_location(url))

    def test_duration_formatter_handles_invalid_optional_values(self):
        for value in (None, -1, 'invalid', '120', 1.5, float('inf'), float('nan'), True, [], {}):
            with self.subTest(value=value):
                self.assertEqual('—', _fmt_len(value))
        for value, expected in ((0, '—'), (1, '00:00:01'), (3661, '01:01:01')):
            self.assertEqual(expected, _fmt_len(value))

    def test_corrupt_duration_keeps_ready_html_and_stored_metadata(self):
        plan = self.ready()
        for value in (None, -1, 'invalid', '120', 1.5, float('inf'), True, [], {}):
            with self.subTest(value=value):
                raw = json.dumps(dict(plan.meta, length_seconds=value))
                self.change(plan.id, meta_json=raw)
                ready = results.get_ready_result(plan.vod_id)
                self.assertIsNotNone(ready)
                self.assertIsNone(ready['meta']['length_seconds'])
                text, _ = handlers_vod._build_card(handlers_vod._make_item(ready['meta'], ready['stats'], 'vod', 'html_online'))
                self.assertIn('⏱ —', text)
                self.assertEqual(raw, self.rows()[0]['meta_json'])
                self.assertEqual('ready', self.rows()[0]['state'])


class VodLinkAuditRegressionTests(unittest.IsolatedAsyncioTestCase):
    setUp = callback_fixture.ResultCallbacksTestCase.setUp
    asyncTearDown = callback_fixture.ResultCallbacksTestCase.asyncTearDown
    plan = registry_fixture.ResultRegistryTestCase.plan
    publish = registry_fixture.ResultRegistryTestCase.publish
    ready = registry_fixture.ResultRegistryTestCase.ready
    rows = registry_fixture.ResultRegistryTestCase.rows
    request = callback_fixture.ResultCallbacksTestCase.request
    source = callback_fixture.ResultCallbacksTestCase.source

    async def test_supported_vod_links_use_cache_before_twitch(self):
        self.ready()
        variants = ['123456789']
        for scheme in ('https://', 'http://', ''):
            for host in ('twitch.tv', 'www.twitch.tv'):
                for path in ('videos/123456789', 'channel/v/123456789'):
                    for suffix in ('', '/', '?t=1h#chat', '/?t=1h'):
                        variants.append(scheme+host+'/'+path+suffix)
        with patch.object(handlers_vod, 'fetch_vod_meta', AsyncMock(side_effect=AssertionError('Twitch called'))), \
             patch.object(handlers_vod, 'get_client_id', side_effect=AssertionError('credentials read')):
            for text in variants:
                with self.subTest(link=text):
                    update, context = self.request()
                    update.message.text = text
                    await handlers_vod.vod_link_entry(update, context)
                    self.assertEqual('123456789', context.user_data['vod_result_choice']['vod_id'])
                    update.message.reply_text.assert_awaited_once()

    async def test_unsupported_links_do_not_lookup_numbers(self):
        with patch.object(results, 'get_ready_result', side_effect=AssertionError('invalid lookup')):
            for text in ('abc 123456789', 'https://evil.invalid/videos/123456789',
                         'https://twitch.tv.evil/videos/123456789', 'https://www.twitch.tv@evil/videos/123456789',
                         'https://evil/twitch.tv/videos/123456789', 'ftp://twitch.tv/videos/123456789',
                         'https://twitch.tv/videos/123abc', 'https://twitch.tv/videos/123/extra',
                         'https://twitch.tv/?videos=123456789', '0', '9'*21):
                with self.subTest(link=text):
                    update, context = self.request()
                    update.message.text = text
                    await handlers_vod.vod_link_entry(update, context)
                    update.message.reply_text.assert_not_awaited()

    async def test_non_www_cache_miss_keeps_first_request_flow(self):
        update, context = self.request()
        update.message.text = 'https://twitch.tv/videos/123456789/?t=1m'
        with patch.object(handlers_vod, 'get_client_id', return_value='test-client'), \
             patch.object(handlers_vod, 'fetch_vod_meta', AsyncMock(return_value=SimpleNamespace(length_seconds=30))):
            await handlers_vod.vod_link_entry(update, context)
        self.assertEqual(handlers_vod._FORMAT_PROMPT_TEXT, update.message.reply_text.await_args.args[0])
        self.assertEqual('123456789', context.user_data['vod_pending']['vod_id'])
