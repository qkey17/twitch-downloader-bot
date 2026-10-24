import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from src.database import database as db
from src.database import db_core
from src.handlers import handlers_vod
from src.html_publisher import (
    is_local_html_reference,
    local_published_html_status,
    publish_html_local,
)
from src.ui.ui_history import build_history_page
from src.ui.ui_labels import CHAT_GENERIC
from src.workers import worker_html


class LocalHtmlPublicationTestCase(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="tdb-local-html-", dir=Path(__file__).parent)
        self.output_dir = Path(self.temp_dir) / "_out"
        self.db_path = str(Path(self.temp_dir) / "history.sqlite3")
        self.db_patch = patch.object(db_core, "DB_PATH", self.db_path)
        self.out_patch = patch("src.html_publisher.OUT_DIR", str(self.output_dir))
        self.mode_patch = patch.object(handlers_vod, "HTML_PUBLISH_MODE", "local")
        self.db_patch.start()
        self.out_patch.start()
        self.mode_patch.start()
        db.init_db()

    def tearDown(self):
        self.mode_patch.stop()
        self.out_patch.stop()
        self.db_patch.stop()
        shutil.rmtree(self.temp_dir)

    def test_local_publisher_writes_html_with_existing_filename_pattern(self):
        html_ref = publish_html_local("<!doctype html><title>viewer</title>")

        self.assertTrue(is_local_html_reference(html_ref))
        self.assertTrue(local_published_html_status(html_ref))
        filename = html_ref.removeprefix("local:")
        self.assertRegex(filename, r"^chat_[0-9a-f]{32}\.html$")
        self.assertEqual(
            "<!doctype html><title>viewer</title>",
            (self.output_dir / filename).read_text(encoding="utf-8"),
        )

    def test_worker_saves_rendered_html_locally_without_public_publisher(self):
        meta = SimpleNamespace(channel_id=None, channel=None, title="VOD", created_at=None,
                               thumbnail_preview_url=None, thumbnail_full_url=None)
        with patch.object(worker_html, "HTML_PUBLISH_MODE", "local"), \
             patch.object(worker_html, "fetch_twitch_global_emote_map", new=AsyncMock(return_value={})), \
             patch.object(worker_html, "fetch_twitch_global_badge_map", new=AsyncMock(return_value={})), \
             patch.object(worker_html, "render_viewer_html", return_value="rendered-html"), \
             patch.object(worker_html, "publish_html", side_effect=AssertionError("public publisher called")):
            files, html_ref = __import__("asyncio").run(worker_html.build_html_result(
                context=None,
                session=object(),
                chat_id=1,
                fmt="html_online",
                meta=meta,
                vod_url="https://www.twitch.tv/videos/123456789",
                base_stem="unused-but-compatible",
                out_dir=self.output_dir,
                chat_rows=[{"text": "message"}],
                token_counter=None,
            ))

        self.assertEqual([], files)
        self.assertTrue(is_local_html_reference(html_ref))
        filename = html_ref.removeprefix("local:")
        self.assertEqual("rendered-html", (self.output_dir / filename).read_text(encoding="utf-8"))

    def test_local_reference_round_trips_through_cache_and_history(self):
        html_ref = publish_html_local("viewer")
        cache_id = db.upsert_cache(
            vod_id="123456789",
            fmt="html_online",
            vod_url="https://www.twitch.tv/videos/123456789",
            meta={"title": "Local VOD", "channel": "streamer", "html_url": html_ref},
            stats={"messages": 5, "unique_users": 2, "parts": 0},
            files=[],
        )
        db.add_user_history(77, cache_id)

        cached = db.get_cache("123456789", "html_online")
        history = db.get_history_for_user(77, limit=1)
        self.assertEqual(html_ref, cached["html_url"])
        self.assertEqual(html_ref, history[0]["html_url"])
        self.assertTrue(handlers_vod._cache_entry_has_usable_output(cached, "html_online"))

        cards, _ = build_history_page(history, page=0, per_page=1)
        text, keyboard = cards[0]
        self.assertIn("Локальный HTML", text)
        self.assertIn("_out/" + html_ref.removeprefix("local:"), text)
        self.assertTrue(all(
            button.text != CHAT_GENERIC
            for row in keyboard.inline_keyboard
            for button in row
        ))

        (self.output_dir / html_ref.removeprefix("local:")).unlink()
        self.assertFalse(handlers_vod._cache_entry_has_usable_output(cached, "html_online"))
        self.assertEqual(1, db.get_history_count(77))
        stale_cards, _ = build_history_page(db.get_history_for_user(77, limit=1), page=0, per_page=1)
        self.assertIn("недоступен локально", stale_cards[0][0])
        self.assertNotIn(html_ref.removeprefix("local:"), stale_cards[0][0])

    def test_legacy_public_cache_url_is_not_shown_as_a_link_in_local_mode(self):
        cards, _ = build_history_page([{
            "fmt": "html_online",
            "html_url": "https://legacy.example.invalid/chat.html",
            "vod_url": "https://www.twitch.tv/videos/123456789",
        }], page=0, per_page=1)

        text, keyboard = cards[0]
        self.assertIn("HTML-файл недоступен локально", text)
        self.assertTrue(all(
            button.text != CHAT_GENERIC
            for row in keyboard.inline_keyboard
            for button in row
        ))


class LocalCompletionMessageTestCase(unittest.IsolatedAsyncioTestCase):
    async def test_runner_persists_local_reference_and_sends_no_public_html_link(self):
        html_ref = "local:chat_0123456789abcdef0123456789abcdef.html"
        meta = {
            "title": "Local VOD",
            "channel": "streamer",
            "created_at": None,
            "length_seconds": 60,
            "html_url": html_ref,
        }
        bot = SimpleNamespace(edit_message_text=AsyncMock())
        context = SimpleNamespace(bot=bot)

        with patch.object(handlers_vod, "HTML_PUBLISH_MODE", "local"), \
             patch.object(handlers_vod, "download_and_send", new=AsyncMock(return_value=(
                 meta, {"messages": 5, "unique_users": 2, "parts": 0}, [], html_ref
             ))), \
             patch.object(handlers_vod.db, "upsert_cache", return_value=42) as cache_write, \
             patch.object(handlers_vod.db, "add_user_history") as history_write, \
             patch("src.ui.ui_history.local_published_html_status", return_value=True), \
             patch.object(handlers_vod, "finish_download"):
            await handlers_vod._runner(
                context=context,
                chat_id=5,
                progress_message_id=9,
                vod_url="https://www.twitch.tv/videos/123456789",
                vod_id="123456789",
                fmt="html_online",
                user_id=7,
                operation_id="operation",
            )

        cache_write.assert_called_once()
        self.assertEqual(html_ref, cache_write.call_args.kwargs["meta"]["html_url"])
        history_write.assert_called_once_with(7, 42)
        call = bot.edit_message_text.call_args.kwargs
        self.assertIn("_out/" + html_ref.removeprefix("local:"), call["text"])
        self.assertTrue(all(
            button.text != CHAT_GENERIC
            for row in call["reply_markup"].inline_keyboard
            for button in row
        ))


if __name__ == "__main__":
    unittest.main()
