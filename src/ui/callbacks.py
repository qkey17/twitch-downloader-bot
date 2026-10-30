"""One bounded acknowledgement per handler invocation; never log callback data."""
import asyncio
import logging
from telegram.error import TelegramError
from .ui_labels import STALE_ACTION_NOTICE, STALE_LIST_NOTICE

# Only retired/malformed variants of these established routes. New/unknown
# prefixes must not be silently treated as stale by a global catch-all.
STALE_CALLBACK_PATTERN = r'^ui:(?:histpage|histfiles|histvod|pending_cancel|format_cancel)(?::|$)'


class CallbackReply:
    def __init__(self, query):
        self.query, self.attempted, self.ok = query, False, False

    async def send(self, text=None):
        if self.attempted:
            return self.ok
        self.attempted = True  # Includes failures, timeout and coroutine cancellation.
        try:
            async with asyncio.timeout(3):
                if text is None:
                    await self.query.answer()
                elif text == STALE_LIST_NOTICE:
                    await self.query.answer(text, show_alert=True)
                else:
                    await self.query.answer(text)
            self.ok = True
        except (TelegramError, TimeoutError):
            try:
                logging.getLogger(__name__).warning('callback_answer_failed')
            except Exception:
                pass
        return self.ok


async def stale_callback(update, context):
    await CallbackReply(update.callback_query).send(STALE_ACTION_NOTICE)
