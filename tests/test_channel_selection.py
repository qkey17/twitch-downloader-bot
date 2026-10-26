"""Channel selection -> existing VOD confirmation, with all services mocked."""
import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from src import config
from src.channel_browser import ChannelBrowser, VodSelection, callback_data
from src.handlers import handlers_channel_vods as channel, handlers_vod as vod
from src.handlers.handlers_state import begin_download, finish_download, set_pending, bind_pending_message
from src.twitch_api.client import TwitchAPIError
from src.twitch_api.videos import ArchiveVod, TwitchUser


class ChannelSelectionTests(unittest.IsolatedAsyncioTestCase):
    def _setupAsyncioRunner(self):
        self._asyncioRunner = asyncio.Runner(debug=True, loop_factory=asyncio.SelectorEventLoop)

    def setUp(self):
        self.now = 1000.0
        self.browser = ChannelBrowser(clock=lambda: self.now)
        self.http = object()
        self.tasks = []
        self.app = SimpleNamespace(bot_data={'aiohttp': self.http, channel._STORE_KEY: self.browser},
                                   create_task=Mock(side_effect=self.task))
        self.context = self.context_for_user()
        self.next_message_id = 100
        self.messages = []
        self.ready = Mock(return_value=None)
        self.metadata = AsyncMock(return_value=SimpleNamespace(length_seconds=60))
        self.runner = AsyncMock()
        archive = ArchiveVod('123456789', '123', 'channel', 'Channel', 'Title',
                             '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z', '1m',
                             'https://www.twitch.tv/videos/123456789', '', 'public')
        for target, value in (
            ('src.twitch_api.videos.fetch_user_by_login', AsyncMock(return_value=TwitchUser('123', 'channel', 'Channel'))),
            ('src.twitch_api.videos.fetch_archive_vods', AsyncMock(return_value=([archive], None))),
            ('src.handlers.handlers_vod.results.get_ready_result', self.ready),
            ('src.handlers.handlers_vod.get_client_id', Mock(return_value='mock-client')),
            ('src.handlers.handlers_vod.fetch_vod_meta', self.metadata),
            ('src.handlers.handlers_vod._runner', self.runner),
            ('src.handlers.handlers_vod._build_card', Mock(return_value=('Existing VOD card', None))),
            ('src.handlers.handlers_vod.HTML_PUBLISH_MODE', 'public'),
            ('src.config.HISTORY_ENABLED', False),
            ('sqlite3.connect', Mock(side_effect=AssertionError('User DB accessed'))),
            ('src.twitch_api.gql_fetch_comments', Mock(side_effect=AssertionError('Chat API called'))),
        ):
            patched = patch(target, value)
            patched.start()
            self.addCleanup(patched.stop)

    def context_for_user(self):
        return SimpleNamespace(application=self.app, user_data={},
                               bot=SimpleNamespace(edit_message_text=AsyncMock(), send_message=AsyncMock()))

    def task(self, coroutine):
        task = asyncio.create_task(coroutine)
        self.tasks.append(task)
        return task

    async def asyncTearDown(self):
        for task in self.tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)

    def message(self, number=20, text=''):
        async def reply(*args, **kwargs):
            self.next_message_id += 1
            result = self.message(self.next_message_id)
            self.messages.append((result, args, kwargs))
            return result
        return SimpleNamespace(chat_id=10, message_id=number, text=text,
                               reply_text=AsyncMock(side_effect=reply), edit_text=AsyncMock(), delete=AsyncMock())

    def update(self, data, message=None, owner=7):
        return SimpleNamespace(callback_query=SimpleNamespace(data=data, message=message or self.message(), answer=AsyncMock()),
                               effective_user=SimpleNamespace(id=owner))

    async def view(self, message=20):
        return await self.browser.open(self.http, 7, 'channel', 10, message)

    async def select(self, view, *, owner=7, context=None):
        update = self.update(callback_data(view, 'select'), owner=owner)
        result = await channel.channel_vods_callback(update, context or self.context)
        return update, result

    def ready_result(self):
        return {'vod_id': '123456789', 'cache_id': 1, 'meta': {'title': 'Title'}, 'stats': {},
                'html_ref': 'https://public.invalid/public_html/chat_'+'a'*32+'.html'}

    async def test_unprocessed_selection_sets_bound_pending_and_never_starts_download(self):
        update, result = await self.select(await self.view())
        self.assertIsInstance(result, VodSelection)
        self.metadata.assert_awaited_once_with(self.http, 'mock-client', '123456789')
        prompt, args, kwargs = self.messages[-1]
        self.assertEqual((vod._FORMAT_PROMPT_TEXT,), args)
        self.assertEqual('vodfmt:html_online', kwargs['reply_markup'].inline_keyboard[0][0].callback_data)
        pending = self.context.user_data['vod_pending']
        self.assertEqual(('123456789', 10, prompt.message_id),
                         (pending['vod_id'], pending['chat_id'], pending['message_id']))
        self.assertNotIn('vod_link_lookup', self.context.user_data)
        self.assertNotIn('vod_global_operation', self.app.bot_data)
        self.app.create_task.assert_not_called()
        self.runner.assert_not_awaited()

    async def test_direct_link_and_channel_selection_use_same_format_prompt(self):
        await self.select(await self.view())
        _, selected_args, selected_options = self.messages[-1]
        direct = self.message(30, 'https://twitch.tv/videos/123456789')
        context = self.context_for_user()
        await vod.vod_link_entry(SimpleNamespace(message=direct, effective_user=SimpleNamespace(id=7)), context)
        _, direct_args, direct_options = self.messages[-1]
        self.assertEqual(selected_args, direct_args)
        self.assertEqual(selected_options, direct_options)
        self.assertEqual('123456789', context.user_data['vod_pending']['vod_id'])

    async def test_ready_html_offers_existing_actions_without_metadata_or_download(self):
        self.ready.return_value = self.ready_result()
        await self.select(await self.view())
        prompt, _, kwargs = self.messages[-1]
        keyboard = kwargs['reply_markup']
        self.assertEqual(['Показать результат', 'Создать заново'],
                         [row[0].text for row in keyboard.inline_keyboard])
        self.assertNotIn('vod_pending', self.context.user_data)
        self.metadata.assert_not_awaited()
        self.runner.assert_not_awaited()
        self.app.create_task.assert_not_called()
        await vod.vod_result_callback(self.update(keyboard.inline_keyboard[0][0].callback_data, prompt), self.context)
        self.context.bot.send_message.assert_awaited_once()
        self.assertEqual(self.ready_result()['html_ref'],
                         self.context.bot.send_message.await_args.kwargs['reply_markup'].inline_keyboard[0][0].url)

    async def test_regeneration_uses_existing_handler_only_after_explicit_confirmation(self):
        self.ready.return_value = self.ready_result()
        await self.select(await self.view())
        prompt, _, options = self.messages[-1]
        self.app.create_task.assert_not_called()
        regenerate = options['reply_markup'].inline_keyboard[1][0].callback_data
        await vod.vod_result_callback(self.update(regenerate, prompt), self.context)
        await asyncio.gather(*self.tasks)
        await asyncio.sleep(0)
        self.runner.assert_awaited_once()
        self.assertEqual('123456789', self.runner.await_args.kwargs['vod_id'])
        self.assertNotIn('vod_global_operation', self.app.bot_data)

    async def test_format_confirmation_starts_existing_runner_with_selected_vod(self):
        await self.select(await self.view())
        prompt = self.messages[-1][0]
        await vod.vod_format_chosen(self.update('vodfmt:html_online', prompt), self.context)
        await asyncio.gather(*self.tasks)
        self.runner.assert_awaited_once()
        self.assertEqual('123456789', self.runner.await_args.kwargs['vod_id'])

    async def test_foreign_stale_and_wrong_vod_callbacks_change_no_user_state(self):
        view = await self.view()
        foreign = self.context_for_user()
        foreign.user_data['marker'] = 'keep'
        _, result = await self.select(view, owner=8, context=foreign)
        self.assertIsNone(result)
        self.assertEqual({'marker': 'keep'}, foreign.user_data)
        self.ready.assert_not_called()
        newer = await self.browser.handle_callback(self.http, 7, callback_data(view, 'prev'), 10, 20)
        _, result = await self.select(view)
        self.assertIsNone(result)
        forged = callback_data(newer, 'select').rsplit(':', 1)[0]+':999'
        await channel.channel_vods_callback(self.update(forged), self.context)
        self.assertEqual({}, self.context.user_data)
        self.metadata.assert_not_awaited()
        self.app.create_task.assert_not_called()

    async def test_parallel_selection_clicks_prepare_only_once(self):
        view = await self.view()
        entered, release = asyncio.Event(), asyncio.Event()
        async def fetch(*args):
            entered.set()
            await release.wait()
            return SimpleNamespace(length_seconds=60)
        self.metadata.side_effect = fetch
        first = self.task(self.select(view))
        await asyncio.wait_for(entered.wait(), 1)
        _, rejected = await self.select(view)
        self.assertIsNone(rejected)
        self.assertNotIn('vod_pending', self.context.user_data)
        release.set()
        await first
        self.metadata.assert_awaited_once()
        self.assertEqual('123456789', self.context.user_data['vod_pending']['vod_id'])
        self.runner.assert_not_awaited()

    async def test_pending_other_vod_is_not_overwritten_even_by_ready_selection(self):
        for ready in (None, self.ready_result()):
            with self.subTest(ready=ready is not None):
                set_pending(self.context, 'https://www.twitch.tv/videos/999', '999')
                bind_pending_message(self.context, self.message(90))
                pending = self.context.user_data['vod_pending']
                snapshot = dict(pending)
                self.ready.return_value = ready
                update, result = await self.select(await self.view())
                self.assertIsNone(result)
                self.assertIn('другого VOD', update.callback_query.message.reply_text.await_args.args[0])
                self.assertIs(pending, self.context.user_data['vod_pending'])
                self.assertEqual(snapshot, pending)
                self.metadata.assert_not_awaited()
                self.runner.assert_not_awaited()

    async def test_pending_same_vod_reuses_dialog_but_expired_pending_can_be_replaced(self):
        set_pending(self.context, 'vod', '123456789')
        bind_pending_message(self.context, self.message(90))
        old = self.context.user_data['vod_pending']
        await self.select(await self.view())
        self.assertIs(old, self.context.user_data['vod_pending'])
        self.metadata.assert_not_awaited()
        old['created_at'] = 0
        await self.select(await self.view())
        self.assertIsNot(old, self.context.user_data['vod_pending'])
        self.assertNotEqual(90, self.context.user_data['vod_pending']['message_id'])

    async def test_global_slot_blocks_uncached_selection_but_allows_opening_ready_html(self):
        other = self.context_for_user()
        operation = begin_download(other)
        try:
            update, result = await self.select(await self.view())
            self.assertIsNone(result)
            self.assertIn('Сейчас обрабатывается чат VOD', update.callback_query.message.reply_text.await_args.args[0])
            self.assertNotIn('vod_pending', self.context.user_data)
            self.metadata.assert_not_awaited()
            self.ready.return_value = self.ready_result()
            await self.select(await self.view())
            prompt, _, options = self.messages[-1]
            keyboard = options['reply_markup']
            await vod.vod_result_callback(self.update(keyboard.inline_keyboard[0][0].callback_data, prompt), self.context)
            self.context.bot.send_message.assert_awaited_once()
            choice = self.context.user_data['vod_result_choice']
            await vod.vod_result_callback(self.update(keyboard.inline_keyboard[1][0].callback_data, prompt), self.context)
            self.assertIs(choice, self.context.user_data['vod_result_choice'])
            self.assertEqual(operation, self.app.bot_data['vod_global_operation'])
            self.app.create_task.assert_not_called()
        finally:
            finish_download(other, operation)

    async def test_global_slot_acquired_during_metadata_wait_prevents_pending_commit(self):
        view = await self.view()
        entered, release = asyncio.Event(), asyncio.Event()
        async def fetch(*args):
            entered.set()
            await release.wait()
            return SimpleNamespace(length_seconds=60)
        self.metadata.side_effect = fetch
        task = self.task(self.select(view))
        await asyncio.wait_for(entered.wait(), 1)
        other = self.context_for_user()
        operation = begin_download(other)
        try:
            release.set()
            await task
            self.assertNotIn('vod_pending', self.context.user_data)
            self.runner.assert_not_awaited()
        finally:
            finish_download(other, operation)

    async def test_stale_format_and_cancel_buttons_cannot_act_on_new_pending_vod(self):
        await self.select(await self.view())
        old_prompt = self.messages[-1][0]
        direct = self.message(30, 'https://twitch.tv/videos/999')
        await vod.vod_link_entry(SimpleNamespace(message=direct, effective_user=SimpleNamespace(id=7)), self.context)
        new_prompt = self.messages[-1][0]
        pending = self.context.user_data['vod_pending']
        await vod.vod_format_chosen(self.update('vodfmt:html_online', old_prompt), self.context)
        await vod.format_cancel_callback(self.update('ui:format_cancel', old_prompt), self.context)
        self.assertIs(pending, self.context.user_data['vod_pending'])
        self.assertEqual('999', pending['vod_id'])
        self.runner.assert_not_awaited()
        await vod.vod_format_chosen(self.update('vodfmt:html_online', new_prompt), self.context)
        await asyncio.gather(*self.tasks)
        self.assertEqual('999', self.runner.await_args.kwargs['vod_id'])

    async def test_session_expiry_or_another_pending_during_lookup_cannot_commit_old_selection(self):
        for replacement in ('expiry', 'pending'):
            with self.subTest(replacement=replacement):
                self.context.user_data.clear()
                view = await self.view()
                entered, release = asyncio.Event(), asyncio.Event()
                async def fetch(*args):
                    entered.set()
                    await release.wait()
                    return SimpleNamespace(length_seconds=60)
                self.metadata.side_effect = fetch
                task = self.task(self.select(view))
                await asyncio.wait_for(entered.wait(), 1)
                if replacement == 'expiry':
                    self.now += self.browser.ttl
                else:
                    set_pending(self.context, 'vod', '999')
                    bind_pending_message(self.context, self.message(90))
                release.set()
                _, result = await task
                self.assertIsNone(result)
                self.assertEqual(None if replacement == 'expiry' else '999',
                                 self.context.user_data.get('vod_pending', {}).get('vod_id'))
                self.assertNotIn('vod_link_lookup', self.context.user_data)

    async def test_missing_vod_and_twitch_failure_leave_no_pending_or_busy_state(self):
        for error, meta, text in ((None, None, 'не найден'),
                                  (None, SimpleNamespace(length_seconds=None), 'не найден'),
                                  (TwitchAPIError('MOCK_SECRET', status=403), None, 'Не удалось')):
            with self.subTest(error=type(error).__name__):
                self.metadata.side_effect, self.metadata.return_value = error, meta
                update, result = await self.select(await self.view())
                self.assertIsNone(result)
                message = update.callback_query.message.reply_text.await_args.args[0]
                self.assertIn(text, message)
                self.assertNotIn('MOCK_SECRET', message)
                self.assertNotIn('vod_pending', self.context.user_data)
                self.assertNotIn('vod_link_lookup', self.context.user_data)
                self.assertNotIn('vod_global_operation', self.app.bot_data)
                self.runner.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()
