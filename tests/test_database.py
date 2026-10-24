import sqlite3
import shutil
import tempfile
import concurrent.futures
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from src.database import database as db
from src.database import db_core


class DatabaseTestCase(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(
            prefix="tdb-db-",
            dir=Path(__file__).resolve().parent,
        )
        self.db_path = str(Path(self.temp_dir) / "test.sqlite3")
        self.db_path_patch = patch.object(db_core, "DB_PATH", self.db_path)
        self.db_path_patch.start()
        db.init_db()

    def tearDown(self):
        self.db_path_patch.stop()
        shutil.rmtree(self.temp_dir)

    def test_schema_and_cache_round_trip(self):
        cache_id = db.upsert_cache(
            vod_id="123456789",
            fmt="html_online",
            vod_url="https://www.twitch.tv/videos/123456789",
            meta={
                "title": "Test VOD",
                "created_at": "2024-01-01T00:00:00Z",
                "channel": "channel",
                "length_seconds": 120,
                "html_url": "https://example.invalid/chat.html",
            },
            stats={"messages": 2, "unique_users": 1, "parts": 0},
            files=[],
        )

        cached = db.get_cache("123456789", "html_online")
        self.assertIsNotNone(cached)
        self.assertEqual(cache_id, cached["id"])
        self.assertEqual("Test VOD", cached["meta"]["title"])
        self.assertEqual(2, cached["stats"]["messages"])

        cached_by_id = db.get_cache_by_id(cache_id)
        self.assertIsNotNone(cached_by_id)
        self.assertEqual("123456789", cached_by_id["vod_id"])

    def test_missing_cache_returns_none(self):
        self.assertIsNone(db.get_cache("missing", "html_online"))
        self.assertIsNone(db.get_cache_by_id(999999))

    def test_history_round_trip_and_cascade_delete(self):
        cache_id = db.upsert_cache(
            vod_id="987654321",
            fmt="html_online",
            vod_url="https://www.twitch.tv/videos/987654321",
            meta={"title": "History VOD"},
            stats={"messages": 1, "unique_users": 1, "parts": 0},
            files=[{"file_id": "telegram-file", "file_name": "chat.html"}],
        )

        db.add_user_history(42, cache_id)
        history = db.get_history_for_user(42)
        self.assertEqual(1, len(history))
        self.assertEqual(cache_id, history[0]["id"])

        con = sqlite3.connect(self.db_path)
        try:
            con.execute("PRAGMA foreign_keys = ON")
            con.execute("DELETE FROM vod_cache WHERE id = ?", (cache_id,))
            remaining_files = con.execute(
                "SELECT COUNT(*) FROM vod_files WHERE cache_id = ?", (cache_id,)
            ).fetchone()[0]
            remaining_history = con.execute(
                "SELECT COUNT(*) FROM user_history WHERE cache_id = ?", (cache_id,)
            ).fetchone()[0]
            con.commit()
        finally:
            con.close()

        self.assertEqual(0, remaining_files)
        self.assertEqual(0, remaining_history)
        self.assertEqual([], db.get_history_for_user(42))

    def test_database_path_parent_is_created_and_data_survives_reopen(self):
        nested_path = Path(self.temp_dir) / "new" / "nested" / "persistent.sqlite3"
        with patch.object(db_core, "DB_PATH", str(nested_path)):
            db.init_db()
            cache_id = db.upsert_cache(
                "reopen-vod", "html_online", "vod-url", {"title": "Persisted"}, {}, []
            )
            db.add_user_history(101, cache_id)

            db.init_db()

            self.assertTrue(nested_path.is_file())
            self.assertEqual("Persisted", db.get_cache("reopen-vod", "html_online")["title"])
            self.assertEqual(1, db.get_history_count(101))

    def test_tilde_database_path_uses_expanded_home_for_directory_and_connection(self):
        test_home = Path(self.temp_dir) / "home"
        expanded_path = test_home / "new" / "nested" / "history.sqlite3"
        tilde_path = str(Path("~") / "new" / "nested" / "history.sqlite3")
        with patch.dict("os.environ", {"HOME": str(test_home), "USERPROFILE": str(test_home)}), \
             patch.object(db_core, "DB_PATH", tilde_path):
            db.init_db()
            cache_id = db.upsert_cache(
                "tilde-vod", "html_online", "vod-url", {"title": "Expanded home"}, {}, []
            )

            self.assertTrue(expanded_path.is_file())
            self.assertEqual("Expanded home", db.get_cache_by_id(cache_id)["title"])

    def test_concurrent_history_adds_are_idempotent_and_isolated_by_user(self):
        cache_id = db.upsert_cache("parallel-vod", "html_online", "vod-url", {}, {}, [])
        workers = 24

        def add_history(user_id, barrier):
            barrier.wait()
            db.add_user_history(user_id, cache_id)

        barrier = threading.Barrier(workers)
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(add_history, 202, barrier) for _ in range(workers)]
            for future in futures:
                future.result()
        self.assertEqual(1, db.get_history_count(202))

        barrier = threading.Barrier(workers)
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(add_history, 300 + index, barrier) for index in range(workers)]
            for future in futures:
                future.result()
        self.assertEqual(workers, sum(db.get_history_count(300 + i) for i in range(workers)))

    def test_concurrent_cache_upserts_keep_one_complete_cache_and_file_set(self):
        workers = 16
        barrier = threading.Barrier(workers)

        def upsert(index):
            barrier.wait()
            return db.upsert_cache(
                "shared-vod", "html_online", "vod-url",
                {"title": f"title-{index}"},
                {"messages": index, "unique_users": 1, "parts": 1},
                [{"file_id": f"file-{index}", "file_name": f"chat-{index}.html"}],
            )

        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            cache_ids = list(pool.map(upsert, range(workers)))

        self.assertEqual(1, len(set(cache_ids)))
        cached = db.get_cache("shared-vod", "html_online")
        self.assertEqual(cache_ids[0], cached["id"])
        self.assertEqual(1, len(cached["files"]))
        self.assertEqual(
            f"file-{cached['title'].removeprefix('title-')}",
            cached["files"][0]["file_id"],
        )

    def test_failed_cache_upsert_rolls_back_previous_cache_and_files(self):
        cache_id = db.upsert_cache(
            "rollback-vod", "html_online", "vod-url", {"title": "Previous"}, {},
            [{"file_id": "old-file", "file_name": "old.html"}],
        )

        with self.assertRaises(KeyError):
            db.upsert_cache(
                "rollback-vod", "html_online", "vod-url", {"title": "Incomplete"}, {},
                [{}],
            )

        cached = db.get_cache("rollback-vod", "html_online")
        self.assertEqual(cache_id, cached["id"])
        self.assertEqual("Previous", cached["title"])
        self.assertEqual("old-file", cached["files"][0]["file_id"])

    def test_connection_is_closed_if_initialization_pragma_fails(self):
        connection = Mock()
        connection.execute.side_effect = sqlite3.OperationalError("pragma failed")
        with patch.object(sqlite3, "connect", return_value=connection):
            with self.assertRaisesRegex(sqlite3.OperationalError, "pragma failed"):
                db_core._connect()
        connection.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
