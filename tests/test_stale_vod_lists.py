from callback_assertions import assert_stale
"""List-only invalidation, no Telegram/Twitch or database access."""
import asyncio
from unittest.mock import AsyncMock, patch
import unittest
import test_channel_selection as fixture
from src.handlers import handlers_channel_vods as h
from src.vod_list import VodListBrowser, callback, build_list
from src.ui.ui_history import ready_offer_text


class StaleListTests(unittest.IsolatedAsyncioTestCase):
    _setupAsyncioRunner = fixture.ChannelSelectionTests._setupAsyncioRunner
    asyncTearDown = fixture.ChannelSelectionTests.asyncTearDown
    context_for_user = fixture.ChannelSelectionTests.context_for_user
    task = fixture.ChannelSelectionTests.task
    message = fixture.ChannelSelectionTests.message
    update = fixture.ChannelSelectionTests.update
    ready_result = fixture.ChannelSelectionTests.ready_result

    def setUp(self):
        fixture.ChannelSelectionTests.setUp(self)
        self.browser = VodListBrowser(clock=lambda: self.now)
        self.app.bot_data[h._STORE_KEY] = self.browser

    async def open(self, number, login='channel', owner=7):
        message = self.message(number)
        view = await h._open_and_show(message, self.context, owner, login)
        return message, view

    async def test_replacement_preserves_rendered_text_removes_keyboard_and_keeps_new_list(self):
        old, a = await self.open(20)
        original = build_list(a)[0]
        old.text = 'untrusted stale snapshot'
        new, b = await self.open(21, 'second')
        self.assertEqual(original+'\n'+h.STALE_LIST, old.edit_text.await_args.args[0])
        self.assertIsNone(old.edit_text.await_args.kwargs['reply_markup'])
        old.reply_text.assert_not_awaited()
        calls = old.edit_text.await_count
        await self.open(22, 'third')
        self.assertEqual(calls, old.edit_text.await_count)
        self.assertFalse(self.browser.is_current(b))
        self.assertIn(h.STALE_LIST, new.edit_text.await_args.args[0])
        self.assertEqual(1, old.edit_text.await_args.args[0].count(h.STALE_LIST))

    async def test_stale_and_restart_callbacks_answer_once_without_edit_even_if_busy(self):
        old, a = await self.open(20)
        new, b = await self.open(21)
        self.context.user_data['vod_busy'] = True
        before = old.edit_text.await_count
        q = self.update(callback(a, 'pages'), old)
        await h.channel_vods_callback(q, self.context)
        assert_stale(self, q)
        self.assertEqual(before, old.edit_text.await_count)
        self.app.bot_data[h._STORE_KEY] = VodListBrowser()
        q = self.update(callback(b, 'pages'), new)
        await h.channel_vods_callback(q, self.context)
        assert_stale(self, q)
        new.reply_text.assert_not_awaited()

    async def test_rejected_request_does_not_expire_valid_list(self):
        message, a = await self.open(20)
        await self.open(21, 'invalid login')
        self.assertTrue(self.browser.is_current(a))
        self.assertEqual(1, message.edit_text.await_count)
        result = await h.channel_vods_callback(self.update(callback(a, 'pages'), message), self.context)
        self.assertTrue(self.browser.is_current(result))

    async def test_foreign_owner_cannot_edit_or_invalidate_list(self):
        message, a = await self.open(20)
        q = self.update(callback(a, 'select', 0), message, owner=8)
        await h.channel_vods_callback(q, self.context)
        assert_stale(self, q)
        await self.open(21, owner=8)
        self.assertTrue(self.browser.is_current(a))
        self.assertEqual(1, message.edit_text.await_count)

    async def test_selected_html_message_is_never_replaced_by_old_list(self):
        message, a = await self.open(20)
        self.ready.return_value = self.ready_result()
        await h.channel_vods_callback(self.update(callback(a, 'select', 0), message), self.context)
        calls = message.edit_text.await_count
        await self.open(21)
        self.assertEqual(calls, message.edit_text.await_count)
        self.assertEqual(ready_offer_text(self.ready_result()), message.edit_text.await_args.args[0])

    async def test_started_processing_and_final_card_are_not_rewritten(self):
        message, a = await self.open(20)
        await h.channel_vods_callback(self.update(callback(a, 'select', 0), message), self.context)
        await asyncio.gather(*self.tasks)
        await asyncio.sleep(0)
        calls = message.edit_text.await_count
        await self.open(21)
        self.assertEqual(calls, message.edit_text.await_count)

    async def test_uneditable_old_message_does_not_break_new_session(self):
        message, a = await self.open(20)
        message.edit_text.side_effect = RuntimeError('unavailable')
        new, b = await self.open(21)
        self.assertTrue(self.browser.is_current(b))
        self.assertIsNotNone(new.edit_text.await_args.kwargs['reply_markup'])

    async def test_all_selected_card_states_are_outside_list_ownership(self):
        for number, text in enumerate(('VOD selected', 'HTML offer', 'Progress', 'HTML result', 'Processing error'), 30):
            with self.subTest(state=text):
                message, a = await self.open(number)
                async def selected(message, *args, **kwargs):
                    await message.edit_text(text)
                with patch.object(h, 'prepare_chat', side_effect=selected):
                    await h.channel_vods_callback(self.update(callback(a, 'select', 0), message), self.context)
                calls = message.edit_text.await_count
                await self.open(number+100)
                self.assertEqual(calls, message.edit_text.await_count)
                self.assertEqual(text, message.edit_text.await_args.args[0])

    async def test_back_restores_list_ownership_before_next_replacement(self):
        message, a = await self.open(20)
        self.ready.return_value = self.ready_result()
        selected = await h.channel_vods_callback(self.update(callback(a, 'select', 0), message), self.context)
        await h.channel_vods_callback(self.update(callback(selected.view, 'back'), message), self.context)
        text = message.edit_text.await_args.args[0]
        await self.open(21)
        self.assertEqual(text+'\n'+h.STALE_LIST, message.edit_text.await_args.args[0])

    async def test_loading_another_owner_is_not_serialized_by_old_list_lock(self):
        old, a = await self.open(20)
        entered, release = asyncio.Event(), asyncio.Event()
        async def slow(*args, **kwargs):
            entered.set()
            await release.wait()
        old.edit_text.side_effect = slow
        newer = self.task(self.open(21))
        await asyncio.wait_for(entered.wait(), 1)
        other, view = await asyncio.wait_for(self.open(22, owner=8), 1)
        self.assertTrue(self.browser.is_current(view))
        release.set()
        await asyncio.wait_for(newer, 1)

    async def test_new_list_is_usable_and_replaceable_while_old_edit_is_delayed(self):
        old, a = await self.open(20)
        entered, release = asyncio.Event(), asyncio.Event()
        async def slow(*args, **kwargs):
            entered.set()
            await release.wait()
        old.edit_text.side_effect = slow
        new = self.message(21)
        opening = self.task(h._open_and_show(new, self.context, 7, 'second'))
        await asyncio.wait_for(entered.wait(), 1)
        self.assertFalse(opening.done())
        new.edit_text.assert_awaited_once()  # Before the old edit is released.
        self.assertIsNotNone(new.edit_text.await_args.kwargs['reply_markup'])
        session = next(s for s in self.browser._sessions.values() if s.owner_id == 7)
        b = self.browser._view(session)
        q = self.update(callback(b, 'pages'), new)
        b = await asyncio.wait_for(h.channel_vods_callback(q, self.context), 1)
        q.callback_query.answer.assert_awaited_once()
        stale_a = self.update(callback(a, 'select', 0), old)
        await h.channel_vods_callback(stale_a, self.context)
        assert_stale(self, stale_a)
        # A third request and its selected HTML card must not wait for A either.
        latest, c = await asyncio.wait_for(self.open(22, 'third'), 1)
        stale_b = self.update(callback(b, 'pages'), new)
        await h.channel_vods_callback(stale_b, self.context)
        assert_stale(self, stale_b)
        self.ready.return_value = self.ready_result()
        await h.channel_vods_callback(self.update(callback(c, 'select', 0), latest), self.context)
        before = latest.edit_text.await_count
        release.set()
        await asyncio.wait_for(opening, 1)
        self.assertEqual(before, latest.edit_text.await_count)
        self.assertEqual(ready_offer_text(self.ready_result()), latest.edit_text.await_args.args[0])
        self.assertTrue(old.edit_text.await_args.args[0].endswith(h.STALE_LIST))
        self.assertIsNone(old.edit_text.await_args.kwargs['reply_markup'])

    async def test_old_edit_timeout_occurs_after_new_render_and_remains_bounded(self):
        old, a = await self.open(20)
        new = self.message(21)
        async def stuck(*args, **kwargs):
            new.edit_text.assert_awaited_once()
            await asyncio.Event().wait()
        old.edit_text.side_effect = stuck
        real_timeout = asyncio.timeout
        with patch.object(h.asyncio, 'timeout', side_effect=lambda seconds: real_timeout(min(seconds, 0.02))):
            view = await asyncio.wait_for(h._open_and_show(new, self.context, 7, 'second'), 1)
        self.assertTrue(self.browser.is_current(view))
        self.assertIsNotNone(new.edit_text.await_args.kwargs['reply_markup'])
        self.assertIsNone(getattr(self.browser._sessions.get(a.session_id), 'list_render', None))

    async def test_cancelling_old_cleanup_leaves_new_session_usable_without_background_work(self):
        old, a = await self.open(20)
        entered = asyncio.Event()
        async def stuck(*args, **kwargs):
            entered.set()
            await asyncio.Event().wait()
        old.edit_text.side_effect = stuck
        new = self.message(21)
        opening = self.task(h._open_and_show(new, self.context, 7, 'second'))
        await asyncio.wait_for(entered.wait(), 1)
        new.edit_text.assert_awaited_once()
        opening.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(opening, 1)
        latest, view = await asyncio.wait_for(self.open(22), 1)
        self.assertTrue(self.browser.is_current(view))
        self.assertEqual(2, old.edit_text.await_count)  # Initial render and one cleanup.

    async def test_navigation_edit_is_serialized_before_replacement(self):
        message, a = await self.open(20)
        entered, release = asyncio.Event(), asyncio.Event()
        async def slow(*args, **kwargs):
            if not entered.is_set():
                entered.set()
                await release.wait()
        message.edit_text.side_effect = slow
        nav = self.task(h.channel_vods_callback(self.update(callback(a, 'pages'), message), self.context))
        await asyncio.wait_for(entered.wait(), 1)
        newer = self.task(self.open(21))
        await asyncio.sleep(0)
        release.set()
        await asyncio.wait_for(asyncio.gather(nav, newer), 2)
        self.assertTrue(message.edit_text.await_args.args[0].endswith(h.STALE_LIST))
        self.assertIsNone(message.edit_text.await_args.kwargs['reply_markup'])

    async def test_selection_during_replacement_cannot_restore_old_content(self):
        message, a = await self.open(20)
        entered, release = asyncio.Event(), asyncio.Event()
        from src.twitch_api import videos
        original = videos.fetch_archive_vods.return_value
        async def slow(*args):
            entered.set()
            await release.wait()
            return original
        videos.fetch_archive_vods.side_effect = slow
        newer = self.task(self.open(21))
        await asyncio.wait_for(entered.wait(), 1)
        q = self.update(callback(a, 'select', 0), message)
        await asyncio.wait_for(h.channel_vods_callback(q, self.context), 1)
        assert_stale(self, q)
        release.set()
        await asyncio.wait_for(newer, 2)
        self.metadata.assert_not_awaited()
        self.assertTrue(message.edit_text.await_args.args[0].endswith(h.STALE_LIST))
