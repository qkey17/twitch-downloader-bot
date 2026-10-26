import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from src.database import database as db
from src.database import db_core
from src.history.history_callbacks import history_page_callback, history_vod_callback
from src.history.history_files import history_files_callback
from src.ui.ui_history import build_history_page


class ImmediateAsyncMock:
    def __init__(self):
        self.call_args = None
        self.call_args_list = []

    async def _resolved(self):
        return None

    def __call__(self, *args, **kwargs):
        self.call_args = SimpleNamespace(args=args, kwargs=kwargs)
        self.call_args_list.append(self.call_args)
        return self._resolved()

    def reset_mock(self):
        self.call_args = None
        self.call_args_list.clear()


def run_callback(coro):
    iterator = coro.__await__()
    try:
        yielded = next(iterator)
    except StopIteration as result:
        return result.value
    raise AssertionError(f"callback unexpectedly yielded: {yielded!r}")


class HistoryPaginationTestCase(unittest.TestCase):
    page_size = 1

    def setUp(self):
        self.history_patch = patch("src.config.HISTORY_ENABLED", True)
        self.history_patch.start()
        self.addCleanup(self.history_patch.stop)
        self.temp_dir = tempfile.mkdtemp(
            prefix="tdb-history-",
            dir=Path(__file__).resolve().parent,
        )
        self.db_path = str(Path(self.temp_dir) / "history.sqlite3")
        self.db_path_patch = patch.object(db_core, "DB_PATH", self.db_path)
        self.db_path_patch.start()
        db.init_db()

    def tearDown(self):
        self.db_path_patch.stop()
        shutil.rmtree(self.temp_dir)

    def add_history(self, user_id, number):
        vod_id = str(100000000 + number)
        cache_id = db.upsert_cache(
            vod_id=vod_id,
            fmt="files",
            vod_url=f"https://www.twitch.tv/videos/{vod_id}",
            meta={"title": f"VOD {number}"},
            stats={"messages": 1, "unique_users": 1, "parts": 1},
            files=[{"file_id": f"file-{number}", "file_name": f"{number}.json"}],
        )
        with patch("src.database.db_history.time.time", return_value=float(number + 1)):
            db.add_user_history(user_id, cache_id)
        return db.get_history_for_user(user_id, limit=1, offset=0)[0]["history_id"]

    def test_short_and_exact_page_counts(self):
        for count in (0, 1):
            items = [
                {"history_id": i + 1, "vod_url": f"url-{i}"}
                for i in range(count)
            ]
            cards, nav = build_history_page(
                items,
                page=0,
                per_page=self.page_size,
                total_items=count,
            )
            self.assertEqual(count, len(cards))
            self.assertIsNotNone(nav)
            labels = [button.text for button in nav.inline_keyboard[0]]
            self.assertNotIn("⬅️", labels)
            self.assertNotIn("➡️", labels)

    def test_forward_and_back_cover_all_pages(self):
        items = [
            {"history_id": i + 1, "vod_url": f"url-{i}"}
            for i in range(3)
        ]

        first_cards, first_nav = build_history_page(
            items[:1], page=0, per_page=1, total_items=3
        )
        middle_cards, middle_nav = build_history_page(
            items[1:2], page=1, per_page=1, total_items=3
        )
        last_cards, last_nav = build_history_page(
            items[2:], page=2, per_page=1, total_items=3
        )

        self.assertEqual("url-0", first_cards[0][1].inline_keyboard[0][1].url)
        self.assertEqual("url-1", middle_cards[0][1].inline_keyboard[0][1].url)
        self.assertEqual("url-2", last_cards[0][1].inline_keyboard[0][1].url)
        self.assertEqual(["1/3", "➡️"], [b.text for b in first_nav.inline_keyboard[0]])
        self.assertEqual(["⬅️", "2/3", "➡️"], [b.text for b in middle_nav.inline_keyboard[0]])
        self.assertEqual(["⬅️", "3/3"], [b.text for b in last_nav.inline_keyboard[0]])
        self.assertEqual("ui:histpage:1", first_nav.inline_keyboard[0][1].callback_data)
        self.assertEqual("ui:histpage:1", last_nav.inline_keyboard[0][0].callback_data)

    def test_history_page_callback_fetches_offset_and_clamps_last_page(self):
        for number in range(3):
            self.add_history(50, number)

        query = SimpleNamespace(
            data="ui:histpage:1",
            answer=ImmediateAsyncMock(),
            message=SimpleNamespace(edit_text=ImmediateAsyncMock(), chat_id=7),
        )
        update = SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=50))
        context = SimpleNamespace(bot=SimpleNamespace(send_message=ImmediateAsyncMock()))

        with patch("src.database.database.get_history_for_user", wraps=db.get_history_for_user) as fetch:
            run_callback(history_page_callback(update, context))

        self.assertEqual(1, fetch.call_args.kwargs["offset"])
        self.assertIn("VOD 1", query.message.edit_text.call_args.args[0])

        query.data = "ui:histpage:0"
        query.message.edit_text.reset_mock()
        with patch("src.database.database.get_history_for_user", wraps=db.get_history_for_user) as fetch:
            run_callback(history_page_callback(update, context))
        self.assertEqual(0, fetch.call_args.kwargs["offset"])
        self.assertIn("VOD 2", query.message.edit_text.call_args.args[0])

        query.data = "ui:histpage:999"
        query.message.edit_text.reset_mock()
        with patch("src.database.database.get_history_for_user", wraps=db.get_history_for_user) as fetch:
            run_callback(history_page_callback(update, context))
        self.assertEqual(2, fetch.call_args.kwargs["offset"])
        self.assertIn("VOD 0", query.message.edit_text.call_args.args[0])

    def test_file_button_uses_stable_id_and_fits_telegram_limit(self):
        history_id = self.add_history(60, 0)
        item = db.get_history_for_user(60, limit=1, offset=0)[0]
        cards, _ = build_history_page([item], page=0, per_page=1, total_items=1)
        callback_data = cards[0][1].inline_keyboard[0][0].callback_data

        self.assertEqual(f"ui:histfiles:h{history_id}", callback_data)
        self.assertLessEqual(len(callback_data.encode("utf-8")), 64)

    def test_file_callback_selects_stable_record_after_history_changes(self):
        first_id = self.add_history(70, 0)
        target_id = self.add_history(70, 1)
        query = SimpleNamespace(
            data=f"ui:histfiles:h{target_id}",
            answer=ImmediateAsyncMock(),
            message=SimpleNamespace(reply_text=ImmediateAsyncMock(), chat_id=8),
        )
        update = SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=70))
        context = SimpleNamespace(bot=SimpleNamespace(
            send_document=ImmediateAsyncMock(),
            send_message=ImmediateAsyncMock(),
        ))

        with patch("src.history.history_files.build_history_page", return_value=([("card", None)], None)):
            run_callback(history_files_callback(update, context))

        self.assertNotEqual(first_id, target_id)
        self.assertEqual("file-1", context.bot.send_document.call_args.kwargs["document"])

        query.data = f"ui:histfiles:h{target_id}"
        update.effective_user.id = 71
        query.message.reply_text.reset_mock()
        run_callback(history_files_callback(update, context))
        self.assertEqual("VOD не найден в истории.", query.message.reply_text.call_args.args[0])
        self.assertEqual(1, len(context.bot.send_document.call_args_list))

        query.data = "ui:histfiles:1"  # legacy positional callback is rejected
        update.effective_user.id = 70
        query.message.reply_text.reset_mock()
        run_callback(history_files_callback(update, context))
        self.assertEqual("VOD не найден в истории.", query.message.reply_text.call_args.args[0])
        self.assertEqual(1, len(context.bot.send_document.call_args_list))

    def test_vod_callback_checks_ownership_missing_and_legacy_index(self):
        history_id = self.add_history(80, 0)
        self.add_history(81, 1)
        query = SimpleNamespace(
            data=f"ui:histvod:h{history_id}",
            answer=ImmediateAsyncMock(),
            message=SimpleNamespace(reply_text=ImmediateAsyncMock(), chat_id=9),
        )
        update = SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=80))
        context = SimpleNamespace(bot=SimpleNamespace(send_message=ImmediateAsyncMock()))

        run_callback(history_vod_callback(update, context))
        self.assertIn("100000000", query.message.reply_text.call_args.args[0])

        query.message.reply_text.reset_mock()
        update.effective_user.id = 81
        run_callback(history_vod_callback(update, context))
        self.assertEqual("VOD не найден в истории.", query.message.reply_text.call_args.args[0])

        query.data = "ui:histvod:0"  # callback from the old position-based format
        query.message.reply_text.reset_mock()
        update.effective_user.id = 80
        run_callback(history_vod_callback(update, context))
        self.assertEqual("VOD не найден в истории.", query.message.reply_text.call_args.args[0])

        query.data = "ui:histvod:h999999"
        query.message.reply_text.reset_mock()
        run_callback(history_vod_callback(update, context))
        self.assertEqual("VOD не найден в истории.", query.message.reply_text.call_args.args[0])

    def test_deleted_cache_entry_invalidates_owned_callback(self):
        history_id = self.add_history(90, 0)
        cache = db.get_history_entry_for_user(90, history_id)
        con = sqlite3.connect(self.db_path)
        try:
            con.execute("PRAGMA foreign_keys = ON")
            con.execute("DELETE FROM vod_cache WHERE id = ?", (cache["id"],))
            con.commit()
        finally:
            con.close()

        query = SimpleNamespace(
            data=f"ui:histvod:h{history_id}",
            answer=ImmediateAsyncMock(),
            message=SimpleNamespace(reply_text=ImmediateAsyncMock(), chat_id=10),
        )
        update = SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=90))
        context = SimpleNamespace(bot=SimpleNamespace(send_message=ImmediateAsyncMock()))

        run_callback(history_vod_callback(update, context))
        self.assertEqual("VOD не найден в истории.", query.message.reply_text.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
