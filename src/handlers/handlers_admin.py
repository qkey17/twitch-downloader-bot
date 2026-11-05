"""Owner-only management of delegated /allvods access."""

import asyncio
import logging
import sqlite3

from src import config
from src.admin_access import role, sync_admin_menu
from src.admin_batch import KEY, stop_batch
from src.database import db_privileges

_UNAVAILABLE = "Команда недоступна"
_STORE_UNAVAILABLE = "Не удалось обновить права доступа"


async def admin_command(update, context):
    access = role(getattr(update.effective_user, "id", None))
    if access is None:
        await update.effective_message.reply_text(_UNAVAILABLE)
        return
    if context.args:
        await update.effective_message.reply_text("/admin")
        return
    text = (
        "<b>Обработка VODs</b>\n"
        "/allvods «канал» — Обработать все VODs канала\n"
        "/newvods «канал» — Обработать VODs без готового HTML"
    )
    if access == 'owner':
        text += (
            "\n\n<b>Управление доступом</b>\n"
            "/adminadd «ID пользователя» — Предоставить доступ\n"
            "/adminremove «ID пользователя» — Отозвать доступ\n"
            "/adminlist — Показать список пользователей с доступом"
        )
    await update.effective_message.reply_text(text, parse_mode='HTML')


def _private_owner(update):
    owner_id = config.get_admin_user_id()
    user_id = getattr(update.effective_user, "id", None)
    if (owner_id is not None and type(user_id) is int and user_id == owner_id
            and getattr(update.effective_chat, "type", None) == "private"):
        return owner_id
    return None


async def _target(update, context, command):
    message = update.effective_message
    owner_id = _private_owner(update)
    if owner_id is None:
        await message.reply_text(_UNAVAILABLE)
        return None
    args = context.args or []
    user_id = config.parse_telegram_user_id(args[0]) if len(args) == 1 else None
    if user_id is None:
        await message.reply_text(f"/{command} «ID пользователя»")
        return None
    if user_id == owner_id:
        await message.reply_text("Права владельца задаются в конфигурации")
        return None
    return user_id


async def adminadd_command(update, context):
    user_id = await _target(update, context, "adminadd")
    if user_id is None:
        return
    try:
        added = db_privileges.grant(user_id)
    except (sqlite3.Error, OSError):
        logging.getLogger(__name__).warning("Could not grant delegated access")
        await update.effective_message.reply_text(_STORE_UNAVAILABLE)
        return
    await sync_admin_menu(context, user_id, force=True)
    text = "Доступ предоставлен" if added else "Доступ уже предоставлен"
    await update.effective_message.reply_text(text)


async def adminremove_command(update, context):
    user_id = await _target(update, context, "adminremove")
    if user_id is None:
        return
    message = update.effective_message
    try:
        removed = db_privileges.revoke(user_id)
    except (sqlite3.Error, OSError):
        logging.getLogger(__name__).warning("Could not revoke delegated access")
        await message.reply_text(_STORE_UNAVAILABLE)
        return

    app = context.application
    batch = app.bot_data.get(KEY)
    # Fence the batch immediately after the transaction commits. A repeated
    # removal also waits for the same cleanup when the grant is already gone.
    task = stop_batch(app, user_id)
    await sync_admin_menu(context, user_id, force=True)
    if task is not None:
        result, = await asyncio.gather(asyncio.shield(task), return_exceptions=True)
        # Cancellation before run_batch starts has no worker to clean up.
        failed = isinstance(result, BaseException) and not isinstance(result, asyncio.CancelledError)
        if failed or batch.cleanup_failed:
            logging.getLogger(__name__).warning("Revoked batch cleanup not confirmed")
            await message.reply_text("Доступ отозван. Не удалось подтвердить остановку массовой обработки")
            return
    await message.reply_text("Доступ отозван" if removed else "У пользователя нет делегированного доступа")


async def adminlist_command(update, context):
    message = update.effective_message
    owner_id = _private_owner(update)
    if owner_id is None:
        await message.reply_text(_UNAVAILABLE)
        return
    if context.args:
        await message.reply_text("/adminlist")
        return
    try:
        users = [user_id for user_id in db_privileges.list_users() if user_id != owner_id]
    except (sqlite3.Error, OSError):
        logging.getLogger(__name__).warning("Could not list delegated access")
        await message.reply_text("Не удалось получить список прав")
        return
    if not users:
        await message.reply_text("Список привилегированных пользователей пуст")
        return
    # Each numeric line is at most 20 characters including its newline.
    for start in range(0, len(users), 180):
        ids = "\n".join(str(user_id) for user_id in users[start:start + 180])
        await message.reply_text(f"Привилегированные пользователи:\n{ids}")
