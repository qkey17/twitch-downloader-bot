"""Shared end-to-end lifecycle across lists, offers, jobs and durable results."""
import asyncio
import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock,patch
import test_ready_chat_preview as ready_fixture
import test_direct_cancel_card as job_fixture
from src.handlers import handlers_chat as chat,handlers_channel_vods as lists,handlers_vod_info as info
from src.ui import html_file_action as files
from src.ui.card_context import for_message,KEY,LIMIT
from src.html_sender import SENDER_KEY,SendResult,SendStatus
from src.vod_list import callback


class NavigationCards(unittest.IsolatedAsyncioTestCase):
    _setupAsyncioRunner=ready_fixture.ReadyPreviewTests._setupAsyncioRunner
    setUp=ready_fixture.ReadyPreviewTests.setUp
    asyncTearDown=ready_fixture.ReadyPreviewTests.asyncTearDown
    context_for_user=ready_fixture.ReadyPreviewTests.context_for_user
    task=ready_fixture.ReadyPreviewTests.task
    message=ready_fixture.ReadyPreviewTests.message
    update=ready_fixture.ReadyPreviewTests.update
    ready_result=ready_fixture.ReadyPreviewTests.ready_result
    open=ready_fixture.ReadyPreviewTests.open
    pick=ready_fixture.ReadyPreviewTests.pick

    async def test_three_ready_cards_from_three_lists_all_download_after_restart(self):
        for i,ready in enumerate(self.available.values()):ready['id']=str(i+1)*32
        sender=NS(send=AsyncMock(return_value=SendResult(SendStatus.SENT)),closing=False)
        self.app.bot_data[SENDER_KEY]=sender;self.app.running=True
        results=[]
        for n in (20,21,22):
            source,view=await self.open(n);await self.pick(source,view)
            ctx=for_message(self.context,7,10,source.message_id);choice=ctx.user_data['chat_choice']
            await chat.chat_callback(self.update(f'ui:chat:open:{choice["token"]}',source),self.context)
            results.append(self.by_id[self.next_message_id])
        await self.open(23,'another')
        restarted=NS(user_data={},bot=self.context.bot,application=NS(running=True,bot_data={SENDER_KEY:sender}))
        for message in results:
            message.from_user=NS(id=self.context.bot.id);message.reply_markup=message.keyboard
            data=next(b.callback_data for row in message.keyboard.inline_keyboard for b in row if b.callback_data)
            await files.file_callback(self.update(data,message),restarted)
        self.assertEqual(3,sender.send.await_count)
        for call in sender.send.await_args_list:self.assertEqual('1'*32,call.kwargs['generation_id'])

    async def test_new_list_does_not_edit_old_and_old_info_remains_interactive(self):
        self.available.clear()
        a,view=await self.open(20);before=a.edit_text.await_count
        b,other=await self.open(21,'another')
        self.assertEqual(before,a.edit_text.await_count);self.assertTrue(self.browser.is_current(view))
        await lists.channel_vods_callback(self.update(callback(view,'info',0),a),self.context)
        first=self.browser._sessions[view.session_id].info_card
        await lists.channel_vods_callback(self.update(callback(other,'info',1),b),self.context)
        second=self.browser._sessions[other.session_id].info_card
        self.assertTrue(first.nonce);self.assertNotEqual(first.nonce,second.nonce)
        await info.info_callback(self.update(f'ui:vi:{view.session_id}:{first.nonce}:back',a),self.context)
        self.assertIs(second,self.browser._sessions[other.session_id].info_card)

    async def test_foreign_and_expired_callbacks_leave_original_message_unchanged(self):
        a,view=await self.open(20);before=a.edit_text.await_count
        await lists.channel_vods_callback(self.update(callback(view,'info',0),a,owner=99),self.context)
        self.now+=601
        await lists.channel_vods_callback(self.update(callback(view,'info',0),a),self.context)
        self.assertEqual(before,a.edit_text.await_count)

    async def test_transient_card_cache_is_bounded(self):
        for i in range(LIMIT+20):for_message(self.context,7,10,1000+i)
        self.assertEqual(LIMIT,len(self.app.bot_data[KEY]))

    async def test_known_ready_offer_survives_browser_metadata_eviction(self):
        source,view=await self.open(20);await self.pick(source,view)
        choice=for_message(self.context,7,10,20).user_data['chat_choice']
        self.now+=601;self.browser._prune()
        self.assertNotIn(view.session_id,self.browser._sessions)
        await chat.chat_callback(self.update(f'ui:chat:open:{choice["token"]}',source),self.context)
        self.context.bot.send_photo.assert_awaited_once()
        self.runner.assert_not_awaited()

    async def test_different_vods_and_users_keep_exact_generation_downloads(self):
        sender=NS(send=AsyncMock(return_value=SendResult(SendStatus.SENT)),closing=False)
        self.app.running=True;self.app.bot_data[SENDER_KEY]=sender
        requests=[]
        for owner,vod,generation,message in ((7,'123','a'*32,30),(7,'456','b'*32,31),(8,'123','c'*32,32)):
            kb=files.keyboard(self.context,owner,10,dict(id=generation,vod_id=vod),None)
            msg=self.message(message);msg.from_user=NS(id=42);msg.reply_markup=kb
            requests.append((owner,vod,generation,self.update(kb.inline_keyboard[0][0].callback_data,msg,owner)))
        for owner,vod,generation,request in reversed(requests):
            await files.file_callback(request,self.context)
            self.assertEqual((owner,10,vod),sender.send.await_args.args)
            self.assertEqual(generation,sender.send.await_args.kwargs['generation_id'])

    async def test_info_has_no_shorter_expiry_than_its_own_browser_snapshot(self):
        self.available.clear();source,view=await self.open(20)
        await lists.channel_vods_callback(self.update(callback(view,'info',0),source),self.context)
        card=self.browser._sessions[view.session_id].info_card
        self.now+=301
        await info.info_callback(self.update(f'ui:vi:{view.session_id}:{card.nonce}:back',source),self.context)
        self.assertIn('Время указано по UTC',source.caption)


class JobCards(unittest.IsolatedAsyncioTestCase):
    _setupAsyncioRunner=job_fixture.DirectCancelTests._setupAsyncioRunner
    setUp=job_fixture.DirectCancelTests.setUp
    asyncTearDown=job_fixture.DirectCancelTests.asyncTearDown
    context_for_user=job_fixture.DirectCancelTests.context_for_user
    task=job_fixture.DirectCancelTests.task
    message=job_fixture.DirectCancelTests.message
    update=job_fixture.DirectCancelTests.update
    ready_result=job_fixture.DirectCancelTests.ready_result
    send_photo=job_fixture.DirectCancelTests.send_photo
    close=job_fixture.DirectCancelTests.close
    work=job_fixture.DirectCancelTests.work
    cancel=job_fixture.DirectCancelTests.cancel
    done=job_fixture.DirectCancelTests.done
    direct=job_fixture.DirectCancelTests.direct
    open=ready_fixture.ReadyPreviewTests.open

    async def test_cancel_photo_after_other_list_restores_same_vod_actions(self):
        self.metadata.return_value.thumbnail_preview_url=job_fixture.URL
        old=await self.direct();source=self.by_id[old['message']]
        self.assertTrue(source.photo)
        await self.open(25,'another')
        self.browser._sessions.clear()  # Restoration must not need a browser snapshot.
        self.context.bot.send_message.reset_mock();self.context.bot.delete_message.reset_mock()
        await self.cancel();await self.done()
        self.assertTrue(source.photo);self.assertIn('Обработать чат',str(source.keyboard))
        self.assertNotIn('Обработка чата отменена',source.caption)
        self.context.bot.send_message.assert_not_awaited();self.context.bot.delete_message.assert_not_awaited()
        ctx=for_message(self.context,7,10,source.message_id)
        choice=ctx.user_data['chat_choice'];self.assertTrue(choice['current']())

    async def test_terminal_phase_rejects_late_progress(self):
        from src.workers.worker_progress import safe_edit_html
        record=await self.direct();job=self.context.user_data['vod_jobs'][record['operation']]
        record['phase']='ready';before=self.context.bot.edit_message_text.await_count
        await safe_edit_html(job.context,record['chat'],record['message'],'late progress')
        self.assertEqual(before,self.context.bot.edit_message_text.await_count)
        await self.cancel();await self.done()

    async def test_first_and_regeneration_share_preview_with_and_without_queue(self):
        from src.vod_scheduler import scheduler
        from src.handlers import handlers_input as inputs
        for queued,regenerate in ((False,False),(True,False),(False,True),(True,True)):
            with self.subTest(queued=queued,regenerate=regenerate):
                self.entered.clear();self.release.clear()
                self.metadata.return_value.thumbnail_preview_url=job_fixture.URL
                self.available.clear()
                if regenerate:
                    self.available['123456789']=self.ready_result()
                    self.available['123456789']['meta']['thumbnail_preview_url']=job_fixture.URL
                manager=scheduler(self.app);blockers=asyncio.Event()
                if queued:
                    for v in ('100','101'):
                        j,_=manager.reserve(99,v);manager.arm(j,blockers.wait)
                await inputs.text_input(NS(message=self.message(80,'123456789'),effective_user=NS(id=7)),self.context)
                if regenerate:
                    choice=self.context.user_data['chat_choice']
                    await chat.chat_callback(self.update(f'ui:chat:regen:{choice["token"]}',self.by_id[choice['message']]),self.context)
                record=self.context.user_data['download_ui'];source=self.by_id[record['message']]
                self.assertTrue(source.photo);self.assertTrue(record['photo'])
                if queued:
                    self.assertFalse(self.entered.is_set());self.assertIn('В очереди',source.caption)
                blockers.set();await asyncio.wait_for(self.entered.wait(),1)
                self.assertEqual(source.message_id,record['message'])
                self.assertIn('Загрузка чата',source.caption);self.assertNotIn('В очереди',source.caption)
                await self.cancel();await self.done()

    async def test_inflight_progress_finishes_before_terminal_caption_and_cannot_return(self):
        from src.workers.worker_progress import safe_edit_html
        from src.ui.result_preview import render_card
        self.metadata.return_value.thumbnail_preview_url=job_fixture.URL
        record=await self.direct();job=self.context.user_data['vod_jobs'][record['operation']]
        entered,release=asyncio.Event(),asyncio.Event()
        original=self.context.bot.edit_message_caption.side_effect
        async def slow(**kw):
            if kw['caption']=='inflight':entered.set();await release.wait()
            return await original(**kw)
        self.context.bot.edit_message_caption.side_effect=slow
        task=self.task(safe_edit_html(job.context,10,record['message'],'inflight'))
        await asyncio.wait_for(entered.wait(),1);record['phase']='ready'
        terminal=self.task(render_card(self.context.bot,10,'ready result',None,
            message_id=record['message'],photo=True,file_context=job.context))
        await asyncio.sleep(0);self.assertFalse(terminal.done())
        release.set();await asyncio.gather(task,terminal)
        await safe_edit_html(job.context,10,record['message'],'late')
        self.assertEqual('ready result',self.by_id[record['message']].caption)
        await self.cancel();await self.done()

    async def test_stale_job_after_restart_cannot_cancel_current_work(self):
        from src.handlers import handlers_vod as vod
        record=await self.direct();source=self.by_id[record['message']]
        fresh=NS(user_data={},bot=self.context.bot,application=NS(bot_data={}))
        request=self.update('ui:pending_cancel:'+record['operation'],source)
        await vod.pending_cancel_callback(request,fresh)
        self.assertFalse(self.context.user_data['vod_jobs'][record['operation']].cancelled)
        await self.cancel();await self.done()

    async def test_finished_job_or_wrong_message_cannot_edit_any_progress(self):
        from src.workers.worker_progress import safe_edit_html
        record=await self.direct();ctx=self.context.user_data['vod_jobs'][record['operation']].context
        before=self.context.bot.edit_message_text.await_count
        await safe_edit_html(ctx,10,999,'wrong card')
        self.assertEqual(before,self.context.bot.edit_message_text.await_count)
        await self.cancel();await self.done()
        before=self.context.bot.edit_message_text.await_count
        await safe_edit_html(ctx,10,record['message'],'finished progress')
        self.assertEqual(before,self.context.bot.edit_message_text.await_count)

    async def test_progress_delivery_deadline_is_nonfatal_and_creates_no_duplicate(self):
        from src.workers.worker_progress import safe_edit_html
        record=await self.direct();ctx=self.context.user_data['vod_jobs'][record['operation']].context
        original=self.context.bot.edit_message_text.side_effect
        async def stuck(**kw):await asyncio.Event().wait()
        self.context.bot.edit_message_text.side_effect=stuck
        real=asyncio.timeout
        with patch.object(asyncio,'timeout',side_effect=lambda n:real(min(n,.02))):
            await safe_edit_html(ctx,10,record['message'],'new progress')
        self.context.bot.send_message.assert_not_awaited()
        self.context.bot.edit_message_text.side_effect=original
        await self.cancel();await self.done()
