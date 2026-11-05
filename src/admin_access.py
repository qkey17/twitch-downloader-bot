"""Admin authorization and best-effort private Telegram menu synchronization."""

import asyncio
from collections import OrderedDict
import logging
import sqlite3
import time

from telegram import BotCommand, BotCommandScopeChat
from telegram.error import RetryAfter, TelegramError

from src import config
from src.database import db_privileges


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


class AdminMenus:
    def __init__(self):
        self.locks = [asyncio.Lock() for _ in range(16)]
        self.recent = OrderedDict()

    async def sync(self, bot, user_id, *, force=False):
        lock = self.locks[user_id % len(self.locks)]
        if not force and lock.locked():
            return  # A later private interaction can retry without a waiter backlog.
        async with lock:
            previous = self.recent.get(user_id)
            if not force and previous and time.monotonic() < previous:
                return
            # Re-read after waiting so a delayed grant cannot undo a revocation.
            allowed = role(user_id) is not None
            scope = BotCommandScopeChat(user_id)
            retry_after = 300
            try:
                async with asyncio.timeout(5):
                    if allowed:
                        await bot.set_my_commands(public_commands() + [
                            BotCommand('admin', 'Команды администратора'),
                        ], scope=scope)
                    else:
                        await bot.delete_my_commands(scope=scope)
            except RetryAfter as exc:
                retry_after = max(5, float(exc.retry_after))
            except (TelegramError, OSError, TimeoutError):
                logging.getLogger(__name__).warning('Private admin menu synchronization unavailable')
                # Bound failed retries. A role change bypasses this cooldown.
                retry_after = 5
            self.recent[user_id] = time.monotonic() + retry_after
            self.recent.move_to_end(user_id)
            if len(self.recent) > 512:
                self.recent.popitem(last=False)


async def sync_admin_menu(context, user_id, *, force=False):
    bot = getattr(context, 'bot', None)
    if bot is None:
        return
    data = context.application.bot_data
    menus = data.get('tdb_admin_menus')
    if menus is None:
        menus = data['tdb_admin_menus'] = AdminMenus()
    await menus.sync(bot, user_id, force=force)


async def refresh_private_menu(update, context):
    chat = update.effective_chat
    user_id = getattr(update.effective_user, 'id', None)
    if getattr(chat, 'type', None) == 'private' and type(user_id) is int and chat.id == user_id:
        await sync_admin_menu(context, user_id)
