import io
import logging
import unittest
from unittest.mock import patch

from src import log_setup


class LoggingSetupTestCase(unittest.TestCase):
    def test_http_client_info_is_suppressed_and_application_info_is_preserved(self):
        root = logging.getLogger()
        client_loggers = [logging.getLogger(name) for name in ("httpx", "httpcore")]
        old_root_level = root.level
        old_client_levels = [logger.level for logger in client_loggers]
        output = io.StringIO()
        try:
            with patch.object(root, "handlers", []), \
                 patch.object(log_setup, "LOG_LEVEL", "INFO"), \
                 patch("sys.stderr", output):
                log_setup.setup_logging()
                for handler in root.handlers:
                    self.addCleanup(handler.close)

                self.assertEqual(logging.INFO, root.level)
                for logger in client_loggers:
                    self.assertEqual(logging.WARNING, logger.level)
                    logger.info("https://api.telegram.org/botTEST_ONLY_TOKEN/getUpdates")
                    logger.debug("HTTP client debug details")
                    logger.warning("HTTP client warning")

                # Use a child with no explicit level to check application inheritance.
                application_logger = logging.getLogger("tcd.logging_setup_test")
                application_logger.info("Application INFO remains visible")

            messages = output.getvalue()
            self.assertIn("Application INFO remains visible", messages)
            self.assertEqual(2, messages.count("HTTP client warning"))
            self.assertNotIn("api.telegram.org", messages)
            self.assertNotIn("TEST_ONLY_TOKEN", messages)
            self.assertNotIn("HTTP client debug details", messages)
        finally:
            root.setLevel(old_root_level)
            for logger, level in zip(client_loggers, old_client_levels):
                logger.setLevel(level)


if __name__ == "__main__":
    unittest.main()
