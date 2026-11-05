"""Privilege checks and cleanup of legacy private Telegram menus."""

import asyncio
from collections import OrderedDict
import logging
import sqlite3
import time

from telegram import BotCommand, BotCommandScopeChat
from telegram.error import RetryAfter, TelegramError

from src import config
from src.database import db_privileges

PREMIUM_SYNTAX = {
    'add': '/premium «ID» add',
    'remove': '/premium «ID» remove',
    'list': '/premium list',
}
PREMIUM_HELP = {
    'add': 'Предоставить Premium',
    'remove': 'Отозвать Premium',
    'list': 'Показать пользователей с Premium',
}


def premium_instructions():
    return '\n'.join(f'{syntax} — {PREMIUM_HELP[action]}' for action, syntax in PREMIUM_SYNTAX.items())


def role(user_id):
    owner = config.get_admin_user_id()
    if owner is None or type(user_id) is not int or not 0 < user_id <= 2**63 - 1:
        return None
    if user_id == owner:
        return 'owner'
    try:
        return 'delegate' if db_privileges.has_access(user_id) else None
    except (sqlite3.Error, OSError):
        logging.getLogger(__name__).warning('Delegated access check unavailable')
        return None


def public_commands():
    return [BotCommand('help', 'Справка')]


class LegacyMenus:
    def __init__(self):
        self.locks = [asyncio.Lock() for _ in range(16)]
        self.recent = OrderedDict()
        self.retry_at = 0

    async def clear(self, bot, user_id):
        lock = self.locks[user_id % len(self.locks)]
        if lock.locked() or time.monotonic() < self.retry_at:
            return  # A later private interaction can retry without a waiter backlog.
        async with lock:
            if time.monotonic() < self.recent.get(user_id, 0):
                return
            scope = BotCommandScopeChat(user_id)
            retry_after = float('inf')
            try:
                async with asyncio.timeout(5):
                    if not await bot.delete_my_commands(scope=scope):
                        retry_after = 5
            except RetryAfter as exc:
                retry_after = max(5, float(exc.retry_after))
                self.retry_at = time.monotonic() + retry_after
            except (TelegramError, OSError, TimeoutError):
                logging.getLogger(__name__).warning('Legacy private menu cleanup unavailable')
                retry_after = 5
            self.recent[user_id] = time.monotonic() + retry_after
            self.recent.move_to_end(user_id)
            if len(self.recent) > 512:
                self.recent.popitem(last=False)


def _menus(app):
    menus = app.bot_data.get('tdb_legacy_menus')
    if menus is None:
        menus = app.bot_data['tdb_legacy_menus'] = LegacyMenus()
    return menus


async def clear_private_menu(update, context):
    chat = update.effective_chat
    user_id = getattr(update.effective_user, 'id', None)
    if getattr(chat, 'type', None) == 'private' and type(user_id) is int and chat.id == user_id:
        await _menus(context.application).clear(context.bot, user_id)


async def clear_known_menus(app):
    owner = config.get_admin_user_id()
    try:
        known = db_privileges.list_users()
    except (sqlite3.Error, OSError):
        logging.getLogger(__name__).warning('Could not list legacy private menu scopes')
        known = []
    if owner is not None:
        known = [owner, *(user_id for user_id in known if user_id != owner)]
    menus = _menus(app)
    for user_id in known:
        if time.monotonic() < menus.retry_at:
            break  # Avoid further requests while Telegram rate-limits this sweep.
        await menus.clear(app.bot, user_id)


def start_menu_cleanup(app):
    app.bot_data['tdb_menu_cleanup_task'] = asyncio.create_task(clear_known_menus(app))


async def stop_menu_cleanup(app):
    task = app.bot_data.pop('tdb_menu_cleanup_task', None)
    if task is not None:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
