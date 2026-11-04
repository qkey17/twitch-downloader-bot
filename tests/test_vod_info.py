"""One active list/card, real sessions, synthetic Telegram/Twitch only."""
import asyncio
from dataclasses import replace
import unittest
from unittest.mock import AsyncMock, patch
from telegram.error import BadRequest
from callback_assertions import assert_stale
import test_ready_chat_preview as fixture
import test_channel_vods as cards
from src.handlers import handlers_channel_vods as lists, handlers_vod_info as info
from src.vod_list import callback, build_list
from src.twitch_api import videos

URL = 'https://static-cdn.jtvnw.net/cf_vods/example/thumb/custom-%{width}x%{height}.jpeg'


class InfoTests(unittest.IsolatedAsyncioTestCase):
    _setupAsyncioRunner = fixture.ReadyPreviewTests._setupAsyncioRunner
    asyncTearDown = fixture.ReadyPreviewTests.asyncTearDown
    context_for_user = fixture.ReadyPreviewTests.context_for_user
    task = fixture.ReadyPreviewTests.task
    update = fixture.ReadyPreviewTests.update
    ready_result = fixture.ReadyPreviewTests.ready_result
    open = fixture.ReadyPreviewTests.open

    def setUp(self):
        fixture.ReadyPreviewTests.setUp(self)
        self.available.clear()
        async def edit(**kwargs):
            message = self.by_id[kwargs.pop('message_id')]
            kwargs.pop('chat_id')
            return await message.edit_text(**kwargs)
        self.context.bot.edit_message_text.side_effect = edit

    def message(self, number=20, text=''):
        message = fixture.ReadyPreviewTests.message(self, number, text)
        async def markup(**kwargs):
            message.keyboard = kwargs.get('reply_markup')
        message.edit_reply_markup = AsyncMock(side_effect=markup)
        return message

    async def setup_list(self, preview=URL, count=2):
        videos.fetch_archive_vods.return_value = ([cards.vod(123456789, thumbnail_url=preview)] +
            [cards.vod(n, thumbnail_url=preview,created_at='2026-01-01T03:04:00Z') for n in range(2, count+1)], None)
        source, view = await self.open(20)
        self.card = source
        return source, view

    async def show(self, source, view, index=0):
        query = self.update(callback(view, 'info', index), source)
        result = await lists.channel_vods_callback(query, self.context)
        query.callback_query.answer.assert_awaited_once()
        card = getattr(self.browser._sessions.get(view.session_id), 'info_card', None)
        if card:
            self.card = card.message
        return result

    def record(self, view):
        return getattr(self.browser._sessions[view.session_id], 'info_card', None)

    def process_update(self, view):
        card = self.record(view)
        return self.update(f'ui:vi:{view.session_id}:{card.nonce}:process', card.message)

    async def back(self, view):
        card = self.record(view)
        await info.info_callback(self.update(f'ui:vi:{view.session_id}:{card.nonce}:back', card.message), self.context)
        session = self.browser._sessions.get(view.session_id)
        return (self.by_id[session.message_id], self.browser._view(session)) if session else (None,None)

    async def test_uncached_date_replaces_list_text_then_adds_photo_in_place(self):
        source, view = await self.setup_list()
        await self.show(source, view)
        card = self.record(view)
        self.assertEqual(source.message_id, card.message.message_id)
        self.assertTrue(card.photo)
        self.assertEqual([('text',20),('media',20)], self.events)
        self.assertEqual(['Обработать чат','Вернуться к списку'], [r[0].text for r in card.keyboard.inline_keyboard])
        for value in ('Channel','Title 123456789','01:02:03','2 янв 2026 03:04 UTC'):
            self.assertIn(value,card.text)
        self.context.bot.send_message.assert_not_awaited()
        self.metadata.assert_not_awaited(); self.runner.assert_not_awaited()
        videos.fetch_archive_vods.assert_awaited_once()


    async def test_back_from_photo_restores_same_page_and_rebinds_message(self):
        source,view = await self.setup_list(count=8)
        view = await lists.channel_vods_callback(self.update(callback(view,'page',1),source),self.context)
        await self.show(source,view,5)
        old = self.process_update(view)
        message,restored = await self.back(view)
        self.assertEqual(1,restored.page)
        self.assertEqual(build_list(restored)[0],message.text)
        self.assertNotEqual(source.message_id,message.message_id)
        self.context.bot.delete_message.assert_awaited_once()  # Explicit fixture refusal of the list cover
        await info.info_callback(old,self.context); assert_stale(self,old)
        self.metadata.assert_not_awaited(); videos.fetch_archive_vods.assert_awaited_once()

    async def test_back_from_text_edits_same_message_without_send(self):
        source,view = await self.setup_list(''); await self.show(source,view)
        message,restored = await self.back(view)
        self.assertIs(source,message); self.assertTrue(self.browser.is_current(restored))
        self.context.bot.send_message.assert_not_awaited()

    async def test_missing_rejected_placeholder_and_failed_media_keep_actions(self):
        for url in ('','https://evil.invalid/SECRET.jpg','https://vod-secure.twitch.tv/_404/404_processing_480x270.png',URL):
            with self.subTest(url=url):
                source,view = await self.setup_list(url)
                self.context.bot.edit_message_media.side_effect = BadRequest('SECRET')
                await self.show(source,view)
                self.assertFalse(self.record(view).photo)
                self.assertIn('Title',source.text); self.assertIsNotNone(source.keyboard)
                self.context.bot.send_message.assert_not_awaited()

    async def test_custom_jpeg_preview_uses_existing_cdn_at_720x405(self):
        source,view = await self.setup_list(); await self.show(source,view)
        media = self.context.bot.edit_message_media.await_args.kwargs['media']
        self.assertEqual(URL.replace('%{width}','720').replace('%{height}','405'),media.media)

    async def test_long_caption_keeps_full_text_and_buttons_without_photo(self):
        source,view = await self.setup_list()
        session = self.browser._sessions[view.session_id]
        session.vods[0] = replace(session.vods[0],title='&'*240)
        self.context.bot.edit_message_media.reset_mock()
        await self.show(source,self.browser._view(session))
        self.assertEqual(240,source.text.count('&amp;')); self.assertIsNotNone(source.keyboard)
        self.context.bot.edit_message_media.assert_not_awaited()

    async def test_old_dates_and_fast_repeats_cannot_choose_another_vod(self):
        source,view = await self.setup_list(); await self.show(source,view)
        card = self.record(view)
        for index in (0,1):
            old = self.update(callback(view,'info',index),source)
            await lists.channel_vods_callback(old,self.context); assert_stale(self,old)
        self.assertIs(card,self.record(view))

    async def test_return_then_select_other_vod_uses_current_message(self):
        source,view = await self.setup_list(); await self.show(source,view)
        old = self.process_update(view)
        message,view = await self.back(view); await self.show(message,view,1)
        self.assertIn('Title 2',self.record(view).text)
        await info.info_callback(old,self.context); assert_stale(self,old)

    async def test_process_photo_transfers_id_before_start_and_rejects_repeat(self):
        source,view = await self.setup_list(); await self.show(source,view)
        query = self.process_update(view)
        await info.info_callback(query,self.context)
        old = self.update(query.callback_query.data,source)
        await info.info_callback(old,self.context); assert_stale(self,old)
        await asyncio.gather(*self.tasks); self.runner.assert_awaited_once()
        mid = self.runner.await_args.kwargs['progress_message_id']
        self.assertEqual(source.message_id,mid); self.assertIn('Загрузка чата',self.by_id[mid].caption)
        self.assertIsNone(self.record(view))

    async def test_first_process_with_another_owned_job_reaches_scheduler(self):
        await self.process_with_owned_jobs(1)

    async def test_first_process_with_two_owned_jobs_queues_then_starts(self):
        await self.process_with_owned_jobs(2)

    async def process_with_owned_jobs(self,count):
        from src.vod_scheduler import scheduler
        manager=scheduler(self.context.application)
        release=asyncio.Event();others=[]
        for i in range(count):
            other,_=manager.reserve(7,str(987654321+i));manager.arm(other,release.wait);others.append(other)
        self.context.user_data.update(vod_busy=True,vod_jobs={j.id:j for j in others})
        source,view=await self.setup_list('');await self.show(source,view)
        entered=asyncio.Event()
        async def run(**kw):entered.set()
        self.runner.side_effect=run
        try:
            await info.info_callback(self.process_update(view),self.context)
            if count==2:
                self.runner.assert_not_awaited()
                queued=next(j for j in manager.jobs.values() if j.vod=='123456789')
                self.assertEqual('waiting',queued.state)
            release.set();await asyncio.wait_for(entered.wait(),2)
            self.runner.assert_awaited_once()
        finally:await manager.close()

    async def test_old_date_racing_info_process_cannot_start_twice(self):
        source,view = await self.setup_list(''); await self.show(source,view)
        entered,release = asyncio.Event(),asyncio.Event(); original = self.metadata.return_value
        async def slow(*args):
            entered.set(); await release.wait(); return original
        self.metadata.side_effect = slow
        task = self.task(info.info_callback(self.process_update(view),self.context))
        await asyncio.wait_for(entered.wait(),1)
        old = self.update(callback(view,'select',1),source)
        await lists.channel_vods_callback(old,self.context); assert_stale(self,old)
        release.set(); await task; await asyncio.gather(*self.tasks); self.runner.assert_awaited_once()

    async def test_result_becoming_ready_before_process_uses_shared_offer(self):
        source,view = await self.setup_list(); await self.show(source,view)
        self.available['123456789'] = self.ready_result()
        await info.info_callback(self.process_update(view),self.context)
        choice = self.context.user_data['chat_choice']
        self.assertEqual(self.browser._sessions[view.session_id].message_id,choice['message'])
        self.assertEqual(['Получить готовый чат','Обработать чат заново','Вернуться к списку'],
                         [r[0].text for r in self.by_id[choice['message']].keyboard.inline_keyboard])
        self.runner.assert_not_awaited()

    async def test_failed_back_delivery_keeps_original_photo_actions_valid(self):
        source,view=await self.setup_list();await self.show(source,view);card=self.record(view)
        self.context.bot.edit_message_caption.side_effect=BadRequest('cannot edit')
        self.context.bot.send_message.side_effect=BadRequest('cannot send')
        await self.back(view)
        self.assertIs(card,self.record(view));self.assertTrue(card.nonce)
        self.assertEqual(card.revision,self.browser._sessions[view.session_id].revision)
        self.context.bot.delete_message.assert_not_awaited()

    async def test_failed_progress_edit_releases_slot_and_never_starts_worker(self):
        source,view=await self.setup_list();await self.show(source,view)
        self.context.bot.edit_message_caption.side_effect=TimeoutError()
        with self.assertRaises(RuntimeError):await info.info_callback(self.process_update(view),self.context)
        self.assertNotIn('vod_global_operation',self.app.bot_data);self.runner.assert_not_awaited()
        self.context.bot.send_message.assert_not_awaited()

    async def test_failed_deletion_disables_only_retired_keyboard(self):
        source,view = await self.setup_list(); await self.show(source,view)
        self.context.bot.edit_message_media.side_effect = BadRequest('Definite cover refusal')
        self.context.bot.edit_message_caption.side_effect = BadRequest('caption edit refused')
        self.context.bot.delete_message.side_effect = BadRequest('cannot delete')
        self.context.bot.edit_message_reply_markup = AsyncMock()
        new,restored = await self.back(view)
        self.context.bot.edit_message_reply_markup.assert_awaited_once_with(chat_id=10,message_id=20,reply_markup=None)
        self.assertTrue(self.browser.is_current(restored)); self.assertIsNotNone(new.keyboard)

    async def test_slow_cleanup_cannot_delete_or_clear_new_card(self):
        source,view = await self.setup_list(); await self.show(source,view)
        self.context.bot.edit_message_media.side_effect = BadRequest('Definite cover refusal')
        self.context.bot.edit_message_caption.side_effect = BadRequest('caption edit refused')
        entered,release = asyncio.Event(),asyncio.Event()
        async def slow(**kwargs):
            entered.set(); await release.wait()
        self.context.bot.delete_message.side_effect = slow
        task = self.task(self.back(view)); await asyncio.wait_for(entered.wait(),1)
        session = self.browser._sessions[view.session_id]
        await self.show(self.by_id[session.message_id],self.browser._view(session),1)
        card = session.info_card; release.set(); await task
        self.assertIs(card,session.info_card); self.assertTrue(card.nonce)
        self.assertIn('Title 2',card.text)
        self.assertEqual(20,self.context.bot.delete_message.await_args.kwargs['message_id'])

    async def test_cancelled_cleanup_keeps_new_list_and_invalidates_old_actions(self):
        source,view = await self.setup_list(); await self.show(source,view)
        self.context.bot.edit_message_media.side_effect = BadRequest('Definite cover refusal')
        self.context.bot.edit_message_caption.side_effect = BadRequest('caption edit refused')
        old = self.process_update(view); entered = asyncio.Event()
        async def stuck(**kwargs):
            entered.set(); await asyncio.Event().wait()
        self.context.bot.delete_message.side_effect = stuck
        task = self.task(self.back(view)); await asyncio.wait_for(entered.wait(),1); task.cancel()
        with self.assertRaises(asyncio.CancelledError): await task
        session = self.browser._sessions[view.session_id]
        self.assertIsNotNone(self.by_id[session.message_id].keyboard)
        await info.info_callback(old,self.context); assert_stale(self,old)


    async def test_new_channel_preserves_old_info_and_its_actions(self):
        source,view=await self.setup_list();await self.show(source,view)
        card=self.record(view);message,new=await self.open(21,'other')
        source.edit_reply_markup.assert_not_awaited();self.assertIs(card,self.record(view))
        await self.back(view)
        self.assertTrue(self.browser.is_current(new));self.assertIsNotNone(message.keyboard)
        self.assertNotIn('Время указано по UTC',self.by_id[self.browser._sessions[view.session_id].message_id].text)


    async def test_session_change_during_text_delivery_discards_late_result(self):
        source,view = await self.setup_list(''); original = self.context.bot.edit_message_text.side_effect
        async def changed(**kwargs):
            result = await original(**kwargs); self.browser._sessions[view.session_id].revision += 1; return result
        self.context.bot.edit_message_text.side_effect = changed
        await self.show(source,view)
        self.assertIsNone(self.record(view))

    async def test_initial_edit_failure_sends_replacement_before_cleanup(self):
        source,view = await self.setup_list(''); source.edit_text.side_effect = BadRequest('cannot edit')
        await self.show(source,view); card = self.record(view)
        self.assertNotEqual(20,card.message.message_id); self.assertIsNotNone(card.message.keyboard)
        self.assertEqual(card.message.message_id,self.browser._sessions[view.session_id].message_id)

    async def test_complete_delivery_failure_preserves_original_list(self):
        source,view = await self.setup_list(''); source.edit_text.side_effect = BadRequest('cannot edit')
        self.context.bot.send_message.side_effect = BadRequest('cannot send')
        await self.show(source,view)
        self.assertTrue(self.browser.is_current(view)); self.assertIsNotNone(source.keyboard)

    async def test_logging_failure_does_not_break_information(self):
        source,view = await self.setup_list('')
        with patch.object(info.logging.Logger,'info',side_effect=RuntimeError('logger')): await self.show(source,view)
        self.assertIn('Title',self.record(view).text)

    def test_preview_allowlist_rejects_unsafe_urls(self):
        for url in ('http://static-cdn.jtvnw.net/cf_vods/x.jpg','https://evil.invalid/x.jpg',
                    'https://static-cdn.jtvnw.net/cf_vods/../x.jpg','https://static-cdn.jtvnw.net/cf_vods/x.jpg?q=1'):
            self.assertIsNone(info.preview_url(url))

    async def test_concurrent_different_dates_commit_only_one_card(self):
        source,view = await self.setup_list()
        await asyncio.gather(self.show(source,view,0),self.show(source,view,1))
        self.assertEqual('Title 123456789',self.browser._sessions[view.session_id].vods[self.record(view).index].title)
        self.assertEqual(2,self.context.bot.edit_message_media.await_count)
        self.context.bot.send_message.assert_not_awaited()

    async def test_expiry_during_atomic_media_never_installs_actions(self):
        source,view=await self.setup_list();original=self.context.bot.edit_message_media.side_effect
        async def expired(**kw):self.now+=601;return await original(**kw)
        self.context.bot.edit_message_media.side_effect=expired
        await self.show(source,view)
        stale=self.update(f'ui:vi:{view.session_id}:0000000000000000:process',source)
        await info.info_callback(stale,self.context);assert_stale(self,stale)
        self.runner.assert_not_awaited()

    async def test_information_expiry_is_bounded_by_session_ttl(self):
        source,view = await self.setup_list('')
        session = self.browser._sessions[view.session_id]
        self.now = session.expires-5
        await self.show(source,self.browser._view(session))
        self.assertEqual(session.expires,session.info_card.expires)


    async def test_text_back_failure_keeps_card_nonce_and_releases_lock(self):
        source,view = await self.setup_list(''); await self.show(source,view)
        card = self.record(view); source.edit_text.side_effect = BadRequest('edit refused')
        self.context.bot.send_message.side_effect=BadRequest('send refused')
        await self.back(view)
        self.assertIs(card,self.record(view)); self.assertTrue(card.nonce)
        self.assertEqual(card.revision,self.browser._sessions[view.session_id].revision)
        self.assertFalse(lists._ui_lock(self.browser._sessions[view.session_id]).locked())

    async def test_back_ack_race_cannot_mutate_replacement_channel(self):
        source,view = await self.setup_list(''); await self.show(source,view)
        card = self.record(view)
        query = self.update(f'ui:vi:{view.session_id}:{card.nonce}:back',source)
        async def answer(): await self.open(30,'other')
        query.callback_query.answer.side_effect = answer
        await info.info_callback(query,self.context)
        query.callback_query.answer.assert_awaited_once()
        self.assertNotIn('Время указано по UTC',self.by_id[30].text)
        self.assertIsNotNone(self.by_id[30].keyboard)

    async def test_new_channel_during_metadata_preserves_old_download(self):
        source,view = await self.setup_list(''); await self.show(source,view)
        entered,release = asyncio.Event(),asyncio.Event(); original = self.metadata.return_value
        async def slow(*args): entered.set(); await release.wait(); return original
        self.metadata.side_effect = slow
        task = self.task(info.info_callback(self.process_update(view),self.context))
        await asyncio.wait_for(entered.wait(),1)
        message,new = await self.open(30,'other'); release.set(); await task
        await asyncio.gather(*self.tasks);self.runner.assert_awaited_once();self.assertTrue(self.browser.is_current(new))
        self.assertIsNotNone(message.keyboard)

    async def test_slow_cleanup_does_not_block_replacement_channel(self):
        source,view = await self.setup_list(); await self.show(source,view)
        self.context.bot.edit_message_media.side_effect = BadRequest('Definite cover refusal')
        self.context.bot.edit_message_caption.side_effect = BadRequest('caption edit refused')
        entered,release = asyncio.Event(),asyncio.Event()
        async def slow(**kw): entered.set(); await release.wait()
        self.context.bot.delete_message.side_effect = slow
        task = self.task(self.back(view)); await asyncio.wait_for(entered.wait(),1)
        message,new = await asyncio.wait_for(self.open(30,'other'),1)
        release.set(); await task
        self.assertTrue(self.browser.is_current(new)); self.assertIsNotNone(message.keyboard)

    async def test_cleanup_timeout_keeps_new_list_usable(self):
        source,view = await self.setup_list(); await self.show(source,view)
        async def stuck(**kw): await asyncio.Event().wait()
        self.context.bot.delete_message.side_effect = stuck
        real = asyncio.timeout
        with patch.object(asyncio,'timeout',side_effect=lambda seconds:real(min(seconds,.02))):
            message,restored = await asyncio.wait_for(self.back(view),1)
        self.assertTrue(self.browser.is_current(restored)); self.assertIsNotNone(message.keyboard)

    async def test_missing_preview_and_back_never_fetch_thumbnail_metadata(self):
        source,view = await self.setup_list('')
        with patch('src.ui.result_preview.fetch_video_thumbnail',new=AsyncMock()) as lookup:
            await self.show(source,view); await self.back(view)
        lookup.assert_not_awaited(); self.metadata.assert_not_awaited()
        videos.fetch_archive_vods.assert_awaited_once()

    async def test_preview_failure_diagnostics_never_expose_exception_contents(self):
        source,view = await self.setup_list()
        self.context.bot.edit_message_media.side_effect = BadRequest('SECRET user=123 https://private.invalid')
        with self.assertLogs(info.__name__,level='INFO') as logs: await self.show(source,view)
        self.assertNotIn('SECRET',str(logs.output)); self.assertNotIn('private.invalid',str(logs.output))
        for record in logs.records: self.assertIsNone(record.exc_info)
