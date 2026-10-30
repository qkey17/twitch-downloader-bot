"""Real starter/runner and bounded browser, synthetic worker and Telegram."""
import asyncio
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from telegram.error import BadRequest
import test_ready_chat_preview as fixture
from src.handlers import handlers_vod as vod, handlers_chat as chat, handlers_channel_vods as lists
from src.handlers import handlers_vod_info as info
from src.handlers.handlers_state import is_cancelled, is_busy, CANCELLED_MESSAGE
from src.workers.worker_progress import safe_edit_html, _build_progress_card
from src.vod_list import callback
from src.ui.ui_history import build_history_page

REAL_RUNNER = vod._runner


class ProgressCancelTests(unittest.IsolatedAsyncioTestCase):
    _setupAsyncioRunner = fixture.ReadyPreviewTests._setupAsyncioRunner
    asyncTearDown = fixture.ReadyPreviewTests.asyncTearDown
    context_for_user = fixture.ReadyPreviewTests.context_for_user
    task = fixture.ReadyPreviewTests.task
    update = fixture.ReadyPreviewTests.update
    ready_result = fixture.ReadyPreviewTests.ready_result
    open = fixture.ReadyPreviewTests.open
    pick = fixture.ReadyPreviewTests.pick
    offer = fixture.ReadyPreviewTests.offer
    act = fixture.ReadyPreviewTests.act
    back = fixture.ReadyPreviewTests.back

    def setUp(self):
        fixture.ReadyPreviewTests.setUp(self)
        self.entered, self.release = asyncio.Event(), asyncio.Event()
        self.cleaning, self.cleaned = asyncio.Event(), asyncio.Event()
        self.allow_cleanup = asyncio.Event(); self.allow_cleanup.set()
        self.closing, self.close_release = asyncio.Event(), asyncio.Event(); self.close_release.set()
        self.closed = False
        self.success = False
        self.error = None
        self.worker = AsyncMock(side_effect=self.work)
        self.metadata.return_value = SimpleNamespace(title='<Title&>', channel='Channel',
            length_seconds=3723, created_at='2026-01-02T03:04:05Z')
        self.context.bot.send_photo = AsyncMock(side_effect=self.send_photo)
        self.publication = SimpleNamespace(ref=self.ready_result()['html_ref'],
            reserve=lambda:None, fail=lambda error:None, close=self.close, complete=lambda:42)
        original_io = vod.run_blocking
        async def io(fn, *args, **kw):
            if fn == self.close:
                self.closing.set(); await self.close_release.wait()
            return await original_io(fn, *args, **kw)
        for name, value in [('_runner', REAL_RUNNER), ('download_and_send', self.worker),
                            ('run_blocking', io), ('results.Publication', lambda *a,**k:self.publication)]:
            target = 'src.handlers.handlers_vod.' + name
            p = patch(target, value); p.start(); self.addCleanup(p.stop)

    def message(self, number=20, text=''):
        msg = fixture.ReadyPreviewTests.message(self, number, text)
        msg.reply_photo = AsyncMock(side_effect=self.send_photo)
        msg.edit_media, msg.edit_reply_markup = AsyncMock(), AsyncMock()
        return msg

    async def send_photo(self, photo=None, **kw):
        self.next_message_id += 1
        msg = self.message(self.next_message_id)
        msg.photo, msg.caption, msg.keyboard = [object()], kw['caption'], kw['reply_markup']
        return msg

    def close(self):
        self.closed = True

    async def work(self, **kw):
        self.entered.set()
        try:
            await self.release.wait()
            if self.error:
                raise self.error
            if not self.success and is_cancelled(self.context, kw['operation_id']):
                raise RuntimeError(CANCELLED_MESSAGE)
            return ({'title':'Result','channel':'Channel','thumbnail_preview_url':fixture.URL}, {}, [], self.publication.ref)
        finally:
            self.cleaning.set(); await self.allow_cleanup.wait(); self.cleaned.set()

    async def first(self):
        self.available.clear()
        source, view = await self.open(20)
        await self.pick(source, view)
        card = self.browser._sessions[view.session_id].info_card
        await info.info_callback(self.update(f'ui:vi:{view.session_id}:{card.nonce}:process', card.message), self.context)
        await asyncio.wait_for(self.entered.wait(), 1)
        return self.by_id[self.context.user_data['download_ui']['message']], view

    async def regenerate(self, **kw):
        source, view, choice = await self.offer(**kw)
        await self.act(choice, 'regen', source)
        await asyncio.wait_for(self.entered.wait(), 1)
        return source, view

    async def cancel(self):
        record = self.context.user_data['download_ui']
        q = self.update('ui:pending_cancel:'+record['operation'], self.by_id[record['message']])
        await vod.pending_cancel_callback(q, self.context)
        q.callback_query.answer.assert_awaited_once()
        return record

    async def done(self):
        self.release.set(); self.allow_cleanup.set(); self.close_release.set()
        await asyncio.wait_for(asyncio.gather(*self.tasks), 2)

    async def test_first_list_start_is_full_card_without_bare_placeholder_or_extra_progress(self):
        source, _ = await self.first()
        text = source.text
        self.assertIn('<b>Загрузка чата…</b>', text)
        self.assertIn('&lt;Title&amp;&gt;', text)
        self.assertIn('01:02:03', text)
        self.assertNotEqual('Загрузка чата…', text)
        before = self.context.bot.edit_message_text.await_count
        await safe_edit_html(self.context, 10, source.message_id, text)
        self.assertEqual(before, self.context.bot.edit_message_text.await_count)
        self.worker.assert_awaited_once()
        await self.cancel(); await self.done()

    async def test_first_cancel_waits_for_worker_cleanup_and_publication_close_then_restores_working_info(self):
        source, view = await self.first()
        self.allow_cleanup.clear(); self.close_release.clear()
        record = await self.cancel()
        self.release.set()
        try:
            await asyncio.wait_for(self.cleaning.wait(), 1)
            self.assertTrue(is_busy(self.context)); self.assertIsNone(self.browser._sessions[view.session_id].info_card)
            self.allow_cleanup.set()
            await asyncio.wait_for(self.closing.wait(), 1)
            self.assertTrue(is_busy(self.context)); self.assertFalse(self.closed)
        finally:
            await self.done()
        self.assertTrue(self.closed); self.assertFalse(is_busy(self.context))
        session = self.browser._sessions[view.session_id]
        self.assertIsNotNone(session.info_card)
        self.assertNotEqual(view.revision, session.info_card.revision)
        self.assertIn('Создать чат', [b.text for r in session.info_card.message.keyboard.inline_keyboard for b in r])
        q = self.update(f'ui:vi:{session.token}:{session.info_card.nonce}:back', session.info_card.message)
        await info.info_callback(q, self.context)
        q.callback_query.answer.assert_awaited_once()
        self.assertIsNotNone(session.list_render)
        self.assertNotIn('download_ui', self.context.user_data)

    async def test_regen_cancel_restores_current_ready_and_back_same_page(self):
        source, view = await self.regenerate(page=1)
        record = await self.cancel(); await self.done()
        self.assertTrue(self.closed); self.assertFalse(is_busy(self.context))
        choice = self.context.user_data['chat_choice']
        self.assertEqual('6', choice['vod_id'])
        new = self.by_id[choice['message']]
        self.assertTrue(choice['photo'])
        self.assertIn('Получить готовый чат', [b.text for r in new.keyboard.inline_keyboard for b in r])
        session = self.browser._sessions[view.session_id]
        restored = await self.back(new, self.browser._view(session))
        self.assertEqual(1, restored.page)
        self.assertNotEqual(record['origin']['revision'], restored.revision)

    async def test_regen_cancel_ready_disappears_restores_create_without_touching_previous_result(self):
        _, view = await self.regenerate()
        old = self.available['123456789'].copy()
        await self.cancel(); self.available.clear(); await self.done()
        session = self.browser._sessions[view.session_id]
        self.assertIsNotNone(session.info_card)
        self.assertNotIn('chat_choice', self.context.user_data)
        self.assertEqual(self.ready_result()['html_ref'], old['html_ref'])
        self.assertEqual('Создать чат', session.info_card.message.keyboard.inline_keyboard[0][0].text)

    async def test_text_regen_start_has_single_full_edit_and_restores_text_on_missing_preview(self):
        source, view = await self.regenerate(url='')
        self.context.bot.send_message.assert_not_awaited()
        self.assertIn('<b>Загрузка чата…</b>', source.text)
        await self.cancel(); await self.done()
        self.assertFalse(self.context.user_data['chat_choice']['photo'])
        self.assertIsNotNone(source.keyboard)

    async def test_direct_first_and_regen_cancel_have_exact_status_without_list(self):
        for regenerate in (False, True):
            with self.subTest(regenerate=regenerate):
                self.entered.clear(); self.release.clear(); self.closed = False
                source = self.message(80, '123456789')
                if regenerate:
                    self.available['123456789'] = self.ready_result()
                    await chat.prepare_chat(source, self.context, 7, '123456789')
                    choice = self.context.user_data['chat_choice']
                    await self.act(choice, 'regen', self.by_id[choice['message']])
                else:
                    self.available.clear()
                    await chat.prepare_chat(source, self.context, 7, '123456789')
                await asyncio.wait_for(self.entered.wait(), 1)
                record = await self.cancel(); await self.done()
                msg = self.by_id[record['message']]
                self.assertEqual('Обработка чата отменена пользователем', msg.text)
                self.assertIsNone(msg.keyboard)
                self.assertFalse(self.browser._sessions)

    async def test_expired_session_does_not_revive_old_buttons(self):
        _, view = await self.first()
        record = await self.cancel(); self.now += 601; await self.done()
        self.assertNotIn(view.session_id, self.browser._sessions)
        self.assertEqual('Обработка чата отменена пользователем', self.by_id[record['message']].text)

    async def test_new_channel_before_cleanup_is_never_overwritten(self):
        _, view = await self.first()
        record = await self.cancel()
        new, latest = await self.open(30, 'other')
        before = new.edit_text.await_count
        await self.done()
        self.assertEqual(before, new.edit_text.await_count)
        self.assertTrue(self.browser.is_current(latest))
        self.assertNotIn(view.session_id, self.browser._sessions)
        self.assertEqual('Обработка чата отменена пользователем', self.by_id[record['message']].text)

    async def test_repeat_cancel_and_old_operation_token_cannot_cancel_new_download(self):
        _, view = await self.first()
        record = await self.cancel(); await self.cancel(); await self.done()
        session = self.browser._sessions[view.session_id]
        card = session.info_card
        self.entered.clear(); self.release.clear()
        await info.info_callback(self.update(f'ui:vi:{session.token}:{card.nonce}:process', card.message), self.context)
        await asyncio.wait_for(self.entered.wait(), 1)
        newer = self.context.user_data['vod_active_operation']
        self.assertNotEqual(record['operation'], newer)
        for data, owner, message in [('ui:pending_cancel:'+record['operation'],7,self.by_id[record['message']]),
                                      ('ui:pending_cancel:'+newer,8,self.by_id[record['message']]),
                                      ('ui:pending_cancel:'+newer,7,self.message(999)), ('ui:pending_cancel',7,self.by_id[record['message']])]:
            await vod.pending_cancel_callback(self.update(data, message, owner), self.context)
            self.assertFalse(is_cancelled(self.context, newer))
        await self.cancel(); await self.done()

    async def test_late_cancel_signal_does_not_replace_successful_result(self):
        source, view = await self.regenerate()
        self.success = True
        await self.cancel(); await self.done()
        self.assertNotIn(view.session_id, self.browser._sessions)
        calls = self.context.bot.edit_message_text.await_args_list
        self.assertFalse(any('отменена' in k.kwargs.get('text','') for k in calls))
        self.assertEqual(42, self.publication.complete())

    async def test_worker_error_and_task_cancel_never_claim_user_cancellation(self):
        for error in (ValueError('synthetic'), asyncio.CancelledError()):
            with self.subTest(error=type(error).__name__):
                self.entered.clear(); self.release.clear()
                self.error = error
                source = self.message(80)
                self.available.clear()
                await chat.prepare_chat(source, self.context, 7, '123456789')
                record = self.context.user_data['download_ui']
                self.release.set()
                await asyncio.wait_for(asyncio.gather(*self.tasks, return_exceptions=True), 2)
                self.assertFalse(is_busy(self.context))
                self.assertNotEqual('Обработка чата отменена пользователем', self.by_id[record['message']].text)
                self.assertNotIn('download_ui', self.context.user_data)

    async def test_cancel_restoration_failure_or_cancellation_keeps_slot_released(self):
        for error in (BadRequest('synthetic'), asyncio.CancelledError()):
            with self.subTest(error=type(error).__name__):
                self.entered.clear(); self.release.clear()
                _, view = await self.first()
                await self.cancel()
                self.context.bot.edit_message_text.side_effect = error
                self.release.set()
                await asyncio.wait_for(asyncio.gather(*self.tasks, return_exceptions=True), 2)
                self.assertFalse(is_busy(self.context)); self.assertTrue(self.closed)
                self.assertNotIn('download_ui', self.context.user_data)
                async def edit(**kw):
                    return await self.by_id[kw['message_id']].edit_text(kw['text'],reply_markup=kw.get('reply_markup'))
                self.context.bot.edit_message_text.side_effect = edit

    async def test_cancel_telemetry_has_one_final_event_for_original_operation(self):
        with self.assertLogs('tcd.resources', level='INFO') as captured:
            await self.first(); record = await self.cancel(); await self.done()
        records = [json.loads(line.split('tcd.resources:',1)[1]) for line in captured.output]
        finals = [x for x in records if x['event']=='operation_end']
        self.assertEqual(1, len(finals))
        self.assertEqual(record['operation'], finals[0]['operation_id'])
        self.assertEqual('cancelled', finals[0]['outcome'])

    async def test_public_result_and_history_body_no_longer_repeat_url_action_label(self):
        from src.ui import ui_history as ui
        item = dict(title='Title',fmt='html_online',html_url=self.ready_result()['html_ref'], vod_url='https://www.twitch.tv/videos/123456789')
        with patch.object(ui,'HTML_PUBLISH_MODE','public'), patch.object(ui,'local_published_html_status',return_value=True):
            text,kb = build_history_page([item],0,1)[0][0]
        self.assertNotIn('Открыть чат', text)
        self.assertEqual('Открыть чат',kb.inline_keyboard[0][0].text)
        self.assertEqual(item['html_url'], kb.inline_keyboard[0][0].url)

    async def test_progress_escapes_known_metadata_and_unknown_values_are_safe(self):
        text = _build_progress_card({'channel':'C<&>','title':'T<&>'}, '', 0, 0, False)
        self.assertIn('C&lt;&amp;&gt;',text); self.assertIn('T&lt;&amp;&gt;',text)
        self.assertIn('💬 0',text)

    async def test_photo_start_sends_full_progress_directly_without_replaying_offer(self):
        await self.regenerate()
        self.context.bot.send_message.assert_awaited_once()
        self.assertIn('<b>Загрузка чата…</b>', self.context.bot.send_message.await_args.kwargs['text'])
        self.assertNotIn('Чат VOD уже готов', self.context.bot.send_message.await_args.kwargs['text'])
        self.assertTrue(is_busy(self.context))  # Slow worker has an immediate usable Cancel.
        await self.cancel(); await self.done()

    async def test_restored_info_image_failure_keeps_text_actions_and_original_page(self):
        _, view = await self.first()
        await self.cancel()
        self.context.bot.edit_message_media.side_effect = BadRequest('synthetic')
        await self.done()
        card = self.browser._sessions[view.session_id].info_card
        self.assertIsNotNone(card); self.assertFalse(card.photo)
        self.assertEqual('Создать чат', card.message.keyboard.inline_keyboard[0][0].text)

    async def test_publication_close_error_never_shows_confirmed_cancel(self):
        await self.first(); record = await self.cancel()
        self.publication.close = lambda: (_ for _ in ()).throw(OSError('synthetic'))
        self.release.set()
        results = await asyncio.wait_for(asyncio.gather(*self.tasks, return_exceptions=True), 2)
        self.assertTrue(any(isinstance(r,OSError) for r in results))
        self.assertFalse(is_busy(self.context))
        self.assertNotIn('download_ui', self.context.user_data)
        self.assertNotEqual('Обработка чата отменена пользователем', self.by_id[record['message']].text)

    async def test_restore_timeout_is_bounded_and_does_not_reopen_slot(self):
        await self.first(); await self.cancel()
        entered = asyncio.Event()
        async def stuck(**kw):
            entered.set(); await asyncio.Event().wait()
        self.context.bot.edit_message_text.side_effect = stuck
        real_timeout = asyncio.timeout
        with patch.object(asyncio,'timeout',side_effect=lambda n:real_timeout(min(n,0.02))):
            await self.done()
        self.assertTrue(entered.is_set()); self.assertFalse(is_busy(self.context))
        self.assertTrue(self.closed); self.assertNotIn('download_ui',self.context.user_data)

    async def test_necessary_ready_photo_transition_is_one_edit_not_resend(self):
        source, _, choice = await self.offer()
        self.context.bot.edit_message_media.reset_mock()
        self.context.bot.edit_message_text.reset_mock()
        self.context.bot.send_message.reset_mock()
        await self.act(choice,'open',source)
        self.context.bot.edit_message_media.assert_awaited_once()
        self.context.bot.edit_message_text.assert_not_awaited()
        self.context.bot.send_message.assert_not_awaited()
        self.context.bot.delete_message.assert_not_awaited()

    async def test_direct_input_full_progress_deduplicates_initial_worker_card(self):
        self.available.clear()
        await chat.prepare_chat(self.message(80),self.context,7,'123456789')
        await asyncio.wait_for(self.entered.wait(),1)
        ui=self.context.user_data['download_ui']
        text=_build_progress_card(vars(self.metadata.return_value),'',0,0,False)
        self.assertEqual(text,self.by_id[ui['message']].text)
        before=len(self.events)
        await safe_edit_html(self.context,ui['chat'],ui['message'],text)
        self.assertEqual(before,len(self.events))
        await self.cancel();await self.done()

    async def test_initial_formatter_failure_cannot_leak_global_slot(self):
        source=self.message(80)
        with self.assertRaises(TypeError):
            await vod._start_download(self.update('',source),self.context,'123456789',modern=True,
                                      initial_meta={'channel':1})
        self.assertFalse(is_busy(self.context))
        self.assertNotIn('download_ui',self.context.user_data)
        self.worker.assert_not_awaited()

    async def test_navigation_hook_failure_cannot_turn_a_running_start_into_failure(self):
        source=self.message(80)
        source.on_download=lambda:(_ for _ in ()).throw(ValueError('synthetic'))
        self.assertTrue(await vod._start_download(self.update('',source),self.context,'123456789',modern=True))
        await asyncio.wait_for(self.entered.wait(),1)
        self.assertTrue(is_busy(self.context))
        await self.cancel();await self.done()
        self.assertEqual('Обработка чата отменена пользователем',source.text)
