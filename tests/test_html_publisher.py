import builtins
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from src import html_publisher


class _FailingWriter:
    def __init__(self, handle):
        self.handle = handle

    @property
    def name(self):
        return self.handle.name

    def fileno(self):
        return self.handle.fileno()

    def __enter__(self):
        self.handle.__enter__()
        return self

    def write(self, _text):
        raise OSError("simulated write failure")

    def flush(self):
        self.handle.flush()

    def __exit__(self, exc_type, exc_value, traceback):
        return self.handle.__exit__(exc_type, exc_value, traceback)


class HtmlPublisherTestCase(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(
            prefix="tdb-publisher-",
            dir=Path(__file__).resolve().parent,
        )
        self.directory = Path(self.temp_dir.name)
        self.patches = [
            patch.object(html_publisher, "PUBLIC_HTML_DIR", str(self.directory)),
            patch.object(html_publisher, "PUBLIC_BASE_URL", "https://publisher-test.invalid"),
            patch.object(html_publisher, "PUBLIC_HTML_URL_PREFIX", "/public_html"),
            patch.object(
                html_publisher,
                "uuid",
                SimpleNamespace(uuid4=lambda: SimpleNamespace(hex="a" * 32)),
            ),
        ]
        for active_patch in self.patches:
            active_patch.start()

    def tearDown(self):
        for active_patch in reversed(self.patches):
            active_patch.stop()
        self.temp_dir.cleanup()

    def target_path(self):
        return self.directory / ("chat_" + "a" * 32 + ".html")

    def test_successful_publication_returns_expected_url(self):
        url = html_publisher.publish_html("<html>viewer</html>")

        self.assertEqual(
            "https://publisher-test.invalid/public_html/chat_" + "a" * 32 + ".html",
            url,
        )
        self.assertEqual("<html>viewer</html>", self.target_path().read_text(encoding="utf-8"))
        self.assertEqual([self.target_path()], list(self.directory.iterdir()))

    def test_publication_replaces_existing_file(self):
        self.target_path().write_text("old viewer", encoding="utf-8")

        html_publisher.publish_html("new viewer")

        self.assertEqual("new viewer", self.target_path().read_text(encoding="utf-8"))
        self.assertEqual([self.target_path()], list(self.directory.iterdir()))

    def test_write_error_removes_temp_file_and_preserves_existing_target(self):
        self.target_path().write_text("previous viewer", encoding="utf-8")
        real_open = builtins.open

        def failing_temp_file(path, *args, **kwargs):
            handle = real_open(path, *args, **kwargs)
            return _FailingWriter(handle) if str(path).endswith(".tmp") else handle

        with patch.object(builtins, "open", side_effect=failing_temp_file):
            with self.assertRaisesRegex(OSError, "simulated write failure"):
                html_publisher.publish_html("new viewer")

        self.assertEqual("previous viewer", self.target_path().read_text(encoding="utf-8"))
        self.assertEqual([self.target_path()], list(self.directory.iterdir()))
        self.assertFalse(any(path.suffix == ".tmp" for path in self.directory.iterdir()))

    def test_replace_error_removes_temp_file_and_preserves_existing_target(self):
        self.target_path().write_text("previous viewer", encoding="utf-8")

        with patch.object(html_publisher.os, "replace", side_effect=OSError("simulated replace failure")):
            with self.assertRaisesRegex(OSError, "simulated replace failure"):
                html_publisher.publish_html("new viewer")

        self.assertEqual("previous viewer", self.target_path().read_text(encoding="utf-8"))
        self.assertEqual([self.target_path()], list(self.directory.iterdir()))

    def test_existing_temp_name_is_not_deleted_when_exclusive_create_fails(self):
        temp_path = self.directory / (".chat_" + "a" * 32 + ".html." + "a" * 32 + ".tmp")
        temp_path.write_text("owned by another operation", encoding="utf-8")

        with self.assertRaises(FileExistsError):
            html_publisher.publish_html("new viewer")

        self.assertEqual("owned by another operation", temp_path.read_text(encoding="utf-8"))

    def test_startup_cleanup_skips_missing_output_directories(self):
        missing_local = self.directory / "missing-local"
        missing_public = self.directory / "missing-public"
        with patch.object(html_publisher, "OUT_DIR", str(missing_local)), \
             patch.object(html_publisher, "PUBLIC_HTML_DIR", str(missing_public)):
            self.assertEqual(0, html_publisher.cleanup_stale_html_temp_files())

        self.assertFalse(missing_local.exists())
        self.assertFalse(missing_public.exists())

    def test_startup_cleanup_handles_lazy_missing_directory_and_continues(self):
        import os

        real_iterdir = Path.iterdir

        def lazy_iterdir(path):
            # Python 3.12 raises directory errors when the iterator advances.
            yield from real_iterdir(path)

        missing_local = self.directory / "missing-local"
        old_temp = self.directory / (".chat_" + "b" * 32 + ".html." + "c" * 32 + ".tmp")
        old_temp.write_text("old publisher temp", encoding="utf-8")
        now = 2_000_000_000
        os.utime(old_temp, (now - html_publisher._STALE_TEMP_HTML_AGE_SECONDS - 1,) * 2)

        with patch.object(html_publisher, "OUT_DIR", str(missing_local)), \
             patch.object(html_publisher, "PUBLIC_HTML_DIR", str(self.directory)), \
             patch.object(Path, "iterdir", lazy_iterdir), \
             patch.object(html_publisher.time, "time", return_value=now):
            self.assertEqual(1, html_publisher.cleanup_stale_html_temp_files())

        self.assertFalse(missing_local.exists())
        self.assertFalse(old_temp.exists())

    def test_startup_cleanup_removes_only_old_exact_publisher_temp_files(self):
        import os

        old_temp = self.directory / (".chat_" + "b" * 32 + ".html." + "c" * 32 + ".tmp")
        recent_temp = self.directory / (".chat_" + "d" * 32 + ".html." + "e" * 32 + ".tmp")
        malformed_temp = self.directory / ".chat-not-a-publisher-file.tmp"
        completed_html = self.directory / ("chat_" + "f" * 32 + ".html")
        for path in (old_temp, recent_temp, malformed_temp, completed_html):
            path.write_text("keep or remove according to the strict stale-temp rule", encoding="utf-8")

        now = 2_000_000_000
        os.utime(old_temp, (now - html_publisher._STALE_TEMP_HTML_AGE_SECONDS - 1,) * 2)
        os.utime(recent_temp, (now - 60,) * 2)
        with patch.object(html_publisher, "OUT_DIR", str(self.directory)), \
             patch.object(html_publisher, "PUBLIC_HTML_DIR", str(self.directory)), \
             patch.object(html_publisher.time, "time", return_value=now):
            removed = html_publisher.cleanup_stale_html_temp_files()

        self.assertEqual(1, removed)
        self.assertFalse(old_temp.exists())
        self.assertTrue(recent_temp.exists())
        self.assertTrue(malformed_temp.exists())
        self.assertTrue(completed_html.exists())

    def test_startup_cleanup_never_removes_locked_publisher_temp(self):
        import os
        from src.file_lock import try_lock_file

        temp = self.directory / (".chat_" + "b" * 32 + ".html." + "c" * 32 + ".tmp")
        with temp.open("x+b") as writer:
            self.assertTrue(try_lock_file(writer))
            writer.write(b"in progress")
            writer.flush()
            os.utime(temp, (1, 1))
            with patch.object(html_publisher, "OUT_DIR", str(self.directory)):
                self.assertEqual(0, html_publisher.cleanup_stale_html_temp_files())
            self.assertTrue(temp.exists())
        with patch.object(html_publisher, "OUT_DIR", str(self.directory)):
            self.assertEqual(1, html_publisher.cleanup_stale_html_temp_files())

    def test_cleanup_rechecks_age_after_acquiring_lock(self):
        import os

        temp = self.directory / (".chat_" + "b" * 32 + ".html." + "c" * 32 + ".tmp")
        temp.write_text("writer resumed", encoding="utf-8")
        os.utime(temp, (1, 1))
        original = html_publisher.try_lock_file

        def lock_after_recent_write(handle):
            # The writer refreshed/flushed the file between the initial stat
            # and cleanup's lock attempt, and is about to rename it.
            os.utime(temp, None)
            return original(handle)

        with patch.object(html_publisher, "OUT_DIR", str(self.directory)), \
             patch.object(html_publisher, "try_lock_file", side_effect=lock_after_recent_write):
            self.assertEqual(0, html_publisher.cleanup_stale_html_temp_files())
        self.assertTrue(temp.exists())


if __name__ == "__main__":
    unittest.main()
