"""Automatic text reuse the existing VOD/browser flows."""
import asyncio
from dataclasses import replace
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import test_channel_selection as fixture
from test_channel_vods import vod as archive_vod
from src.handlers import handlers_input as inputs, handlers_cmd as commands, handlers_vod as vod
from src.handlers.handlers_state import (
    set_pending, bind_pending_message, begin_download, finish_download, is_cancelled,
)
from src.twitch_api import videos
from src.twitch_api.client import TwitchAPIError


class InputRoutingTests(unittest.IsolatedAsyncioTestCase):
    _setupAsyncioRunner = fixture.ChannelSelectionTests._setupAsyncioRunner
    def setUp(self):
        # Preserve regression coverage of the retained legacy preparation flow.
        # Active HTML-only routes are exercised in test_vod_list.py.
        fixture.ChannelSelectionTests.setUp(self)
        legacy = patch.object(inputs, 'prepare_vod_choice', vod.prepare_vod_choice)
        legacy.start()
        self.addCleanup(legacy.stop)
    asyncTearDown = fixture.ChannelSelectionTests.asyncTearDown
    context_for_user = fixture.ChannelSelectionTests.context_for_user
    task = fixture.ChannelSelectionTests.task
    message = fixture.ChannelSelectionTests.message
    callback = fixture.ChannelSelectionTests.update
    def ready_result(self):
        return dict(fixture.ChannelSelectionTests.ready_result(self), id='a'*32)

    def request(self, text='', *, owner=7, number=None):
        if number is None:
            self.incoming_id=getattr(self,'incoming_id',19)+1
            number=self.incoming_id
        message = self.message(number, text)
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

    async def test_ordinary_input_never_claims_a_batch(self):
        from src import admin_batch
        for text, kind in (('all', 'channel'), ('new', 'channel'), ('last', 'channel'), ('channel', 'channel'),
                           ('https://twitch.tv/channel', 'channel'), ('123', 'vod'),
                           ('https://twitch.tv/videos/123', 'vod')):
            with self.subTest(text=text), patch.object(inputs, '_dispatch', AsyncMock()) as dispatch, \
                    patch.object(admin_batch, 'start_batch') as batch:
                await inputs.text_input(self.request(text), self.context)
                self.assertEqual(kind, dispatch.await_args.args[-1].kind)
                batch.assert_not_called()

    async def test_last_routes_offline_or_confirmed_live_to_the_same_vod_flow(self):
        from src import admin_batch, channel_browser
        from src.vod_scheduler import scheduler
        old = replace(archive_vod(1), created_at='2026-01-01T00:00:00Z', stream_id='51')
        recent = replace(archive_vod(2), created_at='2026-01-02T00:00:00Z', stream_id='52')
        for stream_id, expected, text in (
            ('', '2', 'fixture_channel last'),
            ('51', '1', 'https://www.twitch.tv/fixture_channel LAST'),
        ):
            with self.subTest(stream=stream_id), \
                    patch.object(channel_browser, 'current_stream', AsyncMock(return_value=stream_id)), \
                    patch.object(videos, 'fetch_archive_vods', AsyncMock(return_value=([recent, old], None))), \
                    patch.object(admin_batch, 'role', side_effect=AssertionError('last is public')), \
                    patch.object(inputs, 'prepare_vod_choice', AsyncMock()) as prepare:
                update = self.request(text, owner=42)
                await inputs.text_input(update, self.context)
                prepare.assert_awaited_once()
                self.assertEqual((42, expected), prepare.await_args.args[2:])
                self.assertNotIn(admin_batch.KEY, self.app.bot_data)
                self.assertFalse(scheduler(self.app).jobs)
                update.message.reply_text.assert_not_awaited()

    async def test_last_reports_unavailable_live_empty_channel_and_api_failure(self):
        from src import channel_browser
        archived = replace(archive_vod(1), stream_id='51')
        for stream_id, page, notice in (
            ('52', ([archived], None), 'Текущая запись LIVE пока недоступна.'),
            ('', ([], None), 'У канала нет опубликованных VODs.'),
            (None, ([archived], None), 'Не удалось определить последний VOD. Повторите попытку позже.'),
            ('', TwitchAPIError('rate limit', status=429), 'Не удалось определить последний VOD. Повторите попытку позже.'),
        ):
            with self.subTest(notice=notice), \
                    patch.object(channel_browser, 'current_stream', AsyncMock(return_value=stream_id)), \
                    patch.object(videos, 'fetch_archive_vods', AsyncMock(side_effect=[page])), \
                    patch.object(inputs, 'prepare_vod_choice', AsyncMock()) as prepare:
                update = self.request('fixture_channel last')
                await inputs.text_input(update, self.context)
                update.message.reply_text.assert_awaited_once_with(notice)
                prepare.assert_not_awaited()
        for text in ('https://twitch.tv.evil/channel last', 'https://twitch.tv/videos/123 last', 'last new extra'):
            update = self.request(text)
            await inputs.text_input(update, self.context)
            self.assertIn('/help', update.message.reply_text.await_args.args[0])

    async def test_text_for_vod_reuse_bound_confirmation_without_download(self):
        for text in ('https://twitch.tv/videos/123456789', '123456789'):
            with self.subTest(text=text):
                self.context = self.context_for_user()
                self.metadata.reset_mock()
                self.messages.clear()
                update = self.request(text)
                await inputs.text_input(update, self.context)
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

    async def test_text_for_channels_use_real_browser_and_owned_tasks(self):
        for text in ('fixture_channel', 'https://www.twitch.tv/ChAnNeL', 'https://twitch.tv/channel',
                     'https://m.twitch.tv/channel/?tt_medium=mobile_web_share',
                     'https://twitch.tv/channel?sr=a', 'twitch://stream/channel', 'twitch://open?channel=channel'):
            with self.subTest(text=text):
                self.context = self.context_for_user()
                videos.fetch_user_by_login.reset_mock()
                self.app.create_task.reset_mock()
                update = self.request(text)
                result = await inputs.text_input(update, self.context)
                self.assertEqual('ready', result.status)
                login = 'fixture_channel' if text == 'fixture_channel' else 'channel'
                videos.fetch_user_by_login.assert_awaited_once_with(self.http, login)
                self.app.create_task.assert_called_once()
                self.metadata.assert_not_awaited()
                self.assertNotIn('vod_pending', self.context.user_data)
                self.assertNotIn(inputs._ROUTE, self.context.user_data)
                self.assert_no_input_mode()

    async def test_extra_arguments_and_unknown_text_are_not_partial_matches(self):
        for text in ('unrecognized input text', 'https://example.invalid/videos/123', 'https://clips.twitch.tv/Slug'):
            with self.subTest(text=text):
                update = self.request(text)
                await inputs.text_input(update, self.context)
                self.assertIn('Не удалось распознать', update.message.reply_text.await_args.args[0])
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
        self.assertIn('ID или ссылку на трансляцию', text)
        self.assertIn('название канала или ссылку', text)
        self.assertNotIn('Premium', text)
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
                    self.assertFalse(update.callback_query.answer.await_args.kwargs.get('show_alert', False))
                    self.assertEqual('Кнопка больше неактуальна', update.callback_query.answer.await_args.args[0])
                    self.assertIs(pending, self.context.user_data['vod_pending'])
                    self.assert_no_input_mode()
        self.assertEqual({'vod_pending': pending}, self.context.user_data)
        self.metadata.assert_not_awaited()
        self.app.create_task.assert_not_called()

    async def test_help_and_start_preserve_existing_format_selection(self):
        set_pending(self.context, 'vod', '999')
        bind_pending_message(self.context, self.message(90))
        pending = self.context.user_data['vod_pending']
        for command in (commands.start_command, commands.help_command):
            await command(self.request(), self.context)
            self.assertIs(pending, self.context.user_data['vod_pending'])
        await inputs.text_input(self.request('123456789'), self.context)
        self.assertIsNot(pending,self.context.user_data['vod_pending'])
        self.assertEqual('999',pending['vod_id'])
        self.metadata.assert_awaited_once()
        await commands.cancel_command(self.request(), self.context)
        self.assertNotIn('vod_pending', self.context.user_data)

    async def test_missing_channel_and_api_failure_are_reported_and_route_is_reusable(self):
        for error, status in ((None, 'not_found'), (TwitchAPIError('MOCK_SECRET', status=503), 'api_error')):
            with self.subTest(status=status):
                videos.fetch_user_by_login.side_effect = error
                videos.fetch_user_by_login.return_value = None
                result = await inputs.text_input(self.request('channel'), self.context)
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
        waiting = self.task(inputs.text_input(second, self.context))
        await asyncio.sleep(0)
        self.assertFalse(waiting.done())
        self.metadata.assert_awaited_once()
        release.set()
        await first
        await waiting
        self.assertEqual('999',self.context.user_data['vod_pending']['vod_id'])
        self.assertEqual(2,self.metadata.await_count)

    async def test_missing_vod_or_metadata_error_cleans_input_route_for_retry(self):
        for error in (None, TwitchAPIError('MOCK_SECRET', status=503)):
            with self.subTest(error=type(error).__name__):
                self.metadata.side_effect = error
                self.metadata.return_value = None
                update = self.request('123456789')
                result = await inputs.text_input(update, self.context)
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

    async def test_parallel_channel_requests_have_independent_owned_lookups(self):
        entered,release=asyncio.Event(),asyncio.Event()
        async def slow(*args):entered.set();await release.wait();return videos.TwitchUser('123','channel','Channel')
        videos.fetch_user_by_login.side_effect=slow
        first=self.task(inputs.text_input(self.request('channel'),self.context));await asyncio.wait_for(entered.wait(),1)
        second=self.task(inputs.text_input(self.request('other'),self.context))
        for _ in range(10):
            if videos.fetch_user_by_login.await_count==2:break
            await asyncio.sleep(0)
        self.assertEqual(2,videos.fetch_user_by_login.await_count)
        release.set();a,b=await asyncio.gather(first,second)
        self.assertTrue(self.browser.is_current(a));self.assertTrue(self.browser.is_current(b))

    async def test_help_and_start_during_lookup_do_not_cancel_or_replace_request(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def slow(*args):
            entered.set()
            await release.wait()
            return SimpleNamespace(length_seconds=60)
        self.metadata.side_effect = slow
        first = self.task(inputs.text_input(self.request('123456789'), self.context))
        await asyncio.wait_for(entered.wait(), 1)
        route = self.context.user_data[inputs._ROUTE]
        for command in (commands.start_command, commands.help_command):
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
            await inputs.text_input(self.request('123456789'), self.context)
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
        await inputs.text_input(self.request('999'), self.context)
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
                self.assertEqual('123456789',self.context.user_data['vod_result_choice']['vod_id'])
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
