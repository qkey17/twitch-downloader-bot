import asyncio
import errno
import json
import sqlite3
import tempfile
import threading
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from src import config, html_publisher
from src.chat_spool import ChatSpool
from src.database import database as db, db_core
from src.handlers import handlers_vod
from src.handlers.handlers_state import begin_download, finish_download, is_busy, set_cancel
from src.twitch_api import fetch_comments
from src.twitch_api.client import TwitchAPIError
from src.workers import download_worker, worker_html


class DiskPipelineTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="tdb-disk-", dir=Path(__file__).parent)
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        metadata = SimpleNamespace(title="test", channel="channel", length_seconds=30,
                                   channel_id="123", created_at=None, thumbnail_preview_url=None,
                                   thumbnail_full_url=None)
        for module, name, value in (
            (db_core, "DB_PATH", str(self.root / "history.sqlite3")),
            (config, "HISTORY_ENABLED", False),
            (config, "SPOOL_DIR", str(self.root / "spools")),
            (download_worker, "OUT_DIR", str(self.root / "out")),
            (download_worker, "SPOOL_DIR", str(self.root / "spools")),
            (download_worker, "SPOOL_BATCH_MESSAGES", 3),
            (download_worker, "SPOOL_BATCH_BYTES", 100000),
            (download_worker, "get_client_id", lambda: "test-client"),
            (download_worker, "fetch_vod_meta", AsyncMock(return_value=metadata)),
            (download_worker, "make_progress_updater", lambda *args: AsyncMock()),
            (worker_html, "HTML_PUBLISH_MODE", "local"),
            (html_publisher, "OUT_DIR", str(self.root / "out")),
            (worker_html, "render_viewer_html", lambda **kwargs: self.fail("legacy full HTML used")),
        ):
            self.stack.enter_context(patch.object(module, name, value))
        for name in ("fetch_7tv_emote_map", "fetch_bttv_emote_map", "fetch_ffz_emote_map",
                     "fetch_twitch_global_emote_map", "fetch_twitch_global_badge_map",
                     "fetch_twitch_channel_badge_map"):
            self.stack.enter_context(patch.object(worker_html, name, AsyncMock(return_value={})))
        self.stack.enter_context(patch.object(worker_html, "fetch_twitch_channel_emote_maps",
                                            AsyncMock(return_value=({}, {}))))
        db.init_db()
        self.context = SimpleNamespace(
            application=SimpleNamespace(bot_data={"aiohttp": object()}), user_data={},
            bot=SimpleNamespace(edit_message_text=AsyncMock(), send_message=AsyncMock()),
        )

    async def source(self, count):
        for i in range(count):
            yield (1, "", "User" if i % 2 else "user", f"{i} </SCRIPT> 🎮",
                   [{"text": "Kappa", "emoteID": "25"}], "#abcdef", [{"set_id": "subscriber", "version": "1"}])

    async def run_chat(self, source):
        operation = begin_download(self.context)
        with patch.object(download_worker, "gql_fetch_comments", return_value=source):
            await handlers_vod._runner(self.context, 1, 2, "https://www.twitch.tv/videos/123456789",
                                       "123456789", "html_online", 1, operation)
        self.assertFalse(is_busy(self.context))

    def assert_clean(self):
        self.assertFalse(tuple((self.root / "spools").glob("tdb-spool-*")))
        self.assertFalse(tuple((self.root / "out").glob("*.tmp")))

    def payload(self):
        cache = db.get_cache("123456789", "html_online")
        path = self.root / "out" / cache["html_url"].removeprefix("local:")
        html = path.read_text(encoding="utf-8")
        data = html.split('<script id="chat-data" type="application/json">', 1)[1].split('</script>', 1)[0]
        return json.loads(data), cache

    async def test_one_row_and_multiple_batches_preserve_all_fields_and_stats(self):
        for count in (1, 3, 4, 10):
            with self.subTest(count=count):
                await self.run_chat(self.source(count))
                payload, cache = self.payload()
                self.assertEqual(count, len(payload["rows"]))
                self.assertEqual([f"{i} </SCRIPT> 🎮" for i in range(count)], [r["text"] for r in payload["rows"]])
                self.assertEqual("00:00:01", payload["rows"][0]["t"])
                self.assertEqual({"t", "user", "text", "fragments", "color", "badges"}, set(payload["rows"][0]))
                self.assertEqual(count, cache["stats"]["messages"])
                self.assertEqual(min(count, 2), cache["stats"]["unique_users"])
                self.assertEqual(0, db.get_history_count(1))
                self.assert_clean()

    async def test_empty_chat_has_no_ready_cache_or_html(self):
        await self.run_chat(self.source(0))
        self.assertIsNone(db.get_cache("123456789", "html_online"))
        self.assertFalse(tuple((self.root / "out").glob("*.html")))
        self.assert_clean()

    async def test_producer_flushes_before_exhaustion_and_respects_row_and_byte_limits(self):
        received = 0
        batch_sizes = []
        original = ChatSpool.append_batch

        async def source():
            nonlocal received
            async for comment in self.source(12):
                received += 1
                yield comment

        def append(spool, batch, cancel_check=None):
            if not batch_sizes:
                self.assertLess(received, 12)
            self.assertLessEqual(len(batch), 3)
            batch_sizes.append(sum(len(payload.encode("utf-8")) for _, payload in batch))
            return original(spool, batch, cancel_check)

        with patch.object(ChatSpool, "append_batch", append), \
             patch.object(download_worker, "SPOOL_BATCH_BYTES", 512):
            await self.run_chat(source())
        self.assertTrue(all(size <= 512 for size in batch_sizes))
        self.assertGreater(len(batch_sizes), 1)
        self.assert_clean()

    async def test_spool_path_preserves_emote_source_precedence_and_badge_keys(self):
        with patch.object(worker_html, "fetch_7tv_emote_map", AsyncMock(return_value={"Kappa": "7tv"})), \
             patch.object(worker_html, "fetch_bttv_emote_map", AsyncMock(return_value={"Kappa": "bttv"})), \
             patch.object(worker_html, "fetch_ffz_emote_map", AsyncMock(return_value={"Kappa": "ffz"})), \
             patch.object(worker_html, "fetch_twitch_global_emote_map", AsyncMock(return_value={"Kappa": "global"})), \
             patch.object(worker_html, "fetch_twitch_channel_emote_maps", AsyncMock(return_value=({"Kappa": "channel"}, {"25": "by-id"}))), \
             patch.object(worker_html, "fetch_twitch_global_badge_map", AsyncMock(return_value={"subscriber/1": "global"})), \
             patch.object(worker_html, "fetch_twitch_channel_badge_map", AsyncMock(return_value={"subscriber/1": "channel"})):
            await self.run_chat(self.source(1))
        payload, _ = self.payload()
        self.assertEqual({"Kappa": "channel", "25": "by-id"}, payload["meta"]["cdn_emotes"])
        self.assertEqual({"subscriber/1": "channel"}, payload["meta"]["badge_images"])

    async def test_api_failure_and_timeout_never_publish_partial_chat(self):
        for error in (TwitchAPIError("test API error"), asyncio.TimeoutError()):
            async def source():
                async for comment in self.source(4):
                    yield comment
                raise error

            with self.subTest(error=type(error).__name__):
                try:
                    await self.run_chat(source())
                except TwitchAPIError:
                    pass
                self.assertFalse(is_busy(self.context))
                self.assertIsNone(db.get_cache("123456789", "html_online"))
                self.assert_clean()

    async def test_unserializable_incoming_row_cleans_partial_spool(self):
        async def source():
            async for comment in self.source(4):
                yield comment
            yield (1, "", "user", "text", [{"bad": object()}], "#abcdef", [])

        await self.run_chat(source())
        self.assertIsNone(db.get_cache("123456789", "html_online"))
        self.assert_clean()

    async def test_oversized_single_row_is_written_without_losing_data(self):
        text = "длинная строка 🎮 " * 1000

        async def source():
            yield (1, "", "user", text, [{"text": text}], None, [])

        with patch.object(download_worker, "SPOOL_BATCH_BYTES", 64):
            await self.run_chat(source())
        payload, _ = self.payload()
        self.assertEqual(text, payload["rows"][0]["text"])
        self.assertEqual(1, len(payload["rows"]))
        self.assert_clean()

    async def test_sqlite_disk_full_releases_slot_and_cleans_spool(self):
        with patch.object(ChatSpool, "append_batch", side_effect=sqlite3.OperationalError("database or disk is full")):
            await self.run_chat(self.source(5))
        self.assertIsNone(db.get_cache("123456789", "html_online"))
        self.assert_clean()

    async def test_stream_error_preserves_previous_ready_result_and_history(self):
        await self.run_chat(self.source(1))
        old_payload, old_cache = self.payload()
        old_path = self.root / "out" / old_cache["html_url"].removeprefix("local:")
        db.add_user_history(5, old_cache["id"])

        def broken_writer(output, *args, **kwargs):
            output.write("partial")
            raise OSError(errno.ENOSPC, "test disk full")

        with patch.object(worker_html, "write_viewer_html", side_effect=broken_writer):
            await self.run_chat(self.source(4))
        self.assertEqual(old_cache["html_url"], db.get_cache("123456789", "html_online")["html_url"])
        self.assertTrue(old_path.is_file())
        self.assertEqual(1, db.get_history_count(5))
        self.assert_clean()

    async def test_spool_losing_tail_between_passes_cannot_replace_ready_result(self):
        await self.run_chat(self.source(1))
        _, old_cache = self.payload()
        original = ChatSpool.iter_rows
        passes = 0

        def damaged_second_pass(spool):
            nonlocal passes
            passes += 1
            if passes == 2:
                con = sqlite3.connect(spool.path)
                try:
                    con.execute("DELETE FROM messages WHERE seq=?", (spool.messages,))
                    con.commit()
                finally:
                    con.close()
            return original(spool)

        with patch.object(ChatSpool, "iter_rows", damaged_second_pass):
            await self.run_chat(self.source(4))
        payload, cache = self.payload()
        self.assertEqual(old_cache["html_url"], cache["html_url"])
        self.assertEqual(1, len(payload["rows"]))
        self.assertEqual(1, len(tuple((self.root / "out").glob("*.html"))))
        self.assert_clean()

    async def test_download_cancel_discards_partial_chat(self):
        async def source():
            async for comment in self.source(10):
                yield comment
                set_cancel(self.context, True)

        await self.run_chat(source())
        self.assertIsNone(db.get_cache("123456789", "html_online"))
        self.assert_clean()

    async def test_cancel_during_html_write_closes_cursor_and_removes_temp(self):
        started, release = threading.Event(), threading.Event()
        original = worker_html.write_viewer_html

        def writer(output, *args, **kwargs):
            class Output:
                def write(self, text):
                    if '"rows": [' in text:
                        started.set()
                        if not release.wait(5):
                            raise AssertionError("test did not release writer")
                    return output.write(text)
            return original(Output(), *args, **kwargs)

        with patch.object(worker_html, "write_viewer_html", writer):
            task = asyncio.create_task(self.run_chat(self.source(10)))
            try:
                for _ in range(1000):
                    if started.is_set():
                        break
                    await asyncio.sleep(0.001)
                self.assertTrue(started.is_set())
                set_cancel(self.context, True)
                self.assertTrue(is_busy(self.context))
            finally:
                release.set()
                await task
        self.assertIsNone(db.get_cache("123456789", "html_online"))
        self.assert_clean()

    async def test_task_cancellation_waits_for_sqlite_thread_before_cleanup_and_slot_release(self):
        started, release = threading.Event(), threading.Event()
        original = ChatSpool.append_batch

        def append(spool, batch, cancel_check=None):
            started.set()
            if not release.wait(5):
                raise AssertionError("test did not release SQLite writer")
            return original(spool, batch, cancel_check)

        with patch.object(ChatSpool, "append_batch", append), \
             patch.object(asyncio.get_running_loop(), "call_exception_handler") as loop_error:
            task = asyncio.create_task(self.run_chat(self.source(10)))
            try:
                for _ in range(1000):
                    if started.is_set():
                        break
                    await asyncio.sleep(0.001)
                self.assertTrue(started.is_set())
                task.cancel()
                await asyncio.sleep(0.01)
                self.assertFalse(task.done())
                self.assertTrue(is_busy(self.context))
                self.assertTrue(tuple((self.root / "spools").glob("tdb-spool-*")))
            finally:
                release.set()
                await asyncio.gather(task, return_exceptions=True)
            loop_error.assert_not_called()
        self.assertFalse(is_busy(self.context))
        self.assert_clean()

    async def test_repeating_cursor_fails_before_emitting_duplicate_page(self):
        edge = {"node": {"commenter": {"displayName": "user"}, "message": {"fragments": [{"text": "x"}]}}}
        with patch.object(fetch_comments, "gql_fetch_page", AsyncMock(return_value=([edge], "same-cursor", True))), \
             patch.object(fetch_comments, "_sleep_with_cancel", AsyncMock()):
            iterator = fetch_comments.gql_fetch_comments(object(), "test", "123")
            first = await anext(iterator)
            self.assertEqual("x", first[3])
            with self.assertRaisesRegex(TwitchAPIError, "no progress"):
                await anext(iterator)

    async def test_repeated_task_cancel_drains_failing_writer_before_releasing_slot(self):
        # Repeated shutdown/cancel requests must not detach a still-running writer.
        await self.run_chat(self.source(1))
        old_payload, old_cache = self.payload()
        old_path = self.root / 'out' / old_cache['html_url'].removeprefix('local:')
        old_bytes = old_path.read_bytes()
        db.add_user_history(7, old_cache['id'])
        entered = asyncio.Event()
        release = threading.Event()
        loop = asyncio.get_running_loop()
        original_append = ChatSpool.append_batch

        def append(spool, batch, cancel_check=None):
            original_append(spool, batch, cancel_check)
            loop.call_soon_threadsafe(entered.set)
            if not release.wait(5):
                raise AssertionError('test did not release SQLite writer')
            raise OSError(errno.EIO, 'writer failed after cancellation')

        other = SimpleNamespace(application=self.context.application, user_data={})
        with patch.object(ChatSpool, 'append_batch', append), \
             patch.object(loop, 'call_exception_handler') as loop_error:
            task = asyncio.create_task(self.run_chat(self.source(4)))
            try:
                await asyncio.wait_for(entered.wait(), 5)
                # Cancel after the batch is durable but while its thread is still owned.
                task.cancel()
                # This checkpoint lets run_blocking observe the first cancellation.
                await asyncio.sleep(0)
                task.cancel()
                await asyncio.sleep(0)
                self.assertFalse(task.done())
                self.assertTrue(is_busy(self.context))
                self.assertIsNone(begin_download(other))
                self.assertTrue(tuple((self.root / 'spools').glob('tdb-spool-*')))
            finally:
                release.set()
                outcome = await asyncio.gather(task, return_exceptions=True)
            self.assertIsInstance(outcome[0], asyncio.CancelledError)
            loop_error.assert_not_called()

        payload, cache = self.payload()
        self.assertEqual(old_cache['html_url'], cache['html_url'])
        self.assertEqual(old_payload, payload)
        self.assertEqual(old_bytes, old_path.read_bytes())
        self.assertEqual(old_cache['id'], db.get_history_for_user(7)[0]['id'])
        self.assertFalse(is_busy(self.context))
        self.assert_clean()
        operation = begin_download(other)
        self.assertIsNotNone(operation)
        finish_download(other, operation)

    async def test_multi_page_cursor_cycle_terminates_with_constant_state(self):
        edge = {"node": {"commenter": {"displayName": "user"}, "message": {"fragments": [{"text": "x"}]}}}
        page_count = 0

        async def page(**kwargs):
            nonlocal page_count
            page_count += 1
            if page_count > 8:
                raise AssertionError("cursor cycle not detected")
            return [edge], ("cursor-a" if page_count % 2 else "cursor-b"), True

        with patch.object(fetch_comments, "gql_fetch_page", side_effect=page), \
             patch.object(fetch_comments, "_sleep_with_cancel", AsyncMock()):
            with self.assertRaisesRegex(TwitchAPIError, "no progress"):
                async for _ in fetch_comments.gql_fetch_comments(object(), "test", "123"):
                    pass
        self.assertLessEqual(page_count, 4)

    async def test_many_valid_cursor_pages_and_terminal_cursor_are_not_rejected(self):
        page_count = 0

        async def page(**kwargs):
            nonlocal page_count
            page_count += 1
            self.assertEqual(None if page_count == 1 else f"cursor-{page_count - 1}", kwargs["cursor"])
            edge = {"node": {"commenter": {"displayName": "user"},
                             "message": {"fragments": [{"text": str(page_count)}]}}}
            has_next = page_count < 40
            # A terminal response does not need a new continuation cursor.
            return [edge], f"cursor-{page_count}" if has_next else kwargs["cursor"], has_next

        with patch.object(fetch_comments, "gql_fetch_page", side_effect=page), \
             patch.object(fetch_comments, "_sleep_with_cancel", AsyncMock()):
            texts = [comment[3] async for comment in fetch_comments.gql_fetch_comments(object(), "test", "123")]
        self.assertEqual([str(i) for i in range(1, 41)], texts)


if __name__ == "__main__":
    unittest.main()
