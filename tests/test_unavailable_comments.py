"""Unavailable/malformed comments must never become successful HTML results."""
import asyncio
import copy
from unittest.mock import AsyncMock, patch
import unittest

import aiohttp

import test_result_callbacks as registry_fixture
from test_twitch_api_reliability import _FakeSession, _Response, _ResponseContext
from src.database import database as db, db_results as results
from src.handlers import handlers_vod
from src.handlers.handlers_state import begin_download, is_busy
from src.twitch_api import fetch_comments, fetch_page
from src.workers import download_worker


def page(edges=None, has_next=False):
    return _Response(data=[{'data': {'video': {'comments': {
        'edges': [] if edges is None else edges,
        'pageInfo': {'hasNextPage': has_next},
    }}}}])


def edge():
    return {'cursor': 'test-continuation', 'node': {
        'contentOffsetSeconds': 1, 'createdAt': '', 'commenter': None,
        'message': {'fragments': [{'text': 'message'}]},
    }}


class UnavailableCommentsTests(unittest.IsolatedAsyncioTestCase):
    setUp = registry_fixture.ResultCallbacksTestCase.setUp
    asyncTearDown = registry_fixture.ResultCallbacksTestCase.asyncTearDown
    plan = registry_fixture.ResultCallbacksTestCase.plan
    publish = registry_fixture.ResultCallbacksTestCase.publish
    ready = registry_fixture.ResultCallbacksTestCase.ready
    rows = registry_fixture.ResultCallbacksTestCase.rows
    request = registry_fixture.ResultCallbacksTestCase.request
    source = registry_fixture.ResultCallbacksTestCase.source

    def _setupAsyncioRunner(self):
        self._asyncioRunner = asyncio.Runner(debug=True, loop_factory=asyncio.SelectorEventLoop)

    async def run_response(self, response, *, partial=False, cancel=False, fallback=False):
        update, context = self.request()
        session = _FakeSession([])
        responses = [_ResponseContext(page([edge(), edge()], True))] if partial else []
        responses.append(response if isinstance(response, BaseException) else _ResponseContext(response))
        session.post.side_effect = responses
        self.application.bot_data['aiohttp'] = session
        if fallback:
            context.bot.edit_message_text.side_effect = OSError('mock Telegram edit failure')
        operation = begin_download(context)
        before = {row['id'] for row in self.rows()}
        with patch.object(download_worker, 'gql_fetch_comments', fetch_comments.gql_fetch_comments), \
             patch.object(download_worker, 'SPOOL_BATCH_MESSAGES', 1), \
             patch.object(download_worker, 'build_html_result', AsyncMock()) as build, \
             patch.object(fetch_comments, 'GQL_MAX_RETRIES', 1), \
             patch.object(fetch_comments, '_sleep_with_cancel', AsyncMock()):
            runner = handlers_vod._runner(context, 1, 9, update.message.text,
                                           '123456789', 'html_online', 1, operation)
            if cancel:
                with self.assertRaises(asyncio.CancelledError):
                    await runner
            else:
                await runner
            build.assert_not_awaited()
        self.assertFalse(is_busy(context))
        self.assertNotIn('vod_global_operation', self.application.bot_data)
        self.assertNotIn('vod_active_operation', context.user_data)
        self.assertFalse(tuple((self.root/'work').glob('tdb-spool-*')))
        self.assertFalse(tuple((self.root/'html').glob('*.tmp')))
        added = [row for row in self.rows() if row['id'] not in before]
        self.assertEqual(1, len(added))
        self.assertEqual('cancelled' if cancel else 'failed', added[0]['state'])
        self.assertFalse((self.root/'html'/added[0]['filename']).exists())
        self.assertEqual(2 if partial else 1, session.post.call_count)
        return context

    async def test_http_graphql_invalid_json_and_network_failures_never_activate_partial_results(self):
        failures = [
            _Response(status=401, data={'message': 'Unauthorized'}),
            _Response(status=403, data={'message': 'Forbidden'}),
            _Response(data=[{'errors': [{'message': 'Access unavailable',
                'extensions': {'code': 'AccessDenied'}}], 'data': {'video': {'comments': None}}}]),
            _Response(data=[{'errors': [{'message': 'Incomplete response'}],
                'data': {'video': {'comments': {'edges': [edge()], 'pageInfo': {'hasNextPage': False}}}}}]),
            _Response(data=[{'data': {'video': {'comments': None}}}]),
            _Response(data=[{'data': {'video': None}}]),
            page([{'node': {}}]),
            _Response(json_error=ValueError('invalid JSON')),
            aiohttp.ClientConnectionError('mock connection lost'),
            asyncio.TimeoutError(),
        ]
        for regenerate in (False, True):
            old = self.ready() if regenerate else None
            old_bytes = old.path().read_bytes() if old else None
            if old:
                db.add_user_history(5, results.get_ready_result('123456789')['cache_id'])
            for partial in (False, True):
                for failure in failures:
                    with self.subTest(regenerate=regenerate, partial=partial, failure=repr(failure)):
                        context = await self.run_response(failure, partial=partial)
                        text = context.bot.edit_message_text.await_args.kwargs['text']
                        self.assertNotIn('У этого VOD нет чата.', text)
                        self.assertNotIn('подпис', text.lower())
                        self.assertTrue(text.startswith(('Не удалось загрузить чат VOD.', 'Сервис отклонил запрос.')))
                        self.assertIn('Повторите попытку позже.', text)
                        results.recover_results()
                        current = results.get_ready_result('123456789')
                        cache = db.get_cache('123456789', 'html_online')
                        if old:
                            self.assertEqual(old.id, current['id'])
                            self.assertEqual(old.ref, cache['html_url'])
                            self.assertEqual(old_bytes, old.path().read_bytes())
                            self.assertEqual(1, db.get_history_count(5))
                        else:
                            self.assertIsNone(current)
                            self.assertIsNone(cache)
                        self.assertEqual(1 if old else 0, len(tuple((self.root/'html').glob('*.html'))))

    async def test_valid_empty_chat_keeps_existing_message_and_old_generation(self):
        for regenerate in (False, True):
            with self.subTest(regenerate=regenerate):
                old = self.ready() if regenerate else None
                context = await self.run_response(page())
                self.assertEqual('У этого VOD нет чата.', context.bot.edit_message_text.await_args.kwargs['text'])
                current = results.get_ready_result('123456789')
                self.assertEqual(old.id if old else None, current['id'] if current else None)

    async def test_task_interruption_after_partial_comments_releases_slot_and_preserves_old_html(self):
        old = self.ready()
        original = old.path().read_bytes()
        await self.run_response(asyncio.CancelledError(), partial=True, cancel=True)
        self.assertEqual(old.id, results.get_ready_result('123456789')['id'])
        self.assertEqual(original, old.path().read_bytes())

    async def test_access_failure_uses_send_fallback_and_does_not_claim_subscription(self):
        context = await self.run_response(_Response(status=403, data={}), fallback=True)
        text = context.bot.send_message.await_args.kwargs['text']
        self.assertEqual('Сервис отклонил запрос. Повторите попытку позже.', text)
        self.assertNotIn('403', text)
        self.assertNotIn('подпис', text.lower())

    async def test_incomplete_message_data_and_invalid_continuation_are_errors(self):
        for comments in (None, [], {}, {'edges': None, 'pageInfo': {'hasNextPage': False}},
                         {'edges': [], 'pageInfo': None}, {'edges': [], 'pageInfo': {'hasNextPage': 'false'}}):
            with self.subTest(comments=comments):
                session = _FakeSession([_Response(data=[{'data': {'video': {'comments': comments}}}])])
                with self.assertRaises(fetch_page.TwitchAPIError):
                    await fetch_page.gql_fetch_page(session, 'test-client', '123456789', None, 0)
        invalid_nodes = [
            {}, {'message': None}, {'message': {}},
            {'message': {'fragments': None}}, {'message': {'fragments': 'text'}},
            {'message': {'fragments': [None]}}, {'message': {'fragments': [{}]}},
            {'message': {'fragments': [{'text': 7}]}},
        ]
        for node in invalid_nodes:
            with self.subTest(node=node):
                session = _FakeSession([page([{'node': node}])])
                with self.assertRaises(fetch_page.TwitchAPIError):
                    await fetch_page.gql_fetch_page(session, 'test-client', '123456789', None, 0)
        for cursor in (True, 7, ['cursor'], {'cursor': 'x'}):
            with self.subTest(cursor=cursor):
                item = edge()
                item['cursor'] = cursor
                session = _FakeSession([page([item], True)])
                with self.assertRaises(fetch_page.TwitchAPIError):
                    await fetch_page.gql_fetch_page(session, 'test-client', '123456789', None, 0)

    async def test_valid_message_allows_missing_commenter_and_optional_emote_fields(self):
        item = edge()
        item['node']['message']['fragments'][0]['emote'] = None
        session = _FakeSession([page([copy.deepcopy(item)])])
        rows = [row async for row in fetch_comments.gql_fetch_comments(session, 'test-client', '123456789')]
        self.assertEqual(1, len(rows))
        self.assertEqual(('unknown', 'message'), rows[0][2:4])
        item['node']['message']['fragments'] = []
        session = _FakeSession([page([item])])
        self.assertEqual([], [row async for row in fetch_comments.gql_fetch_comments(session, 'test-client', '123456789')])


if __name__ == '__main__':
    unittest.main()
