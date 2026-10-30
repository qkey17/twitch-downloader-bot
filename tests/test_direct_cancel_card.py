"""Real direct input/starter/runner, gated worker and synthetic Telegram only."""
import asyncio
import hashlib
import sqlite3
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from telegram.error import BadRequest
import test_progress_cancel_ui as fixture
import test_result_registry as registry_fixture
from src.database import db_results as results, db_core
from src.handlers import handlers_chat as chat, handlers_vod as vod, handlers_input as inputs
from src.handlers.handlers_state import is_busy, is_cancelled
from src.ui import result_preview as preview
from callback_assertions import assert_stale

REAL_CONNECT = sqlite3.connect
REAL_GET_READY = results.get_ready_result
REAL_PUBLICATION = results.Publication
URL = 'https://static-cdn.jtvnw.net/cf_vods/example/thumb/custom-480x270.jpeg'


class DirectCancelTests(unittest.IsolatedAsyncioTestCase):
    _setupAsyncioRunner = fixture.ProgressCancelTests._setupAsyncioRunner
    asyncTearDown = fixture.ProgressCancelTests.asyncTearDown
    context_for_user = fixture.ProgressCancelTests.context_for_user
    task = fixture.ProgressCancelTests.task
    message = fixture.ProgressCancelTests.message
    update = fixture.ProgressCancelTests.update
    ready_result = fixture.ProgressCancelTests.ready_result
    send_photo = fixture.ProgressCancelTests.send_photo
    close = fixture.ProgressCancelTests.close
    work = fixture.ProgressCancelTests.work
    cancel = fixture.ProgressCancelTests.cancel
    done = fixture.ProgressCancelTests.done

    def setUp(self):
        fixture.ProgressCancelTests.setUp(self)
        self.metadata.return_value.thumbnail_preview_url = ''

    async def direct(self, regenerate=False, text='123456789'):
        self.available.clear()
        if regenerate:
            ready = self.ready_result()
            ready['meta']['thumbnail_preview_url'] = ''
            self.available['123456789'] = ready
        source = self.message(80,text)
        await inputs.text_input(SimpleNamespace(message=source,effective_user=SimpleNamespace(id=7)),self.context)
        if regenerate:
            choice = self.context.user_data['chat_choice']
            await chat.chat_callback(self.update(f'ui:chat:regen:{choice["token"]}',self.by_id[choice['message']]),self.context)
        await asyncio.wait_for(self.entered.wait(),1)
        return self.context.user_data['download_ui']

    def choice_message(self):
        return self.by_id[self.context.user_data['chat_choice']['message']]

    def action(self, action=None, *, owner=7, message=None):
        choice = self.context.user_data['chat_choice']
        return self.update(f'ui:chat:{action or ("create" if choice["create"] else "regen")}:{choice["token"]}',
                           message or self.by_id[choice['message']],owner=owner)

    async def test_url_and_id_restore_same_screen_with_known_metadata_and_create(self):
        for text in ('123456789','https://www.twitch.tv/videos/123456789'):
            record = await self.direct(text=text)
            await self.cancel(); await self.done()
            message = self.choice_message(); choice = self.context.user_data['chat_choice']
            self.assertEqual(record['message'],message.message_id)
            self.assertEqual(['Создать чат'],[r[0].text for r in choice['keyboard'].inline_keyboard])
            for value in ('Channel','&lt;Title&amp;&gt;','01:02:03','2026-01-02 03:04 UTC'):
                self.assertIn(value,choice['text'])
            self.assertNotIn('Обработка чата отменена',message.text)
            self.assertFalse(self.browser._sessions)
            self.context.bot.send_message.assert_not_awaited()
            self.entered.clear(); self.release.clear()

    async def test_restoration_waits_for_worker_cleanup_publication_close_and_free_slot(self):
        await self.direct(); self.allow_cleanup.clear(); self.close_release.clear()
        await self.cancel(); self.release.set()
        try:
            await asyncio.wait_for(self.cleaning.wait(),1)
            self.assertTrue(is_busy(self.context)); self.assertNotIn('chat_choice',self.context.user_data)
            self.allow_cleanup.set(); await asyncio.wait_for(self.closing.wait(),1)
            self.assertTrue(is_busy(self.context)); self.assertNotIn('chat_choice',self.context.user_data)
        finally: await self.done()
        self.assertFalse(is_busy(self.context)); self.assertTrue(self.closed)
        self.assertNotIn('vod_global_operation',self.app.bot_data)
        self.assertIn('chat_choice',self.context.user_data)

    async def test_recreate_after_cancel_uses_new_operation_and_rejects_old_cancel(self):
        old = await self.direct(); await self.cancel(); await self.done()
        create = self.action()
        self.entered.clear(); self.release.clear()
        await chat.chat_callback(create,self.context); await asyncio.wait_for(self.entered.wait(),1)
        newer = self.context.user_data['download_ui']
        self.assertNotEqual(old['operation'],newer['operation'])
        stale = self.update('ui:pending_cancel:'+old['operation'],self.by_id[old['message']])
        await vod.pending_cancel_callback(stale,self.context); assert_stale(self,stale)
        self.assertFalse(is_cancelled(self.context,newer['operation']))
        await chat.chat_callback(self.update(create.callback_query.data,create.callback_query.message),self.context)
        self.assertEqual(2,self.worker.await_count)
        await self.cancel(); await self.done()

    async def test_double_create_during_metadata_starts_only_once(self):
        await self.direct(); await self.cancel(); await self.done()
        query = self.action(); entered,release = asyncio.Event(),asyncio.Event()
        metadata = self.metadata.return_value
        async def slow(*args): entered.set(); await release.wait(); return metadata
        self.metadata.side_effect = slow; self.entered.clear(); self.release.clear()
        task = self.task(chat.chat_callback(query,self.context)); await asyncio.wait_for(entered.wait(),1)
        repeated = self.update(query.callback_query.data,query.callback_query.message)
        await chat.chat_callback(repeated,self.context); assert_stale(self,repeated)
        release.set(); await task; await asyncio.wait_for(self.entered.wait(),1)
        self.assertEqual(2,self.worker.await_count); await self.cancel(); await self.done()

    async def test_repeated_cancel_installs_one_fresh_card(self):
        await self.direct(); await self.cancel(); await self.cancel()
        before = self.context.bot.edit_message_text.await_count
        await self.done()
        self.assertEqual(before+1,self.context.bot.edit_message_text.await_count)
        self.assertEqual(1,self.worker.await_count)
        self.assertNotIn('download_ui',self.context.user_data)

    async def test_ready_regeneration_restores_existing_offer_with_no_back_or_auto_start(self):
        old = await self.direct(regenerate=True); await self.cancel(); await self.done()
        choice = self.context.user_data['chat_choice']
        self.assertFalse(choice['create'])
        self.assertEqual(['Получить готовый чат','Создать чат заново'],[r[0].text for r in choice['keyboard'].inline_keyboard])
        self.assertEqual(1,self.worker.await_count)
        self.entered.clear(); self.release.clear()
        await chat.chat_callback(self.action(),self.context); await asyncio.wait_for(self.entered.wait(),1)
        self.assertNotEqual(old['operation'],self.context.user_data['download_ui']['operation'])
        await self.cancel(); await self.done()

    async def test_disappeared_ready_result_restores_create_and_rejects_forged_open(self):
        await self.direct(regenerate=True); self.available.clear(); await self.cancel(); await self.done()
        self.assertTrue(self.context.user_data['chat_choice']['create'])
        query = self.action('open'); await chat.chat_callback(query,self.context); assert_stale(self,query)
        self.assertEqual(1,self.worker.await_count)

    async def test_result_appearing_before_create_uses_standard_ready_offer(self):
        await self.direct(); await self.cancel(); await self.done()
        ready = self.ready_result(); ready['meta']['thumbnail_preview_url'] = ''
        self.available['123456789'] = ready
        await chat.chat_callback(self.action(),self.context)
        choice = self.context.user_data['chat_choice']
        self.assertFalse(choice['create']); self.assertTrue(choice['current']())
        self.assertEqual(1,self.worker.await_count)

    async def test_new_input_before_old_cleanup_prevents_old_restoration(self):
        record = await self.direct(); await self.cancel()
        source = self.message(81,'987654321')
        await inputs.text_input(SimpleNamespace(message=source,effective_user=SimpleNamespace(id=7)),self.context)
        before = self.context.bot.edit_message_text.await_count
        await self.done()
        self.assertNotIn('chat_choice',self.context.user_data)
        self.assertEqual(before,self.context.bot.edit_message_text.await_count)
        self.assertEqual(1,self.worker.await_count)

    async def test_late_cancellation_signal_does_not_replace_successful_result(self):
        await self.direct(); self.success = True; await self.cancel()
        with patch.object(chat,'restore_direct_cancelled',new=AsyncMock()) as restore:
            await self.done()
        restore.assert_not_awaited(); self.assertNotIn('chat_choice',self.context.user_data)

    async def test_partial_metadata_uses_known_id_and_bounded_failed_thumbnail_lookup(self):
        record = await self.direct(); record['meta'] = {}
        await self.cancel()
        async def failed(*args):
            self.assertIn('chat_choice',self.context.user_data)
            self.assertFalse(is_busy(self.context))
            raise TimeoutError()
        with patch.object(preview,'fetch_video_thumbnail',new=AsyncMock(side_effect=failed)) as lookup:
            await self.done()
        lookup.assert_awaited_once()
        text = self.context.user_data['chat_choice']['text']
        self.assertIn('VOD 123456789',text); self.assertIn('https://www.twitch.tv/videos/123456789',text)
        self.assertIn('⏱ —',text); self.assertEqual(1,self.metadata.await_count)

    async def test_known_metadata_and_explicit_missing_image_do_not_refetch(self):
        await self.direct(); await self.cancel()
        with patch.object(preview,'fetch_video_thumbnail',new=AsyncMock()) as lookup: await self.done()
        lookup.assert_not_awaited(); self.assertEqual(1,self.metadata.await_count)
        self.assertFalse(self.context.user_data['chat_choice']['photo'])

    async def test_long_caption_keeps_complete_text_and_actions(self):
        record = await self.direct(); record['meta'].update(title='<'*1200,thumbnail_preview_url=URL)
        await self.cancel(); await self.done()
        choice = self.context.user_data['chat_choice']
        self.assertEqual(1200,choice['text'].count('&lt;')); self.assertFalse(choice['photo'])
        self.context.bot.edit_message_media.assert_not_awaited()

    async def test_preview_failure_keeps_text_buttons_and_sends_no_extra_card(self):
        record = await self.direct(); record['meta']['thumbnail_preview_url'] = URL
        self.context.bot.edit_message_media.side_effect = BadRequest('synthetic')
        await self.cancel(); await self.done()
        self.assertFalse(self.context.user_data['chat_choice']['photo'])
        self.assertIsNotNone(self.choice_message().keyboard); self.context.bot.send_message.assert_not_awaited()

    async def test_preview_does_not_block_create_and_old_completion_cannot_clear_new_operation(self):
        record = await self.direct(); record['meta']['thumbnail_preview_url'] = URL
        await self.cancel(); entered,release = asyncio.Event(),asyncio.Event()
        async def slow(*args): entered.set(); await release.wait(); return URL
        with patch.object(chat,'resolve_preview',new=AsyncMock(side_effect=slow)):
            self.release.set(); await asyncio.wait_for(entered.wait(),1)
            self.assertFalse(is_busy(self.context)); self.entered.clear(); self.release.clear()
            await chat.chat_callback(self.action(),self.context); await asyncio.wait_for(self.entered.wait(),1)
            newer = self.context.user_data['download_ui']
            release.set(); await asyncio.wait_for(self.tasks[0],1)
            self.assertIs(newer,self.context.user_data['download_ui']); self.assertTrue(is_busy(self.context))
            self.context.bot.edit_message_media.assert_not_awaited()
        await self.cancel(); await self.done()

    async def test_new_channel_during_preview_prevents_late_media(self):
        record = await self.direct(); record['meta']['thumbnail_preview_url'] = URL
        await self.cancel(); entered,release = asyncio.Event(),asyncio.Event()
        async def slow(*args): entered.set(); await release.wait(); return URL
        with patch.object(chat,'resolve_preview',new=AsyncMock(side_effect=slow)):
            self.release.set(); await asyncio.wait_for(entered.wait(),1)
            await inputs.text_input(SimpleNamespace(message=self.message(90,'other'),effective_user=SimpleNamespace(id=7)),self.context)
            release.set(); await self.done()
        self.context.bot.edit_message_media.assert_not_awaited()
        self.assertNotIn('chat_choice',self.context.user_data)

    async def test_failed_edit_replacement_binds_new_id_and_create_still_works(self):
        record = await self.direct(); await self.cancel()
        self.context.bot.edit_message_text.side_effect = BadRequest('message missing')
        await self.done()
        choice = self.context.user_data['chat_choice']
        self.assertNotEqual(record['message'],choice['message'])
        self.context.bot.delete_message.assert_awaited_once_with(chat_id=10,message_id=record['message'])
        self.entered.clear(); self.release.clear()
        await chat.chat_callback(self.action(),self.context); await asyncio.wait_for(self.entered.wait(),1)
        self.assertEqual(choice['message'],self.context.user_data['download_ui']['message'])
        await self.cancel(); await self.done()

    async def test_foreign_wrong_message_expired_and_restart_callbacks_are_inert(self):
        await self.direct(); await self.cancel(); await self.done()
        for owner,message in ((8,None),(7,self.message(999))):
            query = self.action(owner=owner,message=message)
            await chat.chat_callback(query,self.context); assert_stale(self,query)
        choice = self.context.user_data['chat_choice']; query = self.action()
        choice['created_at'] = 0
        await chat.chat_callback(query,self.context); assert_stale(self,query)
        self.context.user_data.pop('chat_choice')
        query = self.update(query.callback_query.data,query.callback_query.message)
        await chat.chat_callback(query,self.context); assert_stale(self,query)
        self.assertEqual(1,self.worker.await_count)

    async def test_coroutine_cancellation_during_restore_does_not_hold_slot(self):
        await self.direct(); await self.cancel(); entered = asyncio.Event()
        async def stuck(**kw): entered.set(); await asyncio.Event().wait()
        self.context.bot.edit_message_text.side_effect = stuck; self.release.set()
        await asyncio.wait_for(entered.wait(),1); self.tasks[0].cancel()
        await asyncio.gather(*self.tasks,return_exceptions=True)
        self.assertFalse(is_busy(self.context)); self.assertNotIn('vod_global_operation',self.app.bot_data)
        self.assertNotIn('download_ui',self.context.user_data)

    async def test_real_previous_html_registry_cache_history_survive_restore(self):
        registry = registry_fixture.ResultRegistryTestCase()
        connection_patch = patch.object(sqlite3,'connect',REAL_CONNECT)
        connection_patch.start()
        try:
            registry.setUp()
            with patch.object(results,'Publication',REAL_PUBLICATION): old = registry.ready('old-result')
            before_html = hashlib.sha256(old.path().read_bytes()).hexdigest()
            await self.direct(regenerate=True)
            before_db = Path(db_core.DB_PATH).read_bytes()
            with patch.object(results,'get_ready_result',REAL_GET_READY):
                await self.cancel(); await self.done()
            self.assertFalse(self.context.user_data['chat_choice']['create'])
            self.assertEqual(before_html,hashlib.sha256(old.path().read_bytes()).hexdigest())
            self.assertEqual(before_db,Path(db_core.DB_PATH).read_bytes())
            self.assertEqual(old.id,REAL_GET_READY('123456789')['id'])
        finally:
            registry.doCleanups()
            connection_patch.stop()

    async def test_restored_photo_create_transfers_progress_id_and_preserves_known_preview(self):
        record = await self.direct(); record['meta']['thumbnail_preview_url'] = URL
        await self.cancel(); await self.done()
        choice = self.context.user_data['chat_choice']; self.assertTrue(choice['photo'])
        self.entered.clear(); self.release.clear()
        await chat.chat_callback(self.action(),self.context); await asyncio.wait_for(self.entered.wait(),1)
        newer = self.context.user_data['download_ui']
        self.assertNotEqual(record['message'],newer['message'])
        self.assertEqual(URL,newer['meta']['thumbnail_preview_url'])
        self.assertIn('Загрузка чата',self.by_id[newer['message']].text)
        await self.cancel(); await self.done()
        self.assertTrue(self.context.user_data['chat_choice']['photo'])

    async def test_restored_ready_photo_can_regenerate_and_cancel_again(self):
        record = await self.direct(regenerate=True); record['meta']['thumbnail_preview_url'] = URL
        await self.cancel(); await self.done()
        self.assertTrue(self.context.user_data['chat_choice']['photo'])
        self.entered.clear(); self.release.clear()
        await chat.chat_callback(self.action(),self.context); await asyncio.wait_for(self.entered.wait(),1)
        newer = self.context.user_data['download_ui']
        self.assertTrue(newer['direct']); self.assertNotEqual(record['message'],newer['message'])
        await self.cancel(); await self.done()
        choice = self.context.user_data['chat_choice']
        self.assertTrue(choice['photo']); self.assertFalse(choice['create'])
        self.assertEqual(2,len(choice['keyboard'].inline_keyboard))

    async def test_new_request_during_delayed_text_restore_keeps_new_screen(self):
        await self.direct(); await self.cancel()
        entered,release = asyncio.Event(),asyncio.Event()
        original = self.context.bot.edit_message_text.side_effect
        async def slow(**kw):
            entered.set(); await release.wait(); return await original(**kw)
        self.context.bot.edit_message_text.side_effect = slow; self.release.set()
        await asyncio.wait_for(entered.wait(),1)
        await inputs.text_input(SimpleNamespace(message=self.message(90,'other'),effective_user=SimpleNamespace(id=7)),self.context)
        new_session = next(iter(self.browser._sessions.values())); new = self.by_id[new_session.message_id]
        before = new.text
        release.set(); await self.done()
        self.assertEqual(before,new.text); self.assertNotIn('chat_choice',self.context.user_data)

    async def test_restore_exception_keeps_slot_released(self):
        await self.direct(); await self.cancel()
        with patch.object(results,'get_ready_result',side_effect=OSError('synthetic')):
            await self.done()
        self.assertFalse(is_busy(self.context)); self.assertNotIn('vod_global_operation',self.app.bot_data)
        self.assertNotIn('download_ui',self.context.user_data)
