"""Two-page ownership and fail-closed invariants, using synthetic HTTP only."""
import asyncio
import gc
import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
import weakref
from unittest.mock import AsyncMock, patch

import test_pagination_repair as f
from src import pagination_diagnostics as d
from src.twitch_api import client, fetch_comments as fetch, pagination_repair as r
from src import config, html_publisher
from src.database import database as db, db_core, db_results as results
from src.html_renderer import render_viewer_html


def ordered(entries):
    return sorted(entries, key=lambda e: (e['node']['contentOffsetSeconds'], e['node']['createdAt']))


def span_session(additions=(), *, after=True):
    """Two committed pages, one pending page, one target and its own Q."""
    return f.Session({
        ('offset', 0): f.page([f.edge(1, 0), f.edge(2, 2)], 'C0'),
        ('cursor', 'C0'): f.page([f.edge(3, 4), f.edge(4, 6)], 'CP'),
        ('cursor', 'CP'): f.page([f.edge(5, 8), f.edge(6, 9)], 'PT'),
        ('cursor', 'PT'): f.page([f.edge(7, 10), f.edge(8, 12)], 'C1'),
        ('offset', 5): f.page(ordered([*[f.edge(n, t) for n, t in
                                      ((3, 4), (4, 6), (5, 8), (6, 9), (7, 10), (8, 12), (9, 13))],
                                     *additions]), 'B1'),
        ('cursor', 'C1'): f.page([f.edge(9, 13), f.edge(10, 14)], 'AFTER' if after else None),
        ('cursor', 'AFTER'): f.page([f.edge(11, 20)]),
    })


def large_session(count=820, text_size=500, *, pending_additions=True):
    """Full bounded graph with many common and distinct IDs, never real data."""
    def entries(base, start, end):
        return [f.edge(base+n, start+(end-start)*n/(count-1), text=f'fixture-{base+n} '+('x'*text_size))
                for n in range(count)]
    pending, target = entries(1000, 8, 9), entries(10000, 10, 12)
    extras = [f.edge(30000+n, 10+1.9*n/count, text='new '+('y'*text_size)) for n in range(count//3)]
    if pending_additions:
        extras += [f.edge(20000+n, 8+0.9*n/count, text='new '+('z'*text_size)) for n in range(count//3)]
    session = span_session(after=False)
    session.routes[('cursor', 'CP')] = f.page(pending, 'PT')
    session.routes[('cursor', 'PT')] = f.page(target, 'C1')
    session.routes[('offset', 5)] = f.page(ordered([f.edge(3, 4), f.edge(4, 6), *pending, *target, *extras, f.edge(9, 13)]), 'B1')
    return session


class Blocked(f.Response):
    def __init__(self):
        super().__init__({})
        self.entered = asyncio.Event()

    async def __aenter__(self):
        self.entered.set()
        try:
            await asyncio.Future()
        finally:
            self.closed = True


class PendingTests(unittest.IsolatedAsyncioTestCase):
    collect = f.RepairTests.collect
    run_worker = f.RepairTests.run_worker

    def _setupAsyncioRunner(self):
        self._asyncioRunner = asyncio.Runner(debug=True, loop_factory=asyncio.SelectorEventLoop)

    async def captured(self, session, **kwargs):
        journal = f.Capture()
        token = d.CURRENT.set(journal)
        try:
            rows = await self.collect(session, **kwargs)
        finally:
            d.CURRENT.reset(token)
        return rows, next(v for v in journal.records if v['event'] == 'repair_window_verified')

    async def rejected(self, session, reason, *, unissued_from=8):
        rows = []
        with self.assertRaisesRegex(client.TwitchAPIError, reason):
            await self.collect(session, rows=rows)
        self.assertTrue(all(row[0] < unissued_from for row in rows), 'P/T must remain unissued')
        self.assertTrue(all(response.closed for response in session.responses))
        self.assertIsNone(client.COMMENTS_BODY_LIMIT.get())

    async def test_01_02_03_fifteen_inside_pending_gap_or_split(self):
        for inside in (15, 0, 7):
            with self.subTest(inside=inside):
                extra = [f.edge(100+n, 8.1+n/100 if n < inside else 9.1+n/100) for n in range(15)]
                session = span_session(extra)
                rows, event = await self.captured(session)
                # Unique synthetic text is only an observable label here; the
                # production merge and preservation checks use Twitch IDs.
                self.assertEqual({f'm{n}' for n in [*range(1, 12), *range(100, 115)]}, {v[3] for v in rows})
                self.assertEqual(len(rows), len({v[3] for v in rows}))
                self.assertEqual(sorted(v[0] for v in rows), [v[0] for v in rows])
                self.assertEqual((2, 4, 19, 15), (event['span_pages'], event['pair_original_ids'],
                                               event['pair_merged_ids'], event['pair_added_ids']))
                self.assertEqual((inside, 15-inside, 0), (event['added_pending_ids'], event['added_gap_ids'], event['added_target_ids']))
                self.assertEqual((2, 2, 0), (event['original_ids'], event['merged_ids'], event['added_ids']))
                self.assertEqual(('cursor', 'AFTER'), session.calls[-1][0])

    async def test_04_unknown_in_committed_last_second_or_gap_before_p_refuses(self):
        for offset in (6, 7.5, 1):
            session = span_session([f.edge(99, offset)])
            await self.rejected(session, 'unverified_prefix')
            self.assertNotIn(('cursor', 'C1'), [key for key, _ in session.calls])

    async def test_04_failure_events_count_examined_unknowns_without_private_data(self):
        journal = f.Capture(); token = d.CURRENT.set(journal)
        try:
            await self.rejected(span_session([f.edge(99,7)]), 'unverified_prefix')
        finally:
            d.CURRENT.reset(token)
        end = journal.records[-1]
        self.assertEqual(('control',1,3,8,12,12), (end['stage'],end['rejected_unknown_ids'],
            end['control_prefix_ids'],end['repair_low'],end['repair_high'],end['verification_high']))
        self.assertTrue(end['pending_present'])
        serialized = json.dumps(journal.records)
        for value in ('synthetic viewer', 'm99', f.edge(99,7)['node']['id'], '"PT"'):
            self.assertNotIn(value, serialized)

    async def test_05_committed_and_pending_same_second_even_if_blank_refuses(self):
        for text in ('committed', '  '):
            session = span_session()
            session.routes[('cursor', 'C0')] = f.page([f.edge(3, 4), f.edge(4, 8.1, text=text)], 'CP')
            session.routes[('cursor', 'CP')] = f.page([f.edge(5, 8.5), f.edge(6, 9)], 'PT')
            await self.rejected(session, 'left_boundary_committed', unissued_from=8.5)
            self.assertEqual(4, len(session.calls))

    async def test_06_07_internal_shared_second_adjacent_repeat_and_new_id(self):
        session = span_session()
        shared = f.edge(6, 9)
        session.routes[('cursor', 'PT')] = f.page([shared, f.edge(7, 9.5), f.edge(8, 12)], 'C1')
        session.routes[('offset', 5)] = f.page([f.edge(3, 4), f.edge(5, 8), shared,
                                              f.edge(90, 9, tick=1), f.edge(7, 9.5), f.edge(9, 13)], 'B1')
        rows, event = await self.captured(session)
        self.assertEqual(1, sum(row[3] == 'm6' for row in rows))
        self.assertEqual(['m6', 'm90', 'm7'], [v[3] for v in rows if 9 <= v[0] < 10])
        self.assertEqual(1, event['pair_added_ids'])
        self.assertGreater(event['repeated_ids'], 0)

    async def test_08_ambiguous_repeat_and_opposite_order_fail_closed(self):
        for reverse in (False, True):
            session = span_session()
            a, b = f.edge(5, 9), f.edge(6, 9)
            session.routes[('cursor', 'CP')] = f.page([a, b], 'PT')
            session.routes[('cursor', 'PT')] = f.page([*([b, a] if reverse else [a, b]), f.edge(8, 12)], 'C1')
            session.routes[('offset', 5)] = f.page([f.edge(3, 4), a, b, f.edge(8, 12), f.edge(9, 13)], 'B1')
            await self.rejected(session, 'order_conflict')

    async def test_09_conflicting_pending_id_and_identical_distinct_messages(self):
        for field, value in (('contentOffsetSeconds', 8.2), ('createdAt', '2026-01-01T00:00:08.000000001Z'),
                             ('userColor', '#abcdef'), ('userBadges', [{'setID': 'new', 'version': '2'}])):
            session = span_session()
            entry = f.edge(5, 8)
            if field in ('userColor', 'userBadges'):
                # Exercise renderer fallback metadata, not just raw message.
                entry['node']['message'].pop(field)
                entry['node'][field] = value
            else:
                entry['node'][field] = value
            controlled = [f.edge(3, 4), entry, f.edge(9, 13)]
            session.routes[('offset', 5)] = f.page(controlled, 'B1')
            await self.rejected(session, 'id_conflict')
        session = span_session([f.edge(90, 8.5, text='same'), f.edge(91, 8.5, text='same')])
        rows, _ = await self.captured(session)
        self.assertEqual(2, sum(row[3] == 'same' for row in rows))

    async def test_10_page_redistribution_preserves_set_and_raw_regression_refuses(self):
        baseline, _ = await self.captured(span_session())
        session = span_session()
        session.routes[('cursor', 'CP')] = f.page([f.edge(5, 8)], 'PT')
        session.routes[('cursor', 'PT')] = f.page([f.edge(6, 9), f.edge(7, 10), f.edge(8, 12)], 'C1')
        self.assertEqual(baseline, (await self.captured(session))[0])
        session = span_session()
        session.routes[('cursor', 'PT')] = f.page([f.edge(90, 8.5), f.edge(8, 12)], 'C1')
        await self.rejected(session, 'late_time_regression')
        session = span_session()
        session.routes[('cursor', 'CP')] = f.page([f.edge(6, 9), f.edge(5, 8)], 'PT')
        await self.rejected(session, 'time_regression')

    async def test_10_next_non_candidate_conflict_refuses_before_releasing_pending(self):
        session, rows = span_session(), []
        session.routes[('cursor', 'C0')] = f.page([f.edge(1, 4), f.edge(4, 6)], 'CP')
        with self.assertRaisesRegex(r.RepairError, 'id_conflict'):
            await self.collect(session, rows=rows)
        self.assertEqual([], rows)

    async def test_11_start_shift_exact_limits_and_end_selection(self):
        for shift in (0, 1, 2, 2.001):
            session = span_session()
            session.routes[('cursor', 'PT')] = f.page([f.edge(7, 10+shift), f.edge(8, 12.5)], 'C1')
            session.routes[('offset', 5)] = f.page([f.edge(3, 4), f.edge(7, 10+shift), f.edge(9, 13)], 'B1')
            if shift <= 2:
                _, event = await self.captured(session)
                self.assertEqual(shift, event['start_shift'])
            else:
                await self.rejected(session, 'window_not_one_page')
        rows = []
        with self.assertRaisesRegex(r.RepairError, 'window_not_one_page'):
            await self.collect(span_session(), env={**f.ENV, 'TCD_PAGINATION_REPAIR_START': '9.5',
                                                   'TCD_PAGINATION_REPAIR_END': '9.9'}, rows=rows)
        self.assertTrue(all(v[0] < 8 for v in rows))

    async def test_12_control_requires_barrier_before_pending_not_just_target(self):
        session = span_session()
        session.routes[('offset', 5)] = f.page([f.edge(5, 8), f.edge(9, 13)], 'B1')
        await self.rejected(session, 'control_no_left_barrier')
        await self.rejected(span_session([f.edge(99, 1)]), 'unverified_prefix')

    async def test_16_continuous_lag_classifies_next_before_release(self):
        session, journal = span_session(), f.Capture()
        token = d.CURRENT.set(journal)
        with patch.dict(os.environ, f.ENV, clear=True), patch.object(fetch, '_sleep_with_cancel', AsyncMock()):
            iterator = fetch.gql_fetch_comments(session, 'client', f.VOD)
            try:
                for expected in (1, 2):
                    self.assertEqual(f'm{expected}', (await iterator.__anext__())[3])
                    self.assertEqual(2, len(session.calls))
                for expected in (3, 4):
                    self.assertEqual(f'm{expected}', (await iterator.__anext__())[3])
                    self.assertEqual(3, len(session.calls))
                rest = [row async for row in iterator]
                self.assertEqual([f'm{n}' for n in range(5, 12)], [row[3] for row in rest])
                self.assertEqual(7, len(session.calls))
                self.assertEqual('success', journal.records[-1]['outcome'])
            finally:
                await iterator.aclose()
                d.CURRENT.reset(token)

    async def test_17_unchecked_eof_blank_pending_empty_and_early_target(self):
        for entries in ([], [f.edge(1, 1)], [f.edge(1, 1, text='  ')]):
            journal = f.Capture(); token = d.CURRENT.set(journal)
            try:
                await self.rejected(f.Session({('offset', 0): f.page(entries)}), 'window_not_checked')
            finally:
                d.CURRENT.reset(token)
            self.assertEqual(bool(entries), journal.records[-1]['pending_present'])
        await self.rejected(f.Session({('offset', 0): f.page([f.edge(7, 10)], 'T')}), 'pending_missing')
        session = span_session()
        session.routes[('cursor', 'CP')] = f.page([f.edge(5, 8, text='  '), f.edge(6, 9, text='  ')])
        await self.rejected(session, 'window_not_checked')

    async def test_18_verified_eof_and_late_failure_never_builds_html(self):
        rows, _ = await self.captured(span_session(after=False))
        self.assertEqual(10, len(rows))
        for data, reason in ((f.page([f.edge(99, 11)]), 'late_time_regression'),
                             (f.Response({}, 403), '403')):
            session = span_session(); session.routes[('cursor', 'AFTER')] = data
            with tempfile.TemporaryDirectory() as root:
                with self.assertRaisesRegex(client.TwitchAPIError, reason):
                    await self.run_worker(session, root)
                self.last_build.assert_not_awaited()
                self.assertEqual([], list((Path(root)/'spool').iterdir()))

    async def test_19_cancel_http_at_first_pending_control_and_q_closes(self):
        for key in (('offset', 0), ('cursor', 'C0'), ('offset', 5), ('cursor', 'C1')):
            with self.subTest(key=key):
                session, blocked = span_session(), Blocked()
                session.routes[key] = blocked
                task = asyncio.create_task(self.collect(session))
                await asyncio.wait_for(blocked.entered.wait(), 1)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                self.assertTrue(blocked.closed)
                self.assertTrue(all(value.closed for value in session.responses))
                self.assertIsNone(client.COMMENTS_BODY_LIMIT.get())
                self.assertEqual([asyncio.current_task()], list(asyncio.all_tasks()))

    async def test_19_close_and_cancel_after_last_yield_does_not_complete_stage(self):
        for stop_at, expected_stage in ((2, 'release_prefix'), (4, 'release_prefix'),
                                         (5, 'emit_repair'), (10, 'emit_repair')):
            for cancel in (False, True):
                session, journal, stopped = span_session(), f.Capture(), False
                token = d.CURRENT.set(journal)
                with patch.dict(os.environ, f.ENV, clear=True), patch.object(fetch, '_sleep_with_cancel', AsyncMock()):
                    iterator = fetch.gql_fetch_comments(session, 'client', f.VOD, cancel_check=lambda: stopped)
                    for _ in range(stop_at):
                        await iterator.__anext__()
                    calls = len(session.calls)
                    if cancel:
                        stopped = True
                        with self.assertRaisesRegex(RuntimeError, 'отменена'):
                            await iterator.__anext__()
                    await iterator.aclose()
                d.CURRENT.reset(token)
                end = journal.records[-1]
                self.assertEqual(expected_stage, end['stage'])
                self.assertFalse(end['verified'])
                self.assertEqual(calls, len(session.calls))
                self.assertTrue(all(value.closed for value in session.responses))

    async def test_19_cancel_inside_real_union(self):
        union = r.ordered_union
        stopped = False
        def cancel_union(*args, **kwargs):
            nonlocal stopped
            stopped = True
            return union(*args, **kwargs)
        rows = []
        with patch.object(r, 'ordered_union', cancel_union), self.assertRaisesRegex(RuntimeError, 'отменена'):
            await self.collect(span_session(), cancel=lambda: stopped, rows=rows)
        self.assertTrue(all(v[0] < 8 for v in rows))

    async def test_19_cancel_control_retry_pause_never_sends_next_attempt(self):
        session, entered = span_session(), asyncio.Event()
        session.routes[('offset',5)] = [f.Response({},429,{'Retry-After':'0.25'}),
                                       f.Response(session.routes[('offset',5)])]
        async def pause(delay, cancel_check):
            if delay == 0.25:
                entered.set()
                await asyncio.Future()
        async def consume():
            iterator = fetch.gql_fetch_comments(session,'client',f.VOD)
            try:
                return [value async for value in iterator]
            finally:
                await iterator.aclose()
        with patch.dict(os.environ,f.ENV,clear=True), patch.object(fetch,'_sleep_with_cancel',pause):
            task = asyncio.create_task(consume())
            await asyncio.wait_for(entered.wait(),1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(5,len(session.calls))  # Four main + one failed control.
        self.assertTrue(all(response.closed for response in session.responses))
        self.assertIsNone(client.COMMENTS_BODY_LIMIT.get())

    async def test_21_retention_exact_limits_roles_and_occurrences(self):
        for attr, limit in (('MAX_RECORDS', 4096), ('MAX_BYTES', 4*1024*1024), ('MAX_ESTIMATED_BYTES', 32*1024*1024)):
            self.assertEqual(limit, getattr(r, attr))
        # Identity of the page, not ID contents, controls ownership accounting.
        retention = r.Retention()
        raw = fetch.CommentPage([f.edge(1, 0)], 'next', True, 1)
        first = retention.accept(raw, 'main')
        retention.retain([first, first])
        self.assertEqual(1, len(retention.pages))
        second = retention.accept(raw, 'control')
        self.assertEqual(2, sum(len(v.records) for v in retention.pages))
        self.assertIsNot(first, second)
        for attr, amount, reason in (('MAX_RECORDS', len(first.records), 'record_limit|buffer_limit'),
                                     ('MAX_BYTES', first.serialized, 'buffer_limit'),
                                     ('MAX_ESTIMATED_BYTES', first.estimated, 'memory_estimate_limit')):
            with patch.object(r, attr, amount):
                fresh = r.Retention()
                fresh.accept(raw, 'main')
                actual = len(fresh.pages[0].records) if attr == 'MAX_RECORDS' else getattr(
                    fresh.pages[0], {'MAX_BYTES':'serialized', 'MAX_ESTIMATED_BYTES':'estimated'}[attr])
                self.assertEqual(amount, actual)
                with self.assertRaisesRegex(r.RepairError, reason):
                    fresh.accept(raw, 'control')
            with patch.object(r, attr, amount-1), self.assertRaisesRegex(r.RepairError, reason):
                r.Retention().accept(raw, 'main')
        with self.assertRaisesRegex(r.RepairError, 'cursor_limit'):
            r.Retention().accept(fetch.CommentPage([f.edge(1,0)], 'x'*4097, True, 1), 'main')
        retention.pages.clear()

    async def test_21_all_owners_included_before_control_and_lookahead(self):
        accepted = []
        original = r.Retention.accept
        def observe(owner, page, branch):
            result = original(owner, page, branch)
            accepted.append((branch, [len(p.records) for p in owner.pages]))
            return result
        with patch.object(r.Retention, 'accept', observe):
            await self.collect(span_session())
        # 2 C pages + P + T + seven control rows + Q.
        self.assertEqual(('control', [2, 2, 2, 2, 7]), accepted[4])
        self.assertEqual(('main', [2, 2, 2, 2, 7, 2]), accepted[5])
        with patch.object(r, 'MAX_RECORDS', 16):
            await self.rejected(span_session(), 'record_limit')

    async def test_20_failed_worker_keeps_real_temp_ready_registry_cache_and_history(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            with patch.object(db_core, 'DB_PATH', str(root/'app.sqlite3')), \
                 patch.object(config, 'SPOOL_DIR', str(root/'spools')), \
                 patch.object(html_publisher, 'OUT_DIR', str(root/'html')):
                db.init_db()
                old = results.Publication(f.VOD, mode='local')
                old.reserve()
                # A synthetic predecessor fixture in the test's own root;
                # no downloaded user HTML and no production pipeline run.
                data = b'<html>synthetic existing generation</html>'
                old.path().write_bytes(data)
                old.meta = {'title':'fixture', 'channel':'fixture', 'length_seconds':30}
                old.stats = {'messages':1, 'unique_users':1, 'parts':0}
                old.stage(len(data), hashlib.sha256(data).hexdigest())
                old._published = True
                cache_id = old.complete()
                old.close()
                db.add_user_history(1, cache_id)
                def snapshot():
                    connection = db_core._connect()
                    try:
                        return {table:[tuple(v) for v in connection.execute('SELECT * FROM '+table)]
                                for table in ('html_current', 'vod_cache', 'user_history')}
                    finally:
                        connection.close()
                before = snapshot()
                for failure in ('control', 'late', 'spool', 'cancel_control'):
                    attempt = results.Publication(f.VOD, mode='local')
                    attempt.reserve()
                    session = span_session()
                    blocked = None
                    if failure == 'cancel_control':
                        blocked = Blocked(); session.routes[('offset',5)] = blocked
                    elif failure != 'spool':
                        session.routes[('offset',5) if failure == 'control' else ('cursor','AFTER')] = f.Response({}, 403)
                    try:
                        with patch.object(f.ChatSpool, 'append_batch', side_effect=OSError('fixture write failure')) if failure == 'spool' else patch.object(f.ChatSpool, 'append_batch', f.ChatSpool.append_batch):
                            try:
                                if blocked is not None:
                                    task = asyncio.create_task(self.run_worker(session, root, publication=attempt))
                                    await asyncio.wait_for(blocked.entered.wait(), 1)
                                    task.cancel()
                                    await task
                                else:
                                    await self.run_worker(session, root, publication=attempt)
                            except (client.TwitchAPIError, OSError, asyncio.CancelledError) as exc:
                                attempt.fail(exc)
                            else:
                                self.fail('failed job unexpectedly completed')
                        self.last_build.assert_not_awaited()
                    finally:
                        attempt.close()
                    self.assertEqual(before, snapshot())
                    self.assertEqual(data, old.path().read_bytes())
                    self.assertEqual(old.id, results.get_ready_result(f.VOD)['id'])
                    self.assertFalse(attempt.path().exists())

    async def test_20_cancel_owned_spool_write_drains_thread_and_never_builds(self):
        entered, release = threading.Event(), threading.Event()
        append = f.ChatSpool.append_batch
        def blocked_append(spool, batch, cancel_check):
            entered.set()
            if not release.wait(3):
                raise AssertionError('test did not release writer')
            return append(spool, batch, cancel_check)
        with tempfile.TemporaryDirectory() as root, patch.object(f.ChatSpool, 'append_batch', blocked_append):
            task = asyncio.create_task(self.run_worker(span_session(), root))
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                task.cancel()
            finally:
                release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.last_build.assert_not_awaited()
            self.assertEqual([], list((Path(root)/'spool').iterdir()))
            self.assertEqual([asyncio.current_task()], list(asyncio.all_tasks()))

    async def test_25_disabled_and_other_vod_exact_rows_spool_and_in_memory_html(self):
        expected = [fetch.message_row(f.edge(n,t)['node']) for n,t in
                    ((1,0),(2,2),(3,4),(4,6),(5,8),(6,9),(7,10),(8,12),(9,13),(10,14),(11,20))]
        def payloads(rows):
            return [f.encode_row({'t':str(v[0]), 'user':v[2], 'text':v[3], 'fragments':v[4],
                                  'color':v[5], 'badges':v[6]})[1].encode('utf-8') for v in rows]
        def html(rows):
            return render_viewer_html([json.loads(value) for value in payloads(rows)],
                                      title='fixture', channel='fixture', vod_url='https://fixture.invalid').encode('utf-8')
        for env in ({'TCD_PAGINATION_REPAIR_PILOT':'false'}, {**f.ENV,'TCD_PAGINATION_REPAIR_VOD':'987654321'}):
            session = span_session([f.edge(99,9.5)])
            rows = await self.collect(session, env=env)
            self.assertEqual(expected, rows)
            self.assertEqual(payloads(expected), payloads(rows))
            self.assertEqual(html(expected), html(rows))
            self.assertEqual([('offset',0),('cursor','C0'),('cursor','CP'),('cursor','PT'),('cursor','C1'),('cursor','AFTER')],
                             [key for key,_ in session.calls])
            with tempfile.TemporaryDirectory() as root:
                spool = f.ChatSpool(root); spool.open()
                try:
                    spool.append_batch([(v[2],value.decode()) for v,value in zip(rows,payloads(rows))])
                    self.assertEqual(payloads(expected), [json.dumps(v,ensure_ascii=False).encode() for v in spool.iter_rows()])
                finally:
                    spool.close()

    async def test_27_large_graph_closes_record_and_page_references(self):
        record_refs, page_refs = [], []
        record_from_edge, accept = r.Record.from_edge, r.Retention.accept
        def record(*args):
            value = record_from_edge(*args); record_refs.append(weakref.ref(value)); return value
        def page(owner, *args):
            value = accept(owner, *args); page_refs.append(weakref.ref(value)); return value
        with patch.object(r.Record, 'from_edge', record), patch.object(r.Retention, 'accept', page):
            rows, event = await self.captured(large_session(200, 200))
        self.assertEqual(2*200+6+2*(200//3), len(rows))
        self.assertLessEqual(event['peak_records'], r.MAX_RECORDS)
        self.assertLessEqual(event['peak_bytes'], r.MAX_BYTES)
        self.assertLessEqual(event['peak_estimated_bytes'], r.MAX_ESTIMATED_BYTES)
        gc.collect()
        self.assertFalse(any(ref() is not None for ref in record_refs))
        self.assertFalse(any(ref() is not None for ref in page_refs))
        self.assertEqual([asyncio.current_task()], list(asyncio.all_tasks()))

    async def test_23_two_six_second_responses_hit_unchanged_worker_timeout(self):
        class Slow(f.Response):
            async def __aenter__(self):
                try:
                    await asyncio.sleep(6)
                    return self
                except BaseException:
                    self.closed = True
                    raise
        session = span_session()
        for key in (('offset', 0), ('cursor', 'C0')):
            session.routes[key] = Slow(session.routes[key])
        began = time.monotonic()
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaises(TimeoutError):
                await self.run_worker(session, root)
            self.last_build.assert_not_awaited()
            self.assertFalse((Path(root)/'spool').exists())
        self.assertGreaterEqual(time.monotonic()-began, 9.8)
        self.assertLess(time.monotonic()-began, 13)
        self.assertEqual(2, len(session.calls))
        self.assertTrue(all(v.closed for v in session.responses))

    async def test_24_request_context_reset_across_tasks_and_close(self):
        session, journal = span_session(), f.Capture()
        token = d.CURRENT.set(journal)
        with patch.dict(os.environ, f.ENV, clear=True), patch.object(fetch, '_sleep_with_cancel', AsyncMock()):
            iterator = fetch.gql_fetch_comments(session, 'client', f.VOD)
            first = await asyncio.wait_for(iterator.__anext__(), 1)
            self.assertIsNone(client.COMMENTS_BODY_LIMIT.get())
            self.assertIs(journal, d.CURRENT.get())
            remaining = [v async for v in iterator]
            await iterator.aclose()
        d.CURRENT.reset(token)
        self.assertEqual(11, len([first, *remaining]))
        self.assertIsNone(client.COMMENTS_BODY_LIMIT.get())

    async def test_26_historical_pair_metrics_run_through_actual_consumer(self):
        fixture = json.loads((Path(__file__).parent/'fixtures/pagination_repair.json').read_text())
        make = lambda numbers: [f.edge(n, *fixture['positions'][n-1]) for n in numbers]
        prefix = [make(p) for p in fixture['prefix']]
        routes = {('offset', 0): f.page(prefix[0], 'A17'), ('cursor', 'A17'): f.page(prefix[1], 'A18'),
                  ('cursor', 'A18'): f.page(make(fixture['main']), 'C1'),
                  ('cursor', 'C1'): f.page(make(fixture['c1']))}
        for i, numbers in enumerate(fixture['control']):
            routes[('offset', 20330) if i == 0 else ('cursor', f'B{i}')] = f.page(make(numbers), f'B{i+1}')
        rows, event = await self.captured(f.Session(routes), env={**f.ENV, 'TCD_PAGINATION_REPAIR_START':'20343',
            'TCD_PAGINATION_REPAIR_END':'20470', 'TCD_PAGINATION_REPAIR_CONTROL_OFFSET':'20330'})
        self.assertEqual((73, 106, 33), (event['original_ids'], event['merged_ids'], event['added_ids']))
        self.assertEqual((48, 73, 121, 154, 33), (event['pending_original_ids'], event['candidate_original_ids'],
            event['pair_original_ids'], event['pair_merged_ids'], event['pair_added_ids']))
        self.assertEqual((27, 27, 4), (event['tail_ids'], event['tail_confirmed_ids'], event['requests']))
        self.assertEqual(155, sum(20330 <= v[0] <= 20440 for v in rows))


if __name__ == '__main__':
    unittest.main()
