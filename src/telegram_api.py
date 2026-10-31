"""Optional fixed loopback endpoint; no login/migration or automatic fallback."""
from contextlib import contextmanager
import logging
from urllib.parse import quote

from telegram.error import TelegramError

from .config import ConfigurationError

LOCAL_BASE_URL = 'http://127.0.0.1:8081/bot'
LOCAL_FILE_URL = 'http://127.0.0.1:8081/file/bot'


class _RedactingFormatter(logging.Formatter):
    def __init__(self, original, token):
        super().__init__()
        self.original = original or logging.Formatter()
        self.secrets = (token, quote(token, safe=''))

    def format(self, record):
        # Redact the final rendering, including exception chains and % arguments.
        text = self.original.format(record)
        for secret in self.secrets:
            if secret:
                text = text.replace(secret, '[redacted]')
        return text


@contextmanager
def _local_diagnostics(token):
    # main.setup_logging installs the root handlers before constructing the bot.
    handlers = [(handler, handler.formatter) for handler in logging.getLogger().handlers]
    for handler, formatter in handlers:
        handler.setFormatter(_RedactingFormatter(formatter, token))
    try:
        yield
    except TelegramError:
        raise ConfigurationError(
            'Local Telegram Bot API failed at 127.0.0.1:8081; '
            'check service readiness and bot authorization. No cloud fallback was attempted.'
        ) from None
    finally:
        for handler, formatter in handlers:
            handler.setFormatter(formatter)


def build_application(builder, mode, token):
    if mode == 'cloud':
        return builder.build()
    if mode != 'local':
        raise ConfigurationError('Invalid Telegram API mode')
    with _local_diagnostics(token):
        return (builder.base_url(LOCAL_BASE_URL).base_file_url(LOCAL_FILE_URL)
                .local_mode(True).build())


def run_polling(app, mode, token):
    if mode == 'cloud':
        return app.run_polling(drop_pending_updates=True)
    if mode != 'local':
        raise ConfigurationError('Invalid Telegram API mode')
    with _local_diagnostics(token):
        return app.run_polling(drop_pending_updates=True)
