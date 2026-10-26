"""Text, commands and inline menus exercise real routing with mocked services."""
import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import test_channel_selection as fixture
from src.handlers import handlers_input as inputs, handlers_cmd as commands, handlers_vod as vod
from src.handlers.handlers_state import set_pending, bind_pending_message, begin_download, finish_download
from src.twitch_api import videos
from src.twitch_api.client import TwitchAPIError


class InputRoutingTests(unittest.IsolatedAsyncioTestCase):
    _setupAsyncioRunner = fixture.ChannelSelectionTests._setupAsyncioRunner
    setUp = fixture.ChannelSelectionTests.setUp
    asyncTearDown = fixture.ChannelSelectionTests.asyncTearDown
    context_for_user = fixture.ChannelSelectionTests.context_for_user
    task = fixture.ChannelSelectionTests.task
    message = fixture.ChannelSelectionTests.message
    callback = fixture.ChannelSelectionTests.update
    ready_result = fixture.ChannelSelectionTests.ready_result

    def request(self, text='', *, args=None, owner=7, number=20):
        message = self.message(number, text)
        self.context.args = args or []
        return SimpleNamespace(message=message, effective_message=message,
                               effective_user=SimpleNamespace(id=owner))

    def latest_button(self, text):
        message, _, options = self.messages[-1]
        button = next(button for row in options['reply_markup'].inline_keyboard for button in row if button.text == text)
        return self.callback(button.callback_data, message)

    async def enter_mode(self, kind):
        await commands.start_command(self.request(), self.context)
        update = self.latest_button('Получить чат VOD' if kind == 'vod' else 'Найти VOD канала')
        await inputs.input_menu_callback(update, self.context)
        return update

    async def test_three_entry_paths_for_vod_reuse_confirmation_without_auto_download(self):
        for method in ('text', 'command', 'menu'):
            with self.subTest(method=method):
                self.context = self.context_for_user()
                self.metadata.reset_mock()
                self.messages.clear()
                if method == 'text':
                    await inputs.text_input(self.request('https://twitch.tv/videos/123456789'), self.context)
                elif method == 'command':
                    await inputs.vod_command(self.request(args=['123456789']), self.context)
                else:
                    await self.enter_mode('vod')
                    await inputs.text_input(self.request('123456789'), self.context)
                self.metadata.assert_awaited_once_with(self.http, 'mock-client', '123456789')
                pending = self.context.user_data['vod_pending']
                prompt, args, options = self.messages[-1]
                self.assertEqual(vod._FORMAT_PROMPT_TEXT, args[0])
                self.assertEqual('123456789', pending['vod_id'])
                self.assertEqual(prompt.message_id, pending['message_id'])
                self.assertEqual('vodfmt:html_online', options['reply_markup'].inline_keyboard[0][0].callback_data)
                self.assertNotIn(inputs._WAIT, self.context.user_data)
                self.assertNotIn(inputs._ROUTE, self.context.user_data)
                self.runner.assert_not_awaited()
                self.app.create_task.assert_not_called()

    async def test_three_entry_paths_for_channels_use_real_browser_and_owned_tasks(self):
        for method in ('text', 'command', 'menu'):
            with self.subTest(method=method):
                self.context = self.context_for_user()
                videos.fetch_user_by_login.reset_mock()
                self.app.create_task.reset_mock()
                if method == 'text':
                    result = await inputs.text_input(self.request('https://www.twitch.tv/ChAnNeL'), self.context)
                elif method == 'command':
                    result = await inputs.channel_command(self.request(args=['channel']), self.context)
                else:
                    await self.enter_mode('channel')
                    result = await inputs.text_input(self.request('channel'), self.context)
                self.assertEqual('ready', result.status)
                videos.fetch_user_by_login.assert_awaited_once_with(self.http, 'channel')
                self.app.create_task.assert_called_once()
                self.metadata.assert_not_awaited()
                self.assertNotIn('vod_pending', self.context.user_data)
                self.assertNotIn(inputs._ROUTE, self.context.user_data)

    async def test_commands_without_arguments_wait_for_the_requested_input(self):
        for kind, command, text in (('vod', inputs.vod_command, '123456789'),
                                    ('channel', inputs.channel_command, 'channel')):
            with self.subTest(kind=kind):
                self.context = self.context_for_user()
                self.metadata.reset_mock()
                videos.fetch_user_by_login.reset_mock()
                await command(self.request(), self.context)
                self.assertEqual(kind, self.context.user_data[inputs._WAIT]['mode'])
                self.assertIn('/cancel', self.messages[-1][1][0])
                self.metadata.assert_not_awaited()
                videos.fetch_user_by_login.assert_not_awaited()
                await inputs.text_input(self.request(text), self.context)
                self.assertNotIn(inputs._WAIT, self.context.user_data)

    async def test_wrong_recognized_type_never_silently_switches_waiting_mode(self):
        for kind, text in (('vod', 'channel'), ('channel', 'https://twitch.tv/videos/123456789')):
            with self.subTest(kind=kind):
                self.context = self.context_for_user()
                await self.enter_mode(kind)
                waiting = self.context.user_data[inputs._WAIT]
                request = self.request(text)
                await inputs.text_input(request, self.context)
                self.assertIs(waiting, self.context.user_data[inputs._WAIT])
                self.assertIn('Для смены режима', request.message.reply_text.await_args.args[0])
        self.metadata.assert_not_awaited()
        videos.fetch_user_by_login.assert_not_awaited()

    async def test_explicit_commands_switch_modes_and_reject_wrong_arguments(self):
        await inputs.channel_command(self.request(), self.context)
        await inputs.vod_command(self.request(args=['channel']), self.context)
        self.assertEqual('vod', self.context.user_data[inputs._WAIT]['mode'])
        await inputs.channel_command(self.request(args=['123456789']), self.context)
        self.assertEqual('channel', self.context.user_data[inputs._WAIT]['mode'])
        self.metadata.assert_not_awaited()
        videos.fetch_user_by_login.assert_not_awaited()
        await inputs.vod_command(self.request(args=['123456789']), self.context)
        self.assertNotIn(inputs._WAIT, self.context.user_data)
        self.assertEqual('123456789', self.context.user_data['vod_pending']['vod_id'])

    async def test_extra_arguments_and_unknown_text_are_not_partial_matches(self):
        for text in ('unrecognized input text', 'https://example.invalid/videos/123', 'https://clips.twitch.tv/Slug'):
            with self.subTest(text=text):
                update = self.request(text)
                await inputs.text_input(update, self.context)
                self.assertIn('Не удалось распознать', update.message.reply_text.await_args.args[0])
                self.assertEqual({}, self.context.user_data)
        await inputs.vod_command(self.request(args=['123', '456']), self.context)
        self.assertEqual('vod', self.context.user_data[inputs._WAIT]['mode'])
        waiting = self.context.user_data[inputs._WAIT]
        await inputs.text_input(self.request('bad input'), self.context)
        self.assertIs(waiting, self.context.user_data[inputs._WAIT])
        self.metadata.assert_not_awaited()
        videos.fetch_user_by_login.assert_not_awaited()

    async def test_start_preserves_help_text_and_has_only_two_inline_actions(self):
        await commands.start_command(self.request(), self.context)
        _, args, options = self.messages[-1]
        self.assertIn('Просто отправь ссылку на Twitch VOD:', args[0])
        self.assertIn('<code>https://www.twitch.tv/videos/0123456789</code>', args[0])
        buttons = [b for row in options['reply_markup'].inline_keyboard for b in row]
        self.assertEqual(['Получить чат VOD', 'Найти VOD канала'], [b.text for b in buttons])
        self.assertTrue(all(len(b.callback_data.encode()) <= 64 for b in buttons))

    async def test_menu_switch_cancel_and_repeated_old_callbacks_are_safe(self):
        old_menu = await self.enter_mode('vod')
        waiting_vod = self.context.user_data[inputs._WAIT]
        old_cancel = self.latest_button('Отмена')
        await inputs.input_menu_callback(old_menu, self.context)
        self.assertIs(waiting_vod, self.context.user_data[inputs._WAIT])
        self.assertTrue(old_menu.callback_query.answer.await_args.kwargs['show_alert'])
        switch = self.latest_button('Найти VOD канала')
        await inputs.input_menu_callback(switch, self.context)
        waiting_channel = self.context.user_data[inputs._WAIT]
        self.assertEqual('channel', waiting_channel['mode'])
        await inputs.input_menu_callback(old_cancel, self.context)
        self.assertIs(waiting_channel, self.context.user_data[inputs._WAIT])
        cancel = self.latest_button('Отмена')
        await inputs.input_menu_callback(cancel, self.context)
        self.assertNotIn(inputs._WAIT, self.context.user_data)
        self.assertNotIn(inputs._MENU, self.context.user_data)
        self.metadata.assert_not_awaited()

    async def test_foreign_or_forwarded_menu_does_not_change_input_state(self):
        await commands.start_command(self.request(), self.context)
        button = self.latest_button('Получить чат VOD')
        original = self.context.user_data[inputs._MENU]
        foreign = self.context_for_user()
        button.effective_user.id = 8
        await inputs.input_menu_callback(button, foreign)
        self.assertEqual({}, foreign.user_data)
        self.assertIs(original, self.context.user_data[inputs._MENU])
        button.effective_user.id = 7
        button.callback_query.message = self.message(999)
        await inputs.input_menu_callback(button, self.context)
        self.assertIs(original, self.context.user_data[inputs._MENU])

    async def test_expired_wait_or_menu_requires_fresh_intent(self):
        await self.enter_mode('vod')
        record = self.context.user_data[inputs._WAIT]
        cancel = self.latest_button('Отмена')
        with patch.object(inputs.time, 'monotonic', return_value=record['created_at']+inputs.PENDING_TTL_SECONDS):
            await inputs.input_menu_callback(cancel, self.context)
            update = self.request('123456789')
            await inputs.text_input(update, self.context)
            self.assertIn('истекло', update.message.reply_text.await_args.args[0])
        self.assertNotIn(inputs._WAIT, self.context.user_data)
        self.metadata.assert_not_awaited()

    async def test_cancel_and_start_do_not_destroy_existing_format_selection(self):
        set_pending(self.context, 'vod', '999')
        bind_pending_message(self.context, self.message(90))
        pending = self.context.user_data['vod_pending']
        await inputs.channel_command(self.request(), self.context)
        await commands.cancel_command(self.request(), self.context)
        self.assertIs(pending, self.context.user_data['vod_pending'])
        await commands.start_command(self.request(), self.context)
        self.assertIs(pending, self.context.user_data['vod_pending'])
        await inputs.text_input(self.request('123456789'), self.context)
        self.assertIs(pending, self.context.user_data['vod_pending'])
        self.metadata.assert_not_awaited()
        await commands.cancel_command(self.request(), self.context)
        self.assertNotIn('vod_pending', self.context.user_data)

    async def test_missing_channel_and_api_failure_are_reported_and_route_is_reusable(self):
        for error, user, status in ((None, None, 'not_found'),
                                     (TwitchAPIError('MOCK_SECRET', status=503), None, 'api_error')):
            with self.subTest(status=status):
                videos.fetch_user_by_login.side_effect = error
                videos.fetch_user_by_login.return_value = user
                result = await inputs.channel_command(self.request(args=['channel']), self.context)
                self.assertEqual(status, result.status)
                self.assertNotIn(inputs._ROUTE, self.context.user_data)
                self.assertNotIn('vod_pending', self.context.user_data)
        videos.fetch_user_by_login.side_effect = None
        from src.twitch_api.videos import TwitchUser
        videos.fetch_user_by_login.return_value = TwitchUser('123', 'channel', 'Channel')
        result = await inputs.text_input(self.request('channel'), self.context)
        self.assertEqual('ready', result.status)

    async def test_parallel_text_messages_do_not_start_two_preparations(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def slow(*args):
            entered.set()
            await release.wait()
            return SimpleNamespace(length_seconds=60)
        self.metadata.side_effect = slow
        first = self.task(inputs.text_input(self.request('123456789'), self.context))
        await asyncio.wait_for(entered.wait(), 1)
        second = self.request('999')
        await inputs.text_input(second, self.context)
        self.assertIn('ещё выполняется', second.message.reply_text.await_args.args[0])
        self.metadata.assert_awaited_once()
        release.set()
        await first
        self.assertEqual('123456789', self.context.user_data['vod_pending']['vod_id'])

    async def test_missing_vod_or_metadata_error_cleans_input_route_for_retry(self):
        for error in (None, TwitchAPIError('MOCK_SECRET', status=503)):
            with self.subTest(error=type(error).__name__):
                self.metadata.side_effect = error
                self.metadata.return_value = None
                update = self.request(args=['123456789'])
                result = await inputs.vod_command(update, self.context)
                self.assertIn(result, ('missing', 'error'))
                self.assertNotIn('MOCK_SECRET', update.message.reply_text.await_args.args[0])
                self.assertNotIn(inputs._ROUTE, self.context.user_data)
                self.assertNotIn('vod_link_lookup', self.context.user_data)
                self.assertNotIn('vod_pending', self.context.user_data)
        self.metadata.side_effect = None
        self.metadata.return_value = SimpleNamespace(length_seconds=60)
        await inputs.text_input(self.request('123456789'), self.context)
        self.assertEqual('123456789', self.context.user_data['vod_pending']['vod_id'])

    async def test_old_route_completion_does_not_clear_new_channel_route(self):
        metadata_entered, metadata_release = asyncio.Event(), asyncio.Event()
        channel_entered, channel_release = asyncio.Event(), asyncio.Event()

        async def slow_metadata(*args):
            metadata_entered.set()
            await metadata_release.wait()
            return SimpleNamespace(length_seconds=60)

        async def slow_channel(*args):
            channel_entered.set()
            await channel_release.wait()
            return videos.TwitchUser('123', 'channel', 'Channel')

        self.metadata.side_effect = slow_metadata
        videos.fetch_user_by_login.side_effect = slow_channel
        old = self.task(inputs.text_input(self.request('123456789'), self.context))
        await asyncio.wait_for(metadata_entered.wait(), 1)
        await inputs.channel_command(self.request(), self.context)
        new = self.task(inputs.text_input(self.request('channel'), self.context))
        await asyncio.wait_for(channel_entered.wait(), 1)
        route = self.context.user_data[inputs._ROUTE]
        metadata_release.set()
        await old
        self.assertIs(route, self.context.user_data[inputs._ROUTE])
        self.assertNotIn('vod_pending', self.context.user_data)
        channel_release.set()
        self.assertEqual('ready', (await new).status)
        self.assertNotIn(inputs._ROUTE, self.context.user_data)

    async def test_parallel_channel_requests_keep_one_owned_lookup(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def slow(*args):
            entered.set()
            await release.wait()
            from src.twitch_api.videos import TwitchUser
            return TwitchUser('123', 'channel', 'Channel')
        videos.fetch_user_by_login.side_effect = slow
        first = self.task(inputs.text_input(self.request('channel'), self.context))
        await asyncio.wait_for(entered.wait(), 1)
        await inputs.channel_command(self.request(args=['other']), self.context)
        videos.fetch_user_by_login.assert_awaited_once()
        self.app.create_task.assert_called_once()
        release.set()
        await first

    async def test_mode_change_or_cancel_during_metadata_suppresses_old_prompt(self):
        for action in ('switch', 'cancel'):
            with self.subTest(action=action):
                self.context = self.context_for_user()
                entered, release = asyncio.Event(), asyncio.Event()
                async def slow(*args):
                    entered.set()
                    await release.wait()
                    return SimpleNamespace(length_seconds=60)
                self.metadata.side_effect = slow
                self.messages.clear()
                first = self.task(inputs.text_input(self.request('123456789'), self.context))
                await asyncio.wait_for(entered.wait(), 1)
                if action == 'switch':
                    await inputs.channel_command(self.request(), self.context)
                else:
                    await commands.cancel_command(self.request(), self.context)
                release.set()
                await first
                self.assertNotIn('vod_pending', self.context.user_data)
                self.assertNotIn('vod_link_lookup', self.context.user_data)
                self.assertFalse(any(args and args[0] == vod._FORMAT_PROMPT_TEXT for _, args, _ in self.messages))
                if action == 'switch':
                    self.assertEqual('channel', self.context.user_data[inputs._WAIT]['mode'])

    async def test_global_slot_and_existing_format_message_binding_still_apply(self):
        other = self.context_for_user()
        operation = begin_download(other)
        try:
            await inputs.vod_command(self.request(args=['123456789']), self.context)
            self.assertNotIn('vod_pending', self.context.user_data)
            self.metadata.assert_not_awaited()
            self.ready.return_value = self.ready_result()
            await inputs.text_input(self.request('123456789'), self.context)
            self.assertIn('vod_result_choice', self.context.user_data)
            self.assertEqual(operation, self.app.bot_data['vod_global_operation'])
            self.runner.assert_not_awaited()
        finally:
            finish_download(other, operation)
        self.ready.return_value = None
        await inputs.text_input(self.request('123456789'), self.context)
        old_prompt = self.messages[-1][0]
        await commands.cancel_command(self.request(), self.context)
        await inputs.vod_command(self.request(args=['999']), self.context)
        pending = self.context.user_data['vod_pending']
        await vod.vod_format_chosen(self.callback('vodfmt:html_online', old_prompt), self.context)
        self.assertIs(pending, self.context.user_data['vod_pending'])
        self.runner.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()
