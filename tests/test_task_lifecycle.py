import asyncio
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import aiohttp
from telegram.ext import Application

from src.handlers import handlers_vod
from src.handlers.handlers_state import (
    begin_download,
    finish_download,
    is_busy,
    is_cancelled,
    set_cancel,
    set_pending,
)
from src.twitch_api import fetch_comments
from src.workers import download_worker, worker_html


class TaskLifecycleTestCase(unittest.IsolatedAsyncioTestCase):
    def _setupAsyncioRunner(self):
        # The Windows sandbox blocks the proactor loop's local socketpair setup.
        self._asyncioRunner = asyncio.Runner(
            debug=True,
            loop_factory=asyncio.SelectorEventLoop,
        )

    def make_context(self):
        return SimpleNamespace(
            user_data={},
            application=SimpleNamespace(bot_data={"aiohttp": object()}, create_task=Mock()),
            bot=SimpleNamespace(
                edit_message_text=AsyncMock(),
                send_message=AsyncMock(),
            ),
        )

    def make_format_update(self, context):
        message = SimpleNamespace(
            chat_id=10,
            message_id=20,
            edit_text=AsyncMock(),
            reply_text=AsyncMock(),
        )
        query = SimpleNamespace(
            data=handlers_vod.CB_FMT_HTML_ONLINE,
            message=message,
            answer=AsyncMock(),
        )
        update = SimpleNamespace(
            callback_query=query,
            effective_user=SimpleNamespace(id=99),
        )
        set_pending(
            context,
            "https://www.twitch.tv/videos/123456789",
            "123456789",
        )
        return update, query, message

    async def start_format_selection(self, context, update):
        with patch.object(handlers_vod.db, "get_cache", return_value=None):
            with patch.object(handlers_vod, "pending_expired", return_value=False):
                await handlers_vod.vod_format_chosen(update, context)

    async def test_start_message_edit_error_rolls_back_busy_and_keeps_pending(self):
        context = self.make_context()
        update, _query, message = self.make_format_update(context)
        message.edit_text.side_effect = OSError("message edit failed")

        with self.assertRaisesRegex(OSError, "message edit failed"):
            await self.start_format_selection(context, update)

        self.assertFalse(is_busy(context))
        self.assertIsNotNone(context.user_data.get("vod_pending"))
        context.application.create_task.assert_not_called()

    async def test_create_task_error_restores_format_selection_and_state(self):
        context = self.make_context()
        update, _query, message = self.make_format_update(context)

        def fail_create(coro):
            coro.close()
            raise RuntimeError("task creation failed")

        context.application.create_task.side_effect = fail_create
        with self.assertRaisesRegex(RuntimeError, "task creation failed"):
            await self.start_format_selection(context, update)

        self.assertFalse(is_busy(context))
        self.assertIsNotNone(context.user_data.get("vod_pending"))
        self.assertEqual(2, message.edit_text.await_count)
        restored = message.edit_text.await_args_list[-1].kwargs
        self.assertEqual(handlers_vod._FORMAT_PROMPT_TEXT, restored["text"])
        self.assertEqual("Загрузка...", message.edit_text.await_args_list[0].kwargs["text"])

    async def test_successful_start_keeps_operation_busy_and_cancel_button_unchanged(self):
        context = self.make_context()
        update, _query, message = self.make_format_update(context)

        def accept_task(coro):
            coro.close()
            return object()

        context.application.create_task.side_effect = accept_task
        await self.start_format_selection(context, update)

        self.assertTrue(is_busy(context))
        self.assertIsNone(context.user_data.get("vod_pending"))
        start_call = message.edit_text.await_args_list[0].kwargs
        self.assertEqual("Загрузка...", start_call["text"])
        cancel_button = start_call["reply_markup"].inline_keyboard[0][0]
        self.assertEqual(handlers_vod.CB_PENDING_CANCEL, cancel_button.callback_data)
        finish_download(context, context.user_data["vod_active_operation"])

    async def test_runner_cleans_state_after_success_and_error(self):
        for failure in (None, ValueError("worker failed")):
            with self.subTest(failure=failure):
                context = self.make_context()
                operation_id = begin_download(context)
                result = (
                    {"title": "VOD", "channel": "streamer"},
                    {"messages": 1, "unique_users": 1, "parts": 0},
                    [],
                    "https://publisher.invalid/chat.html",
                )
                worker = AsyncMock(return_value=result)
                if failure:
                    worker.side_effect = failure
                with patch.object(handlers_vod, "download_and_send", worker), \
                     patch.object(handlers_vod.db, "upsert_cache", return_value=3), \
                     patch.object(handlers_vod.db, "add_user_history"), \
                     patch.object(handlers_vod, "_edit_progress_message_with_card", new=AsyncMock()):
                    await handlers_vod._runner(
                        context, 10, 20, "vod-url", "123456789", "html_online", 99,
                        operation_id=operation_id,
                    )

                self.assertFalse(is_busy(context))
                self.assertFalse(is_cancelled(context))

    async def test_early_worker_setup_failure_cleans_state_through_runner(self):
        context = self.make_context()
        context.application.bot_data = {}
        operation_id = begin_download(context)

        with patch.object(download_worker, "get_client_id", return_value="test-client-id"):
            await handlers_vod._runner(
                context, 10, 20, "vod-url", "123456789", "html_online", 99,
                operation_id=operation_id,
            )

        self.assertFalse(is_busy(context))
        self.assertFalse(is_cancelled(context))
        context.bot.edit_message_text.assert_awaited()

    async def test_runner_cleans_state_after_user_cancellation(self):
        context = self.make_context()
        operation_id = begin_download(context)
        set_cancel(context, True)
        with patch.object(
            handlers_vod,
            "download_and_send",
            side_effect=RuntimeError("Загрузка была отменена."),
        ):
            await handlers_vod._runner(
                context, 10, 20, "vod-url", "123456789", "html_online", 99,
                operation_id=operation_id,
            )

        self.assertFalse(is_busy(context))
        self.assertFalse(is_cancelled(context))
        context.bot.edit_message_text.assert_awaited_once()

    async def test_worker_clears_state_when_cancelled_during_initial_metadata(self):
        context = self.make_context()
        operation_id = begin_download(context)

        async def fetch_meta(*_args, **_kwargs):
            set_cancel(context, True)
            return SimpleNamespace()

        with patch.object(download_worker, "fetch_vod_meta", side_effect=fetch_meta), \
             patch.object(download_worker, "get_client_id", return_value="test-client-id"):
            with self.assertRaisesRegex(RuntimeError, "Загрузка была отменена"):
                await download_worker.download_and_send(
                    context, 10, 20, "vod-url", "123456789", "html_online", operation_id
                )

        self.assertFalse(is_cancelled(context))
        self.assertTrue(is_busy(context))  # _runner owns and releases the busy flag.
        finish_download(context, operation_id)
        self.assertFalse(is_busy(context))

    async def test_comment_request_observes_cancel_after_network_wait(self):
        context = self.make_context()
        operation_id = begin_download(context)
        request_started = asyncio.Event()
        release_request = asyncio.Event()

        async def blocked_page(**_kwargs):
            request_started.set()
            await release_request.wait()
            return [], None, False

        with patch.object(fetch_comments, "gql_fetch_page", side_effect=blocked_page):
            iterator = fetch_comments.gql_fetch_comments(
                object(), "test-client-id", "123456789",
                cancel_check=lambda: is_cancelled(context, operation_id),
            )
            next_page = asyncio.create_task(iterator.__anext__())
            await request_started.wait()
            set_cancel(context, True)
            release_request.set()
            with self.assertRaisesRegex(RuntimeError, "Загрузка была отменена"):
                await next_page

        finish_download(context, operation_id)

    async def test_retry_sleep_is_interrupted_cooperatively_by_cancel_request(self):
        context = self.make_context()
        operation_id = begin_download(context)
        calls = 0

        async def failing_page(**_kwargs):
            nonlocal calls
            calls += 1
            raise aiohttp.ClientError("temporary")

        async def request_cancel():
            await asyncio.sleep(0.02)
            set_cancel(context, True)

        cancel_task = asyncio.create_task(request_cancel())
        with patch.object(fetch_comments, "gql_fetch_page", side_effect=failing_page), \
             patch.object(fetch_comments, "FETCH_DELAY_BASE", 0.01), \
             patch.object(fetch_comments, "FETCH_DELAY_MAX", 1.0), \
             patch.object(fetch_comments.random, "uniform", return_value=0.0):
            iterator = fetch_comments.gql_fetch_comments(
                object(), "test-client-id", "123456789",
                cancel_check=lambda: is_cancelled(context, operation_id),
            )
            with self.assertRaisesRegex(RuntimeError, "Загрузка была отменена"):
                await iterator.__anext__()
        await cancel_task
        self.assertEqual(1, calls)
        finish_download(context, operation_id)

    async def test_cancel_before_emotes_skips_all_external_metadata_calls(self):
        context = self.make_context()
        operation_id = begin_download(context)
        set_cancel(context, True)
        meta = SimpleNamespace(channel_id="channel-id", channel="channel", title="title")
        emotes = AsyncMock(return_value={})

        with patch.object(worker_html, "fetch_7tv_emote_map", emotes):
            with self.assertRaisesRegex(RuntimeError, "Загрузка была отменена"):
                await worker_html.build_html_result(
                    context, object(), 10, "html_online", meta, "vod-url", "base",
                    Path("."), [], None,
                    cancel_check=lambda: is_cancelled(context, operation_id),
                )
        emotes.assert_not_awaited()
        finish_download(context, operation_id)

    async def test_cancel_after_emote_fetch_prevents_badge_requests(self):
        context = self.make_context()
        operation_id = begin_download(context)

        async def emotes_then_cancel(*_args):
            set_cancel(context, True)
            return {}, {}

        twitch_channel_emotes = AsyncMock(side_effect=emotes_then_cancel)
        global_badges = AsyncMock(return_value={})
        with patch.object(worker_html, "fetch_7tv_emote_map", AsyncMock(return_value={})), \
             patch.object(worker_html, "fetch_bttv_emote_map", AsyncMock(return_value={})), \
             patch.object(worker_html, "fetch_ffz_emote_map", AsyncMock(return_value={})), \
             patch.object(worker_html, "fetch_twitch_global_emote_map", AsyncMock(return_value={})), \
             patch.object(worker_html, "fetch_twitch_channel_emote_maps", twitch_channel_emotes), \
             patch.object(worker_html, "fetch_twitch_global_badge_map", global_badges):
            with self.assertRaisesRegex(RuntimeError, "Загрузка была отменена"):
                await worker_html.build_html_result(
                    context, object(), 10, "html_online",
                    SimpleNamespace(channel_id="channel-id", channel="channel", title="title"),
                    "vod-url", "base", Path("."), [], None,
                    cancel_check=lambda: is_cancelled(context, operation_id),
                )
        global_badges.assert_not_awaited()
        finish_download(context, operation_id)

    async def test_cancel_during_bttv_global_fetch_skips_channel_request(self):
        context = self.make_context()
        operation_id = begin_download(context)

        class Response:
            status = 200

            async def json(self):
                set_cancel(context, True)
                return []

        class ResponseContext:
            async def __aenter__(self):
                return Response()

            async def __aexit__(self, *_args):
                return False

        session = SimpleNamespace(get=Mock(return_value=ResponseContext()))
        with patch.object(worker_html, "fetch_7tv_emote_map", AsyncMock(return_value={})):
            with self.assertRaisesRegex(RuntimeError, "Загрузка была отменена"):
                await worker_html.build_html_result(
                    context, session, 10, "html_online",
                    SimpleNamespace(channel_id="channel-id", channel="channel", title="title"),
                    "vod-url", "base", Path("."), [], None,
                    cancel_check=lambda: is_cancelled(context, operation_id),
                )

        self.assertEqual(1, session.get.call_count)
        finish_download(context, operation_id)

    async def test_new_run_ignores_stale_cancel_and_old_cleanup_cannot_clear_it(self):
        context = self.make_context()
        old_operation = begin_download(context)
        started = asyncio.Event()
        release = asyncio.Event()

        async def old_worker(*_args, **_kwargs):
            started.set()
            await release.wait()
            raise RuntimeError("Загрузка была отменена.")

        with patch.object(handlers_vod, "download_and_send", side_effect=old_worker):
            old_task = asyncio.create_task(
                handlers_vod._runner(
                    context, 10, 20, "vod-url", "123456789", "html_online", 99,
                    operation_id=old_operation,
                )
            )
            await started.wait()
            new_operation = begin_download(context)
            self.assertFalse(is_cancelled(context, new_operation))
            self.assertFalse(finish_download(context, None))
            release.set()
            await old_task

        self.assertTrue(is_busy(context))
        self.assertEqual(new_operation, context.user_data["vod_active_operation"])
        set_cancel(context, True)
        self.assertFalse(is_cancelled(context, old_operation))
        self.assertTrue(is_cancelled(context, new_operation))
        finish_download(context, new_operation)

    async def test_ptb_stop_waits_for_task_before_post_shutdown_closes_session(self):
        app = Application.builder().token("123456:test-token").build()
        app._running = True
        task_started = asyncio.Event()
        release_task = asyncio.Event()
        session = SimpleNamespace(close=AsyncMock())
        app.bot_data["aiohttp"] = session

        async def background_work():
            task_started.set()
            await release_task.wait()

        app.create_task(background_work())
        await task_started.wait()
        stop_task = asyncio.create_task(app.stop())
        await asyncio.sleep(0)
        self.assertFalse(stop_task.done())
        session.close.assert_not_awaited()

        release_task.set()
        await stop_task
        from src.main import post_shutdown
        await post_shutdown(app)
        session.close.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
