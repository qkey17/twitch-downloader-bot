"""Deterministic full GraphQL responses; no Twitch or production credentials."""
import asyncio
import copy
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch
import uuid

from src import pagination_diagnostics as diagnostics
from src.chat_spool import ChatSpool, encode_row
from src.twitch_api import client, fetch_comments as fetch, pagination_repair as repair
from src.twitch_api.meta import VodMeta
from src.workers import download_worker as worker

VOD = '123456789'
ENV = {'TCD_PAGINATION_REPAIR_PILOT': 'true', 'TCD_PAGINATION_REPAIR_VOD': VOD,
       'TCD_PAGINATION_REPAIR_START': '10', 'TCD_PAGINATION_REPAIR_END': '12',
       'TCD_PAGINATION_REPAIR_CONTROL_OFFSET': '5'}


def edge(number, offset, tick=0, text=None):
    stamp = (datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=offset, microseconds=tick)).isoformat().replace('+00:00', 'Z')
    return {'cursor': 'synthetic-edge', 'node': {
        'id': str(uuid.UUID(int=number)), 'contentOffsetSeconds': offset, 'createdAt': stamp,
        'commenter': {'displayName': 'synthetic viewer'},
        'message': {'fragments': [{'text': text if text is not None else f'm{number}'}],
                    'userColor': '#123456', 'userBadges': [{'setID': 'subscriber', 'version': '1'}]}}}


def page(entries, cursor=None):
    entries = copy.deepcopy(entries)
    if entries:
        entries[-1]['cursor'] = cursor
    return [{'data': {'video': {'comments': {'edges': entries, 'pageInfo': {'hasNextPage': cursor is not None}}}}}]


class Body:
    def __init__(self, raw):
        self.raw = raw

    async def readexactly(self, amount):
        if len(self.raw) < amount:
            raise asyncio.IncompleteReadError(self.raw, amount)
        return self.raw[:amount]

    async def read(self, amount):
        return self.raw[:amount]


class Response:
    def __init__(self, data, status=200, headers=None, content_length=True):
        self.data, self.status = data, status
        self.headers = headers or {}
        raw = json.dumps(data).encode()
        self.content = Body(raw)
        self.content_length = len(raw) if content_length else None
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        self.closed = True

    async def json(self):
        return self.data


class Session:
    def __init__(self, routes):
        self.routes = routes
        self.calls, self.responses = [], []

    def post(self, url, *, json, headers, timeout):
        variables = json[0]['variables']
        key = ('cursor', variables['cursor']) if 'cursor' in variables else ('offset', variables['contentOffsetSeconds'])
        self.calls.append((key, copy.deepcopy(json)))
        value = self.routes[key]
        if isinstance(value, list) and value and isinstance(value[0], Response):
            response = value.pop(0)
        elif isinstance(value, Response):
            response = value
        elif callable(value):
            return value()
        else:
            response = Response(value)
        self.responses.append(response)
        return response


def simple():
    return Session({('offset', 0): page([edge(1, 0), edge(2, 8)], 'P'),
                    ('cursor', 'P'): page([edge(3, 10), edge(5, 12)], 'C1'),
                    ('offset', 5): page([edge(2, 8), edge(3, 10), edge(4, 11), edge(5, 12), edge(6, 13)], 'B'),
                    ('cursor', 'C1'): page([edge(6, 13), edge(7, 14), edge(8, 20)])})


def control(entries, cursor='B'):
    return page([edge(2, 8), *entries], cursor)


class Capture(diagnostics.Journal):
    def __init__(self):
        super().__init__(diagnostics.Settings(VOD, 0, 30000, Path('unused')))
        self.records = []

    def _write(self, record):
        self.records.append(record)

    def _finish_file(self):
        self.stopped = True


class SettingsTests(unittest.TestCase):
    def test_default_exact_true_and_other_vod(self):
        for value in ('', 'false', '1', 'TRUE', 'true '):
            with patch.dict(os.environ, {'TCD_PAGINATION_REPAIR_PILOT': value}, clear=True):
                self.assertIsNone(repair.settings(VOD))
        with patch.dict(os.environ, ENV, clear=True):
            self.assertIsNone(repair.settings('987654321'))
            self.assertEqual(5, repair.settings(VOD).control_offset)

    def test_invalid_required_settings_fail_closed(self):
        changes = [('VOD', 'secret'), ('START', ''), ('START', 'NaN'), ('START', '1e2'),
                   ('START', '-1'), ('END', '9'), ('CONTROL_OFFSET', '10'),
                   ('CONTROL_OFFSET', '5.1'), ('END', 'Infinity'), ('VOD', '')]
        for name, value in changes:
            with self.subTest(name=name, value=value), patch.dict(os.environ, {
                **ENV, 'TCD_PAGINATION_REPAIR_' + name: value}, clear=True):
                with self.assertRaisesRegex(repair.RepairError, 'configuration'):
                    repair.settings(VOD)


class RepairTests(unittest.IsolatedAsyncioTestCase):
    def _setupAsyncioRunner(self):
        self._asyncioRunner = asyncio.Runner(debug=True, loop_factory=asyncio.SelectorEventLoop)

    async def collect(self, session, *, env=None, cancel=None, rows=None):
        rows = [] if rows is None else rows
        with patch.dict(os.environ, env or ENV, clear=True), patch.object(fetch, '_sleep_with_cancel', AsyncMock()):
            iterator = fetch.gql_fetch_comments(session, 'synthetic client', VOD, cancel_check=cancel)
            try:
                async for row in iterator:
                    rows.append(row)
            finally:
                await iterator.aclose()
        return rows

    async def failure(self, session, reason, **kwargs):
        rows = []
        with self.assertRaisesRegex(client.TwitchAPIError, reason):
            await self.collect(session, rows=rows, **kwargs)
        self.assertTrue(all(r[0] < 10 for r in rows), 'unverified window must never be yielded')
        self.assertTrue(all(r.closed for r in session.responses))
        self.assertIsNone(client.COMMENTS_BODY_LIMIT.get())
        return rows

    async def test_merge_before_spool_counts_rows_preserves_cursor_and_metadata(self):
        session = simple()
        rows = await self.collect(session)
        self.assertEqual([f'm{i}' for i in range(1, 9)], [r[3] for r in rows])
        self.assertEqual([('offset', 0), ('cursor', 'P'), ('offset', 5), ('cursor', 'C1')], [c[0] for c in session.calls])
        self.assertEqual(7, len(rows[0]))
        self.assertEqual('#123456', rows[-1][5])
        self.assertEqual([{'set_id': 'subscriber', 'version': '1'}], rows[-1][6])
        with tempfile.TemporaryDirectory() as root:
            spool = ChatSpool(root)
            spool.open()
            try:
                spool.append_batch([encode_row({'user': r[2], 'text': r[3], 't': str(r[0])}) for r in rows])
                self.assertEqual((8, 1), (spool.messages, spool.unique_users))
            finally:
                spool.close()
            self.assertEqual([], list(Path(root).iterdir()))

    async def test_anonymous_a19_73_to_106_and_narrow_123_to_155(self):
        fixture = json.loads((Path(__file__).parent / 'fixtures/pagination_repair.json').read_text())
        make = lambda numbers: [edge(n, *fixture['positions'][n-1]) for n in numbers]
        prefix = [make(p) for p in fixture['prefix']]
        control = [make(p) for p in fixture['control']]
        routes = {('offset', 0): page(prefix[0], 'A17'), ('cursor', 'A17'): page(prefix[1], 'A18'),
                  ('cursor', 'A18'): page(make(fixture['main']), 'C1'),
                  ('cursor', 'C1'): page(make(fixture['c1']))}
        for i, entries in enumerate(control):
            routes[('offset', 20330) if not i else ('cursor', f'B{i}')] = page(entries, f'B{i+1}')
        session = Session(routes)
        env = {**ENV, 'TCD_PAGINATION_REPAIR_START': '20343', 'TCD_PAGINATION_REPAIR_END': '20470',
               'TCD_PAGINATION_REPAIR_CONTROL_OFFSET': '20330'}
        rows = await self.collect(session, env=env)
        self.assertEqual(73, len(fixture['main']))
        original = make(fixture['main'])
        full = [r for r in rows if 20343 <= r[0] <= 20470]
        self.assertEqual(106, len(full))
        original_texts = {e['node']['message']['fragments'][0]['text'] for e in original}
        self.assertTrue(original_texts <= {r[3] for r in full})
        additions = [r for r in full if r[3] not in original_texts]
        self.assertEqual(33, len(additions))
        self.assertEqual(1, sum(r[0] == 20460 for r in additions))
        narrow_base = [*prefix[0], *prefix[1], *original]
        self.assertEqual(123, sum(20330 <= e['node']['contentOffsetSeconds'] <= 20440 for e in narrow_base))
        self.assertEqual(155, sum(20330 <= r[0] <= 20440 for r in rows))
        self.assertEqual(32, sum(r[0] <= 20440 for r in additions))
        expected = sorted({n for p in fixture['prefix'] for n in p} | set(fixture['main']) |
                          set(fixture['c1']) | {n for p in fixture['control'] for n in p if 20343 <= fixture['positions'][n-1][0] <= 20470})
        self.assertEqual([f'm{n}' for n in expected], [r[3] for r in rows])
        self.assertEqual(4, sum(k[0] == 'offset' and k[1] == 20330 or k[0] == 'cursor' and k[1].startswith('B') for k, _ in session.calls))
        self.assertNotIn(('cursor', 'B4'), [k for k, _ in session.calls])
        c1 = set(fixture['c1']); c2 = set(fixture['c2'])
        missing = [n for n in c1-c2 if fixture['positions'][n-1][0] == 20476]
        self.assertEqual(2, len(missing))
        self.assertTrue({f'm{n}' for n in missing} <= {r[3] for r in rows})
        routes[('cursor', 'C1')] = page(make(fixture['c2']))
        with self.assertRaisesRegex(repair.RepairError, 'control_tail_missing'):
            await self.collect(Session(routes), env=env)

    async def test_window_inside_page_repairs_whole_page(self):
        rows = await self.collect(simple(), env={**ENV, 'TCD_PAGINATION_REPAIR_START': '11', 'TCD_PAGINATION_REPAIR_END': '11'})
        self.assertEqual(8, len(rows))

    async def test_equal_second_at_right_requires_two_main_pages(self):
        session = simple()
        session.routes[('cursor', 'C1')] = page([edge(5, 12), edge(9, 12, 1)], 'C1b')
        session.routes[('cursor', 'C1b')] = page([edge(6, 13), edge(7, 14)])
        rows = await self.collect(session)
        self.assertEqual(['m1', 'm2', 'm3', 'm4', 'm5', 'm9', 'm6', 'm7'], [r[3] for r in rows])
        self.assertEqual(1, sum(r[3] == 'm5' for r in rows))

    async def test_two_tied_pages_without_barrier_fail(self):
        session = simple()
        session.routes[('cursor', 'C1')] = page([edge(5, 12), edge(9, 12, 1)], 'C1b')
        session.routes[('cursor', 'C1b')] = page([edge(10, 12, 2)], 'C1c')
        await self.failure(session, 'main_barrier_or_tail_limit')
        self.assertNotIn(('cursor', 'C1c'), [c[0] for c in session.calls])

    async def test_committed_left_second_is_rejected(self):
        session = simple()
        session.routes[('offset', 0)] = page([edge(1, 10)], 'P')
        with self.assertRaisesRegex(repair.RepairError, 'window_not_one_page'):
            await self.collect(session)
        # First page must contain the full configured window; it cannot be
        # committed and then silently repaired by later pages.
        self.assertEqual(1, len(session.calls))

    async def test_left_same_second_already_committed_is_rejected(self):
        session = simple()
        session.routes[('offset', 0)] = page([edge(1, 9.1)], 'P')
        session.routes[('cursor', 'P')] = page([edge(3, 9.5), edge(5, 12)], 'C1')
        await self.failure(session, 'left_boundary_committed')

    async def test_unknown_prefix_id_and_outside_retained_prefix_fail(self):
        for prior in (edge(90, 8), edge(90, 1)):
            session = simple()
            session.routes[('offset', 5)] = page([prior, edge(4, 11), edge(6, 13)], 'B')
            await self.failure(session, 'unverified_prefix')

    async def test_control_starting_on_left_second_is_not_complete_coverage(self):
        session = simple()
        session.routes[('offset', 5)] = page([edge(3, 10), edge(6, 13)], 'B')
        await self.failure(session, 'control_no_left_barrier')

    async def test_control_prefix_source_order_cannot_rewrite_committed_order(self):
        session = simple()
        a, b = edge(1, 8), edge(2, 8)
        session.routes[('offset', 0)] = page([a, b], 'P')
        session.routes[('offset', 5)] = page([b, a, edge(4, 11), edge(6, 13)], 'B')
        await self.failure(session, 'order_conflict')

    async def test_reused_committed_id_with_later_offset_is_a_conflict(self):
        session = simple()
        session.routes[('offset', 5)] = control([edge(1, 11), edge(6, 13)])
        await self.failure(session, 'id_conflict')

    async def test_control_must_pass_right_boundary_and_not_end_on_it(self):
        session = simple()
        session.routes[('offset', 5)] = control([edge(4, 11), edge(5, 12)], None)
        await self.failure(session, 'control_no_right_barrier')

    async def test_control_time_regression_is_rejected(self):
        session = simple()
        session.routes[('offset', 5)] = control([edge(3, 10)], 'B1')
        session.routes[('cursor', 'B1')] = page([edge(2, 8), edge(6, 13)], 'B2')
        await self.failure(session, 'control_time_regression')

    async def test_control_cursor_cycle_does_not_trigger_additional_attempts(self):
        session = simple()
        session.routes[('offset', 5)] = control([edge(3, 10)], 'B1')
        session.routes[('cursor', 'B1')] = page([edge(4, 11)], 'B1')
        await self.failure(session, 'made no progress')
        self.assertEqual(4, len(session.calls))

    async def test_missing_control_tail_inside_main_coverage_fails(self):
        session = simple()
        session.routes[('offset', 5)] = control([edge(4, 11), edge(90, 13)])
        await self.failure(session, 'control_tail_missing')

    async def test_control_tail_outside_coverage_cannot_be_dropped(self):
        session = simple()
        session.routes[('offset', 5)] = control([edge(4, 11), edge(90, 50)])
        session.routes[('cursor', 'C1')] = page([edge(6, 13)], 'C1b')
        session.routes[('cursor', 'C1b')] = page([edge(7, 14)], 'C1c')
        await self.failure(session, 'main_barrier_or_tail_limit')

    async def test_missing_on_partial_last_second_waits_for_second_page(self):
        session = simple()
        session.routes[('cursor', 'C1')] = page([edge(9, 13, 0)], 'C1b')
        session.routes[('cursor', 'C1b')] = page([edge(6, 13, 1), edge(7, 14)])
        session.routes[('offset', 5)] = control([edge(4, 11), edge(6, 13, 1)])
        rows = await self.collect(session)
        self.assertIn('m6', [r[3] for r in rows])

    async def test_same_id_data_conflicts_fail(self):
        for field, value in (('contentOffsetSeconds', 10.5), ('createdAt', '2026-01-01T00:00:10.000001Z'),
                             ('commenter', {'displayName': 'different'}),
                             ('message', {'fragments': [{'text': 'different'}]})):
            session = simple()
            item = edge(3, 10)
            item['node'][field] = value
            session.routes[('offset', 5)] = control([item, edge(6, 13)])
            await self.failure(session, 'id_conflict')

    async def test_distinct_ids_with_identical_text_author_timestamp_survive(self):
        session = simple()
        a, b = edge(3, 10, text='same'), edge(4, 10, text='same')
        session.routes[('cursor', 'P')] = page([a, edge(5, 12)], 'C1')
        session.routes[('offset', 5)] = control([a, b, edge(6, 13)])
        rows = await self.collect(session)
        self.assertEqual(2, sum(r[3] == 'same' for r in rows))

    async def test_blank_identity_is_checked_but_existing_row_filter_is_preserved(self):
        session = simple()
        session.routes[('offset', 5)] = control([edge(3, 10), edge(4, 11, text='  '), edge(5, 12), edge(6, 13)])
        rows = await self.collect(session)
        self.assertEqual(7, len(rows))
        self.assertFalse(any(not r[3] for r in rows))

    async def test_exact_fractional_offsets_and_nanoseconds_keep_order(self):
        session = simple()
        a, b = edge(3, 10.25), edge(4, 10.25)
        a['node']['createdAt'] = '2026-01-01T00:00:10.123456788Z'
        b['node']['createdAt'] = '2026-01-01T00:00:10.123456789Z'
        session.routes[('cursor', 'P')] = page([a, edge(5, 12)], 'C1')
        session.routes[('offset', 5)] = control([a, b, edge(6, 13)])
        rows = await self.collect(session, env={**ENV, 'TCD_PAGINATION_REPAIR_START': '10.25'})
        tied = [r for r in rows if r[0] == 10.25]
        self.assertEqual(['m3', 'm4'], [r[3] for r in tied])
        self.assertEqual('2026-01-01T00:00:10.123456789Z', tied[1][1])

    async def test_opposite_source_order_at_exact_timestamps_fails(self):
        session = simple()
        a, b = edge(3, 10), edge(4, 10)
        session.routes[('cursor', 'P')] = page([a, b, edge(5, 12)], 'C1')
        session.routes[('offset', 5)] = control([b, a, edge(6, 13)])
        await self.failure(session, 'order_conflict')

    async def test_late_time_regression_never_silently_finishes(self):
        session = simple()
        session.routes[('cursor', 'C1')] = page([edge(6, 13), edge(7, 14)], 'NEXT')
        session.routes[('cursor', 'NEXT')] = page([edge(90, 11)])
        with self.assertRaisesRegex(repair.RepairError, 'late_time_regression'):
            await self.collect(session)

    async def test_retry_attempts_consume_four_request_budget(self):
        session = simple()
        session.routes[('offset', 5)] = [Response({}, 503), Response({}, 429, {'Retry-After': '0'}),
                                       Response(control([edge(3, 10)], 'B1'))]
        session.routes[('cursor', 'B1')] = page([edge(4, 11)], 'B2')
        await self.failure(session, 'request_limit')
        self.assertEqual(4, len(session.calls)-2)
        self.assertNotIn(('cursor', 'B2'), [c[0] for c in session.calls])

    async def test_retry_then_success_uses_existing_delay_policy(self):
        session = simple()
        good = session.routes[('offset', 5)]
        session.routes[('offset', 5)] = [Response({}, 429, {'Retry-After': '0.25'}), Response(good)]
        with patch.dict(os.environ, ENV, clear=True), patch.object(fetch, '_sleep_with_cancel', AsyncMock()) as pause:
            rows = [r async for r in fetch.gql_fetch_comments(session, 'client', VOD)]
        self.assertEqual(8, len(rows))
        self.assertIn(((0.25, None), {}), [(c.args, c.kwargs) for c in pause.await_args_list])

    async def test_http_failure_is_not_repaired_or_published(self):
        session = simple()
        session.routes[('offset', 5)] = Response({}, 403)
        await self.failure(session, '403')

    async def test_control_deadline_cancels_inflight_http_and_closes(self):
        session = simple()
        entered, closed = asyncio.Event(), asyncio.Event()
        class Blocked:
            async def __aenter__(self):
                entered.set()
                try:
                    await asyncio.Future()
                finally:
                    closed.set()
            async def __aexit__(self, *_):
                pass
        session.routes[('offset', 5)] = Blocked
        with patch.object(repair, 'CONTROL_SECONDS', 0.02):
            await self.failure(session, 'control_timeout')
        self.assertTrue(entered.is_set() and closed.is_set())

    async def test_cancel_control_stops_requests_closes_buffers_and_generators(self):
        session = simple()
        entered, closed = asyncio.Event(), asyncio.Event()
        class Blocked:
            async def __aenter__(self):
                entered.set()
                try:
                    await asyncio.Future()
                finally:
                    closed.set()
            async def __aexit__(self, *_):
                pass
        session.routes[('offset', 5)] = Blocked
        task = asyncio.create_task(self.collect(session))
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(closed.is_set())
        self.assertEqual(3, len(session.calls))
        self.assertIsNone(client.COMMENTS_BODY_LIMIT.get())

    async def test_cancel_flag_after_http_prevents_next_attempt(self):
        session = simple()
        rows = []
        with self.assertRaisesRegex(RuntimeError, 'отменена'):
            await self.collect(session, cancel=lambda: len(session.calls) >= 3, rows=rows)
        self.assertTrue(all(r[0] < 10 for r in rows))
        self.assertTrue(all(r.closed for r in session.responses))
        self.assertEqual(3, len(session.calls))

    async def test_buffer_count_memory_and_http_bounds(self):
        for name, maximum, reason in (('MAX_BYTES', 1000, 'buffer_limit|response_limit'),
                                     ('MAX_RECORDS', 3, 'record_limit'),
                                     ('MAX_ESTIMATED_BYTES', 1, 'memory_estimate_limit')):
            with self.subTest(name=name), patch.object(repair, name, maximum):
                await self.failure(simple(), reason)
        for length in (True, False):
            session = simple()
            session.routes[('offset', 5)] = Response(page([edge(4, 11, text='x'*20000), edge(6, 13)]), content_length=length)
            with patch.object(repair, 'MAX_BYTES', 10000):
                await self.failure(session, 'response_limit')

    async def test_empty_or_uncovered_window_is_failure(self):
        for data in (page([]), page([edge(1, 1)])):
            await self.failure(Session({('offset', 0): data}), 'window_not_checked')

    async def test_wrong_main_start_and_invalid_identity_fail(self):
        with patch.dict(os.environ, ENV, clear=True):
            with self.assertRaisesRegex(repair.RepairError, 'main_start_offset'):
                await fetch.gql_fetch_comments(simple(), 'client', VOD, start_offset=1).__anext__()
        session = simple()
        bad = edge(3, 10); del bad['node']['id']
        session.routes[('cursor', 'P')] = page([bad, edge(5, 12)], 'C1')
        await self.failure(session, 'identity')

    async def test_diagnostics_counts_only_actual_yields_and_no_private_fields(self):
        session, journal = simple(), Capture()
        token = diagnostics.CURRENT.set(journal)
        try:
            rows = await self.collect(session)
            self.assertEqual(len(rows), journal.emitted)
            verified = next(r for r in journal.records if r['event'] == 'repair_window_verified')
            self.assertEqual((2, 3, 1, 1), (verified['original_ids'], verified['merged_ids'], verified['added_ids'], verified['requests']))
            self.assertTrue(verified['right_barrier'] and verified['tail_confirmed'])
            encoded = json.dumps(journal.records)
            for secret in ('synthetic viewer', 'synthetic client', 'synthetic-edge', '"C1"', '"B"', 'm1'):
                self.assertNotIn(secret, encoded)
            received = next(i for i,r in enumerate(journal.records) if r['event'] == 'repair_window_verified')
            emitted = [r for r in journal.records[:received] if r['event'] == 'repair_row_processed' and r['emitted']]
            self.assertEqual(2, len(emitted))
        finally:
            diagnostics.CURRENT.reset(token)

    async def test_worker_first_anext_in_separate_task_and_no_context_leak(self):
        session = simple()
        with patch.dict(os.environ, ENV, clear=True), patch.object(fetch, '_sleep_with_cancel', AsyncMock()):
            iterator = fetch.gql_fetch_comments(session, 'client', VOD)
            first = await asyncio.wait_for(iterator.__anext__(), timeout=1)
            remaining = [r async for r in iterator]
        self.assertEqual(8, len([first, *remaining]))
        self.assertIsNone(client.COMMENTS_BODY_LIMIT.get())

    async def run_worker(self, session, root, **kwargs):
        context = SimpleNamespace(application=SimpleNamespace(bot_data={'aiohttp': session}), user_data={})
        with patch.dict(os.environ, ENV, clear=True), patch.object(fetch, '_sleep_with_cancel', AsyncMock()), \
             patch.object(worker, 'fetch_vod_meta', AsyncMock(return_value=VodMeta(VOD, length_seconds=100))), \
             patch.object(worker, 'get_client_id', return_value='client'), \
             patch.object(worker, 'make_progress_updater', return_value=AsyncMock()), \
             patch.object(worker, 'OUT_DIR', Path(root)/'out'), patch.object(worker, 'SPOOL_DIR', Path(root)/'spool'), \
             patch.object(worker, 'build_html_result', AsyncMock(return_value=([], 'synthetic.html'))) as build:
            try:
                result = await worker.download_and_send(context, 1, 2, 'vod-url', VOD, 'html_online', **kwargs)
                return result, build
            finally:
                self.last_build = build

    async def test_worker_failure_never_builds_html_or_replaces_existing_result(self):
        with tempfile.TemporaryDirectory() as root:
            old = Path(root)/'existing.html'; old.write_text('old generation')
            session = simple(); session.routes[('offset', 5)] = Response({}, 403)
            publication = SimpleNamespace(meta='unchanged', stats='unchanged')
            with self.assertRaises(client.TwitchAPIError):
                await self.run_worker(session, root, publication=publication)
            self.last_build.assert_not_awaited()
            self.assertEqual('old generation', old.read_text())
            self.assertEqual(('unchanged', 'unchanged'), (publication.meta, publication.stats))
            self.assertEqual([], list((Path(root)/'spool').iterdir()))

    async def test_worker_late_regression_after_verified_window_never_publishes(self):
        with tempfile.TemporaryDirectory() as root:
            session = simple()
            session.routes[('cursor', 'C1')] = page([edge(6, 13), edge(7, 14)], 'NEXT')
            session.routes[('cursor', 'NEXT')] = page([edge(90, 11)])
            with self.assertRaisesRegex(repair.RepairError, 'late_time_regression'):
                await self.run_worker(session, root)
            self.last_build.assert_not_awaited()
            self.assertEqual([], list((Path(root)/'spool').iterdir()))

    async def test_worker_spool_error_closes_and_never_publishes(self):
        with tempfile.TemporaryDirectory() as root, patch.object(ChatSpool, 'append_batch', side_effect=OSError('synthetic write failure')):
            with self.assertRaises(OSError):
                await self.run_worker(simple(), root)
            self.last_build.assert_not_awaited()
            self.assertEqual([], list((Path(root)/'spool').iterdir()))

    async def test_worker_success_stats_count_only_saved_rows(self):
        with tempfile.TemporaryDirectory() as root:
            result, build = await self.run_worker(simple(), root)
            self.assertEqual(8, result[1]['messages'])
            build.assert_awaited_once()
            self.assertEqual([], list((Path(root)/'spool').iterdir()))

    async def test_disabled_and_other_vod_keep_queries_rows_empty_and_retries(self):
        for env in ({}, {**ENV, 'TCD_PAGINATION_REPAIR_VOD': '987654321'}):
            session = simple()
            rows = await self.collect(session, env={**env, 'UNRELATED': '1'})
            self.assertEqual(['m1', 'm2', 'm3', 'm5', 'm6', 'm7', 'm8'], [r[3] for r in rows])
            self.assertEqual([('offset', 0), ('cursor', 'P'), ('cursor', 'C1')], [c[0] for c in session.calls])
            empty = await self.collect(Session({('offset', 0): page([])}), env={**env, 'UNRELATED': '1'})
            self.assertEqual([], empty)


if __name__ == '__main__':
    unittest.main()
