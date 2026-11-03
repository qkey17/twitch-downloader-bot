from callback_assertions import assert_stale
"""Offer media transitions with real session/slot logic, synthetic Telegram only."""
import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from telegram.error import BadRequest
import test_stale_vod_lists as fixture
import test_channel_vods as cards
from src.handlers import handlers_channel_vods as lists, handlers_chat as chat, handlers_vod as vod
from src.handlers.handlers_state import begin_download, finish_download
from src.vod_list import callback, build_list
from src.twitch_api import videos
from src.ui import result_preview as preview
from src.ui.ui_labels import GET_READY_CHAT, RECREATE_CHAT, CREATE_CHAT, CHAT_GENERIC


URL = 'https://static-cdn.jtvnw.net/cf_vods/example/thumb/custom-480x270.jpeg'


class ReadyPreviewTests(unittest.IsolatedAsyncioTestCase):
    _setupAsyncioRunner = fixture.StaleListTests._setupAsyncioRunner
    asyncTearDown = fixture.StaleListTests.asyncTearDown
    context_for_user = fixture.StaleListTests.context_for_user
    task = fixture.StaleListTests.task
    update = fixture.StaleListTests.update
    ready_result = fixture.StaleListTests.ready_result
    open = fixture.StaleListTests.open

    def setUp(self):
        fixture.StaleListTests.setUp(self)
        self.by_id, self.events = {}, []
        bot = self.context.bot
        async def edit(**kwargs):
            message = self.by_id[kwargs.pop('message_id')]
            kwargs.pop('chat_id')
            return await message.edit_text(**kwargs)
        bot.edit_message_text.side_effect = edit
        async def media(**kw):
            if hasattr(kw['media'].media,'filename'):
                raise BadRequest('Synthetic unsupported repository cover')
            msg = self.by_id[kw['message_id']]
            msg.photo = [object()]
            msg.caption, msg.keyboard = kw['media'].caption, kw['reply_markup']
            self.events.append(('media', msg.message_id))
        async def send(**kw):
            self.next_message_id += 1
            msg = self.message(self.next_message_id)
            msg.text, msg.keyboard = kw['text'], kw.get('reply_markup')
            self.events.append(('send', msg.message_id))
            return msg
        async def edit(**kw):
            return await self.by_id[kw.pop('message_id')].edit_text(**{k:v for k,v in kw.items() if k != 'chat_id'})
        bot.edit_message_media = AsyncMock(side_effect=media)
        bot.send_message = AsyncMock(side_effect=send)
        async def send_photo(**kw):
            if hasattr(kw['photo'],'filename'):
                raise BadRequest('Synthetic unsupported repository cover')
            self.next_message_id += 1
            msg = self.message(self.next_message_id)
            msg.photo=[object()]
            msg.caption, msg.keyboard=kw['caption'],kw.get('reply_markup')
            self.events.append(('send_photo',msg.message_id))
            return msg
        bot.send_photo = AsyncMock(side_effect=send_photo)
        bot.edit_message_text = AsyncMock(side_effect=edit)
        async def caption(**kw):
            return await self.by_id[kw.pop('message_id')].edit_caption(**{k:v for k,v in kw.items() if k != 'chat_id'})
        bot.edit_message_caption=AsyncMock(side_effect=caption)
        bot.delete_message = AsyncMock()
        self.lookup = AsyncMock(return_value=URL)
        p = patch.object(preview, 'fetch_video_thumbnail', self.lookup)
        p.start(); self.addCleanup(p.stop)
        self.available = {'123456789':self.ready_result(), '2':self.ready_result() | {'vod_id':'2'}}
        self.ready.side_effect = lambda key:self.available.get(key)
        videos.fetch_archive_vods.return_value = ([cards.vod(123456789, thumbnail_url=URL), cards.vod(2, thumbnail_url=URL)] +
                                                  [cards.vod(i, thumbnail_url=URL) for i in range(3, 9)], None)

    def message(self, number=20, text=''):
        # This legacy transport rebuilds synthetic messages with reused IDs. Reset
        # only its physical media observation; navigation fences remain intact.
        if number in self.by_id:
            for key,card in self.app.bot_data.get('tdb_card_contexts',{}).items():
                if key[1:]==(10,number):
                    card.data.pop('image_surface',None);card.data.pop('rejected_image',None)
        msg = fixture.StaleListTests.message(self, number, text)
        msg.photo, msg.keyboard = [], None
        async def edit(text=None, **kw):
            if msg.photo:
                raise BadRequest('There is no text in the message to edit')
            msg.text, msg.keyboard = text, kw.get('reply_markup')
            self.events.append(('text', number))
            return msg
        async def caption(**kw):
            if not msg.photo:
                raise BadRequest('There is no caption in the message to edit')
            msg.caption, msg.keyboard = kw.get('caption'), kw.get('reply_markup')
        msg.edit_text.side_effect = edit
        msg.edit_caption = AsyncMock(side_effect=caption)
        self.by_id[number] = msg
        return msg

    async def pick(self, message, view, index=0):
        return await lists.channel_vods_callback(self.update(callback(view, 'info', index), message), self.context)

    async def offer(self, *, url=URL, page=0):
        message, view = await self.open(20)
        if page:
            view = await lists.channel_vods_callback(self.update(callback(view, 'page', page), message), self.context)
            self.available['6'] = self.ready_result() | {'vod_id':'6'}
        if url != URL:
            session = self.browser._sessions[view.session_id]
            from dataclasses import replace
            session.vods[:] = [replace(v, thumbnail_url=url) for v in session.vods]
        result = await self.pick(message, view, page*5)
        return message, result.view, self.context.user_data['chat_choice']

    async def act(self, choice, action, message=None):
        q = self.update(f'ui:chat:{action}:{choice["token"]}', message or self.by_id[choice['message']])
        await chat.chat_callback(q, self.context)
        q.callback_query.answer.assert_awaited_once()

    async def back(self, message, view):
        return await lists.channel_vods_callback(self.update(callback(view, 'back'), message), self.context)


    async def test_rejected_or_timed_out_atomic_media_is_bounded_and_preserves_source(self):
        for error in (BadRequest('SECRET'),TimeoutError()):
            message,view=await self.open(20);before=self.context.bot.send_message.await_count
            self.context.bot.edit_message_media.side_effect=error
            await self.pick(message,view)
            self.assertEqual(before,self.context.bot.send_message.await_count)
            self.context.bot.delete_message.assert_not_awaited()

    async def test_long_caption_stays_text_and_does_not_resolve(self):
        self.available['123456789']['meta']['title'] = '😀'*1200
        message, _, choice = await self.offer()
        self.assertFalse(choice['photo']); self.assertIn('😀',message.text)
        self.context.bot.edit_message_media.reset_mock(); self.lookup.assert_not_awaited()

    async def test_slow_metadata_does_not_hold_ui_lock_back_or_other_vod(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def slow(*args):
            entered.set(); await release.wait(); return URL
        self.lookup.side_effect = slow
        message, view = await self.open(20)
        # Missing list metadata allows the existing single-VOD resolver.
        from dataclasses import replace
        session = self.browser._sessions[view.session_id]
        session.vods[0] = replace(session.vods[0], thumbnail_url=None)
        selecting = self.task(self.pick(message, view))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            choice = self.context.user_data['chat_choice']
            self.assertIsNotNone(message.keyboard)
            self.assertFalse(session.ui_lock.locked())
            back = await asyncio.wait_for(self.back(message, self.browser._view(session)), 1)
            await self.pick(message, back, 1)
            latest = self.context.user_data['chat_choice']
            self.assertNotEqual(choice['token'], latest['token'])
            before = self.context.bot.edit_message_media.await_count
            release.set(); await asyncio.wait_for(selecting, 1)
            self.assertEqual(before, self.context.bot.edit_message_media.await_count)
            self.assertEqual('2', latest['vod_id'])
        finally:
            release.set(); await asyncio.wait_for(selecting, 1)

    async def test_refused_list_cover_uses_confirmed_text_fallback_with_same_page(self):
        message,view,choice=await self.offer(page=1)
        restored=await self.back(message,view);session=self.browser._sessions[view.session_id]
        self.assertNotEqual(message.message_id,restored.message_id)
        self.assertFalse(session.photo);self.assertIn('Стр. 2/2',str(self.by_id[session.message_id].keyboard))
        self.context.bot.send_message.assert_awaited_once();self.context.bot.delete_message.assert_awaited_once()
        self.assertFalse(choice['current']() and self.context.user_data.get('chat_choice') is choice)



    async def test_photo_open_edits_once_and_records_history_only_on_open(self):
        with patch.object(vod.config,'HISTORY_ENABLED',True),patch.object(vod.db,'add_user_history') as history:
            message,view,choice=await self.offer();before=self.context.bot.edit_message_caption.await_count
            await self.act(choice,'open',message);await self.act(choice,'open',message);await self.act(choice,'regen',message)
            history.assert_called_once_with(7,1)
        self.assertEqual(before+1,self.context.bot.edit_message_caption.await_count)
        self.context.bot.send_photo.assert_not_awaited();self.context.bot.send_message.assert_not_awaited();self.context.bot.delete_message.assert_not_awaited()
        self.assertNotIn(view.session_id,self.browser._sessions);self.runner.assert_not_awaited()


    async def test_regen_photo_hands_new_progress_id_to_existing_runner_once(self):
        message, view, choice = await self.offer()
        await self.act(choice, 'regen', message)
        await self.act(choice, 'regen', message)
        await asyncio.gather(*self.tasks)
        self.runner.assert_awaited_once()
        new_id = self.runner.await_args.kwargs['progress_message_id']
        self.assertEqual(20,new_id)
        self.assertIn('<b>Загрузка чата…</b>',self.by_id[new_id].caption)
        self.assertTrue(self.by_id[new_id].keyboard.inline_keyboard[0][0].callback_data.startswith('ui:pending_cancel:'))
        self.assertFalse(self.browser.is_current(view))
        self.assertFalse(self.context.user_data['vod_busy'])

    async def test_failed_or_cancelled_starter_restores_buttons_and_releases_slot(self):
        for cancel in (False, True):
            with self.subTest(cancel=cancel):
                message, view, choice = await self.offer()
                original = self.app.create_task.side_effect
                self.app.create_task.side_effect = asyncio.CancelledError() if cancel else RuntimeError('SECRET')
                with self.assertRaises(asyncio.CancelledError if cancel else RuntimeError):
                    await self.act(choice, 'regen', message)
                self.assertFalse(self.context.user_data['vod_busy'])
                self.assertNotIn('vod_global_operation', self.app.bot_data)
                new = self.by_id[choice['message']]
                self.assertTrue(new.caption.startswith('🟣 ')); self.assertIsNotNone(new.keyboard)
                self.assertTrue(choice['photo']); self.assertNotIn('action', choice)
                self.app.create_task.side_effect = original
                await self.act(choice, 'regen', new)
                await asyncio.gather(*self.tasks)
        self.assertEqual(2, self.runner.await_count)

    async def test_busy_global_slot_keeps_photo_actions_and_does_not_reserve_twice(self):
        message, _, choice = await self.offer()
        other = self.context_for_user()
        operation = begin_download(other)
        try:
            await self.act(choice, 'regen', message)
            self.assertTrue(choice['photo']); self.assertNotIn('action', choice)
            self.assertEqual(operation, self.app.bot_data['vod_global_operation'])
            self.context.bot.send_message.assert_not_awaited(); self.runner.assert_not_awaited()
        finally:
            finish_download(other, operation)
        await self.act(choice, 'regen', message)
        await asyncio.gather(*self.tasks)
        self.runner.assert_awaited_once()


    async def test_new_channel_while_offer_metadata_waits_is_never_overwritten(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def slow(*args):
            entered.set(); await release.wait(); return URL
        message, view = await self.open(20)
        from dataclasses import replace
        session = self.browser._sessions[view.session_id]
        session.vods[0] = replace(session.vods[0], thumbnail_url=None)
        self.lookup.side_effect = slow
        selecting = self.task(self.pick(message, view))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            from src.handlers import handlers_input as inputs
            latest = await asyncio.wait_for(inputs.text_input(SimpleNamespace(
                message=self.message(30, 'other'), effective_user=SimpleNamespace(id=7)), self.context), 1)
            new = self.browser._sessions[latest.session_id].list_render[0]
            release.set(); await asyncio.wait_for(selecting, 1)
            self.assertEqual(message.message_id,self.context.bot.edit_message_media.await_args.kwargs['message_id'])
            self.assertNotIn('Время указано по UTC',new.text)
            self.assertTrue(self.browser.is_current(latest))
        finally:
            release.set(); await asyncio.wait_for(selecting, 1)

    async def test_direct_offer_reuses_known_saved_url_and_no_back_button(self):
        ready = self.available['123456789']
        ready['meta']['thumbnail_url'] = URL
        source = self.message(80)
        await chat.prepare_chat(source, self.context, 7, '123456789')
        choice = self.context.user_data['chat_choice']
        prompt = self.by_id[choice['message']]
        self.assertTrue(choice['photo'])
        self.assertEqual([GET_READY_CHAT, RECREATE_CHAT], [r[0].text for r in prompt.keyboard.inline_keyboard])
        self.lookup.assert_not_awaited()
        await self.act(choice, 'regen', prompt)
        await asyncio.gather(*self.tasks)
        self.runner.assert_awaited_once()

    async def test_missing_ready_photo_reports_unavailable_without_start_or_history(self):
        message, _, choice = await self.offer()
        self.available.clear()
        with patch.object(vod.db, 'add_user_history') as history:
            await self.act(choice, 'open', message)
        self.assertEqual('Готовый чат недоступен. Выберите VOD заново.', message.caption)
        self.assertIsNone(message.keyboard)
        history.assert_not_called(); self.runner.assert_not_awaited()

    async def test_known_placeholder_or_unsafe_url_never_looks_up_or_sends_photo(self):
        for url in ('', 'https://evil.invalid/private.jpg',
                    'https://vod-secure.twitch.tv/_404/404_processing_480x270.png'):
            with self.subTest(url=url):
                _, _, choice = await self.offer(url=url)
                self.assertFalse(choice['photo'])
        self.lookup.assert_not_awaited()



    async def test_progress_edit_timeout_restores_offer_without_start_or_busy_leak(self):
        message, view, choice = await self.offer()
        self.context.bot.edit_message_caption.side_effect=TimeoutError()
        with self.assertRaises(RuntimeError): await self.act(choice, 'regen', message)
        self.assertFalse(self.context.user_data['vod_busy'])
        self.assertNotIn('vod_global_operation', self.app.bot_data)
        self.app.create_task.assert_not_called(); self.runner.assert_not_awaited()
        new = self.by_id[choice['message']]
        self.assertEqual(GET_READY_CHAT, new.keyboard.inline_keyboard[0][0].text)
        self.assertIn(view.session_id, self.browser._sessions)

    async def test_cancel_button_keeps_operation_owned_semantics_after_photo_replacement(self):
        from src.handlers.handlers_state import is_cancelled
        message, _, choice = await self.offer()
        entered, release = asyncio.Event(), asyncio.Event()
        async def running(**kw):
            entered.set(); await release.wait()
        self.runner.side_effect = running
        await self.act(choice, 'regen', message)
        try:
            await asyncio.wait_for(entered.wait(), 1)
            operation = self.context.user_data['vod_active_operation']
            self.assertTrue(self.context.user_data['vod_busy'])
            await vod.pending_cancel_callback(self.update('ui:pending_cancel:'+operation, self.by_id[choice['message']]), self.context)
            self.assertTrue(is_cancelled(self.context, operation))
            self.assertEqual(operation, self.app.bot_data['vod_global_operation'])
        finally:
            release.set(); await asyncio.gather(*self.tasks)
        self.assertFalse(self.context.user_data['vod_busy'])

    async def test_uncached_info_create_still_uses_existing_start(self):
        self.available.clear()
        source, view = await self.open(20)
        # Use text info fallback so its Message has the normal fixture methods.
        from dataclasses import replace
        session = self.browser._sessions[view.session_id]
        session.vods[0] = replace(session.vods[0], thumbnail_url='')
        await self.pick(source, view)
        card = session.info_card
        self.assertEqual(CREATE_CHAT, card.keyboard.inline_keyboard[0][0].text)
        from src.handlers.handlers_vod_info import info_callback
        await info_callback(self.update(f'ui:vi:{session.token}:{card.nonce}:process', card.message), self.context)
        await asyncio.gather(*self.tasks)
        self.runner.assert_awaited_once()
