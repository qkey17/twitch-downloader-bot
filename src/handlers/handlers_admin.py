"""Owner-only management of Premium access."""

import asyncio
import html
import logging
import re
import sqlite3
import time
from datetime import timedelta

from telegram import LinkPreviewOptions
from telegram.error import RetryAfter, TelegramError

from src import config
from src.admin_access import premium_instructions
from src.admin_batch import KEY, stop_batch
from src.database import db_privileges

_UNAVAILABLE = "Команда недоступна"
_STORE_UNAVAILABLE = "Не удалось обновить Premium"


def _private_owner(update):
    owner_id = config.get_admin_user_id()
    user_id = getattr(update.effective_user, "id", None)
    if (owner_id is not None and type(user_id) is int and user_id == owner_id
            and getattr(update.effective_chat, "type", None) == "private"):
        return owner_id
    return None


async def premium_command(update, context):
    message = update.effective_message
    owner_id = _private_owner(update)
    if owner_id is None:
        await message.reply_text(_UNAVAILABLE)
        return
    args = context.args or []
    if len(args) == 1 and args[0].lower() == 'list':
        await _list(message, context, owner_id)
        return
    user_id = config.parse_telegram_user_id(args[0]) if len(args) == 2 else None
    action = args[1].lower() if len(args) == 2 else None
    if user_id is None or action not in ('add', 'remove'):
        await message.reply_text(premium_instructions())
        return
    if user_id == owner_id:
        await message.reply_text("Права владельца задаются в конфигурации")
        return
    if action == 'add':
        await _grant(message, user_id)
    else:
        await _revoke(message, context.application, user_id)


async def _grant(message, user_id):
    try:
        added = db_privileges.grant(user_id)
    except (sqlite3.Error, OSError):
        logging.getLogger(__name__).warning("Could not grant delegated access")
        await message.reply_text(_STORE_UNAVAILABLE)
        return
    await message.reply_text("Premium предоставлен" if added else "Premium уже предоставлен")


async def _revoke(message, app, user_id):
    try:
        removed = db_privileges.revoke(user_id)
    except (sqlite3.Error, OSError):
        logging.getLogger(__name__).warning("Could not revoke delegated access")
        await message.reply_text(_STORE_UNAVAILABLE)
        return

    batch = app.bot_data.get(KEY)
    # Fence the batch immediately after the transaction commits. A repeated
    # removal also waits for the same cleanup when the grant is already gone.
    task = stop_batch(app, user_id)
    if task is not None:
        result, = await asyncio.gather(asyncio.shield(task), return_exceptions=True)
        # Cancellation before run_batch starts has no worker to clean up.
        failed = isinstance(result, BaseException) and not isinstance(result, asyncio.CancelledError)
        if failed or batch.cleanup_failed:
            logging.getLogger(__name__).warning("Revoked batch cleanup not confirmed")
            await message.reply_text("Premium отозван. Не удалось подтвердить остановку массовой обработки")
            return
    await message.reply_text("Premium отозван" if removed else "У пользователя нет Premium")


async def _usernames(context, users):
    # Profile access by numeric ID depends on Telegram. A single bounded sweep
    # per bot can enrich the list without making the IDs depend on the API.
    state = context.application.bot_data
    lock = state.setdefault('tdb_premium_profile_lock', asyncio.Lock())
    if lock.locked() or time.monotonic() < state.get('tdb_premium_profile_retry_at', 0):
        return {}
    usernames = {}
    async with lock:
        try:
            async with asyncio.timeout(3):
                for user_id in users[:10]:
                    try:
                        chat = await context.bot.get_chat(user_id)
                    except RetryAfter as exc:
                        delay = exc.retry_after
                        seconds = delay.total_seconds() if isinstance(delay, timedelta) else float(delay)
                        state['tdb_premium_profile_retry_at'] = time.monotonic() + seconds
                        break
                    except (TelegramError, OSError):
                        continue
                    username = getattr(chat, 'username', None)
                    if (getattr(chat, 'type', None) == 'private' and getattr(chat, 'id', None) == user_id
                            and isinstance(username, str) and re.fullmatch(r'[A-Za-z0-9_]{1,32}', username)):
                        usernames[user_id] = html.escape(username, quote=True)
        except TimeoutError:
            pass
    return usernames


async def _list(message, context, owner_id):
    try:
        users = [user_id for user_id in db_privileges.list_users() if user_id != owner_id]
    except (sqlite3.Error, OSError):
        logging.getLogger(__name__).warning("Could not list delegated access")
        await message.reply_text("Не удалось получить список Premium")
        return
    if not users:
        await message.reply_text("Список Premium пуст")
        return
    usernames = await _usernames(context, users)
    for start in range(0, len(users), 60):
        rows = ["Пользователи с Premium:"]
        for user_id in users[start:start + 60]:
            username = usernames.get(user_id)
            profile = f' · <a href="https://t.me/{username}">@{username}</a>' if username else ''
            rows.append(f'• ID: {user_id}{profile}')
        await message.reply_text("\n".join(rows), parse_mode='HTML',
                                 link_preview_options=LinkPreviewOptions(is_disabled=True))
