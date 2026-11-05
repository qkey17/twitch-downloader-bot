"""Exercise privilege changes against temporary SQLite and synthetic Telegram/Twitch."""

import asyncio
from contextlib import closing
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, patch

from src import admin_batch
from src.database import db_core, db_privileges
from src.handlers import handlers_admin
from src.twitch_api import videos
from src.vod_scheduler import scheduler
import test_admin_batch as batches


def isolated_privileges(test):
    directory = test.enterContext(tempfile.TemporaryDirectory())
    test.enterContext(patch.object(db_core, 'DB_PATH', Path(directory) / 'app.sqlite3'))
    test.enterContext(patch.dict(os.environ, {'TCD_ADMIN_USER_ID': '7'}))
    db_core.init_db()


def invocation(app, user, args, chat='private'):
    message = NS(reply_text=AsyncMock())
    update = NS(effective_user=NS(id=user), effective_chat=NS(type=chat), effective_message=message)
    return update, NS(application=app, args=args)


async def invoke(app, command, user, args, chat='private'):
    update, context = invocation(app, user, args, chat)
    await command(update, context)
    return update.effective_message.reply_text.await_args.args[0]


class PrivilegeCommands(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        isolated_privileges(self)
        self.app = NS(bot_data={})

    async def test_management_is_private_and_owner_only_even_with_a_delegation(self):
        db_privileges.grant(9)
        for command, args, database_call in (
            (handlers_admin.adminadd_command, ['11'], 'grant'),
            (handlers_admin.adminremove_command, ['9'], 'revoke'),
            (handlers_admin.adminlist_command, [], 'list_users'),
        ):
            for user, chat, configured in ((9, 'private', '7'), (8, 'private', '7'),
                                           (7, 'supergroup', '7'), (7, 'private', ''),
                                           (7, 'private', 'invalid'), (True, 'private', '1')):
                with self.subTest(command=command.__name__, user=user, chat=chat, configured=configured), \
                        patch.dict(os.environ, {'TCD_ADMIN_USER_ID': configured}), \
                        patch.object(db_privileges, database_call) as store:
                    result = await invoke(self.app, command, user, args, chat)
                    self.assertEqual('Команда недоступна', result)
                    store.assert_not_called()

    async def test_grant_list_revoke_and_repeated_commands(self):
        self.assertIn('пуст', await invoke(self.app, handlers_admin.adminlist_command, 7, []))
        for expected in ('Доступ предоставлен.', 'Доступ уже предоставлен.'):
            self.assertEqual(expected, await invoke(self.app, handlers_admin.adminadd_command, 7, ['9']))
        self.assertEqual([9], db_privileges.list_users())
        self.assertEqual('Привилегированные пользователи:\n9',
                         await invoke(self.app, handlers_admin.adminlist_command, 7, []))
        self.assertEqual('Доступ отозван.', await invoke(self.app, handlers_admin.adminremove_command, 7, ['9']))
        self.assertEqual('У пользователя нет делегированного доступа.',
                         await invoke(self.app, handlers_admin.adminremove_command, 7, ['9']))
        self.assertEqual([], db_privileges.list_users())

    async def test_owner_id_and_malformed_arguments_are_rejected(self):
        for command in (handlers_admin.adminadd_command, handlers_admin.adminremove_command):
            self.assertIn('владельца', await invoke(self.app, command, 7, ['7']))
            for args in ([], ['9', '10'], ['0'], ['-9'], ['9.1'], [' 9'], ['٠٩'], ['09'],
                         ['9223372036854775808'], ['9 OR 1=1'], ['9); DROP TABLE privileged_users;--']):
                self.assertIn('TELEGRAM_ID', await invoke(self.app, command, 7, args))
        self.assertIn('без аргументов', await invoke(self.app, handlers_admin.adminlist_command, 7, ['9']))
        self.assertEqual([], db_privileges.list_users())
        self.assertEqual('Доступ предоставлен.',
                         await invoke(self.app, handlers_admin.adminadd_command, 7, ['9223372036854775807']))

    async def test_invalid_owner_and_database_read_failure_deny_allvods_before_twitch(self):
        db_privileges.grant(9)
        for config, user in (('', 9), ('invalid', 9), ('7', 8), ('7', 9)):
            with self.subTest(config=config, user=user), patch.dict(os.environ, {'TCD_ADMIN_USER_ID': config}), \
                    patch.object(videos, 'fetch_user_by_login', AsyncMock()) as twitch, \
                    patch.object(db_privileges, 'has_access', side_effect=sqlite3.OperationalError) as lookup:
                result = await invoke(self.app, admin_batch.allvods_command, user, ['channel'])
                self.assertEqual('Команда недоступна', result)
                self.assertNotIn(admin_batch.KEY, self.app.bot_data)
                twitch.assert_not_awaited()
                self.assertEqual(bool(config == '7'), lookup.called)
        # The configured owner has access even when a delegate lookup would fail.
        with patch.object(db_privileges, 'has_access', side_effect=AssertionError('owner must not query')):
            self.assertIn('/allvods', await invoke(self.app, admin_batch.allvods_command, 7, []))

    async def test_database_write_failure_does_not_claim_success_or_stop_batch(self):
        db_privileges.grant(9)
        for command, operation in ((handlers_admin.adminadd_command, 'grant'),
                                   (handlers_admin.adminremove_command, 'revoke')):
            with patch.object(db_privileges, operation, side_effect=sqlite3.OperationalError), \
                    patch.object(handlers_admin, 'stop_batch') as stop:
                self.assertIn('Не удалось', await invoke(self.app, command, 7, ['9']))
                stop.assert_not_called()
        self.assertEqual([9], db_privileges.list_users())

    def test_init_is_additive_idempotent_and_grants_persist(self):
        with closing(db_core._connect()) as con, con:
            cache_id = con.execute(
                "INSERT INTO vod_cache (vod_id, fmt, vod_url, cached_at) VALUES ('1', 'html_online', 'test', 0)"
            ).lastrowid
            con.execute('INSERT INTO user_history (user_id, cache_id, processed_at) VALUES (9, ?, 0)', (cache_id,))
        db_core.init_db()  # Settle the already deployed legacy registry migration.
        with closing(db_core._connect()) as con, con:
            con.execute('DROP TABLE privileged_users')  # Fixture now has the pre-upgrade schema.
            before = list(con.iterdump())
        db_core.init_db()
        db_privileges.grant(9)
        db_core.init_db()
        self.assertTrue(db_privileges.has_access(9))
        with closing(db_core._connect()) as con:
            self.assertEqual(before, [line for line in con.iterdump() if 'privileged_users' not in line])


class Revocation(unittest.IsolatedAsyncioTestCase):
    for name in ('_setupAsyncioRunner', 'task', 'api', 'message', 'update', 'saved', 'publication',
                 'worker', 'context', 'request', 'entered', 'finish', 'launch', 'cancel_all', 'asyncTearDown'):
        locals()[name] = getattr(batches.Lifecycle, name)

    def setUp(self):
        batches.Lifecycle.setUp(self)
        isolated_privileges(self)
        db_privileges.grant(9)

    async def remove(self):
        return await invoke(self.app, handlers_admin.adminremove_command, 7, ['9'])

    async def denied(self):
        calls_before = self.fetch.await_count
        self.assertEqual('Команда недоступна',
                         await invoke(self.app, admin_batch.allvods_command, 9, ['channel']))
        self.assertEqual(calls_before, self.fetch.await_count)

    async def test_revoke_during_twitch_loading_prevents_new_batch(self):
        loading = asyncio.Event()

        async def fetch(*args):
            loading.set()
            await asyncio.Event().wait()

        self.fetch.side_effect = fetch
        batch, _, _ = await self.launch(owner=9)
        self.assertEqual(9, batch.owner)
        await asyncio.wait_for(loading.wait(), 2)
        self.assertEqual('Доступ отозван.', await asyncio.wait_for(self.remove(), 2))
        await self.denied()
        self.assertTrue(batch.task.done())
        self.assertFalse(self.started)

    async def test_revoke_claimed_batch_before_first_twitch_request(self):
        batch, _, source = await self.launch(owner=9)
        self.assertEqual('Доступ отозван.', await self.remove())
        self.assertTrue(batch.task.cancelled())
        source.reply_text.assert_not_awaited()
        await self.denied()

    async def test_launch_racing_revocation_response_cannot_use_old_access(self):
        reply_started = asyncio.Event()
        release_reply = asyncio.Event()
        update, ctx = invocation(self.app, 7, ['9'])

        async def reply(*args):
            reply_started.set()
            await release_reply.wait()

        update.effective_message.reply_text.side_effect = reply
        task = asyncio.create_task(handlers_admin.adminremove_command(update, ctx))
        try:
            await asyncio.wait_for(reply_started.wait(), 2)
            await self.denied()
            self.assertNotIn(admin_batch.KEY, self.app.bot_data)
        finally:
            release_reply.set()
            await task

    async def test_revoke_waiting_item_does_not_run_it_or_stop_other_jobs(self):
        await self.request(8, '3')
        first = await self.entered('3')
        await self.request(9, '4')
        second = await self.entered('4')
        submitted = asyncio.Event()
        original_submit = admin_batch.submit

        async def submit(*args):
            job = await original_submit(*args)
            submitted.set()
            return job

        with patch.object(admin_batch, 'submit', side_effect=submit):
            batch, _, _ = await self.launch(self.vods[:2], owner=9)
            await asyncio.wait_for(submitted.wait(), 2)
        waiting = batch.current_job
        self.assertEqual('waiting', waiting.state)
        self.assertEqual('Доступ отозван.', await asyncio.wait_for(self.remove(), 2))
        self.assertTrue(waiting.finished.is_set())
        self.assertFalse(first.cancelled or second.cancelled)
        await self.finish(first)
        await self.finish(second)
        self.assertEqual({'3', '4'}, set(self.started))

    async def test_revoke_running_batch_preserves_shared_participants_and_ordinary_jobs(self):
        batch, _, _ = await self.launch(self.vods[:2], owner=9)
        shared_job = await self.entered('2')
        await self.request(8, '2')
        await self.request(9, '3')
        ordinary = await self.entered('3')
        self.assertEqual('Доступ отозван.', await asyncio.wait_for(self.remove(), 2))
        self.assertTrue(batch.task.done())
        self.assertFalse(shared_job.cancelled or ordinary.cancelled)
        self.assertTrue(any(p.active and p.owner == 8 for p in shared_job.participants.members.values()))
        self.assertFalse(any(p.active and p.owner == 9 for p in shared_job.participants.members.values()))
        await self.finish(shared_job)
        await self.finish(ordinary)
        self.assertEqual({'2', '3'}, set(self.ready))
        self.assertNotIn('1', self.started)

    async def test_revoke_during_commit_preserves_generation_and_duplicate_waits(self):
        batch, _, _ = await self.launch(self.vods[:2], owner=9)
        job = await self.entered('2')
        committed = asyncio.Event()
        release_commit = threading.Event()
        loop = asyncio.get_running_loop()
        publication = self.publications[0]
        original_complete = publication.complete.side_effect

        def complete():
            loop.call_soon_threadsafe(committed.set)
            if not release_commit.wait(5):
                raise TimeoutError('test commit did not release')
            return original_complete()

        publication.complete.side_effect = complete
        stopped = asyncio.Queue()
        original_stop = admin_batch.stop_batch

        def stop(app, user_id):
            task = original_stop(app, user_id)
            stopped.put_nowait(task)
            return task

        self.gates['2'].set()
        await asyncio.wait_for(committed.wait(), 2)
        try:
            with patch.object(handlers_admin, 'stop_batch', side_effect=stop):
                first = asyncio.create_task(self.remove())
                self.assertIs(batch.task, await asyncio.wait_for(stopped.get(), 2))
                second = asyncio.create_task(self.remove())
                self.assertIs(batch.task, await asyncio.wait_for(stopped.get(), 2))
                await self.denied()
                self.assertFalse(first.done() or second.done() or job.cancelled)
                release_commit.set()
                self.assertEqual('Доступ отозван.', await asyncio.wait_for(first, 2))
                self.assertIn('нет делегированного', await asyncio.wait_for(second, 2))
        finally:
            release_commit.set()
        self.assertIn('2', self.ready)
        self.assertNotIn('1', self.started)
        publication.complete.assert_called_once()

    async def test_revoke_reports_cleanup_failure_separately(self):
        batch, _, _ = await self.launch(self.vods[:2], owner=9)
        job = await self.entered('2')
        self.publications[0].close.side_effect = OSError('synthetic cleanup failure')
        called = asyncio.Event()

        def stop(app, user_id):
            task = admin_batch.stop_batch(app, user_id)
            called.set()
            return task

        with patch.object(handlers_admin, 'stop_batch', side_effect=stop):
            operation = asyncio.create_task(self.remove())
            await asyncio.wait_for(called.wait(), 2)
            self.gates['2'].set()
            self.assertEqual('Доступ отозван. Не удалось подтвердить остановку массовой обработки.',
                             await asyncio.wait_for(operation, 2))
        self.assertTrue(job.critical and batch.cleanup_failed)
        await self.denied()
        self.assertNotIn('1', self.started)

    async def test_revoke_leaves_another_owners_batch_and_cancel_is_owner_scoped(self):
        batch, _, _ = await self.launch(self.vods[:1], owner=7)
        job = await self.entered('1')
        self.assertEqual('Доступ отозван.', await self.remove())
        notice = await self.cancel_all(9)
        self.assertEqual('Нет активных обработок', notice.reply_text.await_args.args[0])
        self.assertFalse(batch.cancelled or job.cancelled)
        await self.finish(job)
        await asyncio.wait_for(batch.task, 2)
