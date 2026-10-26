"""Helix user/archive lookups: mocked requests only, no credentials or API calls."""
import asyncio
import json
import traceback
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import aiohttp

from src.twitch_api import videos
from src.twitch_api.client import TwitchAPIError


def user_data():
    return {'id': '123', 'login': 'channel', 'display_name': 'Channel'}


def vod_data():
    return {
        'id': '456', 'user_id': '123', 'user_login': 'channel', 'user_name': 'Channel',
        'title': 'Archive title', 'created_at': '2026-01-01T10:00:00Z',
        'published_at': '2026-01-01T11:00:00Z', 'duration': '1h2m3s',
        'url': 'https://www.twitch.tv/videos/456',
        'thumbnail_url': 'https://images.invalid/%{width}x%{height}.jpg',
        'type': 'archive', 'viewable': 'public',
    }


class HelixVideosTests(unittest.IsolatedAsyncioTestCase):
    def _setupAsyncioRunner(self):
        # Match the existing API tests on Windows/Python 3.14.
        self._asyncioRunner = asyncio.Runner(debug=True, loop_factory=asyncio.SelectorEventLoop)

    def setUp(self):
        self.headers = {'Client-ID': 'mock-client', 'Authorization': 'Bearer MOCK_ONLY'}
        self.credentials = patch.object(videos, 'get_api_headers', return_value=self.headers)
        self.get_headers = self.credentials.start()
        self.addCleanup(self.credentials.stop)

    def session(self, data=None, *, status=200, headers=None, json_error=None):
        response = MagicMock()
        response.status = status
        response.headers = headers or {}
        response.json = AsyncMock(return_value=data, side_effect=json_error)
        response.__aenter__.return_value = response
        return SimpleNamespace(get=Mock(return_value=response)), response

    async def test_found_user_uses_normalized_login_and_existing_helix_headers(self):
        session, response = self.session({'data': [user_data()]})
        user = await videos.fetch_user_by_login(session, '  ChAnNeL  ')
        self.assertEqual(videos.TwitchUser('123', 'channel', 'Channel'), user)
        self.assertEqual(('https://api.twitch.tv/helix/users',), session.get.call_args.args)
        options = session.get.call_args.kwargs
        self.assertEqual({'login': 'channel'}, options['params'])
        self.assertEqual(self.headers, options['headers'])
        self.assertEqual(10, options['timeout'].total)
        self.assertFalse(options['allow_redirects'])
        session.get.assert_called_once()
        response.__aexit__.assert_awaited_once()

    async def test_absent_user_is_none_only_for_valid_empty_data(self):
        session, response = self.session({'data': []})
        self.assertIsNone(await videos.fetch_user_by_login(session, 'channel'))
        response.__aexit__.assert_awaited_once()

    async def test_invalid_or_mismatched_user_response_is_not_missing_user(self):
        for data in (None, [], {}, {'data': {}}, {'data': [None]},
                     {'data': [], 'error': 'failure'}, {'data': [], 'errors': ['failure']},
                     {'data': [user_data(), user_data()]},
                     {'data': [dict(user_data(), id=123)]},
                     {'data': [dict(user_data(), login='different')]},
                     {'data': [dict(user_data(), display_name=None)]}):
            with self.subTest(data=data):
                session, response = self.session(data)
                with self.assertRaises(TwitchAPIError) as raised:
                    await videos.fetch_user_by_login(session, 'channel')
                self.assertEqual(200, raised.exception.status)
                self.assertFalse(raised.exception.retryable)
                response.__aexit__.assert_awaited_once()

    async def test_archive_page_is_structured_and_preserves_opaque_cursor(self):
        cursor = 'opaque+cursor/=='
        session, response = self.session({'data': [vod_data()], 'pagination': {'cursor': cursor}})
        vods, next_cursor = await videos.fetch_archive_vods(session, '123')
        self.assertEqual(1, len(vods))
        self.assertIsInstance(vods[0], videos.ArchiveVod)
        self.assertEqual(('456', 'Archive title', '1h2m3s', 'public'),
                         (vods[0].id, vods[0].title, vods[0].duration, vods[0].viewable))
        self.assertEqual(vod_data()['thumbnail_url'], vods[0].thumbnail_url)
        self.assertEqual(cursor, next_cursor)
        session.get.assert_called_once()
        self.assertEqual(('https://api.twitch.tv/helix/videos',), session.get.call_args.args)
        self.assertEqual({'user_id': '123', 'type': 'archive', 'sort': 'time', 'first': 100},
                         session.get.call_args.kwargs['params'])
        self.assertEqual(self.headers, session.get.call_args.kwargs['headers'])
        response.__aexit__.assert_awaited_once()

    async def test_requested_cursor_is_sent_unchanged_without_following_next_page(self):
        cursor = 'page+two/=='
        session, _ = self.session({'data': [vod_data()], 'pagination': {'cursor': 'page-three'}})
        _, next_cursor = await videos.fetch_archive_vods(session, '123', cursor)
        self.assertEqual(cursor, session.get.call_args.kwargs['params']['after'])
        self.assertEqual('page-three', next_cursor)
        session.get.assert_called_once()

    async def test_last_and_empty_pages_return_none_cursor(self):
        for records in ([], [vod_data()]):
            with self.subTest(records=records):
                session, _ = self.session({'data': records, 'pagination': {}})
                vods, cursor = await videos.fetch_archive_vods(session, '123')
                self.assertEqual(len(records), len(vods))
                self.assertIsNone(cursor)
                session.get.assert_called_once()

    async def test_empty_page_with_cursor_is_not_silently_treated_as_end(self):
        session, _ = self.session({'data': [], 'pagination': {'cursor': 'continue'}})
        self.assertEqual(([], 'continue'), await videos.fetch_archive_vods(session, '123'))
        session.get.assert_called_once()

    async def test_viewable_is_raw_metadata_not_an_access_filter(self):
        for viewable in ('public', 'private'):
            with self.subTest(viewable=viewable):
                session, _ = self.session({'data': [dict(vod_data(), viewable=viewable)], 'pagination': {}})
                vods, _ = await videos.fetch_archive_vods(session, '123')
                self.assertEqual(viewable, vods[0].viewable)

    async def test_malformed_videos_pages_raise_instead_of_returning_partial_data(self):
        good = {'data': [vod_data()], 'pagination': {}}
        malformed = [None, [], {}, {'data': {}}, {'data': []},
                     dict(good, pagination=[]), dict(good, data=[None]),
                     dict(good, data=[vod_data(), dict(vod_data(), title=None)]),
                     dict(good, data=[dict(vod_data(), type='highlight')]),
                     dict(good, data=[dict(vod_data(), user_id='999')]),
                     dict(good, data=[dict(vod_data(), id=False)]),
                     dict(good, data=[dict(vod_data(), duration=100)]),
                     dict(good, data=[{}]), dict(good, data=[vod_data()]*101)]
        malformed += [dict(good, pagination={'cursor': value}) for value in ('', None, 3)]
        for data in malformed:
            with self.subTest(data=data):
                session, response = self.session(data)
                with self.assertRaises(TwitchAPIError):
                    await videos.fetch_archive_vods(session, '123')
                session.get.assert_called_once()
                response.__aexit__.assert_awaited_once()

    async def test_http_errors_remain_errors_for_both_lookups(self):
        for function, argument in ((videos.fetch_user_by_login, 'channel'), (videos.fetch_archive_vods, '123')):
            for status in (204, 302, 400, 401, 403, 404, 429, 500, 503):
                with self.subTest(function=function.__name__, status=status):
                    session, response = self.session({'data': []}, status=status)
                    with self.assertRaises(TwitchAPIError) as raised:
                        await function(session, argument)
                    self.assertEqual(status, raised.exception.status)
                    self.assertEqual(status == 429 or status >= 500, raised.exception.retryable)
                    response.json.assert_not_awaited()
                    response.__aexit__.assert_awaited_once()
                    session.get.assert_called_once()

    async def test_invalid_json_is_not_an_empty_result(self):
        for function, argument in ((videos.fetch_user_by_login, 'channel'), (videos.fetch_archive_vods, '123')):
            for error in (json.JSONDecodeError('invalid', 'x', 0),
                          aiohttp.ContentTypeError(Mock(), (), message='not JSON')):
                with self.subTest(function=function.__name__, error=type(error).__name__):
                    session, response = self.session(json_error=error)
                    with self.assertRaises(TwitchAPIError) as raised:
                        await function(session, argument)
                    self.assertEqual(200, raised.exception.status)
                    self.assertFalse(raised.exception.retryable)
                    self.assertIn('invalid JSON', str(raised.exception))
                    response.__aexit__.assert_awaited_once()

    async def test_network_errors_and_timeouts_are_retryable_without_leaking_details(self):
        secret = 'MOCK_SECRET_DO_NOT_LOG'
        for function, argument in ((videos.fetch_user_by_login, 'channel'), (videos.fetch_archive_vods, '123')):
            for error in (aiohttp.ClientError(secret), asyncio.TimeoutError(secret), OSError(secret)):
                with self.subTest(function=function.__name__, error=type(error).__name__):
                    session = SimpleNamespace(get=Mock(side_effect=error))
                    with self.assertNoLogs('src.twitch_api.videos'), self.assertRaises(TwitchAPIError) as raised:
                        await function(session, argument)
                    self.assertTrue(raised.exception.retryable)
                    self.assertIsNone(raised.exception.status)
                    self.assertNotIn(secret, ''.join(traceback.format_exception(raised.exception)))
                    session.get.assert_called_once()

    async def test_rate_limit_reset_is_exposed_without_sleep_or_retry(self):
        for reset, expected in (('1030', 30), ('990', 0), ('1000000', 999000)):
            with self.subTest(reset=reset), patch.object(videos.time, 'time', return_value=1000):
                session, response = self.session(status=429, headers={'Ratelimit-Reset': reset})
                with self.assertRaises(TwitchAPIError) as raised:
                    await videos.fetch_archive_vods(session, '123')
                self.assertEqual(expected, raised.exception.retry_after)
                self.assertTrue(raised.exception.retryable)
                session.get.assert_called_once()
                response.__aexit__.assert_awaited_once()

    async def test_rate_limit_header_fallback_and_conservative_combination(self):
        cases = [({}, None), ({'Ratelimit-Reset': 'bad'}, None),
                 ({'Ratelimit-Reset': 'nan'}, None), ({'Ratelimit-Reset': 'inf'}, None),
                 ({'Ratelimit-Reset': '-1', 'Retry-After': '7'}, 7),
                 ({'Ratelimit-Reset': 'invalid', 'Retry-After': '7'}, 7),
                 ({'Ratelimit-Reset': '1030', 'Retry-After': '40'}, 40)]
        for headers, expected in cases:
            with self.subTest(headers=headers), patch.object(videos.time, 'time', return_value=1000):
                session, _ = self.session(status=429, headers=headers)
                with self.assertRaises(TwitchAPIError) as raised:
                    await videos.fetch_user_by_login(session, 'channel')
                self.assertEqual(expected, raised.exception.retry_after)
                session.get.assert_called_once()

    async def test_invalid_arguments_do_not_use_credentials_or_network(self):
        session = SimpleNamespace(get=Mock())
        for login in ('', ' ', None, 'https://twitch.tv/channel', 'bad name', 'x'*26):
            with self.subTest(login=login), self.assertRaises(ValueError):
                await videos.fetch_user_by_login(session, login)
        for user_id in ('', '0', 'abc', None, 123):
            with self.subTest(user_id=user_id), self.assertRaises(ValueError):
                await videos.fetch_archive_vods(session, user_id)
        for cursor in ('', 123, False):
            with self.subTest(cursor=cursor), self.assertRaises(ValueError):
                await videos.fetch_archive_vods(session, '123', cursor)
        session.get.assert_not_called()
        self.get_headers.assert_not_called()

    async def test_cancellation_propagates_and_closes_response(self):
        session, response = self.session(json_error=asyncio.CancelledError())
        with self.assertRaises(asyncio.CancelledError):
            await videos.fetch_archive_vods(session, '123')
        response.__aexit__.assert_awaited_once()


if __name__ == '__main__':
    unittest.main()
