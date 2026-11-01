"""Internal Cloud-only ready HTML sender. No UI, retries, queue or VOD slot."""
import asyncio
from dataclasses import dataclass
from enum import Enum
import sqlite3
import threading

from telegram import InputFile
from telegram.error import BadRequest, NetworkError, TelegramError, TimedOut

from src.maintenance import admission_closed
from src.ready_html import HtmlTooLarge, HtmlUnavailable, ReadyHtml
from src.workers.worker_io import run_blocking


SENDER_KEY = 'tdb_html_sender'
SEND_DEADLINE = 300


class SendStatus(str, Enum):
    SENT = 'sent'
    UNAVAILABLE = 'unavailable'
    TOO_LARGE = 'too_large'
    USER_BUSY = 'user_busy'
    SENDER_BUSY = 'sender_busy'
    ADMISSION_CLOSED = 'admission_closed'
    TELEGRAM_ERROR = 'telegram_error'
    TIMEOUT = 'timeout'
    CANCELLED = 'cancelled'
    SHUTDOWN = 'shutdown'


@dataclass(frozen=True)
class SendResult:
    status: SendStatus
    # True means delivery MAY have happened. Never automatically retry.
    uncertain: bool = False


async def _drain(task):
    """Join owned work even if its waiter is cancelled more than once."""
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
    return task.result()


class HtmlSender:
    def __init__(self, application):
        self.application = application
        self.tasks = set()
        self.users = {}
        self.closing = False
        self._close_task = None

    @property
    def active(self):
        return bool(self.tasks)

    async def send(self, user_id, chat_id, vod_id, *, generation_id=None):
        """Trusted internal API; future UI must authorize user/chat/result first.

        Current generation is selected once, or an exact historical ready ID may
        be supplied. No arbitrary paths/URLs are accepted. Cancellation is a
        structured result, returned only after owned I/O has actually finished.
        """
        if (self.closing or not self.application.running
                or admission_closed(self.application)):
            return SendResult(SendStatus.ADMISSION_CLOSED)
        if user_id in self.users:
            return SendResult(SendStatus.USER_BUSY)
        if len(self.tasks) >= 2:
            return SendResult(SendStatus.SENDER_BUSY)
        # Reservation and task registration are atomic with maintenance.prepare.
        reservation = object()
        self.users[user_id] = reservation
        operation = self._run(chat_id, vod_id, generation_id)
        try:
            task = asyncio.create_task(operation, name='tdb-html-upload')
        except BaseException:
            operation.close()
            self.users.pop(user_id, None)
            raise
        self.tasks.add(task)

        def finished(_task):
            self.tasks.discard(_task)
            if self.users.get(user_id) is reservation:
                self.users.pop(user_id)

        task.add_done_callback(finished)
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            if not task.cancelling():
                task.cancel()
            try:
                return await _drain(task)
            except asyncio.CancelledError:  # Cancelled before its first instruction.
                return SendResult(SendStatus.SHUTDOWN if self.closing else SendStatus.CANCELLED)
        finally:
            if task.done():
                finished(task)

    async def _run(self, chat_id, vod_id, generation_id):
        owned = None
        stop = threading.Event()
        submitted = False
        try:
            async with asyncio.timeout(SEND_DEADLINE):
                owned = ReadyHtml(vod_id, generation_id)
                await run_blocking(owned.open, stop, on_cancel=stop.set)
                # There are no blocking worker reads after this await. PTB/HTTPX
                # read chunks within this task; cancellation unwinds that request
                # before the handle/lease are closed below.
                submitted = True
                await self.application.bot.send_document(
                    chat_id=chat_id,
                    document=InputFile(owned.handle, filename=owned.document_filename, read_file_handle=False),
                    connect_timeout=5, pool_timeout=2, write_timeout=60, read_timeout=30,
                )
                return SendResult(SendStatus.SENT)
        except HtmlTooLarge:
            return SendResult(SendStatus.TOO_LARGE)
        except (TimeoutError, TimedOut):
            return SendResult(SendStatus.TIMEOUT, uncertain=submitted)
        except (HtmlUnavailable, OSError, ValueError, TypeError, RuntimeError, sqlite3.Error):
            return SendResult(SendStatus.UNAVAILABLE, uncertain=submitted)
        except asyncio.CancelledError:
            return SendResult(SendStatus.SHUTDOWN if self.closing else SendStatus.CANCELLED,
                              uncertain=submitted)
        except BadRequest:
            return SendResult(SendStatus.TELEGRAM_ERROR)
        except NetworkError:
            return SendResult(SendStatus.TELEGRAM_ERROR, uncertain=submitted)
        except TelegramError:
            return SendResult(SendStatus.TELEGRAM_ERROR)
        finally:
            stop.set()
            if owned is not None:
                owned.close()

    async def close(self):
        # These tasks deliberately do NOT use Application.create_task: PTB.stop
        # waits for those before post_stop can cancel uploads.
        self.closing = True
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._close(), name='tdb-html-sender-close')
        await _drain(self._close_task)

    async def _close(self):
        tasks = tuple(self.tasks)
        for task in tasks:
            if not task.cancelling():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


def start_sender(app):
    app.bot_data[SENDER_KEY] = HtmlSender(app)


async def stop_sender(app):
    sender = app.bot_data.get(SENDER_KEY)
    if sender is not None:
        await sender.close()
