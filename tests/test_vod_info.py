import asyncio
import unittest
from unittest.mock import AsyncMock
from telegram.error import BadRequest
import test_stale_vod_lists as fixture
import test_channel_vods as cards
from src.handlers import handlers_channel_vods as lists, handlers_vod_info as info
from src.vod_list import callback, build_list
from src.twitch_api import videos


URL = 'https://static-cdn.jtvnw.net/cf_vods/example/thumb/thumb0-%{width}x%{height}.jpg'


class InfoTests(unittest.IsolatedAsyncioTestCase):
    _setupAsyncioRunner = fixture.StaleListTests._setupAsyncioRunner
    setUp = fixture.StaleListTests.setUp
    asyncTearDown = fixture.StaleListTests.asyncTearDown
    context_for_user = fixture.StaleListTests.context_for_user
    task = fixture.StaleListTests.task
    message = fixture.StaleListTests.message
    update = fixture.StaleListTests.update
    ready_result = fixture.StaleListTests.ready_result
    open = fixture.StaleListTests.open

    async def setup_list(self, preview=URL):
        videos.fetch_archive_vods.return_value = ([cards.vod(123456789, thumbnail_url=preview), cards.vod(2, thumbnail_url=preview)], None)
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

    async def test_buttons_compact_date_and_no_image_on_navigation(self):
        source, view = await self.setup_list()
        text, kb = build_list(view)
        self.assertIn('UTC', text)
        self.assertEqual(['02.01.26 03:04', 'ℹ️'], [b.text for b in kb.inline_keyboard[0]])
        await lists.channel_vods_callback(self.update(callback(view, 'pages'), source), self.context)
        source.reply_photo.assert_not_awaited()
        self.metadata.assert_not_awaited()

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

    async def test_processing_from_photo_uses_new_text_message_and_existing_ready_flow(self):
        for ready in (None, self.ready_result()):
            source, view = await self.setup_list()
            await self.show(source, view)
            record = self.record(view)
            data = f'ui:vi:{view.session_id}:{record.nonce}:process'
            self.ready.return_value = ready
            await info.info_callback(self.update(data, self.card), self.context)
            await info.info_callback(self.update(data, self.card), self.context)
            await asyncio.gather(*self.tasks)
            self.card.edit_text.assert_not_awaited()
            source.edit_text.assert_awaited_once()
            if ready:
                progress = self.messages[-1][0]
                self.assertEqual('Готовый HTML доступен.', progress.edit_text.await_args.args[0])
            else:
                self.runner.assert_awaited_once()
            await asyncio.sleep(0)

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
