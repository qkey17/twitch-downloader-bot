"""Owner-only management of Premium access."""

import asyncio
import logging
import sqlite3

from src import config
from src.admin_access import PREMIUM_SYNTAX
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
    action = args[0].lower() if args else None
    if action not in PREMIUM_SYNTAX:
        await message.reply_text("\n".join(PREMIUM_SYNTAX.values()))
        return
    if action == 'list':
        if len(args) == 1:
            await _list(message, owner_id)
        else:
            await message.reply_text(PREMIUM_SYNTAX[action])
        return
    user_id = config.parse_telegram_user_id(args[1]) if len(args) == 2 else None
    if user_id is None:
        await message.reply_text(PREMIUM_SYNTAX[action])
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


async def _list(message, owner_id):
    try:
        users = [user_id for user_id in db_privileges.list_users() if user_id != owner_id]
    except (sqlite3.Error, OSError):
        logging.getLogger(__name__).warning("Could not list delegated access")
        await message.reply_text("Не удалось получить список Premium")
        return
    if not users:
        await message.reply_text("Список Premium пуст")
        return
    # Each numeric line is at most 20 characters including its newline.
    for start in range(0, len(users), 180):
        ids = "\n".join(str(user_id) for user_id in users[start:start + 180])
        await message.reply_text(f"Пользователи с Premium:\n{ids}")
