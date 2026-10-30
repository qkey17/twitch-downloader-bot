import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from telegram.error import BadRequest
import test_stale_vod_lists as fixture
import test_channel_vods as cards
from src.handlers import handlers_channel_vods as lists, handlers_vod_info as info
from src.vod_list import callback, build_list
from src.twitch_api import videos
from src.ui.ui_history import ready_offer_text


URL = 'https://static-cdn.jtvnw.net/cf_vods/example/thumb/thumb0-%{width}x%{height}.jpg'


class InfoTests(unittest.IsolatedAsyncioTestCase):
    _setupAsyncioRunner = fixture.StaleListTests._setupAsyncioRunner
    def setUp(self):
        fixture.StaleListTests.setUp(self)
        # Keep these existing tests on the uncached information surface; the
        # date-to-ready path has its own tests. Do not patch the shared registry.
        p = patch.object(lists, 'results', SimpleNamespace(get_ready_result=lambda vod_id: None))
        p.start()
        self.addCleanup(p.stop)
    asyncTearDown = fixture.StaleListTests.asyncTearDown
    context_for_user = fixture.StaleListTests.context_for_user
    task = fixture.StaleListTests.task
    message = fixture.StaleListTests.message
    update = fixture.StaleListTests.update
    ready_result = fixture.StaleListTests.ready_result
    open = fixture.StaleListTests.open

    async def setup_list(self, preview=URL, count=2):
        vods = [cards.vod(123456789, thumbnail_url=preview)]
        vods.extend(cards.vod(n, thumbnail_url=preview) for n in range(2, count+1))
        videos.fetch_archive_vods.return_value = (vods, None)
        source, view = await self.open(20)
        self.card = self.message(99)
        self.card.edit_media = AsyncMock()
        self.card.edit_reply_markup = AsyncMock()
        source.reply_photo = AsyncMock(return_value=self.card)
        return source, view

    async def show(self, source, view, index=0):
        query = self.update(callback(view, 'info', index), source)
        result = await lists.channel_vods_callback(query, self.context)
        query.callback_query.answer.assert_awaited_once()
        return result

    def record(self, view):
        return self.browser._sessions[view.session_id].info_card

    async def test_full_date_opens_info_only_and_no_image_on_navigation(self):
        source, view = await self.setup_list()
        text, kb = build_list(view)
        self.assertNotIn('Даты VOD', text)
        self.assertEqual(['2026-01-02 03:04 UTC'], [b.text for b in kb.inline_keyboard[0]])
        await lists.channel_vods_callback(self.update(callback(view, 'pages'), source), self.context)
        source.reply_photo.assert_not_awaited()
        self.metadata.assert_not_awaited()
        # Use the actual rendered date callback, not an internal select action.
        view = self.browser._view(self.browser._sessions[view.session_id])
        data = build_list(view)[1].inline_keyboard[0][0].callback_data
        await lists.channel_vods_callback(self.update(data, source), self.context)
        source.reply_photo.assert_awaited_once()
        self.metadata.assert_not_awaited()
        self.runner.assert_not_awaited()

    async def test_confirmed_custom_jpeg_preview_is_sent_at_480x270(self):
        url = URL.replace('thumb0-', 'custom-ed2dc86a-5097-450f-8644-4635266055ed-').replace('.jpg', '.jpeg')
        source, view = await self.setup_list(url)
        await self.show(source, view)
        self.assertEqual(url.replace('%{width}', '480').replace('%{height}', '270'), source.reply_photo.await_args.args[0])
        source.reply_text.assert_not_awaited()

    async def test_safe_diagnostic_categories_and_fallbacks(self):
        cases = [('', False, ['url_missing', 'text_fallback']),
                 ('https://example.invalid/SECRET.jpg', False, ['url_rejected', 'text_fallback']),
                 ('https://vod-secure.twitch.tv/_404/404_processing_480x270.png', False, ['placeholder', 'text_fallback']),
                 (URL, True, ['photo_send_failed', 'text_fallback'])]
        for url, failed, expected in cases:
            with self.subTest(expected=expected):
                source, view = await self.setup_list(url)
                if failed:
                    source.reply_photo.side_effect = BadRequest('SECRET token user=123 https://private.invalid')
                with self.assertLogs(info.__name__, level='INFO') as logs:
                    await self.show(source, view)
                self.assertEqual(expected, [r.args[0] for r in logs.records])
                for record in logs.records:
                    self.assertEqual('vod_preview category=%s', record.msg)
                    self.assertIsNone(record.exc_info)
                self.assertNotIn('SECRET', str(logs.output))
                self.assertFalse(self.record(view).photo)
                self.assertLessEqual(source.reply_photo.await_count, 1)

    async def test_photo_edit_failure_has_bounded_send_and_text_fallback(self):
        source, view = await self.setup_list()
        await self.show(source, view)
        self.card.edit_media.side_effect = BadRequest('SECRET')
        source.reply_photo.side_effect = BadRequest('SECRET')
        with self.assertLogs(info.__name__, level='INFO') as logs:
            await self.show(source, view, 1)
        self.assertEqual(['photo_edit_failed', 'photo_send_failed', 'text_fallback'], [r.args[0] for r in logs.records])
        self.card.edit_media.assert_awaited_once()
        self.card.delete.assert_awaited_once()
        self.assertEqual(2, source.reply_photo.await_count)  # Initial + one fallback attempt.
        self.assertFalse(self.record(view).photo)

    async def test_logging_failure_does_not_break_text_information(self):
        source, view = await self.setup_list('')
        with patch.object(info.logging.Logger, 'info', side_effect=RuntimeError('broken logger')):
            await self.show(source, view)
        self.assertFalse(self.record(view).photo)
        self.assertIn('Title', source.reply_text.await_args.args[0])

    async def test_info_uses_helix_title_duration_and_photo_without_processing(self):
        source, view = await self.setup_list()
        await self.show(source, view)
        source.edit_text.assert_awaited_once()  # Original list untouched.
        caption = source.reply_photo.await_args.kwargs['caption']
        self.assertIn('Title 123456789', caption)
        self.assertIn('01:02:03', caption)
        self.assertIn('2026-01-02 03:04 UTC', caption)
        self.assertIn('Channel', caption)
        self.assertIn('480x270', source.reply_photo.await_args.args[0])
        self.runner.assert_not_awaited()
        self.metadata.assert_not_awaited()
        videos.fetch_archive_vods.assert_awaited_once()

    async def test_missing_or_rejected_preview_falls_back_to_text(self):
        for preview in ('', 'https://evil.invalid/image.jpg', URL):
            source, view = await self.setup_list(preview)
            source.reply_photo.side_effect = BadRequest('failed')
            await self.show(source, view)
            self.assertFalse(self.record(view).photo)
            self.assertIn('Title', source.reply_text.await_args.args[0])

    async def test_repeat_and_different_info_reuse_one_photo_card(self):
        source, view = await self.setup_list()
        await self.show(source, view)
        await self.show(source, view)
        await self.show(source, view, 1)
        source.reply_photo.assert_awaited_once()
        self.card.edit_media.assert_awaited_once()
        self.assertEqual(1, self.record(view).index)

    async def test_back_deletes_info_and_preserves_source_list(self):
        source, view = await self.setup_list()
        await self.show(source, view)
        record = self.record(view)
        query = self.update(f'ui:vi:{view.session_id}:{record.nonce}:back', self.card)
        await info.info_callback(query, self.context)
        query.callback_query.answer.assert_awaited_once()
        self.card.delete.assert_awaited_once()
        self.assertTrue(self.browser.is_current(view))
        source.edit_text.assert_awaited_once()

    def process_update(self, view):
        return self.update(f'ui:vi:{view.session_id}:{self.record(view).nonce}:process', self.card)

    async def test_processing_from_photo_edits_source_text_and_rejects_repeat(self):
        source, view = await self.setup_list()
        await self.show(source, view)
        query = self.process_update(view)
        await info.info_callback(query, self.context)
        stale = self.update(query.callback_query.data, self.card)
        await info.info_callback(stale, self.context)
        stale.callback_query.answer.assert_awaited_once_with(lists.STALE_BUTTON)
        await asyncio.gather(*self.tasks)
        self.runner.assert_awaited_once()
        self.assertEqual(source.message_id, self.runner.await_args.kwargs['progress_message_id'])
        self.assertEqual('Загрузка чата…', source.edit_text.await_args.kwargs['text'])
        self.card.delete.assert_awaited_once()
        self.card.edit_text.assert_not_awaited()
        source.reply_text.assert_not_awaited()
        self.assertEqual([], self.messages)

    async def test_left_selection_consumes_info_before_answer_and_uses_standard_flow(self):
        source, view = await self.setup_list()
        await self.show(source, view)
        record = self.record(view)
        query = self.update(callback(view, 'select', 0), source)
        async def answer():
            self.assertEqual('', record.nonce)
            self.assertIsNone(self.browser._sessions[view.session_id].list_render)
        query.callback_query.answer.side_effect = answer
        await lists.channel_vods_callback(query, self.context)
        await asyncio.gather(*self.tasks)
        self.card.delete.assert_awaited_once()
        self.runner.assert_awaited_once()
        self.assertEqual(source.message_id, self.runner.await_args.kwargs['progress_message_id'])
        source.reply_text.assert_not_awaited()

    async def test_photo_ready_offer_has_all_actions_and_back_restores_list(self):
        source, view = await self.setup_list()
        await self.show(source, view)
        self.ready.return_value = self.ready_result()
        await info.info_callback(self.process_update(view), self.context)
        self.assertEqual(ready_offer_text(self.ready_result()), source.edit_text.await_args.args[0])
        rows = source.edit_text.await_args.kwargs['reply_markup'].inline_keyboard
        self.assertEqual(['Открыть HTML', 'Сгенерировать заново', 'Вернуться к списку'], [r[0].text for r in rows])
        self.assertEqual(source.message_id, self.context.user_data['chat_choice']['message'])
        await lists.channel_vods_callback(self.update(rows[2][0].callback_data, source), self.context)
        self.assertIn('VODs канала: 2', source.edit_text.await_args.args[0])
        self.assertIsNotNone(self.browser._sessions[view.session_id].list_render)
        self.assertNotIn('chat_choice', self.context.user_data)
        self.runner.assert_not_awaited()
        source.reply_text.assert_not_awaited()

    async def test_photo_ready_open_edits_source_and_can_return_via_selected_back(self):
        from src.handlers import handlers_chat as chat
        source, view = await self.setup_list()
        await self.show(source, view)
        self.ready.return_value = self.ready_result()
        await info.info_callback(self.process_update(view), self.context)
        rows = source.edit_text.await_args.kwargs['reply_markup'].inline_keyboard
        await chat.chat_callback(self.update(rows[0][0].callback_data, source), self.context)
        self.assertEqual(source.message_id, self.context.bot.edit_message_text.await_args.kwargs['message_id'])
        await lists.channel_vods_callback(self.update(rows[2][0].callback_data, source), self.context)
        self.assertIn('VODs канала: 2', source.edit_text.await_args.args[0])
        self.context.bot.send_message.assert_not_awaited()
        source.reply_text.assert_not_awaited()

    async def test_photo_ready_regenerate_uses_source_and_single_existing_runner(self):
        from src.handlers import handlers_chat as chat
        source, view = await self.setup_list()
        await self.show(source, view)
        self.ready.return_value = self.ready_result()
        await info.info_callback(self.process_update(view), self.context)
        rows = source.edit_text.await_args.kwargs['reply_markup'].inline_keyboard
        query = self.update(rows[1][0].callback_data, source)
        await chat.chat_callback(query, self.context)
        await chat.chat_callback(self.update(query.callback_query.data, source), self.context)
        await asyncio.gather(*self.tasks)
        self.runner.assert_awaited_once()
        self.assertEqual(source.message_id, self.runner.await_args.kwargs['progress_message_id'])
        source.reply_text.assert_not_awaited()

    async def test_failed_deletion_leaves_no_actionable_photo_or_extra_messages(self):
        for left in (False, True):
            with self.subTest(left=left):
                source, view = await self.setup_list()
                await self.show(source, view)
                self.ready.return_value = self.ready_result()
                stale = self.process_update(view)
                self.card.delete.side_effect = BadRequest('cannot delete')
                if left:
                    await lists.channel_vods_callback(self.update(callback(view, 'select', 0), source), self.context)
                else:
                    await info.info_callback(self.process_update(view), self.context)
                await info.info_callback(stale, self.context)
                stale.callback_query.answer.assert_awaited_once_with(lists.STALE_BUTTON)
                self.assertEqual(ready_offer_text(self.ready_result()), source.edit_text.await_args.args[0])
                self.assertIsNone(self.record(view))
                source.reply_text.assert_not_awaited()
        self.runner.assert_not_awaited()

    async def test_slow_cleanup_does_not_delay_screen_channel_replacement_or_new_info(self):
        for left in (False, True):
            with self.subTest(left=left):
                source, view = await self.setup_list()
                await self.show(source, view)
                self.ready.return_value = self.ready_result()
                old_card = self.card
                entered, release = asyncio.Event(), asyncio.Event()
                async def slow_delete():
                    self.assertEqual(ready_offer_text(self.ready_result()), source.edit_text.await_args.args[0])
                    entered.set()
                    await release.wait()
                old_card.delete.side_effect = slow_delete
                stale = self.process_update(view)
                if left:
                    selecting = self.task(lists.channel_vods_callback(self.update(callback(view, 'select', 0), source), self.context))
                else:
                    selecting = self.task(info.info_callback(self.process_update(view), self.context))
                try:
                    await asyncio.wait_for(entered.wait(), 1)
                    await info.info_callback(stale, self.context)
                    stale.callback_query.answer.assert_awaited_once_with(lists.STALE_BUTTON)
                    rows = source.edit_text.await_args.kwargs['reply_markup'].inline_keyboard
                    await asyncio.wait_for(lists.channel_vods_callback(self.update(rows[2][0].callback_data, source), self.context), 1)
                    session = self.browser._sessions[view.session_id]
                    # Reopen info while the old message's deletion is pending.
                    self.card = self.message(199)
                    self.card.edit_media, self.card.edit_reply_markup = AsyncMock(), AsyncMock()
                    source.reply_photo.return_value = self.card
                    await self.show(source, self.browser._view(session))
                    new_card = self.record(view)
                    release.set()
                    await asyncio.wait_for(selecting, 1)
                    self.assertIs(new_card, self.record(view))
                    self.assertTrue(new_card.nonce)
                    old_card.delete.assert_awaited_once()
                    self.assertIn('VODs канала: 2', source.edit_text.await_args.args[0])
                    # A new channel must also work while a captured cleanup waits.
                    self.card.delete.side_effect = slow_delete
                    entered.clear()
                    release.clear()
                    selecting = self.task(info.info_callback(self.process_update(self.browser._view(session)), self.context))
                    await asyncio.wait_for(entered.wait(), 1)
                    count = source.edit_text.await_count
                    new_source, new_view = await asyncio.wait_for(self.open(21, 'second'), 1)
                    release.set()
                    await asyncio.wait_for(selecting, 1)
                    self.assertTrue(self.browser.is_current(new_view))
                    self.assertEqual(count, source.edit_text.await_count)
                    self.assertIn('VODs канала: 2', new_source.edit_text.await_args.args[0])
                    source.reply_text.assert_not_awaited()
                finally:
                    release.set()
                    await asyncio.wait_for(selecting, 1)

    async def test_left_and_info_process_race_selects_exactly_once(self):
        from types import SimpleNamespace
        for left_first in (False, True):
            with self.subTest(left_first=left_first):
                source, view = await self.setup_list()
                await self.show(source, view)
                self.runner.reset_mock()
                entered, release = asyncio.Event(), asyncio.Event()
                async def slow_meta(*args):
                    entered.set()
                    await release.wait()
                    return SimpleNamespace(length_seconds=60)
                self.metadata.side_effect = slow_meta
                left, photo = self.update(callback(view, 'select', 0), source), self.process_update(view)
                first = lists.channel_vods_callback(left, self.context) if left_first else info.info_callback(photo, self.context)
                selecting = self.task(first)
                try:
                    await asyncio.wait_for(entered.wait(), 1)
                    other = info.info_callback(photo, self.context) if left_first else lists.channel_vods_callback(left, self.context)
                    await asyncio.wait_for(other, 1)
                    rejected = photo if left_first else left
                    rejected.callback_query.answer.assert_awaited_once_with(lists.STALE_BUTTON)
                    release.set()
                    await asyncio.wait_for(selecting, 1)
                    await asyncio.gather(*self.tasks)
                    await asyncio.sleep(0)  # Existing runner's slot-release callback.
                    self.runner.assert_awaited_once()
                    self.card.delete.assert_awaited_once()
                    left.callback_query.answer.assert_awaited_once()
                    photo.callback_query.answer.assert_awaited_once()
                    source.reply_text.assert_not_awaited()
                finally:
                    release.set()

    async def test_cleanup_timeout_is_bounded_after_ready_screen(self):
        source, view = await self.setup_list()
        await self.show(source, view)
        self.ready.return_value = self.ready_result()
        async def stuck():
            self.assertEqual(ready_offer_text(self.ready_result()), source.edit_text.await_args.args[0])
            await asyncio.Event().wait()
        self.card.delete.side_effect = stuck
        real_timeout = asyncio.timeout
        with patch.object(info.asyncio, 'timeout', side_effect=lambda seconds: real_timeout(min(seconds, 0.02))):
            await asyncio.wait_for(info.info_callback(self.process_update(view), self.context), 1)
        self.assertIsNone(self.record(view))
        source.reply_text.assert_not_awaited()

    async def test_slow_photo_deletion_does_not_delay_download_progress(self):
        source, view = await self.setup_list()
        await self.show(source, view)
        entered, release = asyncio.Event(), asyncio.Event()
        async def slow_delete():
            self.assertEqual('Загрузка чата…', source.edit_text.await_args.kwargs['text'])
            entered.set()
            await release.wait()
        self.card.delete.side_effect = slow_delete
        selecting = self.task(info.info_callback(self.process_update(view), self.context))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            self.assertFalse(selecting.done())
            self.runner.assert_awaited_once()
            self.assertEqual(source.message_id, self.runner.await_args.kwargs['progress_message_id'])
            self.assertNotIn(view.session_id, self.browser._sessions)
            count = source.edit_text.await_count
            await asyncio.wait_for(self.open(21, 'second'), 1)
            release.set()
            await asyncio.wait_for(selecting, 1)
            self.assertEqual(count, source.edit_text.await_count)
            source.reply_text.assert_not_awaited()
            self.card.edit_text.assert_not_awaited()
        finally:
            release.set()
            await asyncio.wait_for(selecting, 1)

    async def test_cancelled_cleanup_keeps_screen_and_invalidates_actions(self):
        source, view = await self.setup_list()
        await self.show(source, view)
        self.ready.return_value = self.ready_result()
        entered, cancelled = asyncio.Event(), asyncio.Event()
        async def stuck():
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        self.card.delete.side_effect = stuck
        stale = self.process_update(view)
        selecting = self.task(info.info_callback(self.process_update(view), self.context))
        await asyncio.wait_for(entered.wait(), 1)
        selecting.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(selecting, 1)
        self.assertTrue(cancelled.is_set())
        await info.info_callback(stale, self.context)
        stale.callback_query.answer.assert_awaited_once_with(lists.STALE_BUTTON)
        self.assertEqual(ready_offer_text(self.ready_result()), source.edit_text.await_args.args[0])
        self.assertIsNone(self.record(view))

    async def test_telegram_answer_or_screen_failure_still_cleans_consumed_info(self):
        for failure in ('answer', 'screen'):
            with self.subTest(failure=failure):
                source, view = await self.setup_list()
                await self.show(source, view)
                self.ready.return_value = self.ready_result()
                query = self.process_update(view)
                stale = self.process_update(view)
                if failure == 'answer':
                    query.callback_query.answer.side_effect = BadRequest('cannot answer')
                else:
                    source.edit_text.side_effect = BadRequest('cannot edit')
                with self.assertRaises(BadRequest):
                    await info.info_callback(query, self.context)
                self.card.delete.assert_awaited_once()
                await info.info_callback(stale, self.context)
                stale.callback_query.answer.assert_awaited_once_with(lists.STALE_BUTTON)
                self.assertIsNone(self.record(view))
                source.reply_text.assert_not_awaited()

    async def test_changed_channel_foreign_owner_and_expired_card_cannot_process(self):
        source, view = await self.setup_list()
        await self.show(source, view)
        record = self.record(view)
        data = f'ui:vi:{view.session_id}:{record.nonce}:process'
        query = self.update(data, self.card, owner=8)
        await info.info_callback(query, self.context)
        query.callback_query.answer.assert_awaited_once_with(lists.STALE_BUTTON)
        self.now += 301
        await info.info_callback(self.update(data, self.card), self.context)
        await self.open(21)
        await info.info_callback(self.update(data, self.card), self.context)
        self.runner.assert_not_awaited()

    async def test_navigation_invalidates_info_actions_without_image_fetch(self):
        source, view = await self.setup_list()
        await self.show(source, view)
        record = self.record(view)
        data = f'ui:vi:{view.session_id}:{record.nonce}:process'
        await lists.channel_vods_callback(self.update(callback(view, 'pages'), source), self.context)
        query = self.update(data, self.card)
        await info.info_callback(query, self.context)
        query.callback_query.answer.assert_awaited_once_with(lists.STALE_BUTTON)
        source.reply_photo.assert_awaited_once()
        self.runner.assert_not_awaited()

    async def test_all_navigation_actions_consume_info_before_answer_and_delete_after_render(self):
        cases = [('next', None, None), ('prev', None, 'next'), ('pages', None, None),
                 ('page', 1, 'pages'), ('group', 1, 'pages'), ('back', None, 'pages')]
        for action, number, before in cases:
            with self.subTest(action=action):
                source, view = await self.setup_list(count=70)
                if before:
                    view = await lists.channel_vods_callback(self.update(callback(view, before), source), self.context)
                await self.show(source, view, view.page*5)
                record = self.record(view)
                old_nonce = record.nonce
                query = self.update(callback(view, action, number), source)
                session = self.browser._sessions[view.session_id]
                async def answer():
                    self.assertEqual('', record.nonce)
                    self.assertIsNone(session.info_card)
                    self.card.delete.assert_not_awaited()
                async def deleted():
                    current = self.browser._view(session)
                    self.assertEqual(build_list(current)[0], source.edit_text.await_args.args[0])
                    self.assertFalse(lists._ui_lock(session).locked())
                query.callback_query.answer.side_effect = answer
                self.card.delete.side_effect = deleted
                new_view = await lists.channel_vods_callback(query, self.context)
                query.callback_query.answer.assert_awaited_once()
                self.card.delete.assert_awaited_once()
                self.assertIsNotNone(session.list_render)
                self.assertEqual(build_list(new_view)[0], session.list_render[1])
                for old_action in ('process', 'back'):
                    stale = self.update(f'ui:vi:{view.session_id}:{old_nonce}:{old_action}', self.card)
                    await info.info_callback(stale, self.context)
                    stale.callback_query.answer.assert_awaited_once_with(lists.STALE_BUTTON)
                source.reply_text.assert_not_awaited()
        self.runner.assert_not_awaited()
        self.metadata.assert_not_awaited()

    async def test_grid_sequence_does_not_delete_same_card_again(self):
        source, view = await self.setup_list(count=70)
        await self.show(source, view)
        for action, number in [('pages', None), ('group', 1), ('page', 12), ('pages', None), ('back', None)]:
            view = await lists.channel_vods_callback(self.update(callback(view, action, number), source), self.context)
        self.card.delete.assert_awaited_once()
        self.assertEqual(12, view.page)
        self.assertFalse(view.grid)
        self.assertEqual(build_list(view)[0], source.edit_text.await_args.args[0])
        source.reply_text.assert_not_awaited()

    async def test_failed_navigation_cleanup_preserves_list_and_allows_selection(self):
        source, view = await self.setup_list(count=6)
        await self.show(source, view)
        self.card.delete.side_effect = BadRequest('cannot delete')
        stale = self.process_update(view)
        view = await lists.channel_vods_callback(self.update(callback(view, 'next'), source), self.context)
        await info.info_callback(stale, self.context)
        stale.callback_query.answer.assert_awaited_once_with(lists.STALE_BUTTON)
        self.assertEqual(1, view.page)
        self.assertIsNone(self.record(view))
        self.ready.return_value = self.ready_result()
        await lists.channel_vods_callback(self.update(callback(view, 'select', 5), source), self.context)
        self.assertEqual(ready_offer_text(self.ready_result()), source.edit_text.await_args.args[0])
        source.reply_text.assert_not_awaited()

    async def test_slow_navigation_cleanup_allows_new_info_and_never_clears_or_edits_it(self):
        source, view = await self.setup_list(count=6)
        await self.show(source, view)
        old_card = self.card
        entered, release = asyncio.Event(), asyncio.Event()
        async def slow_delete():
            entered.set()
            await release.wait()
        old_card.delete.side_effect = slow_delete
        data = callback(view, 'next')
        navigating = self.task(lists.channel_vods_callback(self.update(data, source), self.context))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            session = self.browser._sessions[view.session_id]
            latest = self.browser._view(session)
            self.assertFalse(lists._ui_lock(session).locked())
            self.assertEqual(1, latest.page)
            self.assertEqual(build_list(latest)[0], source.edit_text.await_args.args[0])
            repeated = self.update(data, source)
            await asyncio.wait_for(lists.channel_vods_callback(repeated, self.context), 1)
            repeated.callback_query.answer.assert_awaited_once_with(lists.STALE_BUTTON)
            self.card = self.message(199)
            self.card.edit_media, self.card.edit_reply_markup = AsyncMock(), AsyncMock()
            source.reply_photo.return_value = self.card
            await asyncio.wait_for(asyncio.gather(self.show(source, latest, 5), self.show(source, latest, 5)), 1)
            current_card = self.record(view)
            self.assertEqual(2, source.reply_photo.await_count)  # Old + exactly one new.
            list_edits = source.edit_text.await_count
            release.set()
            await asyncio.wait_for(navigating, 1)
            self.assertIs(current_card, self.record(view))
            self.assertTrue(current_card.nonce)
            self.assertEqual(list_edits, source.edit_text.await_count)
            self.card.delete.assert_not_awaited()
            self.card.edit_media.assert_not_awaited()
            # The new info card can still select via the shared preparation path.
            self.ready.return_value = self.ready_result()
            await info.info_callback(self.process_update(latest), self.context)
            self.assertEqual(ready_offer_text(self.ready_result()), source.edit_text.await_args.args[0])
            source.reply_text.assert_not_awaited()
        finally:
            release.set()
            await asyncio.wait_for(navigating, 1)

    async def test_channel_replacement_during_navigation_cleanup_preserves_new_list(self):
        source, view = await self.setup_list(count=6)
        await self.show(source, view)
        entered, release = asyncio.Event(), asyncio.Event()
        async def slow_delete():
            entered.set()
            await release.wait()
        self.card.delete.side_effect = slow_delete
        navigating = self.task(lists.channel_vods_callback(self.update(callback(view, 'next'), source), self.context))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            new_source, latest = await asyncio.wait_for(self.open(21, 'second'), 1)
            old_edits, new_edits = source.edit_text.await_count, new_source.edit_text.await_count
            self.assertTrue(source.edit_text.await_args.args[0].endswith(lists.STALE_LIST))
            release.set()
            await asyncio.wait_for(navigating, 1)
            self.assertTrue(self.browser.is_current(latest))
            self.assertEqual(old_edits, source.edit_text.await_count)
            self.assertEqual(new_edits, new_source.edit_text.await_count)
            self.card.delete.assert_awaited_once()
        finally:
            release.set()
            await asyncio.wait_for(navigating, 1)

    async def test_navigation_cleanup_timeout_and_cancel_leave_rendered_list_usable(self):
        for cancel in (False, True):
            with self.subTest(cancel=cancel):
                source, view = await self.setup_list(count=6)
                await self.show(source, view)
                entered, stopped = asyncio.Event(), asyncio.Event()
                async def stuck():
                    entered.set()
                    try:
                        await asyncio.Event().wait()
                    finally:
                        stopped.set()
                self.card.delete.side_effect = stuck
                real_timeout = asyncio.timeout
                with patch.object(info.asyncio, 'timeout', side_effect=lambda seconds: real_timeout(min(seconds, 0.05))):
                    navigating = self.task(lists.channel_vods_callback(self.update(callback(view, 'next'), source), self.context))
                    await asyncio.wait_for(entered.wait(), 1)
                    if cancel:
                        navigating.cancel()
                        with self.assertRaises(asyncio.CancelledError):
                            await asyncio.wait_for(navigating, 1)
                    else:
                        await asyncio.wait_for(navigating, 1)
                self.assertTrue(stopped.is_set())
                session = self.browser._sessions[view.session_id]
                self.assertIsNone(session.info_card)
                self.assertEqual(build_list(self.browser._view(session))[0], source.edit_text.await_args.args[0])
                latest = await asyncio.wait_for(lists.channel_vods_callback(self.update(callback(self.browser._view(session), 'prev'), source), self.context), 1)
                self.assertEqual(0, latest.page)
                source.reply_text.assert_not_awaited()

    async def test_navigation_render_failure_still_cleans_inert_card_and_unlocks(self):
        source, view = await self.setup_list(count=6)
        await self.show(source, view)
        stale = self.process_update(view)
        source.edit_text.side_effect = BadRequest('cannot edit')
        with self.assertRaises(BadRequest):
            await lists.channel_vods_callback(self.update(callback(view, 'next'), source), self.context)
        self.card.delete.assert_awaited_once()
        session = self.browser._sessions[view.session_id]
        self.assertFalse(lists._ui_lock(session).locked())
        await info.info_callback(stale, self.context)
        stale.callback_query.answer.assert_awaited_once_with(lists.STALE_BUTTON)
        source.edit_text.side_effect = None
        await lists.channel_vods_callback(self.update(callback(self.browser._view(session), 'prev'), source), self.context)
        self.assertEqual(0, self.browser._view(session).page)
        self.runner.assert_not_awaited()

    async def test_invalid_or_foreign_navigation_does_not_consume_info(self):
        source, view = await self.setup_list(count=6)
        await self.show(source, view)
        record = self.record(view)
        for data, owner in [(callback(view, 'page', 999), 7), (callback(view, 'next'), 8)]:
            query = self.update(data, source, owner=owner)
            await lists.channel_vods_callback(query, self.context)
            query.callback_query.answer.assert_awaited_once_with(lists.STALE_BUTTON)
        self.assertIs(record, self.record(view))
        self.assertTrue(record.nonce)
        self.card.delete.assert_not_awaited()

    async def test_concurrent_info_clicks_create_one_card(self):
        source, view = await self.setup_list()
        entered, release = asyncio.Event(), asyncio.Event()
        async def slow(*args, **kwargs):
            entered.set()
            await release.wait()
            return self.card
        source.reply_photo.side_effect = slow
        first = self.task(self.show(source, view))
        await asyncio.wait_for(entered.wait(), 1)
        second = self.task(self.show(source, view))
        release.set()
        await asyncio.wait_for(asyncio.gather(first, second), 2)
        source.reply_photo.assert_awaited_once()

    async def test_photo_to_text_replaces_message_instead_of_editing_photo_text(self):
        source, view = await self.setup_list()
        await self.show(source, view)
        session = self.browser._sessions[view.session_id]
        from dataclasses import replace
        session.vods[1] = replace(session.vods[1], thumbnail_url='')
        await self.show(source, self.browser._view(session), 1)
        self.card.delete.assert_awaited_once()
        self.card.edit_text.assert_not_awaited()
        self.assertFalse(self.record(view).photo)

    async def test_deleted_info_can_be_reopened_without_changing_source(self):
        source, view = await self.setup_list()
        await self.show(source, view)
        self.card.edit_reply_markup.side_effect = BadRequest('Message to edit not found')
        await self.show(source, view)
        self.assertEqual(2, source.reply_photo.await_count)
        source.edit_text.assert_awaited_once()

    async def test_only_one_card_retained_and_source_ttl_bounds_actions(self):
        source, view = await self.setup_list()
        await self.show(source, view)
        self.assertLessEqual(self.record(view).expires, self.browser._sessions[view.session_id].expires)
        await self.show(source, view, 1)
        self.assertEqual(1, self.record(view).index)
        self.assertEqual(1, len(self.browser._sessions))
        self.now += 601
        query = self.update(callback(view, 'info', 0), source)
        await lists.channel_vods_callback(query, self.context)
        query.callback_query.answer.assert_awaited_once_with(lists.STALE_BUTTON)

    def test_preview_allowlist_rejects_unsafe_urls(self):
        self.assertIn('480x270', info.preview_url(URL))
        for url in ('http://static-cdn.jtvnw.net/cf_vods/a.jpg',
                    'https://static-cdn.jtvnw.net.evil/cf_vods/a.jpg',
                    'https://user@static-cdn.jtvnw.net/cf_vods/a.jpg',
                    'https://static-cdn.jtvnw.net/cf_vods/a.jpg?redirect=https://evil',
                    'https://static-cdn.jtvnw.net/cf_vods/../a.jpg', 'file:///tmp/a.jpg'):
            self.assertIsNone(info.preview_url(url))
        for path in ('../a.jpeg', '%2e%2e/a.jpeg', 'a.jpeg?next=secret', 'a.jpeg#fragment', 'a.gif', 'a.svg'):
            self.assertIsNone(info.preview_url('https://static-cdn.jtvnw.net/cf_vods/'+path))
