"""Channel selection -> existing VOD confirmation, with all services mocked."""
import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch
from telegram.error import BadRequest

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
            ('src.ui.result_preview.fetch_video_thumbnail', AsyncMock(return_value='')),
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
        async def edit(**kw):
            message = self.telegram_messages[kw.pop('message_id')]
            kw.pop('chat_id')
            return await message.edit_text(**kw)
        return SimpleNamespace(application=self.app, user_data={},
                               bot=SimpleNamespace(send_photo=AsyncMock(side_effect=BadRequest('Unsupported static fixture photo')),id=42,token='123:synthetic-card-test-key',edit_message_text=AsyncMock(side_effect=edit), edit_message_caption=AsyncMock(),send_message=AsyncMock(),
                                   edit_message_media=AsyncMock(side_effect=BadRequest('Synthetic text fallback'))))

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
        result = SimpleNamespace(chat_id=10, message_id=number, text=text,
                               reply_photo=AsyncMock(side_effect=BadRequest('Unsupported static fixture photo')),reply_text=AsyncMock(side_effect=reply), edit_text=AsyncMock(), delete=AsyncMock())
        if not hasattr(self, 'telegram_messages'):
            self.telegram_messages = {}
        self.telegram_messages[number] = result
        return result

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

    def offered_result(self):
        return dict(self.ready_result(), id='a'*32)

    async def open_offer(self, prompt, keyboard):
        from src.handlers.handlers_chat import ready_open_callback
        prompt.from_user=SimpleNamespace(id=self.context.bot.id)
        prompt.reply_markup=keyboard
        await ready_open_callback(self.update(keyboard.inline_keyboard[0][0].callback_data, prompt), self.context)

    async def test_ready_html_offers_existing_actions_without_metadata_or_download(self):
        self.ready.return_value = self.offered_result()
        await self.select(await self.view())
        prompt, _, kwargs = self.messages[-1]
        keyboard = kwargs['reply_markup']
        self.assertEqual(['Получить готовый чат', 'Обработать чат заново'],
                         [row[0].text for row in keyboard.inline_keyboard])
        self.assertNotIn('vod_pending', self.context.user_data)
        self.metadata.assert_not_awaited()
        self.runner.assert_not_awaited()
        self.app.create_task.assert_not_called()
        await self.open_offer(prompt, keyboard)
        self.assertEqual(prompt.message_id,self.context.bot.edit_message_text.await_args.kwargs['message_id'])
        self.assertEqual(self.ready_result()['html_ref'],
                         self.context.bot.edit_message_text.await_args.kwargs['reply_markup'].inline_keyboard[0][0].url)

    async def test_regeneration_uses_existing_handler_only_after_explicit_confirmation(self):
        self.ready.return_value = self.offered_result()
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

    async def test_separate_message_selection_preserves_existing_bound_pending(self):
        from src.ui.card_context import for_message
        set_pending(self.context,'https://www.twitch.tv/videos/999','999');bind_pending_message(self.context,self.message(90))
        old=self.context.user_data['vod_pending'];old_state=for_message(self.context,7,10,90)
        for ready in (None,self.offered_result()):
            self.ready.return_value=ready;result=await self.select(await self.view())
            self.assertIsInstance(result[1],VodSelection)
            self.assertIs(old,old_state.user_data['vod_pending']);self.assertEqual('999',old['vod_id'])
        self.runner.assert_not_awaited()

    async def test_separate_cards_of_same_vod_have_independent_pending_selection(self):
        from src.ui.card_context import for_message
        set_pending(self.context,'vod','123456789');bind_pending_message(self.context,self.message(90))
        old_state=for_message(self.context,7,10,90);old=old_state.user_data['vod_pending']
        await self.select(await self.view())
        self.assertIs(old,old_state.user_data['vod_pending']);self.assertIsNot(old,self.context.user_data['vod_pending'])
        self.assertNotEqual(90,self.context.user_data['vod_pending']['message_id'])
        self.metadata.assert_awaited_once();self.runner.assert_not_awaited()

    async def test_global_slot_blocks_uncached_selection_but_allows_opening_ready_html(self):
        other = self.context_for_user()
        operation = begin_download(other)
        try:
            update, result = await self.select(await self.view())
            self.assertIsNone(result)
            self.assertIn('Сейчас обрабатывается чат VOD', update.callback_query.message.reply_text.await_args.args[0])
            self.assertNotIn('vod_pending', self.context.user_data)
            self.metadata.assert_not_awaited()
            self.ready.return_value = self.offered_result()
            await self.select(await self.view())
            prompt, _, options = self.messages[-1]
            keyboard = options['reply_markup']
            await self.open_offer(prompt, keyboard)
            self.assertEqual(prompt.message_id,self.context.bot.edit_message_text.await_args.kwargs['message_id'])
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

    async def test_lookup_obeys_own_session_expiry_but_not_other_card_pending(self):
        from src.ui.card_context import for_message
        for replacement in ('expiry','pending'):
            with self.subTest(replacement=replacement):
                view=await self.view();entered,release=asyncio.Event(),asyncio.Event()
                async def fetch(*args):entered.set();await release.wait();return SimpleNamespace(length_seconds=60)
                self.metadata.side_effect=fetch;task=self.task(self.select(view))
                await asyncio.wait_for(entered.wait(),1)
                if replacement=='expiry':self.now+=self.browser.ttl
                else:
                    set_pending(self.context,'vod','999');bind_pending_message(self.context,self.message(90))
                    other=for_message(self.context,7,10,90)
                release.set();_,result=await task
                if replacement=='expiry':self.assertIsNone(result)
                else:
                    self.assertIsInstance(result,VodSelection)
                    self.assertEqual('999',other.user_data['vod_pending']['vod_id'])
                self.assertNotIn('vod_link_lookup',self.context.user_data)

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
