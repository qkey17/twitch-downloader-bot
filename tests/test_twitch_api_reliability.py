import asyncio
import json
import os
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import aiohttp

from src.twitch_api import badges, client, emotes, fetch_comments, fetch_page, meta
from src.twitch_api.client import TwitchAPIError
from src.workers import download_worker, worker_html


class _Response:
    def __init__(self, status=200, data=None, headers=None, json_error=None, body=None):
        self.status = status
        self.headers = headers or {}
        self.data = data
        self.json_error = json_error
        if body is None:
            body = json.dumps(data) if data is not None else ""
        self.content = _FakeContent(body.encode("utf-8") if isinstance(body, str) else body)

    async def json(self):
        if self.json_error:
            raise self.json_error
        return self.data


class _FakeContent:
    def __init__(self, body):
        self.body = body

    async def read(self, amount=-1):
        return self.body if amount < 0 else self.body[:amount]


class _ResponseContext:
    def __init__(self, response):
        self.response = response

    async def __aenter__(self):
        return self.response

    async def __aexit__(self, *_args):
        return False


class _FakeSession:
    def __init__(self, responses):
        self.post = Mock(side_effect=[_ResponseContext(item) for item in responses])


class TwitchGraphqlConfigurationTestCase(unittest.TestCase):
    def test_graphql_client_id_default_and_environment_override(self):
        from src.config import get_twitch_gql_client_id

        expected_default = "kd1unb4b3q4t58fwlpcbzcbnm76a8fp"
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(expected_default, get_twitch_gql_client_id())

        with patch.dict(os.environ, {"TWITCH_GQL_CLIENT_ID": "  custom-graphql-id  "}, clear=True):
            self.assertEqual("custom-graphql-id", get_twitch_gql_client_id())


class TwitchApiReliabilityTestCase(unittest.IsolatedAsyncioTestCase):
    def _setupAsyncioRunner(self):
        self._asyncioRunner = asyncio.Runner(
            debug=True,
            loop_factory=asyncio.SelectorEventLoop,
        )

    async def test_http_200_valid_empty_chat_is_an_empty_page(self):
        session = _FakeSession([_Response(data=[{
            "data": {
                "video": {
                    "comments": {"edges": [], "pageInfo": {"hasNextPage": False}},
                },
            },
        }])])

        page = await fetch_page.gql_fetch_page(session, "client", "123456789", None, 0)

        self.assertEqual(([], None, False), page)

    async def test_comments_request_sends_expected_persisted_query_payload_and_headers(self):
        from src.config import GQL_ENDPOINT, VIDEO_COMMENTS_HASH

        empty_page = _Response(data=[{"data": {"video": {"comments": {
            "edges": [], "pageInfo": {"hasNextPage": False},
        }}}}])
        session = _FakeSession([empty_page, empty_page])
        client_id = "test-graphql-client-id"

        await fetch_page.gql_fetch_page(session, client_id, "123456789", None, 0)
        await fetch_page.gql_fetch_page(session, client_id, "123456789", "cursor-value", 0)

        expected_headers = {
            "Client-Id": client_id,
            "User-Agent": "Mozilla/5.0",
            "Content-Type": "application/json",
        }
        initial_request = session.post.call_args_list[0]
        self.assertEqual(GQL_ENDPOINT, initial_request.args[0])
        self.assertEqual(expected_headers, initial_request.kwargs["headers"])
        self.assertEqual([{
            "operationName": "VideoCommentsByOffsetOrCursor",
            "variables": {"videoID": "123456789", "contentOffsetSeconds": 0},
            "extensions": {
                "persistedQuery": {"version": 1, "sha256Hash": VIDEO_COMMENTS_HASH},
            },
        }], initial_request.kwargs["json"])

        cursor_request = session.post.call_args_list[1]
        self.assertEqual(expected_headers, cursor_request.kwargs["headers"])
        self.assertEqual([{
            "operationName": "VideoCommentsByOffsetOrCursor",
            "variables": {"videoID": "123456789", "cursor": "cursor-value"},
            "extensions": {
                "persistedQuery": {"version": 1, "sha256Hash": VIDEO_COMMENTS_HASH},
            },
        }], cursor_request.kwargs["json"])

    async def test_graphql_request_and_response_diagnostics_are_bounded_and_redacted(self):
        cursor_value = "opaque-cursor-diagnostic-value"
        client_id_value = "client-id-must-not-be-logged"
        access_token_value = "access-token-must-not-be-logged"
        request = [{
            "operationName": "VideoCommentsByOffsetOrCursor",
            "variables": {"videoID": "123456789", "cursor": cursor_value},
            "extensions": {
                "persistedQuery": {"version": 1, "sha256Hash": "test-persisted-hash"},
            },
        }]
        response = _Response(data=[{"errors": [{
            "message": "Integrity check failed",
            "path": ["video", "comments"],
            "extensions": {"code": "IntegrityCheckFailed"},
            "chatMessage": "message data must not appear in diagnostics",
        }]}])
        session = _FakeSession([response])
        headers = {
            "Client-Id": client_id_value,
            "Authorization": f"Bearer {access_token_value}",
            "User-Agent": "Mozilla/5.0",
            "Content-Type": "application/json",
        }

        with self.assertLogs(client.__name__, level="INFO") as captured:
            with self.assertRaises(TwitchAPIError):
                await client.gql_post_json(
                    session, headers, request, page_number=3
                )

        logged = "\n".join(captured.output)
        self.assertIn("page=3", logged)
        self.assertIn("VideoCommentsByOffsetOrCursor", logged)
        self.assertNotIn(cursor_value, logged)
        self.assertIn(f'"cursor":{{"present":true,"length":{len(cursor_value)}}}', logged)
        self.assertEqual(cursor_value, request[0]["variables"]["cursor"])
        self.assertIs(request, session.post.call_args.kwargs["json"])
        self.assertIn('"version":1', logged)
        self.assertIn("test-persisted-hash", logged)
        self.assertIn("User-Agent", logged)
        self.assertIn("application/json", logged)
        self.assertIn("status=200", logged)
        self.assertIn("IntegrityCheckFailed", logged)
        self.assertNotIn(client_id_value, logged)
        self.assertNotIn(access_token_value, logged)
        self.assertNotIn("Authorization", logged)
        self.assertNotIn("message data must not appear", logged)

    async def test_comment_pagination_increments_diagnostic_page_number(self):
        edge = {"node": {
            "commenter": {"displayName": "tester"},
            "contentOffsetSeconds": 1,
            "message": {"fragments": [{"text": "hello"}]},
        }, "cursor": "next-cursor"}
        page = AsyncMock(side_effect=[([edge], "next-cursor", True), ([], None, False)])

        with patch.object(fetch_comments, "gql_fetch_page", page), \
             patch.object(fetch_comments, "_sleep_with_cancel", AsyncMock()):
            iterator = fetch_comments.gql_fetch_comments(object(), "client", "123456789")
            await iterator.__anext__()
            with self.assertRaises(StopAsyncIteration):
                await iterator.__anext__()

        self.assertEqual([1, 2], [call.kwargs["page_number"] for call in page.await_args_list])

    async def test_http_200_graphql_errors_are_not_an_empty_chat(self):
        session = _FakeSession([_Response(data=[{"errors": [{"message": "unauthorized"}]}])])

        with self.assertRaises(TwitchAPIError) as raised:
            await client.gql_post_json(session, {}, {})

        self.assertEqual(200, raised.exception.status)
        self.assertIn("unauthorized", raised.exception.response_body)

    async def test_graphql_error_diagnostics_keep_useful_fields_and_redact_secrets(self):
        secrets = {
            "TWITCH_ACCESS_TOKEN": "graphql-access-secret-value",
            "TELEGRAM_BOT_TOKEN_TWITCH": "123456:telegram-bot-secret-value",
            "TWITCH_CLIENT_ID": "graphql-client-id-sensitive-value",
        }
        errors = [{
            "message": (
                f"Access denied: Bearer {secrets['TWITCH_ACCESS_TOKEN']}; "
                f"client {secrets['TWITCH_CLIENT_ID']}"
            ),
            "path": ["video", "comments", 0],
            "locations": [{"line": 4, "column": 9}],
            "extensions": {
                "code": "FORBIDDEN",
                "classification": "AUTHENTICATION",
                "requestId": "request-123",
                "access_token": secrets["TWITCH_ACCESS_TOKEN"],
                "privateMessage": "user supplied chat text must not be logged",
            },
            "privateData": "unfiltered response data must not be logged",
        }]
        session = _FakeSession([_Response(data=[{"errors": errors}])])

        with patch.dict(os.environ, secrets), \
             self.assertLogs(client.__name__, level="WARNING") as captured:
            with self.assertRaises(TwitchAPIError) as raised:
                await client.gql_post_json(session, {}, {})

        diagnostic = raised.exception.response_body
        logged = "\n".join(captured.output)
        self.assertEqual(200, raised.exception.status)
        self.assertIn("FORBIDDEN", diagnostic)
        self.assertIn("AUTHENTICATION", diagnostic)
        self.assertIn("request-123", diagnostic)
        self.assertIn('"path":["video","comments",0]', diagnostic)
        self.assertIn('"line":4', diagnostic)
        self.assertLessEqual(len(diagnostic), client._ERROR_BODY_LOG_LIMIT)
        for value in (*secrets.values(), "unfiltered response data", "user supplied chat text"):
            self.assertNotIn(value, diagnostic)
            self.assertNotIn(value, logged)

    async def test_graphql_error_diagnostic_is_bounded(self):
        session = _FakeSession([_Response(data=[{"errors": [
            {"message": "x" * (client._ERROR_BODY_LOG_LIMIT * 3)},
        ]}])])

        with self.assertRaises(TwitchAPIError) as raised:
            await client.gql_post_json(session, {}, {})

        self.assertLessEqual(
            len(raised.exception.response_body), client._ERROR_BODY_LOG_LIMIT
        )

    async def test_http_401_and_403_are_api_errors_without_body_interpretation(self):
        for status in (401, 403, 400):
            with self.subTest(status=status):
                session = _FakeSession([_Response(status=status, data={"data": None})])
                with self.assertRaises(TwitchAPIError) as raised:
                    await client.gql_post_json(session, {}, {})
                self.assertEqual(status, raised.exception.status)
                self.assertFalse(raised.exception.retryable)

    async def test_http_400_keeps_bounded_redacted_body_in_error_and_logs(self):
        secret_values = {
            "TWITCH_ACCESS_TOKEN": "twitch-access-secret-value",
            "TWITCH_CLIENT_SECRET": "twitch-client-secret-value",
            "TELEGRAM_BOT_TOKEN_TWITCH": "123456:telegram-bot-secret-value",
        }
        body = json.dumps({
            "error": "Bad Request",
            "message": "Invalid GraphQL operation",
            "access_token": secret_values["TWITCH_ACCESS_TOKEN"],
            "authorization": "Bearer authorization-secret-value",
            "details": "x" * 1600,
            "echo": secret_values["TWITCH_CLIENT_SECRET"],
        })
        session = _FakeSession([_Response(status=400, body=body)])

        with patch.dict(os.environ, secret_values), \
             self.assertLogs(client.__name__, level="WARNING") as captured:
            with self.assertRaises(TwitchAPIError) as raised:
                await client.gql_post_json(
                    session,
                    {"Authorization": "Bearer request-header-secret-value"},
                    {},
                )

        error = raised.exception
        self.assertEqual(400, error.status)
        self.assertIn("Invalid GraphQL operation", error.response_body)
        self.assertLessEqual(len(error.response_body), client._ERROR_BODY_LOG_LIMIT)
        logged = "\n".join(captured.output)
        for secret in (*secret_values.values(), "authorization-secret-value", "request-header-secret-value"):
            self.assertNotIn(secret, error.response_body)
            self.assertNotIn(secret, logged)

    async def test_default_graphql_client_id_reaches_metadata_and_comments_headers(self):
        from src.config import DEFAULT_TWITCH_GQL_CLIENT_ID

        self.assertEqual("kd1unb4b3q4t58fwlpcbzcbnm76a8fp", DEFAULT_TWITCH_GQL_CLIENT_ID)

        metadata_response = _Response(data={"data": {"video": {
            "title": "VOD",
            "lengthSeconds": 60,
            "owner": {"id": "1"},
        }}})
        session = SimpleNamespace(
            post=Mock(return_value=_ResponseContext(metadata_response)),
            get=Mock(return_value=_ResponseContext(_Response(status=401))),
        )

        env = {
            "TWITCH_CLIENT_ID": "helix-client-id-test-1234567890",
            "TWITCH_ACCESS_TOKEN": "test-access-token",
        }
        with patch.dict(os.environ, env, clear=True), patch.object(meta, "get_api_headers", return_value={}):
            client_id = client.get_client_id()
            await meta.fetch_vod_meta(session, client_id, "123456789")

        sent_headers = session.post.call_args.kwargs["headers"]
        self.assertEqual(DEFAULT_TWITCH_GQL_CLIENT_ID, sent_headers["Client-Id"])
        self.assertNotIn("Authorization", sent_headers)
        self.assertEqual("123456789", session.post.call_args.kwargs["json"]["variables"]["id"])

        comment_session = _FakeSession([_Response(data=[{"data": {"video": {"comments": {
            "edges": [], "pageInfo": {"hasNextPage": False},
        }}}}])])
        await fetch_page.gql_fetch_page(
            comment_session, client.get_client_id(), "123456789", None, 0
        )
        self.assertEqual(
            DEFAULT_TWITCH_GQL_CLIENT_ID,
            comment_session.post.call_args.kwargs["headers"]["Client-Id"],
        )

    async def test_explicit_graphql_client_id_is_separate_from_helix_client_id(self):
        from src.config import get_twitch_client_id

        graphql_id = "graphql-client-id-test-1234567890"
        metadata_response = _Response(data={"data": {"video": None}})
        metadata_session = SimpleNamespace(
            post=Mock(return_value=_ResponseContext(metadata_response)),
            get=Mock(),
        )
        comments_session = _FakeSession([_Response(data=[{"data": {"video": {"comments": {
            "edges": [], "pageInfo": {"hasNextPage": False},
        }}}}])])

        with patch.dict(os.environ, {
            "TWITCH_CLIENT_ID": "helix-client-id-test-1234567890",
            "TWITCH_ACCESS_TOKEN": "helix-access-token-test",
            "TWITCH_GQL_CLIENT_ID": graphql_id,
        }, clear=True):
            self.assertEqual("helix-client-id-test-1234567890", get_twitch_client_id())
            self.assertEqual("graphql-client-id-test-1234567890", client.get_client_id())
            from src.config import get_twitch_credentials
            credentials = get_twitch_credentials()
            self.assertEqual("helix-client-id-test-1234567890", credentials.client_id)
            self.assertEqual("helix-access-token-test", credentials.access_token)
            await meta.fetch_vod_meta(metadata_session, client.get_client_id(), "123456789")
            await fetch_page.gql_fetch_page(
                comments_session, client.get_client_id(), "123456789", None, 0
            )

        self.assertEqual(graphql_id, metadata_session.post.call_args.kwargs["headers"]["Client-Id"])
        self.assertEqual(graphql_id, comments_session.post.call_args.kwargs["headers"]["Client-Id"])

    async def test_http_429_parses_retry_after_and_comments_wait_that_duration(self):
        response = _Response(status=429, headers={"Retry-After": "7.5"})
        with self.assertRaises(TwitchAPIError) as raised:
            await client.gql_post_json(_FakeSession([response]), {}, {})
        self.assertEqual(7.5, raised.exception.retry_after)

        attempts = 0

        async def page_with_rate_limit(**_kwargs):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise TwitchAPIError("rate limit", status=429, retry_after=7.5, retryable=True)
            return [], None, False

        sleeper = AsyncMock()
        with patch.object(fetch_comments, "gql_fetch_page", side_effect=page_with_rate_limit), \
             patch.object(fetch_comments, "_sleep_with_cancel", sleeper):
            iterator = fetch_comments.gql_fetch_comments(object(), "client", "123456789")
            with self.assertRaises(StopAsyncIteration):
                await iterator.__anext__()

        self.assertEqual(2, attempts)
        sleeper.assert_awaited_once_with(7.5, None)

    async def test_http_5xx_is_retryable_but_exhaustion_remains_an_error(self):
        session = _FakeSession([_Response(status=503, data={})])
        with self.assertRaises(TwitchAPIError) as raised:
            await client.gql_post_json(session, {}, {})
        self.assertTrue(raised.exception.retryable)

        api_error = TwitchAPIError("upstream failure", status=503, retryable=True)
        page = AsyncMock(side_effect=api_error)
        with patch.object(fetch_comments, "gql_fetch_page", page), \
             patch.object(fetch_comments, "GQL_MAX_RETRIES", 2), \
             patch.object(fetch_comments, "_sleep_with_cancel", AsyncMock()):
            with self.assertRaises(TwitchAPIError):
                await fetch_comments.gql_fetch_comments(object(), "client", "123456789").__anext__()
        self.assertEqual(2, page.await_count)

        network_page = AsyncMock(side_effect=aiohttp.ClientError("connection lost"))
        with patch.object(fetch_comments, "gql_fetch_page", network_page), \
             patch.object(fetch_comments, "GQL_MAX_RETRIES", 1):
            with self.assertRaises(TwitchAPIError):
                await fetch_comments.gql_fetch_comments(object(), "client", "123456789").__anext__()

    async def test_invalid_json_and_unexpected_graphql_shapes_raise(self):
        invalid_json = _FakeSession([_Response(json_error=ValueError("invalid json"))])
        with self.assertRaises(TwitchAPIError):
            await client.gql_post_json(invalid_json, {}, {})

        unexpected_json = _FakeSession([_Response(data="not-an-object")])
        with self.assertRaises(TwitchAPIError):
            await client.gql_post_json(unexpected_json, {}, {})

        unexpected_page = _FakeSession([_Response(data={"data": {"video": None}})])
        with self.assertRaises(TwitchAPIError):
            await fetch_page.gql_fetch_page(unexpected_page, "client", "123456789", None, 0)

    async def _run_download_with_comment_iterator(self, iterator, *, timeout=False):
        context = SimpleNamespace(application=SimpleNamespace(bot_data={"aiohttp": object()}), user_data={})
        vod_meta = meta.VodMeta(vod_id="123456789", length_seconds=42)
        progress = AsyncMock()
        def timeout_first_page(awaitable, timeout=None):
            awaitable.close()
            raise asyncio.TimeoutError

        wait_patch = patch.object(download_worker.asyncio, "wait_for", side_effect=timeout_first_page) if timeout else patch.object(download_worker.asyncio, "wait_for", wraps=asyncio.wait_for)
        with patch.object(download_worker, "get_client_id", return_value="client"), \
             patch.object(download_worker, "fetch_vod_meta", AsyncMock(return_value=vod_meta)), \
             patch.object(download_worker, "make_progress_updater", return_value=progress), \
             patch.object(download_worker, "gql_fetch_comments", return_value=iterator), \
             wait_patch:
            return await download_worker.download_and_send(
                context, 1, 2, "vod-url", "123456789", "html_online"
            )

    async def test_initial_page_timeout_is_not_reported_as_empty_chat(self):
        class NeverIterator:
            async def __anext__(self):
                await asyncio.Future()

        with self.assertRaises(asyncio.TimeoutError):
            await self._run_download_with_comment_iterator(NeverIterator(), timeout=True)

    async def test_valid_empty_stream_still_uses_empty_chat_path(self):
        async def empty_stream():
            if False:
                yield None

        with self.assertRaisesRegex(RuntimeError, "CHAT_EMPTY"):
            await self._run_download_with_comment_iterator(empty_stream())

    async def test_comment_api_error_is_not_reported_as_empty_chat(self):
        async def api_failure():
            raise TwitchAPIError("Twitch HTTP error (401)")
            yield None

        with self.assertRaisesRegex(TwitchAPIError, "401"):
            await self._run_download_with_comment_iterator(api_failure())

    async def test_metadata_retry_exhaustion_raises_instead_of_returning_partial_meta(self):
        failing_request = AsyncMock(
            side_effect=TwitchAPIError("upstream failure", status=503, retryable=True)
        )
        with patch.object(client, "gql_post_json", failing_request), \
             patch.object(meta.asyncio, "sleep", AsyncMock()):
            with self.assertRaises(TwitchAPIError):
                await meta.fetch_vod_meta(object(), "client", "123456789")

        self.assertEqual(3, failing_request.await_count)

        malformed_metadata = AsyncMock(return_value={"data": {"video": {"lengthSeconds": 42}}})
        with patch.object(client, "gql_post_json", malformed_metadata):
            with self.assertRaises(TwitchAPIError):
                await meta.fetch_vod_meta(object(), "client", "123456789")

    async def test_valid_missing_vod_is_preserved_and_thumbnail_failure_is_optional(self):
        with patch.object(client, "gql_post_json", AsyncMock(return_value={"data": {"video": None}})):
            missing = await meta.fetch_vod_meta(object(), "client", "missing")
        self.assertIsNone(missing.length_seconds)

        session = SimpleNamespace(get=Mock(side_effect=aiohttp.ClientError("thumbnail unavailable")))
        payload = {"data": {"video": {
            "title": "VOD", "lengthSeconds": 42, "createdAt": "date", "owner": {"id": "1"},
        }}}
        with patch.object(client, "gql_post_json", AsyncMock(return_value=payload)), \
             patch.object(meta, "get_api_headers", return_value={"Authorization": "Bearer test"}):
            result = await meta.fetch_vod_meta(session, "client", "123456789")

        self.assertEqual("VOD", result.title)
        self.assertIsNone(result.thumbnail_preview_url)
        self.assertEqual(10, session.get.call_args.kwargs["timeout"])

    async def test_badges_http_timeout_and_json_errors_are_optional_and_keys_stay_compatible(self):
        with patch.object(badges, "get_api_headers", return_value={}):
            unauthorized = SimpleNamespace(get=Mock(return_value=_ResponseContext(_Response(status=401, data={"error": "Unauthorized"}))))
            self.assertEqual({}, await badges.fetch_twitch_global_badge_map(unauthorized))

            invalid_json = SimpleNamespace(get=Mock(return_value=_ResponseContext(_Response(json_error=ValueError("bad json")))))
            self.assertEqual({}, await badges.fetch_twitch_global_badge_map(invalid_json))

            malformed = SimpleNamespace(get=Mock(return_value=_ResponseContext(_Response(data={"data": "not-a-list"}))))
            self.assertEqual({}, await badges.fetch_twitch_global_badge_map(malformed))

            timed_out = SimpleNamespace(get=Mock(side_effect=asyncio.TimeoutError()))
            self.assertEqual({}, await badges.fetch_twitch_global_badge_map(timed_out))
            self.assertEqual(10, timed_out.get.call_args.kwargs["timeout"])

            valid = SimpleNamespace(get=Mock(return_value=_ResponseContext(_Response(data={"data": [
                {"set_id": "subscriber", "versions": [{"id": "1", "image_url_2x": "https://img.invalid/badge.png"}]},
            ]}))))
            self.assertEqual(
                {"subscriber/1": "https://img.invalid/badge.png"},
                await badges.fetch_twitch_global_badge_map(valid),
            )

    async def test_7tv_bttv_and_ffz_failures_are_isolated_and_partial_maps_survive(self):
        failed = SimpleNamespace(get=Mock(side_effect=aiohttp.ClientError("offline")))
        self.assertEqual({}, await emotes.fetch_7tv_emote_map(failed, "channel"))
        self.assertEqual({}, await emotes.fetch_bttv_emote_map(failed, "channel"))
        self.assertEqual({}, await emotes.fetch_ffz_emote_map(failed, "channel"))

        bttv_session = SimpleNamespace(get=Mock(side_effect=[
            _ResponseContext(_Response(data=[{"code": "Global", "id": "g1"}, {"code": None, "id": "bad"}])),
            aiohttp.ClientError("channel endpoint offline"),
        ]))
        bttv_map = await emotes.fetch_bttv_emote_map(bttv_session, "channel")
        self.assertIn("Global", bttv_map)
        self.assertEqual(2, bttv_session.get.call_count)
        self.assertTrue(all(call.kwargs["timeout"] == 10 for call in bttv_session.get.call_args_list))

        bttv_after_global_failure = SimpleNamespace(get=Mock(side_effect=[
            aiohttp.ClientError("global endpoint offline"),
            _ResponseContext(_Response(data={"channelEmotes": [{"code": "Channel", "id": "c1"}]})),
        ]))
        self.assertIn("Channel", await emotes.fetch_bttv_emote_map(bttv_after_global_failure, "channel"))

        ffz_global = {"sets": {"1": {"emoticons": [
            {"name": "GlobalFFZ", "urls": {"1": "//cdn.invalid/1"}},
            None,
        ]}}}
        ffz_session = SimpleNamespace(get=Mock(side_effect=[
            _ResponseContext(_Response(data=ffz_global)),
            aiohttp.ClientError("room endpoint offline"),
        ]))
        ffz_map = await emotes.fetch_ffz_emote_map(ffz_session, "channel")
        self.assertEqual("https://cdn.invalid/1", ffz_map["GlobalFFZ"])
        self.assertTrue(all(call.kwargs["timeout"] == 3 for call in ffz_session.get.call_args_list))

        ffz_after_global_failure = SimpleNamespace(get=Mock(side_effect=[
            aiohttp.ClientError("global endpoint offline"),
            _ResponseContext(_Response(data={"sets": {"1": {"emoticons": [
                {"name": "ChannelFFZ", "urls": {"1": "//cdn.invalid/room"}},
            ]}}})),
        ]))
        self.assertIn("ChannelFFZ", await emotes.fetch_ffz_emote_map(ffz_after_global_failure, "channel"))

    async def test_ffz_timeouts_are_bounded_and_partial_emotes_are_preserved(self):
        timed_out = SimpleNamespace(get=Mock(side_effect=[
            asyncio.TimeoutError(),
            asyncio.TimeoutError(),
        ]))
        self.assertEqual({}, await emotes.fetch_ffz_emote_map(timed_out, "channel"))
        self.assertEqual(2, timed_out.get.call_count)
        self.assertTrue(all(call.kwargs["timeout"] == 3 for call in timed_out.get.call_args_list))

        global_data = {"sets": {"1": {"emoticons": [
            {"name": "GlobalFFZ", "urls": {"1": "//cdn.invalid/global"}},
        ]}}}
        channel_timeout = SimpleNamespace(get=Mock(side_effect=[
            _ResponseContext(_Response(data=global_data)),
            asyncio.TimeoutError(),
        ]))
        self.assertEqual(
            {"GlobalFFZ": "https://cdn.invalid/global"},
            await emotes.fetch_ffz_emote_map(channel_timeout, "channel"),
        )

    async def test_malformed_7tv_entry_does_not_discard_valid_entries(self):
        data = {"emote_set": {"emotes": [
            {"name": "Valid", "data": {"host": {"url": "//cdn.invalid", "files": [{"name": "1x.webp"}]}}},
            {"name": "Malformed", "data": []},
        ]}}
        session = SimpleNamespace(get=Mock(return_value=_ResponseContext(_Response(data=data))))

        result = await emotes.fetch_7tv_emote_map(session, "channel")

        self.assertEqual({"Valid": "https://cdn.invalid/1x.webp"}, result)
        self.assertEqual(10, session.get.call_args.kwargs["timeout"])

        twitch_shape = SimpleNamespace(
            get=Mock(return_value=_ResponseContext(_Response(data=["unexpected"]))),
        )
        with patch.object(emotes, "get_api_headers", return_value={}):
            self.assertEqual({}, await emotes.fetch_twitch_global_emote_map(twitch_shape))

    async def test_worker_preserves_emote_merge_precedence_and_badge_slash_keys(self):
        render = Mock(return_value="<html></html>")
        with patch.object(worker_html, "fetch_7tv_emote_map", AsyncMock(return_value={"same": "7tv"})), \
             patch.object(worker_html, "fetch_bttv_emote_map", AsyncMock(return_value={"same": "bttv"})), \
             patch.object(worker_html, "fetch_ffz_emote_map", AsyncMock(return_value={"same": "ffz"})), \
             patch.object(worker_html, "fetch_twitch_global_emote_map", AsyncMock(return_value={"same": "global"})), \
             patch.object(worker_html, "fetch_twitch_channel_emote_maps", AsyncMock(return_value=({"same": "channel"}, {}))), \
             patch.object(worker_html, "fetch_twitch_global_badge_map", AsyncMock(return_value={"shared/1": "global-badge"})), \
             patch.object(worker_html, "fetch_twitch_channel_badge_map", AsyncMock(return_value={"shared/1": "channel-badge"})), \
             patch.object(worker_html, "render_viewer_html", render), \
             patch.object(worker_html, "HTML_PUBLISH_MODE", "public"), \
             patch.object(worker_html, "publish_html", return_value="https://publisher.invalid/chat.html"):
            result = await worker_html.build_html_result(
                None, object(), 1, "html_online",
                meta.VodMeta(vod_id="123456789", channel_id="channel", channel="name", title="title"),
                "vod-url", "base", Path("."), [], None,
            )

        self.assertEqual(([], "https://publisher.invalid/chat.html"), result)
        self.assertEqual("channel", render.call_args.kwargs["cdn_emotes"]["same"])
        self.assertEqual("channel-badge", render.call_args.kwargs["badge_images"]["shared/1"])


if __name__ == "__main__":
    unittest.main()
