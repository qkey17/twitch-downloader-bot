import errno
import io
import json
import tempfile
import unittest
import weakref
import os
import runpy
import subprocess
import sys
import sqlite3
from html.parser import HTMLParser
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace

from src import html_publisher, resource_usage
from src.chat_spool import ChatSpool, encode_row, cleanup_orphaned_chat_spools
from src.html_renderer import render_viewer_html, write_viewer_html
from src.resource_usage import memory_usage


def rows(count):
    for i in range(count):
        yield {"t": "00:00:01", "user": ("User", "user", "é", "e\u0301")[i % 4],
               "text": f'{i} </SCRIPT><script> & " \\ Привет 🎮',
               "fragments": [{"text": "Kappa", "emoteID": "25", "emoteName": "Kappa"}],
               "color": "#abcdef", "badges": [{"set_id": "subscriber", "version": "1"}]}


class ChatDataParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.inside = False
        self.blocks = []

    def handle_starttag(self, tag, attrs):
        if tag == "script" and dict(attrs).get("id") == "chat-data":
            self.inside = True

    def handle_endtag(self, tag):
        if tag == "script":
            self.inside = False

    def handle_data(self, data):
        if self.inside:
            self.blocks.append(data)


class SpoolAndStreamingTestCase(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="tdb-stream-", dir=Path(__file__).parent)
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)

    def spool(self):
        spool = ChatSpool(self.root / "spools")
        spool.open()
        self.addCleanup(spool.close)
        return spool

    def test_order_counts_case_sensitive_users_and_multiple_batches(self):
        spool = self.spool()
        for start in range(0, 11, 3):
            spool.append_batch([encode_row(row) for row in tuple(rows(11))[start:start + 3]])
        self.assertEqual(11, spool.messages)
        self.assertEqual(4, spool.unique_users)
        self.assertEqual(tuple(rows(11)), tuple(spool.iter_rows()))
        self.assertGreater(spool.size_bytes(), 0)
        spool.close()
        self.assertFalse(spool.directory.exists())

    def test_unique_users_are_on_disk_for_high_cardinality(self):
        spool = self.spool()
        for start in range(0, 10000, 500):
            spool.append_batch((encode_row({"user": f"u{i}", "text": "x"})
                                for i in range(start, start + 500)))
        self.assertEqual(10000, spool.messages)
        self.assertEqual(10000, spool.unique_users)
        self.assertFalse(any(isinstance(value, (set, list)) for value in vars(spool).values()))

    def test_batch_rollback_on_failure_and_cancellation(self):
        spool = self.spool()
        spool.append_batch([encode_row(next(rows(1)))])

        def failed_batch():
            yield encode_row({"user": "new", "text": "not committed"})
            raise OSError(errno.ENOSPC, "test disk full")

        with self.assertRaises(OSError):
            spool.append_batch(failed_batch())
        with self.assertRaisesRegex(RuntimeError, "отменена"):
            spool.append_batch([encode_row({"user": "new"})], cancel_check=lambda: True)
        self.assertEqual(1, spool.messages)
        self.assertEqual(1, spool.unique_users)
        self.assertEqual(1, sum(1 for _ in spool.iter_rows()))

    def test_missing_and_corrupt_spool_are_errors_not_empty_chat(self):
        for corrupt in (False, True):
            spool = self.spool()
            if corrupt:
                spool.path.write_bytes(b"not sqlite")
            else:
                spool.path.unlink()
            import sqlite3
            with self.assertRaises(sqlite3.DatabaseError):
                tuple(spool.iter_rows())

    def test_mid_batch_cancellation_rolls_back_rows_users_and_sequence_for_retry(self):
        spool = self.spool()
        original = {"user": "kept", "text": "committed"}
        spool.append_batch([encode_row(original)])
        pending = [{"user": "new", "text": "first"}, {"user": "other", "text": "second"}]
        checks = iter((False, True))

        with self.assertRaisesRegex(RuntimeError, "отменена"):
            spool.append_batch(map(encode_row, pending), cancel_check=lambda: next(checks))

        self.assertEqual((1, 1), (spool.messages, spool.unique_users))
        self.assertEqual([original], list(spool.iter_rows()))
        # A rolled-back username must be inserted on retry, and seq must have no gap.
        spool.append_batch(map(encode_row, pending))
        self.assertEqual((3, 3), (spool.messages, spool.unique_users))
        self.assertEqual([original, *pending], list(spool.iter_rows()))

    def test_commit_failure_rolls_back_spool_and_allows_same_batch_retry(self):
        spool = self.spool()
        original = {"user": "kept", "text": "committed"}
        spool.append_batch([encode_row(original)])
        pending = [{"user": "new", "text": "Привет 🎮"}, {"user": "kept", "text": "again"}]
        connection = spool._connect()
        self.addCleanup(connection.close)

        def deny_commit(action, argument, *_):
            if action == sqlite3.SQLITE_TRANSACTION and argument == "COMMIT":
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        # Let real SQLite execute every INSERT, then fail the transaction boundary.
        connection.set_authorizer(deny_commit)
        with patch.object(spool, "_connect", return_value=connection):
            with self.assertRaises(sqlite3.DatabaseError):
                spool.append_batch(map(encode_row, pending))

        self.assertEqual((1, 1), (spool.messages, spool.unique_users))
        self.assertEqual([original], list(spool.iter_rows()))
        spool.append_batch(map(encode_row, pending))
        self.assertEqual((3, 2), (spool.messages, spool.unique_users))
        self.assertEqual([original, *pending], list(spool.iter_rows()))

    def test_missing_sequence_or_truncated_spool_is_not_accepted_as_complete(self):
        import sqlite3

        for deleted_seq in (1, 2, 3):
            with self.subTest(deleted_seq=deleted_seq):
                spool = self.spool()
                spool.append_batch(encode_row(row) for row in rows(3))
                con = sqlite3.connect(spool.path)
                try:
                    con.execute("DELETE FROM messages WHERE seq=?", (deleted_seq,))
                    con.commit()
                finally:
                    con.close()
                with self.assertRaises(sqlite3.DatabaseError):
                    tuple(spool.iter_rows())

    def test_stream_matches_old_html_byte_for_byte_and_json_round_trip(self):
        for count in (0, 1, 3, 12):
            with self.subTest(count=count):
                spool = self.spool()
                spool.append_batch(encode_row(row) for row in rows(count))
                options = dict(title="</ScRiPt> 🎮", channel="стример", vod_url="https://test.invalid/?a=1&b=2",
                               created_at="2026-01-01T00:00:00Z", thumbnail_preview_url="preview",
                               thumbnail_full_url="full", channel_id="id", local_emotes={"x": "local"},
                               cdn_emotes={"Kappa": "https://cdn.invalid/a&b"}, badge_images={"subscriber/1": "badge"})
                output = io.StringIO()
                write_viewer_html(output, spool.iter_rows(), **options)
                html = output.getvalue()
                self.assertEqual(render_viewer_html(tuple(rows(count)), **options), html)
                parser = ChatDataParser()
                parser.feed(html)
                self.assertEqual(1, len(parser.blocks))
                self.assertNotIn("<", parser.blocks[0])
                self.assertEqual(tuple(rows(count)), tuple(json.loads(parser.blocks[0])["rows"]))
                self.assertEqual(options["title"], json.loads(parser.blocks[0])["meta"]["title"])

    def test_stream_never_collects_rows_or_queries_length(self):
        live = weakref.WeakValueDictionary()

        class Row(dict):
            pass

        class Source:
            def __len__(self):
                raise AssertionError("must not materialize rows")

            def __iter__(self):
                for i in range(200):
                    row = Row(user=str(i), text="row")
                    live[i] = row
                    if len(live) > 2:
                        raise AssertionError("rows retained in memory")
                    yield row

        write_viewer_html(io.StringIO(), Source(), "title", "channel", "vod")

    def test_stream_short_write_and_cancel_close_input_cursor(self):
        closed = []

        def source():
            try:
                yield {"text": "x"}
                yield {"text": "y"}
            finally:
                closed.append(True)

        iterator = source()
        next(iterator)
        with self.assertRaisesRegex(RuntimeError, "отменена"):
            write_viewer_html(io.StringIO(), iterator, "t", "c", "v", cancel_check=lambda: True)
        self.assertEqual([True], closed)

        class Output:
            def write(self, text):
                return 0

        iterator = source()
        next(iterator)
        with self.assertRaises(OSError):
            write_viewer_html(Output(), iterator, "t", "c", "v")
        self.assertEqual([True, True], closed)

    def test_stream_publication_both_modes_keep_existing_url_format(self):
        for local in (True, False):
            with patch.object(html_publisher, "OUT_DIR", str(self.root)), \
                 patch.object(html_publisher, "PUBLIC_HTML_DIR", str(self.root)), \
                 patch.object(html_publisher, "PUBLIC_BASE_URL", "https://test.invalid"), \
                 patch.object(html_publisher, "PUBLIC_HTML_URL_PREFIX", "/public_html"):
                ref = html_publisher.publish_html_stream(
                    lambda output: write_viewer_html(output, rows(2), "t", "c", "v"), local=local,
                )
                filename = ref.split(":" if local else "/")[-1]
                self.assertRegex(filename, r"^chat_[0-9a-f]{32}\.html$")
                self.assertTrue((self.root / filename).is_file())
                if not local:
                    self.assertTrue(ref.startswith("https://test.invalid/public_html/"))

    def test_failed_stream_never_replaces_old_html_or_leaves_temp(self):
        old = self.root / ("chat_" + "a" * 32 + ".html")
        old.write_text("old result", encoding="utf-8")

        def fail(output):
            output.write("partial")
            raise OSError(errno.ENOSPC, "test disk full")

        with patch.object(html_publisher, "OUT_DIR", str(self.root)):
            for callback in (fail, lambda output: write_viewer_html(output, [{"bad": object()}], "t", "c", "v")):
                with self.assertRaises((OSError, TypeError)):
                    html_publisher.publish_html_stream(callback, local=True)
        self.assertEqual("old result", old.read_text(encoding="utf-8"))
        self.assertEqual([old], list(self.root.iterdir()))

    def test_restart_cleanup_skips_active_unknown_and_ready_files(self):
        spool = self.spool()
        root = spool.directory.parent
        self.assertEqual(0, cleanup_orphaned_chat_spools(root))
        spool._lease.close()  # Simulate process exit: OS releases its lease.
        spool._lease = None
        self.assertEqual(1, cleanup_orphaned_chat_spools(root))
        spool._created = False
        unknown = root / ("tdb-spool-" + "b" * 32)
        unknown.mkdir()
        (unknown / "owner.lock").touch()
        (unknown / "ready.html").write_text("keep", encoding="utf-8")
        database = root / "vod_history.db"
        database.write_text("keep", encoding="utf-8")
        self.assertEqual(0, cleanup_orphaned_chat_spools(root))
        self.assertTrue((unknown / "ready.html").exists())
        self.assertTrue(database.exists())

    def test_cleanup_handles_missing_roots_and_memory_metrics_are_portable(self):
        self.assertEqual(0, cleanup_orphaned_chat_spools(self.root / "missing"))
        metrics = memory_usage()
        self.assertIn("rss_bytes", metrics)
        self.assertIn("process_peak_rss_bytes", metrics)

    def test_process_exit_releases_lease_and_restart_removes_only_owned_spool(self):
        root = self.root / "crash"
        code = (
            "import os,sys; from src.chat_spool import ChatSpool,encode_row; "
            "spool=ChatSpool(sys.argv[1]); spool.open(); "
            "spool.append_batch([encode_row({'user':'viewer','text':'x'})]); os._exit(0)"
        )
        subprocess.run([sys.executable, "-B", "-c", code, str(root)],
                       cwd=Path(__file__).resolve().parents[1], check=True, timeout=10)
        self.assertEqual(1, len(tuple(root.iterdir())))
        self.assertEqual(1, cleanup_orphaned_chat_spools(root))
        self.assertFalse(tuple(root.iterdir()))

    def test_linux_rss_peak_and_swap_parsing_and_missing_proc(self):
        status = "Name:\tpython\nVmRSS:\t123 kB\nVmHWM:\t456 kB\nVmSwap:\t7 kB\n"
        with patch.object(resource_usage, "os", SimpleNamespace(name="posix")), \
             patch.object(resource_usage, "Path") as path:
            path.return_value.read_text.return_value = status
            self.assertEqual({"rss_bytes": 123 * 1024, "process_peak_rss_bytes": 456 * 1024,
                              "swap_bytes": 7 * 1024}, memory_usage())
            path.return_value.read_text.side_effect = FileNotFoundError()
            self.assertIsNone(memory_usage()["rss_bytes"])

    def test_spool_settings_are_project_relative_and_positive(self):
        config_file = Path(__file__).resolve().parents[1] / "src/config.py"
        with patch.dict(os.environ, {"TCD_SPOOL_DIR": "scratch", "TCD_SPOOL_BATCH_MESSAGES": "2",
                                     "TCD_SPOOL_BATCH_BYTES": "512"}):
            settings = runpy.run_path(str(config_file))
            self.assertEqual(str(config_file.parent.parent / "scratch"), settings["SPOOL_DIR"])
            self.assertEqual(2, settings["SPOOL_BATCH_MESSAGES"])
            self.assertEqual(512, settings["SPOOL_BATCH_BYTES"])
            os.environ["TCD_SPOOL_BATCH_BYTES"] = "0"
            with self.assertRaisesRegex(RuntimeError, "TCD_SPOOL_BATCH_BYTES"):
                runpy.run_path(str(config_file))


if __name__ == "__main__":
    unittest.main()
