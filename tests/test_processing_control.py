import asyncio
import os
import runpy
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from telegram import Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, MessageHandler

from src import config, main
from src.database import database as db, db_core
from src.handlers import handlers_cmd, handlers_vod
from src.handlers.handlers_state import (
    begin_download, clear_pending, finish_download, is_busy, set_pending,
)
from src.history.history_callbacks import (
    history_page_callback, history_vod_callback, ui_buttons,
)
from src.history.history_files import history_files_callback
from src.ui.ui_history import build_history_page
from src.workers import worker_html
from publication_stub import PublicationStub


class HistoryConfigurationTestCase(unittest.TestCase):
    def test_history_defaults_off_and_can_be_enabled(self):
        config_file = Path(__file__).resolve().parents[1] / "src" / "config.py"
        for value, expected in ((None, False), ("false", False), ("true", True), (" TRUE ", True)):
            with self.subTest(value=value), patch.dict(os.environ):
                os.environ.pop("TCD_HISTORY_ENABLED", None)
                if value is not None:
                    os.environ["TCD_HISTORY_ENABLED"] = value
                self.assertIs(expected, runpy.run_path(str(config_file))["HISTORY_ENABLED"])


class OperationOwnershipTestCase(unittest.TestCase):
    def test_foreign_user_state_cannot_release_slot_even_with_matching_token(self):
        application = SimpleNamespace(bot_data={})
        owner = SimpleNamespace(application=application, user_data={})
        foreign = SimpleNamespace(application=application, user_data={})
        operation = begin_download(owner)

        self.assertFalse(finish_download(foreign, operation))
        self.assertEqual(operation, application.bot_data["vod_global_operation"])
        self.assertTrue(is_busy(owner))
        self.assertIsNone(begin_download(foreign))

        self.assertTrue(finish_download(owner, operation))
        self.assertIsNotNone(next_operation := begin_download(foreign))
        finish_download(foreign, next_operation)


class ProcessingControlTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        registry=patch.object(handlers_vod.results,"Publication",PublicationStub)
        registry.start()
        self.addCleanup(registry.stop)
        temp = tempfile.TemporaryDirectory(prefix="tdb-control-", dir=Path(__file__).parent)
        self.addCleanup(temp.cleanup)
        self.temp_dir = Path(temp.name)
        db_patch = patch.object(db_core, "DB_PATH", str(self.temp_dir / "test.sqlite3"))
        db_patch.start()
        self.addCleanup(db_patch.stop)
        history_patch = patch.object(config, "HISTORY_ENABLED", False)
        history_patch.start()
        self.addCleanup(history_patch.stop)
        db.init_db()
        self.application = SimpleNamespace(bot_data={"aiohttp": object()})
        self.tasks = []

        def create_task(coro):
            task = asyncio.create_task(coro)
            self.tasks.append(task)
            return task

        self.application.create_task = Mock(side_effect=create_task)

    async def asyncTearDown(self):
        for task in self.tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)

    def request(self, user_id=1, vod_id="123456789"):
        context = SimpleNamespace(
            application=self.application, user_data={},
            bot=SimpleNamespace(edit_message_text=AsyncMock(), send_message=AsyncMock()),
        )
        message = SimpleNamespace(
            chat_id=user_id, message_id=20, edit_text=AsyncMock(), reply_text=AsyncMock(),
            delete=AsyncMock(), text=f"https://www.twitch.tv/videos/{vod_id}",
        )
        query = SimpleNamespace(
            data=handlers_vod.CB_FMT_HTML_ONLINE, message=message, answer=AsyncMock(),
        )
        update = SimpleNamespace(
            callback_query=query, message=message, effective_message=message,
            effective_chat=SimpleNamespace(id=user_id), effective_user=SimpleNamespace(id=user_id),
        )
        set_pending(context, message.text, vod_id)
        return context, update

    def result(self):
        return (
            {"title": "VOD", "channel": "streamer", "length_seconds": 10},
            {"messages": 1, "unique_users": 1, "parts": 0}, [],
            "local:chat_0123456789abcdef0123456789abcdef.html",
        )

    async def finish_tasks(self):
        if self.tasks:
            await asyncio.gather(*self.tasks)
        await asyncio.sleep(0)  # Run task completion callbacks as PTB does.

    async def test_two_users_cannot_start_together_and_next_can_start_after_success(self):
        context1, update1 = self.request(1)
        context2, update2 = self.request(2, "234567890")
        release = asyncio.Event()
        entered = asyncio.Event()

        async def worker(**kwargs):
            entered.set()
            await release.wait()
            return self.result()

        with patch.object(handlers_vod, "download_and_send", side_effect=worker) as download:
            await asyncio.gather(
                handlers_vod.vod_format_chosen(update1, context1),
                handlers_vod.vod_format_chosen(update2, context2),
            )
            await entered.wait()
            self.assertEqual(1, download.await_count)
            self.assertTrue(is_busy(context1))
            self.assertFalse(is_busy(context2))
            self.assertIn("Сейчас обрабатывается чат VOD", update2.message.reply_text.await_args.args[0])
            self.assertIsNotNone(context2.user_data.get("vod_pending"))
            release.set()
            await self.finish_tasks()
            await handlers_vod.vod_format_chosen(update2, context2)
            await self.finish_tasks()
            self.assertEqual(2, download.await_count)
        self.assertFalse(is_busy(context1))
        self.assertFalse(is_busy(context2))
        self.assertIsNotNone(db.get_cache("123456789", "html_online"))
        self.assertIsNotNone(db.get_cache("234567890", "html_online"))

    async def test_reservation_precedes_start_message_wait(self):
        first, update1 = self.request(1)
        second, update2 = self.request(2)
        entered = asyncio.Event()
        release = asyncio.Event()

        async def edit(**kwargs):
            entered.set()
            await release.wait()

        update1.message.edit_text.side_effect = edit
        with patch.object(handlers_vod, "download_and_send", AsyncMock(return_value=self.result())):
            start = asyncio.create_task(handlers_vod.vod_format_chosen(update1, first))
            try:
                await entered.wait()
                await handlers_vod.vod_format_chosen(update2, second)
                self.assertIn("Сейчас обрабатывается чат VOD", update2.message.reply_text.await_args.args[0])
                self.application.create_task.assert_not_called()
            finally:
                release.set()
                await start
            await self.finish_tasks()

    async def test_start_failures_release_global_slot(self):
        for stage in ("edit", "create"):
            with self.subTest(stage=stage):
                context, update = self.request()
                if stage == "edit":
                    update.message.edit_text.side_effect = OSError("edit failed")
                    error = OSError
                else:
                    self.application.create_task.side_effect = RuntimeError("create failed")
                    error = RuntimeError
                with self.assertRaises(error):
                    await handlers_vod.vod_format_chosen(update, context)
                self.assertFalse(is_busy(context))
                other, _ = self.request(2)
                operation = begin_download(other)
                self.assertIsNotNone(operation)
                finish_download(other, operation)

    async def test_error_and_cancel_release_slot_and_allow_retry(self):
        for error in (ValueError("failed"), RuntimeError("Загрузка была отменена."), asyncio.CancelledError()):
            with self.subTest(error=type(error).__name__):
                context, _ = self.request()
                operation = begin_download(context)
                with patch.object(handlers_vod, "download_and_send", AsyncMock(side_effect=error)):
                    task = asyncio.create_task(handlers_vod._runner(
                        context, 1, 20, "vod", "123456789", "html_online", 1, operation,
                    ))
                    await asyncio.gather(task, return_exceptions=True)
                self.assertFalse(is_busy(context))
                self.assertIsNotNone(new_operation := begin_download(context))
                finish_download(context, new_operation)

    async def test_old_task_and_done_callback_cannot_release_new_owner(self):
        context, update = self.request()
        release = asyncio.Event()
        entered = asyncio.Event()

        async def worker(**kwargs):
            entered.set()
            await release.wait()
            raise RuntimeError("Загрузка была отменена.")

        with patch.object(handlers_vod, "download_and_send", side_effect=worker):
            await handlers_vod.vod_format_chosen(update, context)
            await entered.wait()
            old_operation = context.user_data["vod_active_operation"]
            self.assertIsNone(begin_download(context))  # Cannot overwrite a live owner.
            finish_download(context, old_operation)
            other, _ = self.request(2)
            new_operation = begin_download(other)
            release.set()
            await self.finish_tasks()
            self.assertTrue(is_busy(other))
            self.assertEqual(new_operation, self.application.bot_data["vod_global_operation"])
            self.assertFalse(finish_download(context, old_operation))
            finish_download(other, new_operation)

    async def test_cancel_button_keeps_slot_until_worker_exits_and_allows_restart(self):
        context, update = self.request()
        entered = asyncio.Event()
        release = asyncio.Event()

        async def worker(**kwargs):
            entered.set()
            await release.wait()
            from src.handlers.handlers_state import raise_if_cancelled
            raise_if_cancelled(context, kwargs["operation_id"])
            return self.result()

        with patch.object(handlers_vod, "download_and_send", side_effect=worker):
            await handlers_vod.vod_format_chosen(update, context)
            await entered.wait()
            await handlers_vod.pending_cancel_callback(update, context)
            other, _ = self.request(2)
            self.assertTrue(is_busy(context))
            self.assertIsNone(begin_download(other))
            release.set()
            await self.finish_tasks()
        self.assertFalse(is_busy(context))
        self.assertNotIn("vod_cancel", context.user_data)
        set_pending(context, update.message.text, "123456789")
        with patch.object(handlers_vod, "download_and_send", AsyncMock(return_value=self.result())):
            await handlers_vod.vod_format_chosen(update, context)
            await self.finish_tasks()
        self.assertIsNotNone(db.get_cache("123456789", "html_online"))
        self.assertFalse(is_busy(context))

    async def test_task_cancelled_before_runner_starts_releases_reservation(self):
        context, update = self.request()

        def cancel_before_start(coro):
            task = asyncio.create_task(coro)
            task.cancel()
            self.tasks.append(task)
            return task

        self.application.create_task.side_effect = cancel_before_start
        await handlers_vod.vod_format_chosen(update, context)
        await asyncio.gather(*self.tasks, return_exceptions=True)
        await asyncio.sleep(0)
        self.assertFalse(is_busy(context))
        other, _ = self.request(2)
        self.assertIsNotNone(operation := begin_download(other))
        finish_download(other, operation)

    async def test_cache_hit_remains_available_while_busy_without_history_writes(self):
        from src import html_publisher

        with patch.object(html_publisher, "OUT_DIR", str(self.temp_dir)):
            ref = html_publisher.publish_html_local("<html>viewer</html>")
            cache_id = db.upsert_cache(
                "123456789", "html_online", "https://www.twitch.tv/videos/123456789",
                {"title": "cached", "html_url": ref}, {"messages": 1}, [],
            )
            db.add_user_history(2, cache_id)  # Existing history must survive.
            active, _ = self.request(1, "234567890")
            operation = begin_download(active)
            context, update = self.request(2)
            with patch.object(handlers_vod, "HTML_PUBLISH_MODE", "local"), \
                 patch.object(handlers_vod.db, "add_user_history", wraps=db.add_user_history) as history:
                await handlers_vod.vod_format_chosen(update, context)
                history.assert_not_called()
            self.application.create_task.assert_not_called()
            context.bot.edit_message_text.assert_awaited_once()
            self.assertTrue(is_busy(active))
            self.assertEqual(cache_id, db.get_cache("123456789", "html_online")["id"])
            self.assertEqual(1, db.get_history_count(2))
            finish_download(active, operation)

    async def test_completion_records_cache_but_history_only_when_enabled(self):
        for enabled in (False, True):
            with self.subTest(enabled=enabled), patch.object(config, "HISTORY_ENABLED", enabled):
                context, update = self.request(1 if not enabled else 2)
                with patch.object(handlers_vod, "download_and_send", AsyncMock(return_value=self.result())):
                    await handlers_vod.vod_format_chosen(update, context)
                    await self.finish_tasks()
                self.assertIsNotNone(db.get_cache("123456789", "html_online"))
                self.assertEqual(int(enabled), db.get_history_count(update.effective_user.id))

    async def test_enabled_cache_hit_records_history(self):
        cache_id = db.upsert_cache(
            "123456789", "html_online", "vod", {"title": "cached"}, {}, [],
        )
        context, update = self.request()
        with patch.object(config, "HISTORY_ENABLED", True), \
             patch.object(handlers_vod.results,"get_ready_result",return_value={
                 'vod_id':'123456789','cache_id':cache_id,'meta':{'title':'cached'},'stats':{},'html_ref':None,
             }):
            await handlers_vod.vod_format_chosen(update, context)
        self.assertEqual(1, db.get_history_count(1))
        self.assertEqual(cache_id, db.get_history_for_user(1)[0]["id"])

    async def test_disabled_history_command_and_all_old_callbacks_do_not_read_database(self):
        context, update = self.request()
        with patch.object(db, "get_history_count", side_effect=AssertionError("history read")), \
             patch.object(db, "get_history_entry_for_user", side_effect=AssertionError("history read")):
            await main.history_command(update, context)
            update.message.reply_text.assert_awaited_once_with("История отключена.")
            for callback, data in (
                (ui_buttons, "ui:history"), (history_page_callback, "ui:histpage:0"),
                (history_vod_callback, "ui:histvod:h1"), (history_vod_callback, "ui:histvod:0"),
                (history_files_callback, "ui:histfiles:h1"), (history_files_callback, "ui:histfiles:0"),
            ):
                update.callback_query.data = data
                await callback(update, context)
                update.callback_query.answer.assert_awaited_with("История отключена.")
        context.bot.send_message.assert_not_awaited()

    async def test_reenabled_history_command_opens_existing_history(self):
        cache_id = db.upsert_cache("123456789", "html_online", "vod", {"title": "old"}, {}, [])
        db.add_user_history(1, cache_id)
        context, update = self.request()
        await main.history_command(update, context)
        self.assertEqual(1, db.get_history_count(1))
        with patch.object(config, "HISTORY_ENABLED", True):
            await main.history_command(update, context)
        context.bot.send_message.assert_awaited_once()
        self.assertIn("old", context.bot.send_message.await_args.kwargs["text"])

    async def test_command_menu_respects_history_setting(self):
        for enabled in (False, True):
            app = SimpleNamespace(bot_data={}, bot=SimpleNamespace(set_my_commands=AsyncMock()))
            session = SimpleNamespace(close=AsyncMock())
            with patch.object(config, "HISTORY_ENABLED", enabled), \
                 patch.object(main.aiohttp, "ClientSession", return_value=session), \
                 patch.object(main, "start_cleanup", AsyncMock()), \
                 patch.object(main, "cleanup_stale_html_temp_files", return_value=0):
                await main.post_init(app)
                commands = [command.command for command in app.bot.set_my_commands.await_args.args[0]]
                expected = ['help']
                self.assertEqual(expected + (['history'] if enabled else []), commands)
                self.assertNotIn('cancel', commands)
                expected_descriptions = ['Справка']
                self.assertEqual(expected_descriptions + (['История'] if enabled else []),
                                 [command.description for command in app.bot.set_my_commands.await_args.args[0]])
                await main.post_shutdown(app)
            session.close.assert_awaited_once()

    async def test_history_buttons_hidden_without_changing_result_card(self):
        cards, _ = build_history_page([{"fmt": "files", "vod_url": "https://www.twitch.tv/videos/1"}], 0, 1)
        buttons = [button for row in cards[0][1].inline_keyboard for button in row]
        self.assertTrue(buttons)
        self.assertTrue(all(button.callback_data is None for button in buttons))
        with patch.object(config, "HISTORY_ENABLED", True):
            enabled_cards, _ = build_history_page([{"fmt": "files", "vod_url": "https://www.twitch.tv/videos/1"}], 0, 1)
        self.assertEqual(cards[0][0], enabled_cards[0][0])
        self.assertEqual("ui:histfiles:0", enabled_cards[0][1].inline_keyboard[0][0].callback_data)

    async def test_metadata_wait_does_not_reserve_global_slot_or_overwrite_same_user_dialog(self):
        context, update = self.request()
        clear_pending(context)
        entered = asyncio.Event()
        release = asyncio.Event()

        async def metadata(*args):
            entered.set()
            await release.wait()
            return SimpleNamespace(length_seconds=10)

        with patch.object(handlers_vod, "get_client_id", return_value="test"), \
             patch.object(handlers_vod, "fetch_vod_meta", side_effect=metadata) as fetch:
            task = asyncio.create_task(handlers_vod.vod_link_entry(update, context))
            try:
                await entered.wait()
                await handlers_cmd.about_command(update, context)
                await handlers_vod.vod_link_entry(update, context)
                self.assertEqual(1, fetch.await_count)
                other, _ = self.request(2)
                self.assertIsNotNone(operation := begin_download(other))
                finish_download(other, operation)
                await handlers_cmd.cancel_command(update, context)
            finally:
                release.set()
                await task
        self.assertIsNone(context.user_data.get("vod_pending"))
        self.assertNotIn("vod_link_lookup", context.user_data)
        self.assertFalse(any(call.args and call.args[0] == handlers_vod._FORMAT_PROMPT_TEXT
                             for call in update.message.reply_text.await_args_list))

    async def test_old_metadata_completion_does_not_clear_new_lookup(self):
        context, update = self.request()
        entered = asyncio.Event()
        release = asyncio.Event()

        async def metadata(*args):
            entered.set()
            await release.wait()
            return SimpleNamespace(length_seconds=10)

        with patch.object(handlers_vod, "get_client_id", return_value="test"), \
             patch.object(handlers_vod, "fetch_vod_meta", side_effect=metadata):
            task = asyncio.create_task(handlers_vod.vod_link_entry(update, context))
            try:
                await entered.wait()
                clear_pending(context)
                newer = object()
                context.user_data["vod_link_lookup"] = newer
            finally:
                release.set()
                await task
        self.assertIs(newer, context.user_data["vod_link_lookup"])
        self.assertIsNone(context.user_data.get("vod_pending"))

    async def test_metadata_failure_does_not_leave_lookup_busy(self):
        context, update = self.request()
        with patch.object(handlers_vod, "get_client_id", return_value="test"), \
             patch.object(handlers_vod, "fetch_vod_meta", AsyncMock(side_effect=ValueError("metadata failed"))):
            with self.assertRaisesRegex(ValueError, "metadata failed"):
                await handlers_vod.vod_link_entry(update, context)
        self.assertNotIn("vod_link_lookup", context.user_data)
        self.assertFalse(is_busy(context))
        self.assertIsNotNone(operation := begin_download(context))
        finish_download(context, operation)

    async def test_metadata_and_channel_browsing_are_nonblocking_in_real_application_registration(self):
        app = Application.builder().token("123456:test-token").build()
        with patch.object(main.Application, "builder") as builder, \
             patch.object(main, "get_telegram_bot_token", return_value="test"), \
             patch.object(main, "validate_credentials"), \
             patch.object(main, "setup_logging"), \
             patch.object(Application, "run_polling"):
            builder.return_value.token.return_value.post_init.return_value.post_stop.return_value.post_shutdown.return_value.build.return_value = app
            main.main()
        links = [h for group in app.handlers.values() for h in group if isinstance(h, MessageHandler)]
        callbacks = [h for group in app.handlers.values() for h in group if isinstance(h, CallbackQueryHandler)]
        self.assertEqual(1, len(links))
        self.assertFalse(links[0].block)
        from src.handlers.handlers_input import text_input, vod_command, channel_command
        self.assertIs(text_input, links[0].callback)
        commands = [h for group in app.handlers.values() for h in group if isinstance(h, CommandHandler)]
        self.assertTrue({'start', 'help', 'vod', 'channel', 'cancel', 'about', 'history'}
                        <= {name for handler in commands for name in handler.commands})
        for name, callback in (('vod', vod_command), ('channel', channel_command)):
            handler = next(h for h in commands if name in h.commands)
            self.assertIs(callback, handler.callback)
            self.assertFalse(handler.block)
            for suffix in ('', '@TDBBot'):
                for trailing in ('', '   '):
                    with self.subTest(command=name+suffix, trailing=bool(trailing)):
                        command = '/'+name+suffix
                        update = Update.de_json({'update_id': 1, 'message': {
                            'message_id': 1, 'date': 0, 'chat': {'id': 10, 'type': 'private'},
                            'text': command+trailing,
                            'entities': [{'type': 'bot_command', 'offset': 0, 'length': len(command)}],
                        }}, SimpleNamespace(username='TDBBot'))
                        result = handler.check_update(update)
                        self.assertIsInstance(result, tuple)
                        self.assertEqual([], result[0])
                        self.assertFalse(links[0].check_update(update))
        channel = [h for h in callbacks if h.pattern.pattern == '^ui:cv:']
        self.assertEqual(1, len(channel))
        self.assertFalse(channel[0].block)
        self.assertTrue(all(h.block for h in callbacks if h is not channel[0]))
        self.assertEqual(1, app.update_processor.max_concurrent_updates)

    async def test_html_write_does_not_block_commands_and_task_cancel_keeps_slot_until_finished(self):
        context, _ = self.request()
        operation = begin_download(context)
        started = threading.Event()
        release = threading.Event()
        main_thread = threading.get_ident()
        thread_ids = []

        def publish(html):
            thread_ids.append(threading.get_ident())
            started.set()
            if not release.wait(5):
                raise AssertionError("test did not release publisher")
            return "local:chat_0123456789abcdef0123456789abcdef.html"

        async def worker(**kwargs):
            return {}, {}, [], (await worker_html.build_html_result(
                context, object(), 1, "html_online",
                SimpleNamespace(channel_id=None, channel=None, title="test", created_at=None,
                                thumbnail_preview_url=None, thumbnail_full_url=None),
                "vod", "unused", self.temp_dir, [], None,
            ))[1]

        with patch.object(worker_html, "HTML_PUBLISH_MODE", "local"), \
             patch.object(worker_html, "fetch_twitch_global_emote_map", AsyncMock(return_value={})), \
             patch.object(worker_html, "fetch_twitch_global_badge_map", AsyncMock(return_value={})), \
             patch.object(worker_html, "render_viewer_html", return_value="html"), \
             patch.object(worker_html, "publish_html_local", side_effect=publish), \
             patch.object(handlers_vod, "download_and_send", side_effect=worker):
            task = asyncio.create_task(handlers_vod._runner(context, 1, 20, "vod", "1", "html_online", 1, operation))
            try:
                for _ in range(500):
                    if started.is_set():
                        break
                    await asyncio.sleep(0.001)
                self.assertTrue(started.is_set())
                other, update = self.request(2)
                await handlers_cmd.about_command(update, other)
                update.message.reply_text.assert_awaited_once()
                task.cancel()
                await asyncio.sleep(0.01)
                self.assertFalse(task.done())
                self.assertIsNone(begin_download(other))
                task.cancel()  # A repeated cancellation must not detach the thread.
                await asyncio.sleep(0.01)
                self.assertFalse(task.done())
            finally:
                release.set()
                await asyncio.gather(task, return_exceptions=True)
        self.assertNotEqual(main_thread, thread_ids[0])
        self.assertFalse(is_busy(context))
        self.assertIsNotNone(operation := begin_download(other))
        finish_download(other, operation)

    async def test_render_and_publication_thread_errors_release_slot_without_cache(self):
        for failure_stage in ("render", "publish"):
            with self.subTest(stage=failure_stage):
                context, _ = self.request()
                operation = begin_download(context)

                async def worker(**kwargs):
                    files, ref = await worker_html.build_html_result(
                        context, object(), 1, "html_online",
                        SimpleNamespace(channel_id=None, channel=None, title="test", created_at=None,
                                        thumbnail_preview_url=None, thumbnail_full_url=None),
                        "vod", "unused", self.temp_dir, [], None,
                    )
                    return {}, {}, files, ref

                render = Mock(return_value="html")
                publish = Mock(return_value="local:unused.html")
                (render if failure_stage == "render" else publish).side_effect = OSError("test failure")
                with patch.object(worker_html, "HTML_PUBLISH_MODE", "local"), \
                     patch.object(worker_html, "fetch_twitch_global_emote_map", AsyncMock(return_value={})), \
                     patch.object(worker_html, "fetch_twitch_global_badge_map", AsyncMock(return_value={})), \
                     patch.object(worker_html, "render_viewer_html", render), \
                     patch.object(worker_html, "publish_html_local", publish), \
                     patch.object(handlers_vod, "download_and_send", side_effect=worker), \
                     self.assertLogs(level="ERROR"):
                    await handlers_vod._runner(context, 1, 20, "vod", "1", "html_online", 1, operation)
                self.assertFalse(is_busy(context))
                self.assertIsNone(db.get_cache("1", "html_online"))
                if failure_stage == "render":
                    publish.assert_not_called()
                other, _ = self.request(2)
                self.assertIsNotNone(next_operation := begin_download(other))
                finish_download(other, next_operation)


if __name__ == "__main__":
    unittest.main()
