import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.database import database as db
from src.database import db_core
from src.handlers.handlers_vod import _cache_entry_has_usable_output
from src.html_publisher import local_published_html_status, publish_html


class CacheReliabilityTestCase(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(
            prefix="tdb-cache-",
            dir=Path(__file__).resolve().parent,
        )
        self.db_path = str(Path(self.temp_dir) / "cache.sqlite3")
        self.db_path_patch = patch.object(db_core, "DB_PATH", self.db_path)
        self.db_path_patch.start()
        self.html_dir_patch = patch("src.html_publisher.PUBLIC_HTML_DIR", self.temp_dir)
        self.html_dir_patch.start()
        self.base_url_patch = patch("src.html_publisher.PUBLIC_BASE_URL", "https://cache-test.invalid")
        self.base_url_patch.start()
        self.url_prefix_patch = patch("src.html_publisher.PUBLIC_HTML_URL_PREFIX", "/public_html")
        self.publish_mode_patch = patch("src.handlers.handlers_vod.HTML_PUBLISH_MODE", "public")
        self.url_prefix_patch.start()
        self.publish_mode_patch.start()
        db.init_db()

    def tearDown(self):
        self.publish_mode_patch.stop()
        self.url_prefix_patch.stop()
        self.base_url_patch.stop()
        self.html_dir_patch.stop()
        self.db_path_patch.stop()
        shutil.rmtree(self.temp_dir)

    def add_cache(self, html_url):
        return db.upsert_cache(
            vod_id="123456789",
            fmt="html_online",
            vod_url="https://www.twitch.tv/videos/123456789",
            meta={"title": "Cached VOD", "html_url": html_url},
            stats={"messages": 3, "unique_users": 2, "parts": 0},
            files=[],
        )

    def test_ordinary_cache_hit_with_existing_html(self):
        html_url = publish_html("<html>cached viewer</html>")
        self.add_cache(html_url)
        cached = db.get_cache("123456789", "html_online")

        self.assertTrue(_cache_entry_has_usable_output(cached, "html_online"))
        self.assertIsNotNone(db.get_cache("123456789", "html_online"))

    def test_missing_cache_record_is_not_a_hit(self):
        cached = db.get_cache("missing", "html_online")
        self.assertIsNone(cached)
        self.assertFalse(_cache_entry_has_usable_output(cached, "html_online"))

    def test_cache_referencing_missing_html_is_not_usable(self):
        html_url = "https://cache-test.invalid/public_html/chat_0123456789abcdef0123456789abcdef.html"
        self.add_cache(html_url)
        cached = db.get_cache("123456789", "html_online")

        self.assertFalse(_cache_entry_has_usable_output(cached, "html_online"))
        self.assertFalse(local_published_html_status(html_url))
        self.assertIsNotNone(db.get_cache("123456789", "html_online"))

    def test_existing_html_is_verified_through_publisher_path_mapping(self):
        html_url = publish_html("<html>available</html>")
        self.assertTrue(local_published_html_status(html_url))
        self.assertTrue(_cache_entry_has_usable_output({"html_url": html_url}, "html_online"))

    def test_invalid_html_cache_does_not_delete_history(self):
        html_url = "https://cache-test.invalid/public_html/chat_abcdef0123456789abcdef0123456789.html"
        cache_id = self.add_cache(html_url)
        db.add_user_history(444, cache_id)

        cached = db.get_cache("123456789", "html_online")
        self.assertFalse(_cache_entry_has_usable_output(cached, "html_online"))
        self.assertEqual(1, db.get_history_count(444))
        history = db.get_history_for_user(444, limit=1, offset=0)
        self.assertEqual(cache_id, history[0]["id"])

        con = sqlite3.connect(self.db_path)
        try:
            self.assertEqual(1, con.execute("SELECT COUNT(*) FROM vod_cache").fetchone()[0])
            self.assertEqual(1, con.execute("SELECT COUNT(*) FROM user_history").fetchone()[0])
        finally:
            con.close()

    def test_unmapped_external_url_is_not_claimed_missing(self):
        url = "https://storage.example.invalid/chat.html"
        self.assertIsNone(local_published_html_status(url))
        self.assertTrue(_cache_entry_has_usable_output({"html_url": url}, "html_online"))


if __name__ == "__main__":
    unittest.main()
