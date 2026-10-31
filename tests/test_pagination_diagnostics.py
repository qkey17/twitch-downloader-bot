import asyncio
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
import uuid

from src import pagination_diagnostics as d
from src.twitch_api import fetch_comments
from src.twitch_api.client import TwitchAPIError
from src.workers import download_worker as worker


def edge(number, offset=10, text='private message', author='private author'):
    return {'cursor': 'opaque-private-cursor', 'node': {
        'id': str(uuid.UUID(int=number)), 'contentOffsetSeconds': offset,
        'createdAt': '2026-01-01T00:00:10.123Z',
        'commenter': {'displayName': author}, 'message': {'fragments': [{'text': text}]}}}


class Capture(d.Journal):
    def __init__(self, start=10, end=15):
        super().__init__(d.Settings('123456789', start, end, Path('unused')))
        self.records = []

    def _write(self, record):
        self.size += len(json.dumps(record, ensure_ascii=True, separators=(',', ':'))) + 1
        self.records.append(record)

    def _finish_file(self):
        self.stopped = True


class PageEvidenceTests(unittest.TestCase):
    def test_lease_helper_receives_file_object_without_closing_descriptor(self):
        with tempfile.TemporaryFile('w+b') as original:
            fd = original.fileno()
            def lock(handle):
                self.assertEqual(fd, handle.fileno())
                return True
            with patch.object(d, 'try_lock_file', side_effect=lock):
                self.assertTrue(d._lock_fd(fd))
            self.assertEqual(0, os.fstat(fd).st_size)

    def test_default_and_other_vod_never_open_storage(self):
        for env in ({}, {'TCD_PAGINATION_DIAGNOSTICS': 'true', 'TCD_PAGINATION_DIAG_VOD': '987654321'}):
            with patch.dict(os.environ, env, clear=True), patch.object(d.Journal, 'open') as opened:
                self.assertIsNone(d.begin('123456789'))
                opened.assert_not_called()

    def test_identical_boundaries_and_count_do_not_hide_internal_id_change(self):
        a = [edge(i) for i in (1, 2, 3, 4)]
        b = [edge(i) for i in (1, 5, 3, 4)]
        c = [edge(i) for i in (1, 3, 2, 4)]
        self.assertEqual(a[0], b[0])
        self.assertEqual(a[-1], b[-1])
        self.assertEqual(len(a), len(b))
        self.assertNotEqual(d.ids_hash(a), d.ids_hash(b))
        self.assertNotEqual(d.ids_hash(a), d.ids_hash(c))
        self.assertEqual(d.ids_hash(a), hashlib.sha256(json.dumps(
            [e['node']['id'] for e in a], ensure_ascii=True, separators=(',', ':')).encode()).hexdigest())

    def test_unambiguous_serialization_and_chunked_unicode_content(self):
        def sequence(values):
            return [{'node': {'id': v}} for v in values]
        self.assertNotEqual(d.ids_hash(sequence(['ab', 'c'])), d.ids_hash(sequence(['a', 'bc'])))
        value = {'message': {'text': '🎮"\\\n' * 5000}, 'commenter': {'name': 'hidden'}}
        expected = hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True,
                                             separators=(',', ':'), allow_nan=False).encode()).hexdigest()
        self.assertEqual(expected, d.content_hash(value))

    def test_intersection_records_whole_page_and_equal_offsets(self):
        journal = Capture()
        entries = [edge(1, 0), edge(2, 20), edge(3, 20)]
        journal.request(7, 2, 'secret cursor', 0)
        journal.response(entries, 'secret next cursor', True)
        details = [r for r in journal.records if r['event'] == 'message_received']
        self.assertEqual([0, 1, 2], [r['position'] for r in details])
        self.assertEqual([0, 20, 20], [r['offset'] for r in details])
        self.assertEqual(3, len({r['id'] for r in details}))
        summary = next(r for r in journal.records if r['event'] == 'page_received')
        self.assertIsNone(summary['emitted'])
        self.assertIsNone(summary['spooled'])
        self.assertTrue(summary['identity_complete'])
        self.assertTrue(journal.records[-1]['complete'])
        journal.response([edge(4, 9)], None, False)
        self.assertEqual(3, sum(r['event'] == 'message_received' for r in journal.records))

    def test_private_fields_only_and_invalid_identity_marked(self):
        journal = Capture()
        item = edge(1)
        item['node']['id'] = 'not-an-id-with-secret'
        journal.request(1, 1, 'private cursor credential', 0)
        journal.response([item], 'private continuation credential', False)
        journal.error('request', RuntimeError('private exception credential'))
        text = json.dumps(journal.records)
        for forbidden in ('private message', 'private author', 'private cursor', 'private continuation',
                          'private exception', 'not-an-id-with-secret', 'Client-Id', 'Authorization'):
            self.assertNotIn(forbidden, text)
        summary = next(r for r in journal.records if r['event'] == 'page_received')
        self.assertFalse(summary['identity_complete'])
        self.assertIsNone(summary['first_id'])

    def test_window_truncation_is_explicit_and_never_marked_complete(self):
        journal = Capture()
        with patch.object(d, 'MAX_BYTES', 5200):
            journal.response([edge(i) for i in range(100)], 'next', True)
        self.assertTrue(journal.stopped)
        self.assertEqual('truncated', journal.records[-1]['event'])
        self.assertTrue(journal.records[-1]['incomplete'])
        self.assertLessEqual(journal.size, 5200)
        self.assertFalse(any(r['event'] == 'detail_end' for r in journal.records))

    def test_offline_equivalent_anonymous_projections_123_131_133(self):
        # Captured set/sequence relationships, relabelled to anonymous integers.
        # Synthetic offsets/text/UUIDs; control is 1..155, outside-window 156+.
        head = list(range(55, 70))
        original = head + list(range(77, 115)) + [137, 138, 140, 142, 143, 144, 145, 146, 147,
                    148, 149, 151, 152, 153, 154, 155, 156, 157, 158, 159]
        second = head + list(range(77, 115)) + [129, 130, 131, 132, 133, 134, 135, 136,
                    140, 142, 144, 145, 146, 147, 148, 149, 151, 153, 156, 158, 159]
        third = head + list(range(77, 92)) + list(range(107, 115)) + [129, 130, 131, 132, 133,
                    134, 135, 136, 137, 139, 140, 141, 142, 143, 148, 153, 154, 155, 160, 159, 161, 162]
        self.assertEqual([73, 74, 60], [len(x) for x in (original, second, third)])
        seen = {str(uuid.UUID(int=i)) for i in range(1, 55)}
        counts = []
        for values in (original, second, third):
            journal = Capture()
            journal.response([edge(i, offset=10 if i <= 155 else 100) for i in values], 'same', True)
            seen.update(r['id'] for r in journal.records if r['event'] == 'message_received' and 10<=r['offset']<=15)
            counts.append(len(seen))
        self.assertEqual([123, 131, 133], counts)


class Reply:
    status = 200
    def __init__(self, edges, has_next):
        self.data = [{'data': {'video': {'comments': {'edges': edges, 'pageInfo': {'hasNextPage': has_next}}}}}]
    async def __aenter__(self): return self
    async def __aexit__(self, *args): return False
    async def json(self): return self.data


class Session:
    def __init__(self): self.calls = 0
    def post(self, *args, **kwargs):
        self.calls += 1
        return Reply([edge(1), edge(2, 11, text='  ')] if self.calls == 1 else [edge(3, 12)], self.calls == 1)


class WorkerEvidenceTests(unittest.IsolatedAsyncioTestCase):
    async def run_worker(self, journal=None, fail=None):
        session = Session()
        rows = []
        async def html(**kwargs):
            rows.extend(kwargs['chat_rows'].iter_rows())
            if fail is not None:
                raise fail
            return [], 'unchanged-result'
        def begin(vod):
            if journal is not None:
                journal.token = d.CURRENT.set(journal)
            return journal
        with tempfile.TemporaryDirectory() as temp, \
             patch.object(worker, 'begin', side_effect=begin), \
             patch.object(worker, 'OUT_DIR', temp), patch.object(worker, 'SPOOL_DIR', temp), \
             patch.object(worker, 'get_client_id', return_value='private-client-id'), \
             patch.object(worker, 'fetch_vod_meta', AsyncMock(return_value=SimpleNamespace(
                 title='t', channel='c', length_seconds=20, created_at=None,
                 thumbnail_preview_url=None, thumbnail_full_url=None))), \
             patch.object(worker, 'make_progress_updater', return_value=AsyncMock()), \
             patch.object(worker, 'build_html_result', side_effect=html), \
             patch.object(fetch_comments, '_sleep_with_cancel', AsyncMock()):
            context = SimpleNamespace(application=SimpleNamespace(bot_data={'aiohttp': session}), user_data={})
            result = await worker.download_and_send(context, 1, 2, 'url', '123456789', 'html_online')
            self.assertFalse(list(Path(temp).glob('tdb-spool-*')))
        self.assertIsNone(d.CURRENT.get())
        return result, rows, session.calls

    async def test_enabled_evidence_preserves_rows_stats_and_request_count(self):
        baseline = await self.run_worker()
        journal = Capture()
        observed = await self.run_worker(journal)
        self.assertEqual(baseline, observed)
        self.assertEqual(2, observed[-1])
        pages = [r for r in journal.records if r['event'] == 'page_emitted']
        self.assertEqual([1, 1], [r['emitted'] for r in pages])
        self.assertEqual([1, 0], [r['discarded'] for r in pages])
        self.assertEqual(2, journal.records[-1]['spooled_total'])
        self.assertEqual('success', journal.records[-1]['outcome'])

    async def test_write_failure_does_not_change_result_or_leak_exception(self):
        baseline = await self.run_worker()
        journal = Capture()
        with patch.object(journal, '_write', side_effect=OSError('private filesystem credential')), \
             self.assertLogs(d.__name__, level='WARNING') as logs:
            observed = await self.run_worker(journal)
        self.assertEqual(baseline, observed)
        self.assertNotIn('private filesystem credential', str(logs.output))

    async def test_cancel_and_downloader_errors_propagate_and_close_context(self):
        for failure in (asyncio.CancelledError(), TwitchAPIError('synthetic failure')):
            journal = Capture()
            with self.assertRaises(type(failure)):
                await self.run_worker(journal, failure)
            self.assertTrue(journal.stopped)
            self.assertIsNone(d.CURRENT.get())
            self.assertEqual('cancelled' if isinstance(failure, asyncio.CancelledError) else 'error',
                             journal.records[-1]['outcome'])

    async def test_retry_attempt_evidence_does_not_add_requests(self):
        journal = Capture()
        token = d.CURRENT.set(journal)
        try:
            with patch.object(fetch_comments, 'gql_fetch_page', AsyncMock(side_effect=[
                    TwitchAPIError('secret failure', retryable=True), ([edge(1)], None, False)])) as page, \
                 patch.object(fetch_comments, '_sleep_with_cancel', AsyncMock()):
                rows = [r async for r in fetch_comments.gql_fetch_comments(object(), 'x', '123456789')]
                self.assertEqual(1, len(rows))
                self.assertEqual(2, page.await_count)
            self.assertEqual([1, 2], [r['attempt'] for r in journal.records if r['event'] == 'request'])
            self.assertNotIn('secret failure', json.dumps(journal.records))
        finally:
            d.CURRENT.reset(token)


@unittest.skipUnless(os.name == 'posix', 'Private storage requires POSIX dir_fd, O_NOFOLLOW and flock')
class PrivateStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix=f'tdb-pagination-{os.getuid()}-', dir='/tmp')
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)
        self.selected = d.Settings('123456789', 10, 15, self.path)

    def opened(self):
        journal = d.Journal(self.selected)
        def unexpected_failure():
            error = sys.exception()
            journal._release()
            if error is not None:
                raise error
            raise AssertionError('unexpected diagnostic initialization failure')
        # Fail-closed production warnings intentionally omit filesystem details;
        # tests must expose the underlying exception to diagnose platform failures.
        with patch.object(journal, '_failure', side_effect=unexpected_failure):
            self.assertTrue(journal.open())
        self.addCleanup(journal.close, 'error', None)
        return journal

    def test_private_modes_close_and_four_slot_bound(self):
        for _ in range(4):
            journal = self.opened()
            self.assertEqual(0o700, self.path.stat().st_mode & 0o777)
            journal.close('success', 0)
        self.assertEqual(4, len(list(self.path.glob('slot-*.ndjson'))))
        for path in self.path.iterdir(): self.assertEqual(0o600, path.stat().st_mode & 0o777)
        self.assertFalse(d.Journal(self.selected).open())
        self.assertLessEqual(sum(p.stat().st_size for p in self.path.iterdir()), 32 * 1024 * 1024)

    def test_concurrent_diagnostic_is_skipped_without_touching_active(self):
        journal = self.opened()
        active = self.path / 'slot-0.active'
        before = active.read_bytes()
        self.assertFalse(d.Journal(self.selected).open())
        self.assertEqual(before, active.read_bytes())
        journal.close('success', 0)
        self.opened()

    def test_lease_excludes_another_process(self):
        self.opened()
        code = ('import sys; from pathlib import Path; from src.pagination_diagnostics import Journal, Settings; '
                'j=Journal(Settings("123456789",10,15,Path(sys.argv[1]))); '
                'ok=j.open(); j.close("success",0); sys.exit(1 if ok else 0)')
        result = subprocess.run([sys.executable, '-B', '-c', code, str(self.path)],
                                capture_output=True, timeout=15)
        self.assertEqual(0, result.returncode, result.stderr.decode(errors='replace'))
        self.assertEqual(1, len(list(self.path.glob('slot-*.active'))))

    def test_cleanup_only_expired_own_completed_not_active_or_foreign(self):
        journal = self.opened()
        journal.close('success', 0)
        expired = self.path / 'slot-0.ndjson'
        os.utime(expired, (0, 0))
        active = self.path / 'slot-1.active'; active.write_bytes(b'crash residue'); active.chmod(0o600)
        os.utime(active, (0, 0))
        foreign = self.path / 'unrelated'; foreign.write_text('keep')
        spoof = self.path / 'slot-2.ndjson'; spoof.write_text('foreign'); spoof.chmod(0o600)
        os.utime(spoof, (0, 0))
        next_journal = self.opened()
        self.assertFalse(expired.exists())
        self.assertEqual(b'crash residue', active.read_bytes())
        self.assertEqual('keep', foreign.read_text())
        self.assertEqual('foreign', spoof.read_text())
        next_journal.close('success', 0)

    def test_symlink_hardlink_and_public_root_rejected(self):
        target = self.path / 'unrelated'; target.write_text('keep'); target.chmod(0o600)
        link = self.path / 'slot-0.ndjson'; link.symlink_to(target)
        self.assertFalse(d.Journal(self.selected).open())
        link.unlink(); os.link(target, link)
        self.assertFalse(d.Journal(self.selected).open())
        self.assertEqual('keep', target.read_text())
        with patch.dict(os.environ, {'TCD_PAGINATION_DIAGNOSTICS': 'true', 'TCD_PAGINATION_DIAG_VOD': '123456789',
                                    'TCD_PAGINATION_DIAG_START': '10', 'TCD_PAGINATION_DIAG_END': '20',
                                    'TCD_PAGINATION_DIAG_DIR': str(self.path)}), \
             patch.object(d.config, 'PUBLIC_HTML_DIR', '/tmp'):
            self.assertIsNone(d.begin('123456789'))

    def test_truncation_real_file_respects_cap_and_is_parseable(self):
        journal = self.opened()
        with patch.object(d, 'MAX_BYTES', 6000):
            journal.response([edge(i) for i in range(100)], 'next', True)
        path = self.path / 'slot-0.ndjson'
        self.assertLessEqual(path.stat().st_size, 6000)
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        self.assertEqual('truncated', rows[-1]['event'])

    def test_permissions_directory_symlink_and_write_error_close_handles(self):
        self.path.chmod(0o755)
        self.assertFalse(d.Journal(self.selected).open())
        self.path.chmod(0o700)
        alias = self.path.with_name(self.path.name + '-link')
        alias.symlink_to(self.path, target_is_directory=True)
        try:
            self.assertFalse(d.Journal(d.Settings('123456789', 10, 15, alias)).open())
        finally:
            alias.unlink()
        journal = self.opened()
        fd = journal.file
        with patch.object(os, 'write', side_effect=OSError('secret error')):
            journal.event('synthetic')
        self.assertTrue(journal.stopped)
        with self.assertRaises(OSError): os.fstat(fd)
        self.assertTrue((self.path/'slot-0.active').exists())
        self.opened()

    def test_settings_require_valid_window_and_private_location(self):
        env = {'TCD_PAGINATION_DIAGNOSTICS':'true', 'TCD_PAGINATION_DIAG_VOD':'123456789',
               'TCD_PAGINATION_DIAG_START':'10.25', 'TCD_PAGINATION_DIAG_END':'20.75',
               'TCD_PAGINATION_DIAG_DIR':str(self.path)}
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(10.25, d.settings('123456789').start)
            for changes in ({'TCD_PAGINATION_DIAG_START':'nan'}, {'TCD_PAGINATION_DIAG_END':'9'},
                            {'TCD_PAGINATION_DIAG_DIR':'/tmp/../public_html'},
                            {'TCD_PAGINATION_DIAG_START':'secret-invalid-number'}):
                with patch.dict(os.environ, changes):
                    self.assertIsNone(d.begin('123456789'))


if __name__ == '__main__':
    unittest.main()
