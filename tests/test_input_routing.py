"""Automatic text and strict commands reuse the existing VOD/browser flows."""
import asyncio
from types import SimpleNamespace
import unittest

from telegram import Update
from telegram.ext import CommandHandler

import test_channel_selection as fixture
from src.handlers import handlers_input as inputs, handlers_cmd as commands, handlers_vod as vod
from src.handlers.handlers_state import (
    set_pending, bind_pending_message, begin_download, finish_download, is_cancelled,
)
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

    def assert_no_input_mode(self):
        self.assertNotIn('text_input_wait', self.context.user_data)
        self.assertNotIn('text_input_menu', self.context.user_data)

    def assert_instruction(self, message):
        options = message.reply_text.await_args.kwargs
        self.assertNotIn('reply_markup', options)
        self.assertTrue(options['link_preview_options'].is_disabled)
        self.assert_no_input_mode()

    async def test_text_and_commands_for_vod_reuse_bound_confirmation_without_download(self):
        for method, text in (('text', 'https://twitch.tv/videos/123456789'), ('text', '123456789'),
                             ('command', '123456789'), ('command', 'https://twitch.tv/videos/123456789')):
            with self.subTest(method=method, text=text):
                self.context = self.context_for_user()
                self.metadata.reset_mock()
                self.messages.clear()
                update = self.request(text, args=[text])
                await (inputs.text_input if method == 'text' else inputs.vod_command)(update, self.context)
                self.metadata.assert_awaited_once_with(self.http, 'mock-client', '123456789')
                pending = self.context.user_data['vod_pending']
                prompt, args, options = self.messages[-1]
                self.assertEqual(vod._FORMAT_PROMPT_TEXT, args[0])
                self.assertEqual(('123456789', prompt.message_id), (pending['vod_id'], pending['message_id']))
                self.assertEqual('vodfmt:html_online', options['reply_markup'].inline_keyboard[0][0].callback_data)
                self.assertNotIn(inputs._ROUTE, self.context.user_data)
                self.assert_no_input_mode()
                self.runner.assert_not_awaited()
                self.app.create_task.assert_not_called()

    async def test_text_and_commands_for_channels_use_real_browser_and_owned_tasks(self):
        for method, text in (('text', 'fixture_channel'), ('text', 'https://www.twitch.tv/ChAnNeL'),
                             ('command', 'channel'), ('command', 'https://twitch.tv/channel')):
            with self.subTest(method=method, text=text):
                self.context = self.context_for_user()
                videos.fetch_user_by_login.reset_mock()
                self.app.create_task.reset_mock()
                update = self.request(text, args=[text])
                result = await (inputs.text_input if method == 'text' else inputs.channel_command)(update, self.context)
                self.assertEqual('ready', result.status)
                login = 'fixture_channel' if text == 'fixture_channel' else 'channel'
                videos.fetch_user_by_login.assert_awaited_once_with(self.http, login)
                self.app.create_task.assert_called_once()
                self.metadata.assert_not_awaited()
                self.assertNotIn('vod_pending', self.context.user_data)
                self.assertNotIn(inputs._ROUTE, self.context.user_data)
                self.assert_no_input_mode()

    async def test_empty_commands_only_show_examples_without_state_or_buttons(self):
        for command in (inputs.vod_command, inputs.channel_command):
            with self.subTest(command=command.__name__):
                update = self.request()
                await command(update, self.context)
                self.assert_instruction(update.message)
                self.assertEqual({}, self.context.user_data)
                self.assertEqual(0, len(self.browser))
                self.app.create_task.assert_not_called()
                videos.fetch_user_by_login.assert_not_awaited()
                self.metadata.assert_not_awaited()

    async def test_empty_command_does_not_control_next_text_type(self):
        await inputs.vod_command(self.request(), self.context)
        self.assertEqual('ready', (await inputs.text_input(self.request('fixture_channel'), self.context)).status)
        await inputs.channel_command(self.request(), self.context)
        await inputs.text_input(self.request('123456789'), self.context)
        self.assertEqual('123456789', self.context.user_data['vod_pending']['vod_id'])
        self.assert_no_input_mode()

    async def test_vod_channel_name_is_format_error_but_plain_name_opens_channel(self):
        update = self.request(args=['fixture_channel'])
        await inputs.vod_command(update, self.context)
        self.assertIn('Неверный формат.', update.message.reply_text.await_args.args[0])
        self.assert_instruction(update.message)
        videos.fetch_user_by_login.assert_not_awaited()
        self.metadata.assert_not_awaited()
        self.assertEqual({}, self.context.user_data)
        self.assertEqual('ready', (await inputs.text_input(self.request('fixture_channel'), self.context)).status)
        videos.fetch_user_by_login.assert_awaited_once_with(self.http, 'fixture_channel')

    async def test_wrong_command_type_never_reroutes_or_installs_mode(self):
        for command, argument in ((inputs.vod_command, 'https://twitch.tv/channel'),
                                  (inputs.channel_command, 'https://twitch.tv/videos/123456789'),
                                  (inputs.vod_command, 'https://example.invalid/videos/123'),
                                  (inputs.channel_command, 'bad input')):
            with self.subTest(argument=argument):
                update = self.request(args=argument.split())
                await command(update, self.context)
                self.assertIn('Неверный формат', update.message.reply_text.await_args.args[0])
                self.assert_instruction(update.message)
                self.assertEqual({}, self.context.user_data)
        self.metadata.assert_not_awaited()
        videos.fetch_user_by_login.assert_not_awaited()

    async def test_channel_command_can_force_numeric_login_without_starting_vod(self):
        result = await inputs.channel_command(self.request(args=['123456789']), self.context)
        self.assertEqual('ready', result.status)
        videos.fetch_user_by_login.assert_awaited_once_with(self.http, '123456789')
        self.metadata.assert_not_awaited()

    async def test_extra_arguments_and_unknown_text_are_not_partial_matches(self):
        for text in ('unrecognized input text', 'https://example.invalid/videos/123', 'https://clips.twitch.tv/Slug'):
            with self.subTest(text=text):
                update = self.request(text)
                await inputs.text_input(update, self.context)
                self.assertIn('Не удалось распознать', update.message.reply_text.await_args.args[0])
                self.assertEqual({}, self.context.user_data)
        update = self.request(args=['123', '456'])
        await inputs.vod_command(update, self.context)
        self.assert_instruction(update.message)
        self.assertEqual({}, self.context.user_data)
        self.metadata.assert_not_awaited()
        videos.fetch_user_by_login.assert_not_awaited()

    async def test_start_and_help_are_stateless_and_disable_link_previews(self):
        for command in (commands.start_command, commands.help_command):
            with self.subTest(command=command.__name__):
                update = self.request()
                await command(update, self.context)
                self.assert_instruction(update.message)
                text = update.message.reply_text.await_args.args[0]
                self.assertNotIn('Выбери действие', text)
                self.assertEqual({}, self.context.user_data)
        text = self.messages[-1][1][0]
        for phrase in ('Бот автоматически определяет тип запроса.', '/vod — получить чат VOD.',
                       '/channel — посмотреть VODs канала.', 'Команды позволяют указать тип запроса вручную.'):
            self.assertIn(phrase, text)
        self.assertNotIn('http', text)
        self.app.create_task.assert_not_called()
        videos.fetch_user_by_login.assert_not_awaited()
        self.metadata.assert_not_awaited()

    async def test_all_legacy_input_callbacks_are_rejected_without_state_changes(self):
        set_pending(self.context, 'vod', '999')
        pending = self.context.user_data['vod_pending']
        for owner in (7, 8):
            for action in ('vod', 'channel', 'cancel', 'unknown'):
                update = self.callback('ui:input:'+'a'*16+':'+action, owner=owner)
                for _ in range(2):
                    await inputs.input_menu_callback(update, self.context)
                    self.assertTrue(update.callback_query.answer.await_args.kwargs['show_alert'])
                    self.assertIn('устарело', update.callback_query.answer.await_args.args[0])
                    self.assertIs(pending, self.context.user_data['vod_pending'])
                    self.assert_no_input_mode()
        self.assertEqual({'vod_pending': pending}, self.context.user_data)
        self.metadata.assert_not_awaited()
        self.app.create_task.assert_not_called()

    async def test_addressed_commands_use_ptb_arguments_and_ignore_other_bots(self):
        bot = SimpleNamespace(username='TDBBot')
        for name, callback, argument in (('vod', inputs.vod_command, '123456789'),
                                          ('channel', inputs.channel_command, 'channel')):
            with self.subTest(name=name):
                self.context = self.context_for_user()
                command = '/'+name+'@TDBBot'
                payload = {'update_id': 1, 'message': {
                    'message_id': 1, 'date': 0, 'chat': {'id': 10, 'type': 'private'},
                    'from': {'id': 7, 'is_bot': False, 'first_name': 'Test'},
                    'text': command+' '+argument,
                    'entities': [{'type': 'bot_command', 'offset': 0, 'length': len(command)}],
                }}
                update = Update.de_json(payload, bot)
                handler = CommandHandler(name, callback)
                check = handler.check_update(update)
                self.assertIsInstance(check, tuple)
                request = self.request()
                handler.collect_additional_context(self.context, update, self.app, check)
                self.assertEqual([argument], self.context.args)
                await handler.callback(request, self.context)
                payload['message']['text'] = '/'+name+'@OtherBot '+argument
                payload['message']['entities'][0]['length'] = len('/'+name+'@OtherBot')
                self.assertIsNone(handler.check_update(Update.de_json(payload, bot)))

    async def test_help_and_empty_commands_preserve_existing_format_selection(self):
        set_pending(self.context, 'vod', '999')
        bind_pending_message(self.context, self.message(90))
        pending = self.context.user_data['vod_pending']
        for command in (commands.start_command, commands.help_command, inputs.vod_command, inputs.channel_command):
            await command(self.request(), self.context)
            self.assertIs(pending, self.context.user_data['vod_pending'])
        await inputs.text_input(self.request('123456789'), self.context)
        self.assertIs(pending, self.context.user_data['vod_pending'])
        self.metadata.assert_not_awaited()
        await commands.cancel_command(self.request(), self.context)
        self.assertNotIn('vod_pending', self.context.user_data)

    async def test_missing_channel_and_api_failure_are_reported_and_route_is_reusable(self):
        for error, status in ((None, 'not_found'), (TwitchAPIError('MOCK_SECRET', status=503), 'api_error')):
            with self.subTest(status=status):
                videos.fetch_user_by_login.side_effect = error
                videos.fetch_user_by_login.return_value = None
                result = await inputs.channel_command(self.request(args=['channel']), self.context)
                self.assertEqual(status, result.status)
                self.assertNotIn(inputs._ROUTE, self.context.user_data)
                self.assertNotIn('vod_pending', self.context.user_data)
                self.assertNotIn('MOCK_SECRET', self.messages[-1][1][0])
        videos.fetch_user_by_login.side_effect = None
        videos.fetch_user_by_login.return_value = videos.TwitchUser('123', 'channel', 'Channel')
        self.assertEqual('ready', (await inputs.text_input(self.request('channel'), self.context)).status)

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
        self.assertIn('Дождитесь завершения предыдущего запроса.', second.message.reply_text.await_args.args[0])
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
                text = update.message.reply_text.await_args.args[0]
                self.assertIn('не найден' if error is None else 'Не удалось', text)
                self.assertNotIn('MOCK_SECRET', text)
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
        await commands.cancel_command(self.request(), self.context)
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
            return videos.TwitchUser('123', 'channel', 'Channel')
        videos.fetch_user_by_login.side_effect = slow
        first = self.task(inputs.text_input(self.request('channel'), self.context))
        await asyncio.wait_for(entered.wait(), 1)
        await inputs.channel_command(self.request(args=['other']), self.context)
        videos.fetch_user_by_login.assert_awaited_once()
        self.app.create_task.assert_called_once()
        release.set()
        await first

    async def test_help_and_empty_commands_during_lookup_do_not_cancel_or_replace_request(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def slow(*args):
            entered.set()
            await release.wait()
            return SimpleNamespace(length_seconds=60)
        self.metadata.side_effect = slow
        first = self.task(inputs.text_input(self.request('123456789'), self.context))
        await asyncio.wait_for(entered.wait(), 1)
        route = self.context.user_data[inputs._ROUTE]
        for command in (commands.start_command, commands.help_command, inputs.vod_command, inputs.channel_command):
            await command(self.request(), self.context)
            self.assertIs(route, self.context.user_data[inputs._ROUTE])
            self.assert_no_input_mode()
        release.set()
        await first
        self.assertEqual('123456789', self.context.user_data['vod_pending']['vod_id'])

    async def test_cancel_during_metadata_suppresses_old_prompt(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def slow(*args):
            entered.set()
            await release.wait()
            return SimpleNamespace(length_seconds=60)
        self.metadata.side_effect = slow
        first = self.task(inputs.text_input(self.request('123456789'), self.context))
        await asyncio.wait_for(entered.wait(), 1)
        await commands.cancel_command(self.request(), self.context)
        release.set()
        await first
        self.assertNotIn('vod_pending', self.context.user_data)
        self.assertNotIn('vod_link_lookup', self.context.user_data)
        self.assertFalse(any(args and args[0] == vod._FORMAT_PROMPT_TEXT for _, args, _ in self.messages))

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
        await vod.format_cancel_callback(self.callback('ui:format_cancel', old_prompt), self.context)
        self.assertIs(pending, self.context.user_data['vod_pending'])
        self.runner.assert_not_awaited()

    async def test_old_ready_callbacks_do_not_replace_new_format_dialog(self):
        old = self.ready_result()
        self.ready.side_effect = lambda vod_id: old if vod_id == '123456789' else None
        await inputs.text_input(self.request('123456789'), self.context)
        ready_message, _, options = self.messages[-1]
        regen = options['reply_markup'].inline_keyboard[1][0].callback_data
        await inputs.text_input(self.request('999'), self.context)
        pending = self.context.user_data['vod_pending']
        for data in (regen, 'ui:result:open:123456789'):
            if ':open:' in data:
                self.ready.side_effect = lambda vod_id: None
            for _ in range(2):
                await vod.vod_result_callback(self.callback(data, ready_message), self.context)
                self.assertIs(pending, self.context.user_data['vod_pending'])
                self.assertNotIn('vod_result_choice', self.context.user_data)
        self.runner.assert_not_awaited()
        self.app.create_task.assert_not_called()

    async def test_existing_ready_html_can_still_open_with_another_format_dialog(self):
        set_pending(self.context, 'vod', '999')
        pending = self.context.user_data['vod_pending']
        self.ready.return_value = self.ready_result()
        await vod.vod_result_callback(self.callback('ui:result:open:123456789'), self.context)
        self.context.bot.send_message.assert_awaited_once()
        self.assertIs(pending, self.context.user_data['vod_pending'])
        self.runner.assert_not_awaited()

    async def test_progress_cancel_still_targets_current_users_operation(self):
        operation = begin_download(self.context)
        try:
            await vod.pending_cancel_callback(self.callback('ui:pending_cancel'), self.context)
            self.assertTrue(is_cancelled(self.context, operation))
            self.assertEqual(operation, self.app.bot_data['vod_global_operation'])
        finally:
            finish_download(self.context, operation)


if __name__ == '__main__':
    unittest.main()
