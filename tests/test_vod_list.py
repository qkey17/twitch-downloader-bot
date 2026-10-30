"""Active list/chat UI, synthetic services only; legacy UI tests stay intact."""
import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from telegram.error import BadRequest

import test_channel_vods as cards
import test_channel_selection as selection
from src.vod_list import VodListBrowser, callback, build_list
from src.channel_browser import VodSelection
from src.handlers import handlers_chat as chat, handlers_input as inputs
from src.handlers import handlers_channel_vods as channel
from src.twitch_api.client import TwitchAPIError


class ListTests(unittest.IsolatedAsyncioTestCase):
    _setupAsyncioRunner = cards.ChannelVodsTests._setupAsyncioRunner
    asyncTearDown = cards.ChannelVodsTests.asyncTearDown
    task = cards.ChannelVodsTests.task

    def setUp(self):
        cards.ChannelVodsTests.setUp(self)
        self.browser = VodListBrowser(clock=lambda: self.now)

    async def open(self, count):
        records = [cards.vod(n+1) for n in range(count)]
        pages = [(records[n:n+100], str(n+100) if n+100 < count else None)
                 for n in range(0, count, 100)] or [([], None)]
        self.fetch_vods.side_effect = pages
        return await self.browser.open(self.http, 7, 'channel', 10, 20)

    async def go(self, view, action, number=None, owner=7, message=20):
        return await self.browser.handle_callback(self.http, owner, callback(view, action, number), 10, message)

    async def test_complete_counts_and_five_date_buttons(self):
        for count in (0, 1, 2, 5, 6, 34, 200):
            with self.subTest(count=count):
                self.fetch_vods.reset_mock()
                view = await self.open(count)
                self.assertTrue(view.complete)
                self.assertEqual(count, view.loaded)
                self.assertEqual((count+4)//5, view.page_count)
                text, kb = build_list(view)
                self.assertNotIn('+', text)
                self.assertIn(f'VODs канала: {count}', text)
                self.assertEqual(max(1, (count+99)//100), self.fetch_vods.await_count)
                if count:
                    rows = kb.inline_keyboard
                    self.assertEqual(min(count, 5)+(count > 5), len(rows))
                    for row in rows[:-1] if count > 5 else rows:
                        self.assertEqual(1, len(row))
                        self.assertEqual('2026-01-02 03:04 UTC', row[0].text)
                        self.assertIn(':info:', row[0].callback_data)
                    if count > 5:
                        self.assertEqual(f'Стр. 1/{view.page_count}', rows[-1][0].text)
                    else:
                        self.assertTrue(all(':info:' in row[0].callback_data for row in rows))
                else:
                    self.assertIsNone(kb)

    async def test_page_grid_groups_selection_and_back_without_fetch(self):
        view = await self.open(100)
        view = await self.go(view, 'next')
        self.assertEqual(1, view.page)
        self.assertEqual('Стр. 2/20', build_list(view)[1].inline_keyboard[-1][1].text)
        grid = await self.go(view, 'pages')
        self.assertTrue(grid.grid)
        text, kb = build_list(grid)
        self.assertIn('Выберите страницу', text)
        self.assertEqual(12, sum(b.text.isdigit() for row in kb.inline_keyboard for b in row))
        grid = await self.go(grid, 'group', 1)
        self.assertEqual(1, grid.group)
        view = await self.go(grid, 'page', 19)
        self.assertEqual(19, view.page)
        self.assertEqual('Стр. 20/20', build_list(view)[1].inline_keyboard[-1][-1].text)
        middle = await self.go(view, 'page', 12)
        self.assertEqual(['⬅️', 'Стр. 13/20', '➡️'], [b.text for b in build_list(middle)[1].inline_keyboard[-1]])
        view = await self.go(middle, 'page', 19)
        picked = await self.go(view, 'select', 99)
        self.assertIsInstance(picked, VodSelection)
        self.assertEqual('100', picked.vod_id)
        self.assertEqual('stale', (await self.go(view, 'select', 99)).status)
        view = await self.go(picked.view, 'back')
        self.assertEqual(19, view.page)
        self.assertEqual(1, self.fetch_vods.await_count)

    async def test_foreign_stale_and_out_of_page_callbacks(self):
        view = await self.open(6)
        self.assertEqual('forbidden', (await self.go(view, 'select', 0, owner=8)).status)
        self.assertEqual('forbidden', (await self.go(view, 'next', message=21)).status)
        self.assertEqual('invalid', (await self.go(view, 'select', 5)).status)
        self.assertEqual('invalid', (await self.go(view, 'page', 2)).status)
        await self.go(view, 'next')
        self.assertEqual('stale', (await self.go(view, 'next')).status)

    async def test_one_page_does_not_open_page_picker(self):
        for count in (1, 2, 5):
            with self.subTest(count=count):
                view = await self.open(count)
                view = await self.go(view, 'pages')
                self.assertFalse(view.grid)
                text, keyboard = build_list(view)
                self.assertNotIn('Выберите страницу', text)
                self.assertEqual(count, len(keyboard.inline_keyboard))

    async def test_incomplete_cycle_limit_and_api_failure_never_display_partial_total(self):
        for pages in (([cards.vod()], 'A'),):
            self.fetch_vods.side_effect = [pages, pages]
            view = await self.browser.open(self.http, 7, 'channel', 10, 20)
            self.assertEqual('api_error', view.status)
            self.assertIsNone(build_list(view)[1])
            self.assertNotIn('VODs канала:', build_list(view)[0])
        self.fetch_vods.side_effect = [([cards.vod()], 'A'), TwitchAPIError('SECRET', status=429, retry_after=1)]
        view = await self.browser.open(self.http, 7, 'channel', 10, 20)
        self.assertEqual('rate_limited', view.status)
        self.assertNotIn('SECRET', build_list(view)[0])
        self.browser = VodListBrowser(max_vods=1)
        self.fetch_vods.side_effect = [([cards.vod()], 'A')]
        view = await self.browser.open(self.http, 7, 'channel', 10, 20)
        self.assertEqual('limit', view.status)
        self.assertIsNone(build_list(view)[1])

    async def test_total_timeout_and_expiry_bound_loading(self):
        original = asyncio.timeout
        self.fetch_vods.side_effect = lambda *args: None
        async def slow(*args):
            await asyncio.sleep(10)
        self.fetch_vods.side_effect = slow
        with patch('src.vod_list.asyncio.timeout', side_effect=lambda seconds: original(0.01)):
            view = await self.browser.open(self.http, 7, 'channel', 10, 20)
        self.assertEqual(('api_error', 'timeout'), (view.status, view.reason))
        self.assertIsNone(build_list(view)[1])

    async def test_public_only_deduplication_and_empty_continuation(self):
        self.fetch_vods.side_effect = [
            ([cards.vod(1), cards.vod(2, viewable='private')], 'A'),
            ([], 'B'), ([cards.vod(1), cards.vod(3)], None)]
        view = await self.browser.open(self.http, 7, 'channel', 10, 20)
        self.assertEqual(['1', '3'], [v.id for v in view.items])
        self.assertEqual(3, self.fetch_vods.await_count)
        self.assertTrue(all(v.title and not v.url for v in view.items))

    async def test_cancelled_loading_releases_owner_and_expired_load_cannot_return_list(self):
        entered = asyncio.Event()
        async def slow(*args):
            entered.set()
            await asyncio.Event().wait()
        self.fetch_vods.side_effect = slow
        task = self.task(self.browser.open(self.http, 7, 'channel', 10, 20))
        await asyncio.wait_for(entered.wait(), 1)
        self.assertEqual('busy', (await self.browser.open(self.http, 7, 'channel', 10, 20)).status)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(task, 1)
        self.fetch_vods.side_effect = None
        self.fetch_vods.return_value = ([cards.vod()], None)
        view = await self.browser.open(self.http, 7, 'channel', 10, 20)
        self.assertTrue(view.complete)
        self.now += 601
        self.assertEqual('expired', (await self.go(view, 'select', 0)).status)


class ChatTests(unittest.IsolatedAsyncioTestCase):
    _setupAsyncioRunner = selection.ChannelSelectionTests._setupAsyncioRunner
    setUp = selection.ChannelSelectionTests.setUp
    asyncTearDown = selection.ChannelSelectionTests.asyncTearDown
    context_for_user = selection.ChannelSelectionTests.context_for_user
    task = selection.ChannelSelectionTests.task
    message = selection.ChannelSelectionTests.message
    update = selection.ChannelSelectionTests.update
    ready_result = selection.ChannelSelectionTests.ready_result

    async def test_direct_input_starts_without_format_confirmation(self):
        for text in ('https://m.twitch.tv/videos/123456789', '123456789'):
            self.runner.reset_mock()
            message = self.message(text=text)
            await inputs.text_input(SimpleNamespace(message=message, effective_user=SimpleNamespace(id=7)), self.context)
            await asyncio.gather(*self.tasks)
            await asyncio.sleep(0)
            self.runner.assert_awaited_once()
            self.assertNotIn('vod_pending', self.context.user_data)
            self.assertFalse(any(args and 'казуал' in args[0] for _, args, _ in self.messages))

    async def test_ready_offer_survives_route_completion_and_consumes_regen(self):
        self.ready.return_value = self.ready_result()
        message = self.message(text='123456789')
        await inputs.text_input(SimpleNamespace(message=message, effective_user=SimpleNamespace(id=7)), self.context)
        prompt = self.messages[-1][0]
        kb = prompt.edit_text.await_args.kwargs['reply_markup']
        self.assertEqual(['Открыть HTML', 'Сгенерировать заново'], [r[0].text for r in kb.inline_keyboard])
        open_data, regen = [r[0].callback_data for r in kb.inline_keyboard]
        await chat.chat_callback(self.update(regen, prompt, owner=8), self.context)
        self.runner.assert_not_awaited()
        await chat.chat_callback(self.update(open_data, prompt), self.context)
        self.context.bot.edit_message_text.assert_awaited_once()
        self.context.bot.send_message.assert_not_awaited()
        await chat.chat_callback(self.update(open_data, prompt), self.context)
        self.context.bot.edit_message_text.assert_awaited_once()
        await chat.chat_callback(self.update(regen, prompt), self.context)
        await chat.chat_callback(self.update(regen, prompt), self.context)
        await asyncio.gather(*self.tasks)
        self.runner.assert_awaited_once()

    async def test_channel_ready_back_keeps_page_and_invalidates_choice(self):
        self.browser = VodListBrowser()
        self.app.bot_data[channel._STORE_KEY] = self.browser
        from src.twitch_api import videos
        videos.fetch_archive_vods.return_value = ([cards.vod(n+1) for n in range(6)], None)
        view = await self.browser.open(self.http, 7, 'channel', 10, 20)
        view = await self.browser.handle_callback(self.http, 7, callback(view, 'next'), 10, 20)
        ready = self.ready_result() | {'vod_id': '6'}
        self.ready.return_value = ready
        message = self.message()
        await channel.channel_vods_callback(self.update(callback(view, 'select', 5), message), self.context)
        rows = message.edit_text.await_args.kwargs['reply_markup'].inline_keyboard
        regen, back = rows[1][0].callback_data, rows[2][0].callback_data
        await channel.channel_vods_callback(self.update(back, message), self.context)
        self.assertEqual('Стр. 2/2', message.edit_text.await_args.kwargs['reply_markup'].inline_keyboard[-1][1].text)
        await chat.chat_callback(self.update(regen, message), self.context)
        self.runner.assert_not_awaited()
        message.reply_text.assert_not_awaited()

    async def test_legacy_open_uses_ready_generation_and_history_without_format(self):
        self.ready.return_value = self.ready_result()
        with patch.object(chat.vod.config, 'HISTORY_ENABLED', True), patch.object(chat.vod.db, 'add_user_history') as history:
            await chat.legacy_open_callback(self.update('ui:result:open:123456789'), self.context)
            history.assert_called_once_with(7, 1)
        options = self.context.bot.edit_message_text.await_args.kwargs
        self.assertEqual('🔗 Ссылка на VOD', options['reply_markup'].inline_keyboard[0][-1].text)
        urls = [b.url for row in options['reply_markup'].inline_keyboard for b in row]
        self.assertEqual([self.ready_result()['html_ref'], 'https://www.twitch.tv/videos/123456789'], urls)
        self.assertEqual((10, 20), (options['chat_id'], options['message_id']))
        self.context.bot.send_message.assert_not_awaited()
        self.metadata.assert_not_awaited()
        self.runner.assert_not_awaited()
        self.assertNotIn('vod_pending', self.context.user_data)

    async def test_legacy_missing_and_regen_never_restore_format(self):
        message = self.message()
        await chat.legacy_open_callback(self.update('ui:result:open:123456789', message), self.context)
        self.assertIn('недоступен', message.edit_text.await_args.args[0])
        self.assertNotIn('reply_markup', message.edit_text.await_args.kwargs)
        self.ready.reset_mock()
        await chat.legacy_open_callback(self.update('ui:result:regen:123456789:0123456789abcdef', message), self.context)
        self.ready.assert_not_called()
        self.metadata.assert_not_awaited()
        self.runner.assert_not_awaited()

    async def test_list_open_edits_current_card_without_new_message(self):
        self.browser = VodListBrowser()
        self.app.bot_data[channel._STORE_KEY] = self.browser
        view = await self.browser.open(self.http, 7, 'channel', 10, 20)
        self.ready.return_value = self.ready_result()
        message = self.message()
        await channel.channel_vods_callback(self.update(callback(view, 'select', 0), message), self.context)
        data = message.edit_text.await_args.kwargs['reply_markup'].inline_keyboard[0][0].callback_data
        await chat.chat_callback(self.update(data, message), self.context)
        self.context.bot.edit_message_text.assert_awaited_once()
        self.assertEqual(20, self.context.bot.edit_message_text.await_args.kwargs['message_id'])
        message.reply_text.assert_not_awaited()
        self.context.bot.send_message.assert_not_awaited()

    async def test_ready_disappeared_does_not_start_processing(self):
        self.ready.return_value = self.ready_result()
        message = self.message()
        await chat.prepare_chat(message, self.context, 7, '123456789', edit=True)
        data = message.edit_text.await_args.kwargs['reply_markup'].inline_keyboard[0][0].callback_data
        self.ready.return_value = None
        await chat.chat_callback(self.update(data, message), self.context)
        self.assertIn('недоступен', message.edit_text.await_args.args[0])
        self.runner.assert_not_awaited()
        self.context.bot.send_message.assert_not_awaited()

    async def test_edit_rejected_falls_back_but_unchanged_message_does_not(self):
        self.ready.return_value = self.ready_result()
        self.context.bot.edit_message_text.side_effect = BadRequest('Message is not modified')
        await chat.legacy_open_callback(self.update('ui:result:open:123456789'), self.context)
        self.context.bot.send_message.assert_not_awaited()
        self.context.bot.edit_message_text.side_effect = BadRequest('Message cannot be edited')
        await chat.legacy_open_callback(self.update('ui:result:open:123456789'), self.context)
        self.context.bot.send_message.assert_awaited_once()

    async def test_local_reference_is_not_exposed_as_telegram_url(self):
        self.ready.return_value = self.ready_result() | {'html_ref': 'file:///private/chat.html'}
        with patch.object(chat.vod, 'HTML_PUBLISH_MODE', 'local'):
            await chat.legacy_open_callback(self.update('ui:result:open:123456789'), self.context)
        buttons = self.context.bot.edit_message_text.await_args.kwargs['reply_markup'].inline_keyboard[0]
        self.assertEqual(['https://www.twitch.tv/videos/123456789'], [b.url for b in buttons])

    async def test_channel_without_html_starts_in_existing_message(self):
        self.browser = VodListBrowser()
        self.app.bot_data[channel._STORE_KEY] = self.browser
        view = await self.browser.open(self.http, 7, 'channel', 10, 20)
        message = self.message()
        data = callback(view, 'select', 0)
        await channel.channel_vods_callback(self.update(data, message), self.context)
        await channel.channel_vods_callback(self.update(data, message), self.context)
        await asyncio.gather(*self.tasks)
        self.runner.assert_awaited_once()
        message.reply_text.assert_not_awaited()
        self.assertFalse(self.browser.is_current(view))

    async def test_failed_task_creation_releases_slot_without_legacy_prompt(self):
        self.app.create_task.side_effect = RuntimeError('synthetic creation failure')
        message = self.message()
        with self.assertRaises(RuntimeError):
            await chat.prepare_chat(message, self.context, 7, '123456789', edit=True)
        self.assertNotIn('vod_global_operation', self.app.bot_data)
        self.assertFalse(self.context.user_data.get('vod_busy'))
        self.assertNotIn('казуал', message.edit_text.await_args.args[0])

    async def test_back_during_metadata_prevents_start(self):
        self.browser = VodListBrowser()
        self.app.bot_data[channel._STORE_KEY] = self.browser
        view = await self.browser.open(self.http, 7, 'channel', 10, 20)
        entered, release = asyncio.Event(), asyncio.Event()
        async def slow(*args):
            entered.set()
            await release.wait()
            return SimpleNamespace(length_seconds=60)
        self.metadata.side_effect = slow
        message = self.message()
        task = self.task(channel.channel_vods_callback(self.update(callback(view, 'select', 0), message), self.context))
        await asyncio.wait_for(entered.wait(), 1)
        session = self.browser._sessions[view.session_id]
        selected = self.browser._view(session)
        await channel.channel_vods_callback(self.update(callback(selected, 'back'), message), self.context)
        release.set()
        await asyncio.wait_for(task, 1)
        self.runner.assert_not_awaited()

    async def test_new_input_invalidates_old_ready_actions(self):
        self.ready.return_value = self.ready_result()
        await chat.prepare_chat(self.message(), self.context, 7, '123456789')
        prompt = self.messages[-1][0]
        regen = prompt.edit_text.await_args.kwargs['reply_markup'].inline_keyboard[1][0].callback_data
        self.ready.return_value = None
        self.metadata.return_value = None
        await inputs.text_input(SimpleNamespace(message=self.message(text='999'), effective_user=SimpleNamespace(id=7)), self.context)
        await chat.chat_callback(self.update(regen, prompt), self.context)
        self.runner.assert_not_awaited()
