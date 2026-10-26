"""Isolated archive browsing: fake Helix, fake Telegram, no SQLite or chat API."""
import asyncio
from dataclasses import replace
import inspect
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from telegram.error import BadRequest

from src.channel_browser import ChannelBrowser, VodSelection, callback_data
from src.handlers import handlers_channel_vods as handlers
from src.twitch_api import videos
from src.twitch_api.client import TwitchAPIError
from src.twitch_api.videos import ArchiveVod, TwitchUser
from src.ui.ui_channel_vods import build_channel_vod_card


def vod(number=1, **changes):
    value = ArchiveVod(str(number), '123', 'channel', 'Channel', 'Title '+str(number),
                       '2026-01-02T03:04:05Z', '2026-01-02T04:04:05Z', '1h2m3s',
                       'https://www.twitch.tv/videos/'+str(number), 'https://images.invalid/a.jpg', 'public')
    return replace(value, **changes)


def buttons(view):
    _, keyboard = build_channel_vod_card(view)
    return [button for row in keyboard.inline_keyboard for button in row] if keyboard else []


class ChannelVodsTests(unittest.IsolatedAsyncioTestCase):
    def _setupAsyncioRunner(self):
        self._asyncioRunner = asyncio.Runner(debug=True, loop_factory=asyncio.SelectorEventLoop)

    def setUp(self):
        self.now = 1000.0
        self.browser = ChannelBrowser(clock=lambda: self.now)
        self.user = TwitchUser('123', 'channel', 'Channel')
        self.fetch_user = AsyncMock(return_value=self.user)
        self.fetch_vods = AsyncMock(return_value=([vod()], None))
        for name, value in (('fetch_user_by_login', self.fetch_user), ('fetch_archive_vods', self.fetch_vods)):
            patched = patch.object(videos, name, value)
            patched.start()
            self.addCleanup(patched.stop)
        chat_patch = patch('src.twitch_api.gql_fetch_comments', side_effect=AssertionError('Chat API called'))
        self.chat = chat_patch.start()
        self.addCleanup(chat_patch.stop)
        preparation = patch.object(handlers, 'prepare_vod_choice', AsyncMock(return_value='format'))
        self.prepare = preparation.start()
        self.addCleanup(preparation.stop)
        self.tasks = []
        self.http = object()

    async def asyncTearDown(self):
        for task in self.tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.chat.assert_not_called()

    def task(self, coroutine):
        task = asyncio.create_task(coroutine)
        self.tasks.append(task)
        return task

    async def open(self, records=None, cursor=None, owner=7, message=20):
        if records is not None:
            self.fetch_vods.return_value = (records, cursor)
        return await self.browser.open(self.http, owner, 'channel', 10, message)

    async def go(self, view, action, owner=7, chat=10, message=20):
        return await self.browser.handle_callback(self.http, owner, callback_data(view, action), chat, message)

    async def test_empty_single_and_full_api_page_have_exact_counts(self):
        for count in (0, 1, 100):
            with self.subTest(count=count):
                view = await self.open([vod(i+1) for i in range(count)])
                self.assertEqual(str(count), view.count_label)
                self.assertTrue(view.complete)
                self.assertEqual('ready' if count else 'empty', view.status)
                self.assertEqual(count > 1, view.has_next)
                self.assertFalse(view.has_previous)
                self.assertEqual('1' if count else None, view.vod.id if view.vod else None)
                self.assertFalse(any(b.text == '⬅️' for b in buttons(view)))

    async def test_lazy_pagination_forward_and_back_do_not_refetch_loaded_vods(self):
        first = await self.open([vod(1), vod(2)], 'A')
        self.assertEqual('2+', first.count_label)
        second = await self.go(first, 'next')
        self.assertEqual('2', second.vod.id)
        self.fetch_vods.assert_awaited_once()
        self.fetch_vods.return_value = ([vod(3)], None)
        third = await self.go(second, 'next')
        self.fetch_vods.assert_awaited_with(self.http, '123', 'A')
        self.assertEqual(2, self.fetch_vods.await_count)
        self.assertEqual(('3', '3', False), (third.vod.id, third.count_label, third.has_next))
        self.assertEqual('3/3', next(b.text for b in buttons(third) if b.text.startswith('3/')))
        second = await self.go(third, 'prev')
        first = await self.go(second, 'prev')
        self.assertEqual('1', first.vod.id)
        self.assertFalse(first.has_previous)
        again = await self.go(first, 'prev')
        self.assertEqual(0, again.position)
        self.assertEqual(2, self.fetch_vods.await_count)

    async def test_deduplication_within_and_across_pages_preserves_first_metadata(self):
        first = await self.open([vod(1), vod(1, title='duplicate'), vod(2)], 'A')
        self.assertEqual(2, first.loaded)
        self.assertEqual('Title 1', first.vod.title)
        last = await self.go(first, 'next')
        self.fetch_vods.return_value = ([vod(2), vod(3), vod(3)], None)
        third = await self.go(last, 'next')
        self.assertEqual(('3', '3'), (third.vod.id, third.count_label))

    async def test_empty_and_duplicate_only_pages_keep_continuation_without_auto_walk(self):
        empty = await self.open([], 'A')
        self.assertEqual(('empty_page', '0+', True), (empty.status, empty.count_label, empty.has_next))
        self.fetch_vods.assert_awaited_once()
        self.fetch_vods.return_value = ([vod(1)], 'B')
        first = await self.go(empty, 'next')
        self.assertEqual('1', first.vod.id)
        self.fetch_vods.return_value = ([vod(1)], 'C')
        duplicate = await self.go(first, 'next')
        self.assertEqual(('1', '1+', True), (duplicate.vod.id, duplicate.count_label, duplicate.has_next))
        self.assertEqual(3, self.fetch_vods.await_count)

    async def test_cursor_repetition_and_longer_cycles_stop_requests(self):
        for next_cursors in (('A',), ('B', 'A')):
            with self.subTest(next_cursors=next_cursors):
                view = await self.open([vod(1)], 'A')
                for index, cursor in enumerate(next_cursors):
                    self.fetch_vods.return_value = ([vod(index+2)], cursor)
                    view = await self.go(view, 'next')
                self.assertEqual(('api_error', 'pagination'), (view.status, view.reason))
                self.assertFalse(view.has_next)
                self.assertFalse(view.can_retry)
                count = self.fetch_vods.await_count
                view = await self.go(view, 'retry')
                self.assertEqual(count, self.fetch_vods.await_count)
                self.assertIsInstance(await self.go(view, 'select'), VodSelection)

    async def test_record_and_page_limits_keep_bounded_partial_results(self):
        self.browser = ChannelBrowser(max_vods=2, clock=lambda: self.now)
        view = await self.open([vod(1), vod(2), vod(3)])
        self.assertEqual(('limit', '2+', False), (view.status, view.count_label, view.complete))
        view = await self.go(view, 'next')
        self.assertFalse(view.has_next)
        self.assertEqual('2', (await self.go(view, 'select')).vod_id)
        self.browser = ChannelBrowser(max_pages=1, clock=lambda: self.now)
        view = await self.open([vod(1)], 'A')
        self.assertEqual('limit', view.status)
        self.assertFalse(view.has_next)
        count = self.fetch_vods.await_count
        await self.go(view, 'next')
        self.assertEqual(count, self.fetch_vods.await_count)

    async def test_metadata_limits_reject_oversized_page_without_partial_commit(self):
        for records, cursor in (([vod(1), vod(2, title='x'*5000)], None), ([vod(1)], 'x'*2049)):
            with self.subTest(cursor_length=len(cursor or '')):
                view = await self.open(records, cursor)
                self.assertEqual(('api_error', 0, False), (view.status, view.loaded, view.complete))

    async def test_session_capacity_absolute_expiry_and_one_channel_per_owner(self):
        self.browser = ChannelBrowser(max_sessions=1, ttl=10, clock=lambda: self.now)
        first = await self.open()
        count = self.fetch_user.await_count
        self.assertEqual('capacity', (await self.open(owner=8)).status)
        self.assertEqual(count, self.fetch_user.await_count)
        replacement = await self.open(message=21)
        self.assertNotEqual(first.session_id, replacement.session_id)
        self.assertEqual('expired', (await self.go(first, 'select')).status)
        self.assertEqual(1, len(self.browser))
        self.now += 10
        self.assertEqual('expired', (await self.go(replacement, 'select', message=21)).status)
        self.assertEqual(0, len(self.browser))
        self.assertEqual('ready', (await self.open(owner=8)).status)

    async def test_foreign_owner_and_different_message_are_rejected(self):
        view = await self.open()
        count = self.fetch_vods.await_count
        for values in ({'owner': 8}, {'chat': 11}, {'message': 21}):
            with self.subTest(values=values):
                rejected = await self.go(view, 'select', **values)
                self.assertEqual('forbidden', rejected.status)
                self.assertIsNone(rejected.vod)
        self.assertTrue(self.browser.is_current(view))
        self.assertEqual(count, self.fetch_vods.await_count)

    async def test_old_keyboard_malformed_and_wrong_selection_callbacks_are_safe(self):
        first = await self.open([vod(1), vod(2)])
        current = await self.go(first, 'next')
        self.assertEqual('stale', (await self.go(first, 'select')).status)
        wrong = callback_data(current, 'select').rsplit(':', 1)[0]+':1'
        result = await self.browser.handle_callback(self.http, 7, wrong, 10, 20)
        self.assertEqual('stale', result.status)
        for data in ('ui:histpage:0', 'ui:cv:1:0:next', 'ui:cv:'+current.session_id+':2:select',
                     callback_data(current, 'next')+':1'):
            with self.subTest(data=data):
                result = await self.browser.handle_callback(self.http, 7, data, 10, 20)
                self.assertEqual('invalid', result.status)
        self.assertTrue(self.browser.is_current(current))

    async def test_selection_returns_stable_id_and_no_chat_request(self):
        view = await self.open([vod(123, url='https://untrusted.invalid/link')])
        count = self.fetch_vods.await_count
        selected = await self.go(view, 'select')
        self.assertIsInstance(selected, VodSelection)
        self.assertEqual((7, '123', 'channel', 'https://www.twitch.tv/videos/123'),
                         (selected.owner_id, selected.vod_id, selected.channel_login, selected.vod_url))
        self.assertEqual('selected', selected.view.status)
        self.assertEqual(count, self.fetch_vods.await_count)
        self.assertEqual('stale', (await self.go(view, 'select')).status)

    async def test_not_found_empty_and_api_errors_have_different_states(self):
        self.fetch_user.return_value = None
        view = await self.open()
        self.assertEqual('not_found', view.status)
        self.fetch_vods.assert_not_awaited()
        self.fetch_user.side_effect = TwitchAPIError('hidden details', status=401)
        view = await self.open()
        self.assertEqual(('api_error', 'authorization'), (view.status, view.reason))
        self.assertNotIn('hidden details', build_channel_vod_card(view)[0])
        self.fetch_user.side_effect = None
        self.fetch_user.return_value = self.user
        self.fetch_vods.side_effect = TwitchAPIError('temporary', status=503, retryable=True)
        view = await self.open()
        self.assertEqual('api_error', view.status)
        count = self.fetch_user.await_count
        self.fetch_vods.side_effect = None
        self.fetch_vods.return_value = ([], None)
        view = await self.go(view, 'retry')
        self.assertEqual(('empty', '0'), (view.status, view.count_label))
        self.assertEqual(count, self.fetch_user.await_count)

    async def test_rate_limit_cooldown_is_shared_and_does_not_sleep(self):
        self.fetch_user.side_effect = TwitchAPIError('limited', status=429, retry_after=30)
        view = await self.open()
        self.assertEqual(('rate_limited', 30), (view.status, view.retry_after))
        self.assertEqual('rate_limited', (await self.open(owner=8)).status)
        view = await self.go(view, 'retry')
        self.fetch_user.assert_awaited_once()
        self.fetch_vods.assert_not_awaited()
        self.now += 31
        self.fetch_user.side_effect = None
        view = await self.go(view, 'retry')
        self.assertEqual('ready', view.status)

    async def test_error_on_next_page_preserves_loaded_vods_and_retry_cursor(self):
        view = await self.open([vod(1), vod(2)], 'A')
        view = await self.go(view, 'next')
        self.fetch_vods.side_effect = TwitchAPIError('temporary failure', status=503, retryable=True)
        failed = await self.go(view, 'next')
        self.assertEqual(('api_error', '2', '2+'), (failed.status, failed.vod.id, failed.count_label))
        self.assertTrue(failed.can_retry)
        self.fetch_vods.side_effect = None
        self.fetch_vods.return_value = ([vod(3)], None)
        finished = await self.go(failed, 'retry')
        self.fetch_vods.assert_awaited_with(self.http, '123', 'A')
        self.assertEqual(('ready', '3', '3'), (finished.status, finished.vod.id, finished.count_label))

    async def test_concurrent_next_clicks_make_only_one_additional_request(self):
        view = await self.open([vod(1)], 'A')
        entered, release = asyncio.Event(), asyncio.Event()
        async def slow(*args):
            entered.set()
            await release.wait()
            return [vod(2)], None
        self.fetch_vods.side_effect = slow
        task = self.task(self.go(view, 'next'))
        await asyncio.wait_for(entered.wait(), 1)
        rejected = await self.go(view, 'next')
        self.assertEqual('stale', rejected.status)
        self.assertEqual(2, self.fetch_vods.await_count)
        release.set()
        self.assertEqual('2', (await task).vod.id)

    async def test_slow_user_does_not_block_other_users_and_same_owner_is_guarded(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def slow(http, login):
            if login == 'channel':
                entered.set()
                await release.wait()
            return self.user
        self.fetch_user.side_effect = slow
        task = self.task(self.open())
        await asyncio.wait_for(entered.wait(), 1)
        self.assertEqual('busy', (await self.open()).status)
        other = await asyncio.wait_for(self.browser.open(self.http, 8, 'other', 10, 21), 1)
        self.assertEqual('ready', other.status)
        release.set()
        self.assertEqual('ready', (await task).status)

    async def test_expired_inflight_operation_cannot_replace_new_session(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def slow(http, login):
            if login == 'channel':
                entered.set()
                await release.wait()
            return self.user
        self.fetch_user.side_effect = slow
        task = self.task(self.open())
        await asyncio.wait_for(entered.wait(), 1)
        self.now += self.browser.ttl
        new = await self.browser.open(self.http, 7, 'newchannel', 10, 21)
        release.set()
        self.assertEqual('expired', (await task).status)
        self.assertTrue(self.browser.is_current(new))
        self.assertEqual(1, len(self.browser))
        self.fetch_vods.assert_awaited_once()

    async def test_page_arriving_after_expiry_cannot_update_replacement_card(self):
        view = await self.open([vod(1)], 'A')
        entered, release = asyncio.Event(), asyncio.Event()
        async def slow(*args):
            entered.set()
            await release.wait()
            return [vod(2)], None
        self.fetch_vods.side_effect = slow
        task = self.task(self.go(view, 'next'))
        await asyncio.wait_for(entered.wait(), 1)
        self.now += self.browser.ttl
        self.fetch_vods.side_effect = None
        new = await self.open([vod(99)], message=21)
        release.set()
        self.assertEqual('expired', (await task).status)
        self.assertTrue(self.browser.is_current(new))
        self.assertEqual('99', new.vod.id)

    async def test_cancellation_releases_loading_state_for_a_fresh_session(self):
        entered = asyncio.Event()
        async def slow(*args):
            entered.set()
            await asyncio.Event().wait()
        self.fetch_vods.side_effect = slow
        task = self.task(self.open())
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.fetch_vods.side_effect = None
        self.assertEqual('ready', (await self.open()).status)

    async def test_card_escapes_metadata_has_no_fake_chat_counts_and_limits_callback_size(self):
        self.fetch_user.return_value = replace(self.user, display_name='<Owner>&')
        view = await self.open([vod(99999999999999999999, title='<'*3000)])
        text, _ = build_channel_vod_card(view)
        self.assertIn('&lt;Owner&gt;&amp;', text)
        self.assertIn('01:02:03', text)
        self.assertIn('2026-01-02 03:04 UTC', text)
        self.assertNotIn('💬', text)
        self.assertNotIn('👥', text)
        self.assertLess(len(text), 4096)
        self.assertTrue(all(len(b.callback_data.encode('utf-8')) <= 64 for b in buttons(view) if b.callback_data))
        largest = replace(view, revision=999999999)
        self.assertLessEqual(len(callback_data(largest, 'select').encode('utf-8')), 64)
        for duration, expected in (('0s', '00:00:00'), ('2m', '00:02:00'), ('bad', '—')):
            with self.subTest(duration=duration):
                text, _ = build_channel_vod_card(replace(view, vod=replace(view.vod, duration=duration)))
                self.assertIn('⏱ '+expected, text)

    def adapter_context(self):
        app = SimpleNamespace(bot_data={'aiohttp': self.http, 'vod_global_operation': 'unchanged'},
                              create_task=Mock(side_effect=self.task))
        app.bot_data[handlers._STORE_KEY] = self.browser
        return SimpleNamespace(application=app, user_data={'vod_busy': True})

    def message(self):
        return SimpleNamespace(chat_id=10, message_id=20, edit_text=AsyncMock(), reply_text=AsyncMock())

    def update(self, view, action, owner=7):
        query = SimpleNamespace(data=callback_data(view, action), message=self.message(), answer=AsyncMock())
        return SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=owner))

    async def test_show_returns_owned_task_before_slow_helix_finishes(self):
        context = self.adapter_context()
        progress, incoming = self.message(), self.message()
        incoming.reply_text.return_value = progress
        entered, release = asyncio.Event(), asyncio.Event()
        async def slow(*args):
            entered.set()
            await release.wait()
            return self.user
        self.fetch_user.side_effect = slow
        task = await handlers.show_channel_vods(incoming, context, 7, 'channel')
        await asyncio.wait_for(entered.wait(), 1)
        self.assertFalse(task.done())
        progress.edit_text.assert_not_awaited()
        release.set()
        await task
        progress.edit_text.assert_awaited_once()
        self.assertEqual('unchanged', context.application.bot_data['vod_global_operation'])
        self.assertEqual({'vod_busy': True}, context.user_data)

    async def test_adapter_rejects_foreign_callback_without_editing_owners_card(self):
        view = await self.open()
        context = self.adapter_context()
        update = self.update(view, 'select', owner=8)
        self.assertIsNone(await handlers.channel_vods_callback(update, context))
        update.callback_query.answer.assert_awaited_once()
        update.callback_query.message.edit_text.assert_not_awaited()
        self.assertIn('другому пользователю', update.callback_query.message.reply_text.await_args.args[0])
        self.assertTrue(self.browser.is_current(view))

    async def test_adapter_selection_delegates_preparation_and_updates_browse_card(self):
        view = await self.open()
        context = self.adapter_context()
        update = self.update(view, 'select')
        selected = await handlers.channel_vods_callback(update, context)
        self.assertIsInstance(selected, VodSelection)
        self.assertEqual('1', selected.vod_id)
        update.callback_query.message.edit_text.assert_awaited_once()
        self.assertIn('VOD выбран.', update.callback_query.message.edit_text.await_args.args[0])
        self.assertEqual({'vod_busy': True}, context.user_data)
        self.fetch_vods.assert_awaited_once()
        self.prepare.assert_awaited_once()
        self.assertEqual('1', self.prepare.await_args.args[3])
        self.assertTrue(self.prepare.await_args.kwargs['is_current']())

    async def test_task_creation_failure_closes_coroutine_without_creating_a_session(self):
        context = self.adapter_context()
        context.application.create_task.side_effect = RuntimeError('task startup failed')
        incoming = self.message()
        incoming.reply_text.return_value = self.message()
        with self.assertRaises(RuntimeError):
            await handlers.show_channel_vods(incoming, context, 7, 'channel')
        coroutine = context.application.create_task.call_args.args[0]
        self.assertEqual(inspect.CORO_CLOSED, inspect.getcoroutinestate(coroutine))
        self.assertEqual(0, len(self.browser))
        self.fetch_user.assert_not_awaited()

    async def test_noop_handles_unchanged_message_and_handler_factory_is_nonblocking(self):
        view = await self.open()
        update = self.update(view, 'noop')
        update.callback_query.message.edit_text.side_effect = BadRequest('Message is not modified')
        await handlers.channel_vods_callback(update, self.adapter_context())
        handler = handlers.build_channel_vods_handler()
        self.assertFalse(handler.block)
        self.assertIs(handlers.channel_vods_callback, handler.callback)
        self.assertTrue(handler.pattern.match(callback_data(view, 'next')))
        self.assertFalse(handler.pattern.match('ui:histpage:0'))


if __name__ == '__main__':
    unittest.main()
