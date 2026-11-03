"""Terminal job cards, synthetic Telegram and isolated publication only."""
import asyncio
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock,Mock,patch
from telegram.error import BadRequest,TimedOut
import test_processing_control as fixture
from src.handlers import handlers_vod as vod
from src.handlers.handlers_state import begin_download
from src.job_context import JobContext
from src.vod_scheduler import scheduler,stop_scheduler
from src.twitch_api.client import TwitchAPIError


class TerminalVodErrors(unittest.IsolatedAsyncioTestCase):
    setUp=fixture.ProcessingControlTestCase.setUp
    request=fixture.ProcessingControlTestCase.request

    async def asyncTearDown(self):
        await stop_scheduler(self.application)
        await fixture.ProcessingControlTestCase.asyncTearDown(self)

    def card(self,photo=True):
        root,_=self.request()
        root.bot.edit_message_caption=AsyncMock()
        root.bot.edit_message_reply_markup=AsyncMock()
        if photo:root.bot.edit_message_text.side_effect=BadRequest('There is no text in the message to edit')
        manager=scheduler(self.application);job,error=manager.reserve(1,'123456789')
        self.assertIsNone(error)
        ctx=JobContext(root,job);job.context=ctx;begin_download(ctx)
        record=dict(operation=job.id,chat=1,message=20,photo=photo)
        ctx.user_data['download_ui']=record
        # A later shared navigation/job card is not the errored job's surface.
        foreign=dict(operation='f'*32,chat=1,message=21)
        root.user_data['download_ui']=foreign
        return root,ctx,job,record,foreign

    async def fail_worker(self,error,photo=True):
        root,ctx,job,record,foreign=self.card(photo)
        publication=NS(reserve=Mock(),fail=Mock(),close=Mock(),complete=Mock())
        with patch.object(vod.results,'Publication',return_value=publication),patch.object(vod,'download_and_send',side_effect=error):
            scheduler(self.application).arm(job,lambda:vod._runner(ctx,1,20,'url','123456789','html_online',1,job.id))
            await asyncio.gather(job.task,return_exceptions=True);await asyncio.sleep(0)
        publication.fail.assert_called_once();publication.close.assert_called_once();publication.complete.assert_not_called()
        self.assertEqual('error',job.state);self.assertEqual(0,scheduler(self.application).active)
        self.assertIs(foreign,root.user_data['download_ui'])
        return root

    async def test_empty_photo_gets_terminal_caption_without_cancel_or_new_message(self):
        root=await self.fail_worker(RuntimeError('CHAT_EMPTY'))
        root.bot.edit_message_caption.assert_awaited_once_with(chat_id=1,message_id=20,caption='У этого VOD нет чата.',reply_markup=None)
        root.bot.send_message.assert_not_awaited()

    async def test_processing_photo_error_gets_own_terminal_caption(self):
        root=await self.fail_worker(TwitchAPIError('synthetic',status=503))
        root.bot.edit_message_caption.assert_awaited_once_with(chat_id=1,message_id=20,caption='Не удалось загрузить чат VOD. Повторите попытку позже.',reply_markup=None)
        root.bot.send_message.assert_not_awaited()

    async def test_queued_photo_registration_failure_gets_terminal_caption(self):
        root,ctx,job,record,foreign=self.card()
        await scheduler(self.application)._notify_start_failure(job)
        root.bot.edit_message_caption.assert_awaited_once_with(chat_id=1,message_id=20,caption='Не удалось начать обработку. Выберите VOD заново.',reply_markup=None)
        root.bot.send_message.assert_not_awaited()

    async def test_registration_fails_immediately_after_queued_preview_delivery(self):
        manager=scheduler(self.application);release=asyncio.Event()
        for v in ('1','2'):
            job,_=manager.reserve(9,v);manager.arm(job,release.wait)
        root,u=self.request()
        root.bot.edit_message_caption=AsyncMock();root.bot.edit_message_reply_markup=AsyncMock()
        async def media(**kw):
            release.set()
            await asyncio.sleep(0);await asyncio.sleep(0)
        root.bot.edit_message_media=AsyncMock(side_effect=media)
        self.application.create_task.side_effect=RuntimeError('synthetic registration failure')
        url='https://static-cdn.jtvnw.net/cf_vods/example/thumb/720x405.jpeg'
        with patch.object(vod,'download_and_send') as worker,patch.object(vod.results,'Publication') as publication:
            with self.assertRaisesRegex(RuntimeError,'registration failure'):
                await vod._start_download(u,root,'123456789',modern=True,initial_meta=dict(thumbnail_preview_url=url))
            worker.assert_not_called();publication.assert_not_called()
        root.bot.edit_message_caption.assert_awaited_once_with(chat_id=1,message_id=20,
            caption='Не удалось начать обработку. Выберите VOD заново.',reply_markup=None)
        root.bot.send_message.assert_not_awaited();self.assertEqual(0,manager.active)

    async def test_html_timeout_and_access_errors_keep_existing_messages(self):
        cases=[(ValueError('HTML'), 'Не удалось обработать чат VOD. Повторите попытку.'),
               (RuntimeError('HTML'), 'Не удалось обработать чат VOD. Повторите попытку.'),
               (TimeoutError(), 'Время ожидания истекло. Повторите попытку позже.'),
               (TwitchAPIError('synthetic',status=403), 'Сервис отклонил запрос. Повторите попытку позже.')]
        for error,text in cases:
            with self.subTest(error=type(error).__name__):
                root=await self.fail_worker(error)
                self.assertEqual(text,root.bot.edit_message_caption.await_args.kwargs['caption'])
                self.assertIsNone(root.bot.edit_message_caption.await_args.kwargs['reply_markup'])
                root.bot.send_message.assert_not_awaited()

    async def test_text_card_terminal_edit_removes_cancel(self):
        root=await self.fail_worker(RuntimeError('CHAT_EMPTY'),photo=False)
        root.bot.edit_message_text.assert_awaited_once_with(chat_id=1,message_id=20,text='У этого VOD нет чата.',reply_markup=None)
        root.bot.edit_message_caption.assert_not_awaited();root.bot.send_message.assert_not_awaited()

    async def test_uncertain_photo_flag_definite_no_caption_falls_back_to_same_text(self):
        from src.ui.result_preview import finish_error_card
        root,ctx,job,record,_=self.card()
        root.bot.edit_message_caption.side_effect=BadRequest('There is no caption in the message to edit')
        root.bot.edit_message_text.side_effect=None
        self.assertTrue(await finish_error_card(ctx,1,20,'error',record))
        root.bot.edit_message_text.assert_awaited_once_with(chat_id=1,message_id=20,text='error',reply_markup=None)
        root.bot.send_message.assert_not_awaited();self.assertFalse(record['photo'])

    async def test_unknown_photo_after_media_timeout_uses_definite_text_rejection(self):
        from src.ui.result_preview import finish_error_card
        root,ctx,job,record,_=self.card(photo=False)
        root.bot.edit_message_text.side_effect=BadRequest('There is no text in the message to edit')
        self.assertTrue(await finish_error_card(ctx,1,20,'error',record))
        root.bot.edit_message_caption.assert_awaited_once_with(chat_id=1,message_id=20,caption='error',reply_markup=None)
        root.bot.send_message.assert_not_awaited();self.assertTrue(record['photo'])

    async def test_terminal_timeout_never_sends_duplicate_and_clears_keyboard(self):
        from src.ui.result_preview import finish_error_card
        root,ctx,job,record,_=self.card()
        # The caption may already have reached Telegram before this timeout.
        root.bot.edit_message_caption.side_effect=TimedOut()
        self.assertFalse(await finish_error_card(ctx,1,20,'error',record))
        root.bot.edit_message_caption.assert_awaited_once()
        root.bot.edit_message_reply_markup.assert_awaited_once_with(chat_id=1,message_id=20,reply_markup=None)
        root.bot.send_message.assert_not_awaited()

    async def test_definite_rejection_strips_keyboard_then_one_fallback(self):
        from src.ui.result_preview import finish_error_card
        root,ctx,job,record,_=self.card()
        root.bot.edit_message_caption.side_effect=BadRequest("Message can't be edited")
        self.assertTrue(await finish_error_card(ctx,1,20,'error',record))
        root.bot.edit_message_reply_markup.assert_awaited_once()
        root.bot.send_message.assert_awaited_once_with(chat_id=1,text='error')

    async def test_stale_record_cannot_edit_another_card(self):
        from src.ui.result_preview import finish_error_card
        root,ctx,job,record,_=self.card()
        ctx.user_data['download_ui']=dict(record,message=21)
        self.assertFalse(await finish_error_card(ctx,1,20,'error',record))
        root.bot.edit_message_caption.assert_not_awaited();root.bot.edit_message_text.assert_not_awaited()
        root.bot.edit_message_reply_markup.assert_not_awaited();root.bot.send_message.assert_not_awaited()

    async def test_photo_error_does_not_touch_other_active_job(self):
        a,u1=self.request(1,'123456789');b,u2=self.request(1,'234567890');b.user_data=a.user_data
        u2.message.message_id=21
        for root in (a,b):
            root.bot.edit_message_caption=AsyncMock();root.bot.edit_message_reply_markup=AsyncMock()
        entered=[asyncio.Event(),asyncio.Event()];release=[asyncio.Event(),asyncio.Event()]
        async def worker(**kw):
            index=0 if kw['vod_id']=='123456789' else 1
            if index==0:kw['context'].user_data['download_ui']['photo']=True
            entered[index].set();await release[index].wait()
            if index==0:raise RuntimeError('CHAT_EMPTY')
            return fixture.ProcessingControlTestCase.result(self)
        with patch.object(vod,'download_and_send',side_effect=worker):
            await vod._start_download(u1,a,'123456789');await vod._start_download(u2,b,'234567890')
            await asyncio.wait_for(asyncio.gather(*(e.wait() for e in entered)),2)
            first,second=list(scheduler(self.application).jobs.values());other=a.user_data['download_ui']
            try:
                release[0].set();await first.task;await asyncio.sleep(0)
                self.assertFalse(second.task.done());self.assertIs(other,a.user_data['download_ui'])
                self.assertEqual(20,a.bot.edit_message_caption.await_args.kwargs['message_id'])
                b.bot.edit_message_caption.assert_not_awaited()
            finally:release[1].set();await second.task
