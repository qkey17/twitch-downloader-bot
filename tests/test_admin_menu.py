"""Individual Telegram scopes and role-aware administrative help."""

import asyncio
import os
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, patch

from telegram.error import Forbidden, NetworkError

from src import admin_access, admin_batch
from src.database import db_privileges
from src.handlers import handlers_admin
from test_delegated_admin import invocation, isolated_privileges


class AdminMenuTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        isolated_privileges(self)
        self.app = NS(bot_data={})
        self.bot = NS(set_my_commands=AsyncMock(), delete_my_commands=AsyncMock())

    async def refresh(self, user, *, chat_type='private'):
        update = NS(effective_chat=NS(id=user, type=chat_type), effective_user=NS(id=user))
        await admin_access.refresh_private_menu(update, NS(bot=self.bot, application=self.app))

    async def call(self, command, user, args):
        update, context = invocation(self.app, user, args)
        context.bot = self.bot
        await command(update, context)
        return update.effective_message.reply_text.await_args.args[0]

    async def test_help_is_role_aware_and_denied_users_cannot_run_either_mode(self):
        db_privileges.grant(9)
        owner = await self.call(handlers_admin.admin_command, 7, [])
        delegate = await self.call(handlers_admin.admin_command, 9, [])
        self.assertIn('Управление доступом', owner)
        self.assertIn('/adminadd «ID пользователя»', owner)
        self.assertIn('Обработка VODs', delegate)
        self.assertIn('/allvods «канал»', delegate)
        self.assertIn('/newvods «канал»', delegate)
        self.assertNotIn('/adminadd', delegate)
        self.assertEqual('/admin', await self.call(handlers_admin.admin_command, 9, ['extra']))
        for command in (handlers_admin.admin_command, admin_batch.allvods_command, admin_batch.newvods_command):
            self.assertEqual('Команда недоступна', await self.call(command, 8, ['channel']))
            with patch.dict(os.environ, {'TCD_ADMIN_USER_ID': 'invalid'}):
                self.assertEqual('Команда недоступна', await self.call(command, 9, ['channel']))
        self.assertEqual({}, self.app.bot_data)

    async def test_private_scopes_preserve_help_and_restart_reads_saved_delegates(self):
        db_privileges.grant(9)
        await self.refresh(7)
        await self.refresh(9)
        await self.refresh(8)
        await self.refresh(9, chat_type='group')
        for call, user in zip(self.bot.set_my_commands.await_args_list, (7, 9), strict=True):
            self.assertEqual(user, call.kwargs['scope'].chat_id)
            self.assertEqual(['help', 'admin'], [entry.command for entry in call.args[0]])
            self.assertEqual('Команды администратора', call.args[0][1].description)
        self.assertEqual(8, self.bot.delete_my_commands.await_args.kwargs['scope'].chat_id)
        self.app.bot_data.clear()  # Simulate restart with the same DB.
        await self.refresh(9)
        self.assertEqual(9, self.bot.set_my_commands.await_args.kwargs['scope'].chat_id)

    async def test_failed_grant_and_revoke_sync_do_not_undo_permissions(self):
        self.bot.set_my_commands.side_effect = Forbidden('synthetic not started')
        self.assertEqual('Доступ предоставлен', await self.call(handlers_admin.adminadd_command, 7, ['9']))
        self.assertTrue(db_privileges.has_access(9))
        self.bot.set_my_commands.side_effect = None
        menus = self.app.bot_data['tdb_admin_menus']
        menus.recent[9] = 0
        await self.refresh(9)
        self.assertEqual(2, self.bot.set_my_commands.await_count)
        self.bot.delete_my_commands.side_effect = NetworkError('synthetic')
        self.assertEqual('Доступ отозван', await self.call(handlers_admin.adminremove_command, 7, ['9']))
        self.assertFalse(db_privileges.has_access(9))
        self.bot.delete_my_commands.side_effect = None
        menus.recent[9] = 0
        await self.refresh(9)
        self.assertEqual(2, self.bot.delete_my_commands.await_count)

    async def test_revocation_serializes_against_in_flight_grant_menu(self):
        entered, release = asyncio.Event(), asyncio.Event()
        events = []

        async def set_commands(*args, **kwargs):
            entered.set()
            await release.wait()
            events.append('grant-menu')

        async def delete_commands(*args, **kwargs):
            events.append('delete-menu')

        self.bot.set_my_commands.side_effect = set_commands
        self.bot.delete_my_commands.side_effect = delete_commands
        grant = asyncio.create_task(self.call(handlers_admin.adminadd_command, 7, ['9']))
        await asyncio.wait_for(entered.wait(), 2)
        revoke = asyncio.create_task(self.call(handlers_admin.adminremove_command, 7, ['9']))
        try:
            while db_privileges.has_access(9):
                await asyncio.sleep(0)
            self.assertIsNone(admin_access.role(9))
            self.assertEqual([], events)
        finally:
            release.set()
            await asyncio.wait_for(asyncio.gather(grant, revoke), 2)
        self.assertEqual(['grant-menu', 'delete-menu'], events)
