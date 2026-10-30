"""Notice classification, at-most-one answers, routing and state preservation."""
import asyncio
import ast
import re
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from telegram.error import BadRequest, NetworkError
from telegram.ext import Application, CallbackQueryHandler
from src.ui.callbacks import CallbackReply, stale_callback, STALE_CALLBACK_PATTERN
from src.ui.ui_labels import (STALE_ACTION_NOTICE as STALE, STALE_LIST_NOTICE as LIST,
                              ACTION_DONE_NOTICE as DONE, PROCESSING_NOTICE as BUSY, ACTION_PENDING_NOTICE as PENDING)
from src.handlers import handlers_chat as chat, handlers_vod as vod, handlers_channel_vods as lists
from src.handlers import handlers_vod_info as info
from src.handlers.handlers_state import begin_download, finish_download, is_cancelled
from src.history import history_callbacks as hc, history_files as hf
from src.vod_list import callback
import test_ready_chat_preview as fixture
import test_vod_info as info_fixture


class NoticeTests(unittest.IsolatedAsyncioTestCase):
    _setupAsyncioRunner = fixture.ReadyPreviewTests._setupAsyncioRunner
    setUp = fixture.ReadyPreviewTests.setUp
    asyncTearDown = fixture.ReadyPreviewTests.asyncTearDown
    context_for_user = fixture.ReadyPreviewTests.context_for_user
    task = fixture.ReadyPreviewTests.task
    update = fixture.ReadyPreviewTests.update
    message = fixture.ReadyPreviewTests.message
    ready_result = fixture.ReadyPreviewTests.ready_result
    open = fixture.ReadyPreviewTests.open
    pick = fixture.ReadyPreviewTests.pick
    offer = fixture.ReadyPreviewTests.offer
    back = fixture.ReadyPreviewTests.back

    def notice(self, update, text):
        if text == STALE:
            update.callback_query.answer.assert_awaited_once_with(text, show_alert=False)
        else:
            update.callback_query.answer.assert_awaited_once_with(text)

    async def test_single_reply_including_failure_and_no_sensitive_error_logs(self):
        for error in (None, BadRequest('SECRET'), NetworkError('SECRET'), TimeoutError()):
            q = SimpleNamespace(answer=AsyncMock(side_effect=error))
            reply = CallbackReply(q)
            if error:
                with self.assertLogs('src.ui.callbacks',level='WARNING') as logs:
                    self.assertFalse(await reply.send(STALE))
                self.assertNotIn('SECRET',str(logs.output))
            else:
                self.assertTrue(await reply.send(STALE))
            await reply.send(LIST)
            q.answer.assert_awaited_once_with(STALE, show_alert=False)

    async def test_actual_answer_timeout_and_cancellation_are_not_retried(self):
        for cancel in (False, True):
            entered = asyncio.Event()
            async def stuck(*a,**kw):
                entered.set(); await asyncio.Event().wait()
            q = SimpleNamespace(answer=AsyncMock(side_effect=stuck))
            reply = CallbackReply(q)
            original = asyncio.timeout
            with patch.object(asyncio,'timeout',side_effect=lambda n:original(min(n,0.02))):
                task = self.task(reply.send(STALE))
                await asyncio.wait_for(entered.wait(),1)
                if cancel:
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError):await task
                else:
                    self.assertFalse(await asyncio.wait_for(task,1))
            self.assertFalse(await reply.send(LIST))
            q.answer.assert_awaited_once()

    async def test_expired_replaced_revision_message_and_foreign_lists(self):
        for reason in ('expired','replaced','revision','message','foreign','malformed'):
            source, view = await self.open(20)
            q = self.update(callback(view,'pages'), source, owner=8 if reason=='foreign' else 7)
            if reason=='expired':self.now += 601
            if reason=='replaced':await self.open(30,'other')
            if reason=='revision':self.browser._sessions[view.session_id].revision += 1
            if reason=='message':q.callback_query.message=self.message(999)
            if reason=='malformed':q.callback_query.data += ':invalid'
            before = list(self.events)
            await lists.channel_vods_callback(q,self.context)
            self.notice(q, STALE if reason=='foreign' else LIST)
            self.assertEqual(before,self.events)
        self.runner.assert_not_awaited()

    async def test_valid_navigation_acknowledges_outside_lock_and_before_edit(self):
        source, view = await self.open(20)
        session = self.browser._sessions[view.session_id]
        q = self.update(callback(view,'next'),source)
        async def answer():
            self.assertFalse(lists._ui_lock(session).locked())
            self.assertEqual(view.revision,session.revision)
        q.callback_query.answer.side_effect=answer
        result=await lists.channel_vods_callback(q,self.context)
        self.assertEqual(1,result.page);q.callback_query.answer.assert_awaited_once_with()

    async def test_waiting_lock_answers_promptly_then_expiry_has_no_second_answer(self):
        source, view = await self.open(20)
        lock=lists._ui_lock(self.browser._sessions[view.session_id])
        await lock.acquire()
        q=self.update(callback(view,'next'),source)
        answered=asyncio.Event()
        q.callback_query.answer.side_effect=lambda:answered.set()
        before=list(self.events)
        task=self.task(lists.channel_vods_callback(q,self.context))
        try:
            await asyncio.wait_for(answered.wait(),1)
            self.assertFalse(task.done());self.now+=601
        finally:lock.release()
        await asyncio.wait_for(task,1)
        q.callback_query.answer.assert_awaited_once_with()
        self.assertEqual(before,self.events)

    async def test_failed_success_ack_does_not_navigate_or_start(self):
        source,view=await self.open(20)
        q=self.update(callback(view,'next'),source)
        q.callback_query.answer.side_effect=BadRequest('SECRET')
        before=list(self.events)
        await lists.channel_vods_callback(q,self.context)
        self.assertTrue(self.browser.is_current(view));self.assertEqual(before,self.events)
        self.runner.assert_not_awaited()

    async def test_old_photo_callbacks_after_back_cannot_touch_new_binding(self):
        source,view,choice=await self.offer()
        restored=await self.back(source,view)
        q=self.update('ui:chat:regen:'+choice['token'],source)
        before=list(self.events)
        await chat.chat_callback(q,self.context)
        self.notice(q,STALE);self.assertEqual(before,self.events)
        self.assertTrue(self.browser.is_current(restored));self.runner.assert_not_awaited()

    async def test_ready_stale_repeated_pending_busy_and_foreign_are_distinct(self):
        for reason,text in [('nonce',STALE),('message',STALE),('foreign',STALE),('ttl',STALE),
                            ('done',DONE),('pending',PENDING),('busy',BUSY),('global',BUSY)]:
            source,view,choice=await self.offer(url='')
            q=self.update('ui:chat:regen:'+choice['token'],source,owner=8 if reason=='foreign' else 7)
            other=None
            if reason=='nonce':q.callback_query.data+='bad'
            if reason=='message':q.callback_query.message=self.message(999)
            if reason=='ttl':choice['created_at']=0
            if reason=='done':choice['media_final']=True
            if reason=='pending':choice['action']='open'
            if reason=='busy':self.context.user_data['vod_busy']=True
            if reason=='global':other=self.context_for_user();operation=begin_download(other)
            before=list(self.events)
            try:
                await chat.chat_callback(q,self.context)
                self.notice(q,text);self.assertEqual(before,self.events)
            finally:
                self.context.user_data['vod_busy']=False
                if other:finish_download(other,operation)
        self.runner.assert_not_awaited()

    async def test_cancel_valid_old_foreign_wrong_message_and_completed(self):
        op=begin_download(self.context)
        self.context.user_data['download_ui']=dict(operation=op,owner=7,chat=10,message=20)
        try:
            for data,owner,mid in [('ui:pending_cancel:'+'f'*32,7,20),('ui:pending_cancel:'+op,8,20),
                                  ('ui:pending_cancel:'+op,7,99)]:
                q=self.update(data,self.message(mid),owner)
                await vod.pending_cancel_callback(q,self.context)
                self.notice(q,STALE);self.assertFalse(is_cancelled(self.context,op))
            q=self.update('ui:pending_cancel:'+op,self.message())
            await vod.pending_cancel_callback(q,self.context)
            q.callback_query.answer.assert_awaited_once_with()
            self.assertTrue(is_cancelled(self.context,op));self.assertTrue(self.context.user_data['vod_busy'])
        finally:finish_download(self.context,op)
        q=self.update('ui:pending_cancel:'+op,self.message())
        await vod.pending_cancel_callback(q,self.context)
        self.notice(q,STALE);q.callback_query.message.reply_text.assert_not_awaited()

    async def test_operation_changes_during_ack_cannot_cancel_new_run(self):
        op=begin_download(self.context)
        self.context.user_data['download_ui']=dict(operation=op,owner=7,chat=10,message=20)
        q=self.update('ui:pending_cancel:'+op,self.message())
        async def answer():
            finish_download(self.context,op)
            newer=begin_download(self.context)
            self.context.user_data['download_ui']=dict(operation=newer,owner=7,chat=10,message=20)
        q.callback_query.answer.side_effect=answer
        try:
            await vod.pending_cancel_callback(q,self.context)
            newer=self.context.user_data['vod_active_operation']
            self.assertNotEqual(op,newer);self.assertFalse(is_cancelled(self.context,newer))
            q.callback_query.answer.assert_awaited_once_with()
        finally:finish_download(self.context,self.context.user_data.get('vod_active_operation'))

    async def test_legacy_ready_repeat_does_not_repeat_history(self):
        self.ready.side_effect=None;self.ready.return_value=self.ready_result()
        with patch.object(vod.config,'HISTORY_ENABLED',True),patch.object(vod.db,'add_user_history') as history:
            q=self.update('ui:result:open:123456789',self.message())
            await chat.legacy_open_callback(q,self.context)
            repeat=self.update(q.callback_query.data,q.callback_query.message)
            await chat.legacy_open_callback(repeat,self.context)
            self.notice(repeat,DONE);history.assert_called_once_with(7,1)

    async def test_history_missing_state_and_foreign_rows_notify_without_messages(self):
        with patch.object(hc.config,'HISTORY_ENABLED',True),patch.object(hf.db,'get_history_entry_for_user',return_value=None):
            for data,handler in [('ui:histpage:1',hc.history_page_callback),('ui:histvod:h1',hc.history_vod_callback),
                                 ('ui:histfiles:h1',hf.history_files_callback),('ui:histvod:0',hc.history_vod_callback)]:
                q=self.update(data,self.message())
                await handler(q,self.context)
                self.notice(q,STALE);q.callback_query.message.reply_text.assert_not_awaited()
        self.context.bot.send_message.assert_not_awaited()

    async def test_history_dispatch_delegates_without_double_ack(self):
        row=dict(history_id=1,vod_url='https://www.twitch.tv/videos/1')
        with patch.object(hc.config,'HISTORY_ENABLED',True),patch.object(hf.db,'get_history_entry_for_user',return_value=row):
            q=self.update('ui:histvod:h1',self.message())
            await hc.ui_buttons(q,self.context)
        q.callback_query.answer.assert_awaited_once_with()
        q.callback_query.message.reply_text.assert_awaited_once()

    async def test_history_binding_replaced_during_ack_has_no_output_or_second_answer(self):
        row=dict(history_id=1,vod_url='https://www.twitch.tv/videos/1')
        q=self.update('ui:histvod:h1',self.message())
        async def answer():
            self.context.user_data['history_view']=dict(chat=10,message_id=99,owner=7,history_id=2)
        q.callback_query.answer.side_effect=answer
        with patch.object(hc.config,'HISTORY_ENABLED',True),patch.object(hf.db,'get_history_entry_for_user',return_value=row):
            await hc.history_vod_callback(q,self.context)
        q.callback_query.answer.assert_awaited_once_with()
        q.callback_query.message.reply_text.assert_not_awaited()

    async def test_format_cancel_cannot_clear_a_new_pending_during_ack(self):
        from src.handlers.handlers_state import set_pending, bind_pending_message, get_pending
        source=self.message()
        set_pending(self.context,'vod','1');bind_pending_message(self.context,source)
        q=self.update('ui:format_cancel',source)
        async def answer():
            set_pending(self.context,'vod','2')
        q.callback_query.answer.side_effect=answer
        await vod.format_cancel_callback(q,self.context)
        self.assertEqual('2',get_pending(self.context)['vod_id'])
        q.callback_query.answer.assert_awaited_once_with();source.delete.assert_not_awaited()

    async def test_scoped_fallback_and_ptb_first_match_order(self):
        handler=CallbackQueryHandler(stale_callback,pattern=STALE_CALLBACK_PATTERN)
        for data in ('ui:histpage:old','ui:histfiles:invalid','ui:pending_cancel:bad'):
            self.assertTrue(re.match(STALE_CALLBACK_PATTERN,data))
            q=self.update(data,self.message());await stale_callback(q,self.context);self.notice(q,STALE)
        for data in ('ui:new_feature:test','unknown','noop','ui:vi:whatever','ui:chat:open:nonce'):
            self.assertIsNone(re.match(STALE_CALLBACK_PATTERN,data))
        app=Application.builder().token('123:synthetic').build()
        active=CallbackQueryHandler(vod.pending_cancel_callback,pattern=r'^ui:pending_cancel(?::[0-9a-f]{32})?$')
        app.add_handler(active);app.add_handler(handler)
        self.assertIs(app.handlers[0][0],active)
        from telegram import Update
        def update_for(data):
            return Update.de_json({'update_id':1,'callback_query':{'id':'synthetic','chat_instance':'test',
                'from':{'id':7,'is_bot':False,'first_name':'Test'},'data':data}},app.bot)
        matched=next(h for h in app.handlers[0] if h.check_update(update_for('ui:pending_cancel:'+'a'*32)))
        self.assertIs(matched,active)
        self.assertIs(next(h for h in app.handlers[0] if h.check_update(update_for('ui:pending_cancel:bad'))),handler)
        # Verify actual registration order and absence of a universal callback handler.
        tree=ast.parse((Path(__file__).resolve().parents[1]/'src/main.py').read_text(encoding='utf-8'))
        callbacks=[n for n in ast.walk(tree) if isinstance(n,ast.Call) and isinstance(n.func,ast.Name) and n.func.id=='CallbackQueryHandler']
        names=[n.args[0].id for n in callbacks]
        self.assertGreater(names.index('stale_callback'),names.index('pending_cancel_callback'))
        self.assertGreater(names.index('stale_callback'),names.index('history_files_callback'))


class InfoNoticeTests(unittest.IsolatedAsyncioTestCase):
    _setupAsyncioRunner=info_fixture.InfoTests._setupAsyncioRunner
    setUp=info_fixture.InfoTests.setUp
    asyncTearDown=info_fixture.InfoTests.asyncTearDown
    context_for_user=info_fixture.InfoTests.context_for_user
    task=info_fixture.InfoTests.task
    message=info_fixture.InfoTests.message
    update=info_fixture.InfoTests.update
    ready_result=info_fixture.InfoTests.ready_result
    open=info_fixture.InfoTests.open
    setup_list=info_fixture.InfoTests.setup_list
    show=info_fixture.InfoTests.show
    record=info_fixture.InfoTests.record

    async def test_stale_info_nonce_revision_expiry_message_and_owner_are_neutral(self):
        for reason in ('nonce','revision','expiry','message','owner','replaced'):
            source,view=await self.setup_list()
            await self.show(source,view)
            card=self.record(view)
            q=self.update(f'ui:vi:{view.session_id}:{card.nonce}:process',self.card,owner=8 if reason=='owner' else 7)
            if reason=='nonce':q.callback_query.data=q.callback_query.data.replace(card.nonce,'0'*16)
            if reason=='revision':self.browser._sessions[view.session_id].revision+=1
            if reason=='expiry':self.now+=301
            if reason=='message':q.callback_query.message=self.message(999)
            if reason=='replaced':await self.open(30,'other')
            before=source.edit_text.await_count
            nonce=card.nonce
            await info.info_callback(q,self.context)
            q.callback_query.answer.assert_awaited_once_with(STALE, show_alert=False)
            self.assertEqual(before,source.edit_text.await_count)
            self.assertEqual(nonce,card.nonce)
        self.runner.assert_not_awaited();self.metadata.assert_not_awaited()

    async def test_info_global_busy_is_not_stale_and_ack_has_no_lock(self):
        source,view=await self.setup_list();await self.show(source,view)
        card=self.record(view)
        q=self.update(f'ui:vi:{view.session_id}:{card.nonce}:process',self.card)
        other=self.context_for_user();operation=begin_download(other)
        try:
            await info.info_callback(q,self.context)
            q.callback_query.answer.assert_awaited_once_with(BUSY)
            self.assertIs(card,self.record(view));self.runner.assert_not_awaited()
        finally:finish_download(other,operation)
        q=self.update(f'ui:vi:{view.session_id}:{card.nonce}:process',self.card)
        async def answer():
            self.assertFalse(lists._ui_lock(self.browser._sessions[view.session_id]).locked())
        q.callback_query.answer.side_effect=answer
        await info.info_callback(q,self.context)
        await asyncio.gather(*self.tasks)
        q.callback_query.answer.assert_awaited_once_with();self.runner.assert_awaited_once()
