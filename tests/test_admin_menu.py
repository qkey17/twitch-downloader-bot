"""Role-aware help and bounded removal of legacy Telegram menu scopes."""

import asyncio
import os
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, patch

from telegram.error import Forbidden, NetworkError, RetryAfter

from src import admin_access
from src.database import db_privileges
from src.handlers import handlers_cmd, handlers_input
from test_delegated_admin import invocation, invoke, isolated_privileges


class AdminMenuTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        isolated_privileges(self)
        self.bot = NS(set_my_commands=AsyncMock(), delete_my_commands=AsyncMock(return_value=True))
        self.app = NS(bot=self.bot, bot_data={})

    async def refresh(self, user, *, chat_type='private'):
        update = NS(effective_chat=NS(id=user, type=chat_type), effective_user=NS(id=user))
        await admin_access.clear_private_menu(update, NS(bot=self.bot, application=self.app))

    async def help(self, user, chat='private'):
        update, context = invocation(self.app, user, [], chat)
        await handlers_cmd.help_command(update, context)
        return update.effective_message.reply_text.await_args.args[0]

    async def test_help_is_role_aware_only_in_private_chat(self):
        db_privileges.grant(9)
        basic = await self.help(8)
        premium = await self.help(9)
        owner = await self.help(7)
        self.assertIn('ID или ссылку на VOD', basic)
        self.assertIn('название канала или ссылку', basic)
        self.assertNotIn('Premium', basic)
        self.assertTrue(premium.startswith(basic + '\n\nPremium'))
        self.assertIn('«канал» all', premium)
        self.assertIn('«канал» new', premium)
        self.assertNotIn('/premium', premium)
        self.assertTrue(owner.startswith(premium + '\n\nAdmin'))
        for command in ('/premium add «ID»', '/premium remove «ID»', '/premium list'):
            self.assertIn(command, owner)
        for user in (7, 9):
            self.assertEqual(basic, await self.help(user, 'supergroup'))
            with patch.dict(os.environ, {'TCD_ADMIN_USER_ID': 'invalid'}):
                self.assertEqual(basic, await self.help(user))

    async def test_startup_clears_known_scopes_and_next_private_update_clears_revoked(self):
        db_privileges.grant(9)
        await admin_access.clear_known_menus(self.app)
        self.assertEqual([7, 9], [call.kwargs['scope'].chat_id
                                  for call in self.bot.delete_my_commands.await_args_list])
        await self.refresh(8, chat_type='supergroup')
        await self.refresh(9)  # Already cleared during startup.
        self.assertEqual(2, self.bot.delete_my_commands.await_count)
        await self.refresh(8)  # May have a scope from a previously revoked grant.
        self.assertEqual(8, self.bot.delete_my_commands.await_args.kwargs['scope'].chat_id)
        self.bot.set_my_commands.assert_not_awaited()
        self.assertEqual(['help'], [command.command for command in admin_access.public_commands()])

    async def test_cleanup_failure_can_retry_and_never_gates_privilege_writes(self):
        self.bot.delete_my_commands.side_effect = Forbidden('synthetic inaccessible chat')
        await self.refresh(9)
        self.assertEqual('Premium предоставлен', await invoke(self.app, 7, ['add', '9']))
        self.assertTrue(db_privileges.has_access(9))
        self.assertEqual('Premium отозван', await invoke(self.app, 7, ['remove', '9']))
        self.assertFalse(db_privileges.has_access(9))
        self.assertEqual(1, self.bot.delete_my_commands.await_count)
        await self.refresh(9)
        self.assertEqual(1, self.bot.delete_my_commands.await_count)
        self.app.bot_data['tdb_legacy_menus'].recent[9] = 0
        self.bot.delete_my_commands.side_effect = None
        await self.refresh(9)
        self.assertEqual(2, self.bot.delete_my_commands.await_count)

    async def test_background_cleanup_does_not_wait_in_ordinary_input(self):
        entered = asyncio.Event()

        async def unavailable(**kwargs):
            entered.set()
            await asyncio.Event().wait()
            raise NetworkError('synthetic')

        self.bot.delete_my_commands.side_effect = unavailable
        admin_access.start_menu_cleanup(self.app)
        try:
            await asyncio.wait_for(entered.wait(), 2)
            msg = NS(text='sample_channel', reply_text=AsyncMock())
            update = NS(message=msg, effective_user=NS(id=8))
            with patch.object(handlers_input, '_dispatch', AsyncMock()) as dispatch:
                await asyncio.wait_for(handlers_input.text_input(update, NS(application=self.app)), 2)
                dispatch.assert_awaited_once()
            msg.reply_text.assert_not_awaited()
        finally:
            await admin_access.stop_menu_cleanup(self.app)
        self.assertNotIn('tdb_menu_cleanup_task', self.app.bot_data)

    async def test_rate_limit_stops_startup_sweep_until_later_interaction(self):
        db_privileges.grant(9)
        self.bot.delete_my_commands.side_effect = RetryAfter(12)
        await admin_access.clear_known_menus(self.app)
        await self.refresh(9)
        self.assertEqual(1, self.bot.delete_my_commands.await_count)
        menus = self.app.bot_data['tdb_legacy_menus']
        menus.retry_at = 0
        self.bot.delete_my_commands.side_effect = None
        await self.refresh(9)
        self.assertEqual(9, self.bot.delete_my_commands.await_args.kwargs['scope'].chat_id)
