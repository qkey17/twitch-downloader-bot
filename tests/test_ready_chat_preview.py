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
from src.ui.ui_labels import GET_READY_CHAT, RECREATE_CHAT, CREATE_CHAT, CHAT_GENERIC, READY_CHAT


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
        bot.edit_message_text = AsyncMock(side_effect=edit)
        bot.delete_message = AsyncMock()
        self.lookup = AsyncMock(return_value=URL)
        p = patch.object(preview, 'fetch_video_thumbnail', self.lookup)
        p.start(); self.addCleanup(p.stop)
        self.available = {'123456789':self.ready_result(), '2':self.ready_result() | {'vod_id':'2'}}
        self.ready.side_effect = lambda key:self.available.get(key)
        videos.fetch_archive_vods.return_value = ([cards.vod(123456789, thumbnail_url=URL), cards.vod(2, thumbnail_url=URL)] +
                                                  [cards.vod(i, thumbnail_url=URL) for i in range(3, 9)], None)

    def message(self, number=20, text=''):
        msg = fixture.StaleListTests.message(self, number, text)
        msg.photo, msg.keyboard = [], None
        async def edit(text=None, **kw):
            if msg.photo:
                raise BadRequest('There is no text in the message to edit')
            msg.text, msg.keyboard = text, kw.get('reply_markup')
            self.events.append(('text', number))
            return msg
        async def caption(**kw):
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

    async def test_text_buttons_precede_same_message_media_without_lookup_or_writes(self):
        with patch.object(vod.db, 'add_user_history') as history:
            message, view, choice = await self.offer()
        self.assertEqual([GET_READY_CHAT, RECREATE_CHAT, 'Вернуться к списку'],
                         [r[0].text for r in message.keyboard.inline_keyboard])
        self.assertTrue(message.caption.startswith(READY_CHAT))
        self.assertEqual([('text',20), ('text',20), ('media',20)], self.events)
        self.assertTrue(choice['photo']); self.assertTrue(self.browser.is_current(view))
        self.lookup.assert_not_awaited(); history.assert_not_called()
        self.context.bot.send_message.assert_not_awaited(); self.runner.assert_not_awaited()

    async def test_rejected_or_timed_out_media_keeps_text_and_working_back(self):
        for error in (BadRequest('SECRET'), TimeoutError()):
            with self.subTest(error=type(error).__name__):
                self.context.bot.edit_message_media.side_effect = error
                message, view, choice = await self.offer()
                self.assertEqual(isinstance(error, TimeoutError), choice['photo'])
                self.context.bot.send_message.assert_not_awaited()
                await self.back(message, view)
                session = self.browser._sessions[view.session_id]
                restored = self.by_id[session.message_id]
                self.assertIn('VODs канала', restored.text)
                self.assertIsNotNone(restored.keyboard)
                self.context.bot.send_message.reset_mock()

    async def test_long_caption_stays_text_and_does_not_resolve(self):
        self.available['123456789']['meta']['title'] = '😀'*1200
        message, _, choice = await self.offer()
        self.assertFalse(choice['photo']); self.assertIn('😀', message.text)
        self.context.bot.edit_message_media.assert_not_awaited(); self.lookup.assert_not_awaited()

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

    async def test_photo_back_rebinds_same_page_before_slow_cleanup_and_old_actions_fail(self):
        message, view, choice = await self.offer(page=1)
        entered, release = asyncio.Event(), asyncio.Event()
        async def slow(**kw):
            entered.set(); await release.wait()
        self.context.bot.delete_message.side_effect = slow
        returning = self.task(self.back(message, view))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            session = self.browser._sessions[view.session_id]
            new = self.by_id[session.message_id]
            self.assertNotEqual(message.message_id, new.message_id)
            self.assertFalse(session.ui_lock.locked())
            self.assertIn('Стр. 2/2', [b.text for r in new.keyboard.inline_keyboard for b in r])
            self.assertIs(session.list_render[0], new)
            for action in ('open','regen'):
                await self.act(choice, action, message)
            q = self.update(callback(self.browser._view(session), 'prev'), message)
            await lists.channel_vods_callback(q, self.context)
            assert_stale(self, q)
            current = await lists.channel_vods_callback(self.update(callback(self.browser._view(session), 'prev'), new), self.context)
            self.assertEqual(0, current.page)
            await self.pick(new, current, 0)
            latest = self.context.user_data['chat_choice']
            release.set(); await asyncio.wait_for(returning, 1)
            self.assertIs(latest, self.context.user_data['chat_choice'])
            self.assertTrue(latest['photo'])
            self.assertEqual(message.message_id, self.context.bot.delete_message.await_args.kwargs['message_id'])
            self.runner.assert_not_awaited()
        finally:
            release.set(); await asyncio.wait_for(returning, 1)

    async def test_failed_or_cancelled_cleanup_never_unbinds_new_list(self):
        for cancel in (False, True):
            with self.subTest(cancel=cancel):
                message, view, _ = await self.offer()
                entered = asyncio.Event()
                async def deleting(**kw):
                    if cancel:
                        entered.set(); await asyncio.Event().wait()
                    raise BadRequest('SECRET')
                self.context.bot.delete_message.side_effect = deleting
                returning = self.task(self.back(message, view))
                if cancel:
                    await asyncio.wait_for(entered.wait(), 1); returning.cancel()
                    with self.assertRaises(asyncio.CancelledError): await asyncio.wait_for(returning, 1)
                else:
                    await asyncio.wait_for(returning, 1)
                session = self.browser._sessions[view.session_id]
                new = self.by_id[session.message_id]
                self.assertIsNotNone(new.keyboard); self.assertFalse(session.photo)
                self.assertTrue(self.browser.is_current(self.browser._view(session)))

    async def test_failed_or_cancelled_list_send_preserves_old_back(self):
        for error in (RuntimeError('SECRET'), asyncio.CancelledError()):
            with self.subTest(error=type(error).__name__):
                message, view, choice = await self.offer()
                original = self.context.bot.send_message.side_effect
                self.context.bot.send_message.side_effect = error
                if isinstance(error, asyncio.CancelledError):
                    with self.assertRaises(asyncio.CancelledError): await self.back(message, view)
                else:
                    self.assertIsNone(await self.back(message, view))
                self.assertTrue(self.browser.is_current(view))
                self.assertIs(choice, self.context.user_data['chat_choice'])
                self.context.bot.send_message.side_effect = original
                restored = await self.back(message, view)
                self.assertNotEqual(20, restored.message_id)

    async def test_photo_open_edits_media_once_and_records_history_only_on_open(self):
        with patch.object(vod.config, 'HISTORY_ENABLED', True), patch.object(vod.db, 'add_user_history') as history:
            message, view, choice = await self.offer()
            history.assert_not_called()
            self.context.bot.edit_message_media.reset_mock()
            await self.act(choice, 'open', message)
            await self.act(choice, 'open', message)
            await self.act(choice, 'regen', message)
            history.assert_called_once_with(7, 1)
        self.context.bot.edit_message_media.assert_awaited_once()
        row = message.keyboard.inline_keyboard[0]
        self.assertEqual([CHAT_GENERIC, '🔗 Ссылка на VOD'], [b.text for b in row])
        self.assertEqual(self.available['123456789']['html_ref'], row[0].url)
        self.assertNotIn(view.session_id, self.browser._sessions)
        self.context.bot.send_message.assert_not_awaited(); self.runner.assert_not_awaited()

    async def test_open_photo_rejection_delivers_bound_terminal_text_links(self):
        message, view, choice = await self.offer()
        self.context.bot.edit_message_media.side_effect = BadRequest('SECRET')
        await self.act(choice, 'open', message)
        new_id = self.context.bot.send_message.await_args
        self.assertEqual(self.available['123456789']['html_ref'], new_id.kwargs['reply_markup'].inline_keyboard[0][0].url)
        self.assertNotIn(view.session_id, self.browser._sessions)
        self.context.bot.send_message.assert_awaited_once()
        self.context.bot.delete_message.assert_awaited_once_with(chat_id=10, message_id=20)

    async def test_regen_photo_hands_new_progress_id_to_existing_runner_once(self):
        message, view, choice = await self.offer()
        await self.act(choice, 'regen', message)
        await self.act(choice, 'regen', message)
        await asyncio.gather(*self.tasks)
        self.runner.assert_awaited_once()
        new_id = self.runner.await_args.kwargs['progress_message_id']
        self.assertNotEqual(20, new_id)
        self.assertIn('<b>Загрузка чата…</b>', self.by_id[new_id].text)
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
                self.assertTrue(new.text.startswith(READY_CHAT)); self.assertIsNotNone(new.keyboard)
                self.assertFalse(choice['photo']); self.assertNotIn('action', choice)
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

    async def test_cancelled_media_application_keeps_safe_photo_back_transition(self):
        entered = asyncio.Event()
        async def ambiguous(**kw):
            self.by_id[kw['message_id']].photo = [object()]
            entered.set(); await asyncio.Event().wait()
        self.context.bot.edit_message_media.side_effect = ambiguous
        message, view = await self.open(20)
        selecting = self.task(self.pick(message, view))
        await asyncio.wait_for(entered.wait(), 1)
        selecting.cancel()
        with self.assertRaises(asyncio.CancelledError): await asyncio.wait_for(selecting, 1)
        session = self.browser._sessions[view.session_id]
        self.assertTrue(session.photo)
        new = await self.back(message, self.browser._view(session))
        self.assertNotEqual(20, new.message_id)
        self.assertFalse(session.ui_lock.locked())

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
            self.context.bot.edit_message_media.assert_not_awaited()
            self.assertIn('VODs канала', new.text)
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
        self.lookup.assert_not_awaited(); self.context.bot.edit_message_media.assert_not_awaited()

    async def test_open_or_regen_before_optional_lookup_finishes_fences_late_offer(self):
        for action in ('open', 'regen'):
            with self.subTest(action=action):
                entered, release = asyncio.Event(), asyncio.Event()
                async def slow(*args):
                    entered.set(); await release.wait(); return URL
                self.available['123456789']['meta']['thumbnail_url'] = URL
                with patch.object(chat, 'resolve_preview', side_effect=slow):
                    message, view = await self.open(20)
                    selecting = self.task(self.pick(message, view))
                    try:
                        await asyncio.wait_for(entered.wait(), 1)
                        choice = self.context.user_data['chat_choice']
                        await asyncio.wait_for(self.act(choice, action, message), 1)
                        before = list(self.events)
                        release.set(); await asyncio.wait_for(selecting, 1)
                        self.assertEqual(before, self.events)
                    finally:
                        release.set(); await asyncio.wait_for(selecting, 1)
                await asyncio.gather(*self.tasks)
        self.runner.assert_awaited_once()

    async def test_actions_wait_for_inflight_media_then_use_actual_photo_state(self):
        apply_media = self.context.bot.edit_message_media.side_effect
        for action in ('back', 'open', 'regen'):
            with self.subTest(action=action):
                entered, release = asyncio.Event(), asyncio.Event()
                async def slow(**kw):
                    entered.set(); await release.wait(); return await apply_media(**kw)
                self.context.bot.edit_message_media.side_effect = slow
                message, view = await self.open(20)
                selecting = self.task(self.pick(message, view))
                try:
                    await asyncio.wait_for(entered.wait(), 1)
                    choice = self.context.user_data['chat_choice']
                    selected = self.browser._view(self.browser._sessions[view.session_id])
                    acting = self.task(self.back(message, selected) if action == 'back' else self.act(choice, action, message))
                    await asyncio.sleep(0)
                    self.assertFalse(acting.done())
                    release.set()
                    await asyncio.wait_for(asyncio.gather(selecting, acting), 1)
                    if action == 'back':
                        self.assertNotEqual(20, self.browser._sessions[view.session_id].message_id)
                    elif action == 'open':
                        self.assertNotIn(view.session_id, self.browser._sessions)
                    else:
                        self.assertFalse(self.browser.is_current(selected))
                finally:
                    release.set(); await asyncio.wait_for(selecting, 1)
                await asyncio.gather(*self.tasks)
        self.runner.assert_awaited_once()

    async def test_progress_edit_timeout_restores_offer_without_start_or_busy_leak(self):
        message, view, choice = await self.offer()
        self.context.bot.send_message.side_effect = TimeoutError()
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
