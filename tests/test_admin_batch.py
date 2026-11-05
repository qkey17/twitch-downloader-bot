"""Synthetic admin batches and owner-wide cancellation on the real scheduler/runner."""
import asyncio
import os
from dataclasses import replace
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch
import unittest
from src import admin_access, admin_batch as admin, config
from src.vod_scheduler import scheduler, stop_scheduler
from src.handlers import handlers_cmd as commands, handlers_input as inputs
from src.twitch_api import videos
from src.twitch_api.client import TwitchAPIError
from src.vod_cancellation import request_owner, finish_owner
import test_shared_vods as shared


class Authorization(unittest.IsolatedAsyncioTestCase):
    async def test_missing_invalid_and_other_ids_fail_before_any_side_effect(self):
        for value,user in ((None,7),('',7),('0',7),('-7',7),('7.0',7),(' 7',7),('٧',7),('9'*30,7),('8',7),('7',True)):
            with self.subTest(value=value,user=user),patch.dict(os.environ,{},clear=True), \
                    patch.object(admin_access.db_privileges, 'has_access', return_value=False):
                if value is not None:os.environ['TCD_ADMIN_USER_ID']=value
                message=NS(reply_text=AsyncMock())
                app=NS(bot_data={});ctx=NS(application=app)
                message.text='channel all'
                with patch.object(videos,'fetch_user_by_login',AsyncMock()) as api:
                    await inputs.text_input(NS(effective_user=NS(id=user),message=message),ctx)
                message.reply_text.assert_awaited_once_with('Команда недоступна');api.assert_not_awaited()
                self.assertEqual({},app.bot_data)

    async def test_invalid_batch_syntax_never_creates_batch(self):
        for raw in ('channel all extra', 'https://evil.invalid/x new', 'https://twitch.tv/videos/123 all'):
            ctx=NS(application=NS(bot_data={}))
            msg=NS(text=raw,reply_text=AsyncMock())
            with patch.dict(os.environ,{'TCD_ADMIN_USER_ID':'7'}):
                await inputs.text_input(NS(effective_user=NS(id=7),message=msg),ctx)
            self.assertIn('/help',msg.reply_text.await_args.args[0]);self.assertFalse(ctx.application.bot_data)


class Lifecycle(unittest.IsolatedAsyncioTestCase):
    for name in ('_setupAsyncioRunner','setUp','task','api','message','update','saved','publication','worker','context','request','entered','finish','cancel'):
        locals()[name]=getattr(shared.SharedCards,name)

    def setUp(self):
        shared.SharedCards.setUp(self)
        self.enterContext(patch.object(admin, 'available_results',
            side_effect=lambda ids: {key: self.ready[key] for key in ids if key in self.ready}))
        self.stream = self.enterContext(patch.object(admin, 'current_stream', AsyncMock(return_value='')))

    async def asyncTearDown(self):
        for gate in self.gates.values():gate.set()
        await stop_scheduler(self.app)
        await shared.SharedCards.asyncTearDown(self)

    async def launch(self, records=None, argument='channel', *, owner=7, mode='all', photo=False):
        if records is not None:self.vods[:]=records
        source=NS(reply_text=AsyncMock(),reply_photo=AsyncMock())
        control=NS(chat_id=10,message_id=9000,edit_text=AsyncMock(),edit_caption=AsyncMock())
        source.reply_photo.return_value=control
        if not photo and isinstance(self.fetch.return_value,videos.TwitchUser):
            self.fetch.return_value=replace(self.fetch.return_value,profile_image_url='')
        source.reply_text.return_value=control
        ctx=self.context(owner)
        source.text=f'{argument} {mode}'
        with patch.dict(os.environ,{'TCD_ADMIN_USER_ID':'7'}):
            await inputs.text_input(NS(effective_user=NS(id=owner),message=source),ctx)
        batch=self.app.bot_data[admin.KEY]
        return batch,control,source

    async def cancel_all(self,owner=7):
        msg=NS(chat_id=99,reply_text=AsyncMock())
        await commands.cancel_command(NS(effective_user=NS(id=owner),effective_message=msg),self.context(owner))
        return msg

    async def test_cancel_restoration_exception_drains_other_parts_and_reports_failure(self):
        import src.vod_cancellation as cancellation
        completed=asyncio.Event()
        job=NS(critical=False)
        plan=(None,[(job,NS()),(job,NS())],[])
        calls=0
        async def finish(*args,**kwargs):
            nonlocal calls
            calls+=1
            if calls==1:raise OSError('synthetic failure')
            completed.set()
        with patch.object(cancellation,'request_owner',return_value=plan),patch.object(cancellation,'finish_member',side_effect=finish):
            notice=await asyncio.wait_for(self.cancel_all(),2)
        self.assertTrue(completed.is_set());self.assertEqual(2,calls)
        self.assertEqual('Не удалось подтвердить штатное завершение отмены',notice.reply_text.await_args.args[0])

    async def test_summary_failures_do_not_stop_next_vod_or_change_final_results(self):
        from telegram.error import BadRequest
        for failure in (BadRequest('synthetic failure'),TimeoutError()):
            with self.subTest(failure=type(failure).__name__):
                batch,control,_=await self.launch(self.vods[:2])
                control.edit_text.side_effect=failure
                for vid in ('2','1'):
                    job=await self.entered(vid);await self.finish(job)
                await asyncio.wait_for(batch.task,2)
                self.assertEqual((2,0,0,0),(batch.success,batch.skipped,batch.errors,batch.cancelled_vods))
                self.assertIn('1',self.ready);self.assertIn('2',self.ready)
                self.assertIs(False,await batch.summary('Итог', terminal=True))
                self.assertEqual(2,batch.success)
                # Reset fixture's worker entry events for another independent batch.
                self.started.clear();self.gates.clear()

    async def test_cancel_with_failing_summary_still_fences_next_vod(self):
        from telegram.error import NetworkError
        batch,control,_=await self.launch(self.vods[:2])
        control.edit_text.side_effect=NetworkError('synthetic failure')
        job=await self.entered('2')
        cancel=asyncio.create_task(self.cancel_all())
        while not job.cancelled:await asyncio.sleep(0)
        self.gates['2'].set();await asyncio.wait_for(cancel,2)
        self.assertEqual({'2'},set(self.started));self.assertEqual(2,batch.cancelled_vods)

    async def test_summary_does_not_swallow_task_cancellation(self):
        batch=admin.Batch(7,self.context(7),message=NS(edit_text=AsyncMock(side_effect=asyncio.CancelledError())))
        with self.assertRaises(asyncio.CancelledError):await batch.summary('Итог')

    async def test_one_at_a_time_newest_first_including_regeneration_and_one_control_card(self):
        records=[replace(self.vods[i-1],created_at=f'2026-01-0{4-i}T00:00:00Z') for i in (1,2,3)]
        self.ready['2']=self.saved('2');old=self.ready['2']
        batch,control,source=await self.launch(records+[records[0]],'https://www.twitch.tv/channel')
        for v,regen in (('1',False),('2',True),('3',False)):
            job=await self.entered(v)
            self.assertEqual(v,batch.current_job.vod);self.assertEqual(1,len(scheduler(self.app).jobs))
            self.assertEqual(regen,job.compatibility[-1]=='regeneration')
            self.assertEqual(batch.success,batch.index)
            if v=='2':self.assertIs(old,self.ready['2'])
            await self.finish(job)
        await asyncio.wait_for(batch.task,2)
        self.assertEqual((3,3,0,0),(batch.total,batch.success,batch.skipped,batch.errors))
        source.reply_text.assert_awaited_once();self.assertFalse(self.calls) # all per-VOD Telegram UI suppressed
        self.assertNotIn(admin.KEY,self.app.bot_data)
        self.assertEqual('<b>✅ Массовая обработка завершена</b>\n🟣 Channel', control.edit_text.await_args.args[0])
        self.assertEqual(['🎬 0 из 3', '🎬 0 из 3', '🎬 1 из 3', '🎬 2 из 3'],
                         [call.args[0].splitlines()[1] for call in control.edit_text.await_args_list[:-1]])
        self.assertEqual(3,len(self.publications))

    async def test_newvods_skips_ready_and_counts_only_candidate_ordinals(self):
        records = [replace(self.vods[i-1], created_at=f'2026-01-0{day}T00:00:00Z')
                   for i, day in ((1, 1), (2, 3), (3, 2))]
        ready = self.ready['2'] = self.saved('2')
        batch, card, source = await self.launch(records, mode='new')
        for vid, index in (('3', 0), ('1', 1)):
            job = await self.entered(vid)
            self.assertEqual(index, batch.index)
            self.assertNotEqual('regeneration', job.compatibility[-1])
            await self.finish(job)
        await asyncio.wait_for(batch.task, 2)
        self.assertEqual((2, 2, 1, 0), (batch.total, batch.success, batch.skipped, batch.errors))
        self.assertIs(ready, self.ready['2'])
        self.assertNotIn('2', self.started)
        source.reply_text.assert_awaited_once()
        source.reply_photo.assert_not_awaited()
        edited = [call.args[0] for call in card.edit_text.await_args_list]
        self.assertEqual(['🎬 0 из 2', '🎬 0 из 2', '🎬 1 из 2'],
                         [text.splitlines()[1] for text in edited[:-1]])
        self.assertEqual('<b>✅ Массовая обработка завершена</b>\n🟣 Channel', edited[-1])
        self.assertNotIn('Отменено', edited[-1])
        self.assertEqual('Открыть список', card.edit_text.await_args.kwargs['reply_markup'].inline_keyboard[0][0].text)

    async def test_new_combines_current_live_incomplete_and_missing_and_rechecks_ready(self):
        from src.handlers import handlers_vod as vod
        records = [replace(item, stream_id='55' if item.id == '4' else '54') for item in self.vods[:5]]
        self.stream.return_value = '55'
        for vid in ('3', '4', '5'):
            self.ready[vid] = self.saved(vid)
        self.ready['3']['meta']['captured_during_live'] = True
        self.ready['5']['meta']['capture_complete'] = True
        old = self.ready['3']
        async def metadata(_http, _client, vod_id):
            return NS(**(vars(self.meta) | {'is_live': vod_id == '4'}))
        with patch.object(vod, 'fetch_vod_meta', AsyncMock(side_effect=metadata)):
            batch, card, _ = await self.launch(records + [records[3]], mode='new')
            job = await self.entered('4')
            self.assertEqual((0, 4), (batch.index, batch.total))
            self.assertEqual('regeneration', job.compatibility[-1])
            self.ready['2'] = self.saved('2')  # Published after the queue was fixed.
            self.ready['2']['meta']['capture_complete'] = True
            await self.finish(job)
            job = await self.entered('3')
            self.assertIs(old, self.ready['3'])
            self.assertEqual('regeneration', job.compatibility[-1])
            await self.finish(job)
            job = await self.entered('1')
            self.assertEqual((3, 4), (batch.index, batch.total))
            await self.finish(job)
            await asyncio.wait_for(batch.task, 2)
        self.assertEqual({'1', '3', '4'}, set(self.started))
        self.assertEqual((3, 2, 0), (batch.success, batch.skipped, batch.errors))
        self.assertNotEqual(old['id'], self.ready['3']['id'])
        edits = [call.args[0] for call in card.edit_text.await_args_list]
        # The ready check settles a position before its first worker card.
        self.assertEqual({'🎬 0 из 4', '🎬 1 из 4', '🎬 3 из 4'},
                         {text.splitlines()[1] for text in edits[:-1]})

    async def test_final_card_opens_the_normal_list_without_new_messages(self):
        from src import channel_archive
        from src.handlers import handlers_channel_vods as lists
        from src.ui.ui_labels import STALE_ACTION_NOTICE

        for photo in (False, True):
            with self.subTest(photo=photo), patch.object(channel_archive, 'candidates', return_value=[]):
                self.fetch.return_value = self.user if photo else replace(self.user, profile_image_url='')
                self.vods[:] = self.vods[:6]
                self.started.clear()
                self.gates.clear()
                # Use the real synthetic Telegram transport for both surfaces.
                source = self.message(20, 'channel new')
                self.ready.update({item.id: self.saved(item.id) for item in self.vods})
                with patch.dict(os.environ, {'TCD_ADMIN_USER_ID': '7'}):
                    await inputs.text_input(self.update(source), self.ctx)
                batch = self.app.bot_data[admin.KEY]
                await asyncio.wait_for(batch.task, 2)
                card = batch.message
                self.assertIs(bool(card.photo), photo)
                button = card.reply_markup.inline_keyboard[0][0]
                self.assertEqual('Открыть список', button.text)
                before = len(self.calls)
                for update in (self.update(card, button.callback_data, owner=9),
                               self.update(source, button.callback_data)):
                    await admin.batch_list_callback(update, self.ctx)
                    update.callback_query.answer.assert_awaited_once_with(STALE_ACTION_NOTICE, show_alert=False)
                self.assertEqual(before, len(self.calls))

                entered, release = asyncio.Event(), asyncio.Event()
                fetch = self.fetch.return_value
                async def delayed(*_):
                    entered.set()
                    await release.wait()
                    return fetch
                with patch.object(videos, 'fetch_user_by_login', AsyncMock(side_effect=delayed)):
                    opened = asyncio.create_task(admin.batch_list_callback(self.update(card, button.callback_data), self.ctx))
                    await asyncio.wait_for(entered.wait(), 2)
                    duplicate = self.update(card, button.callback_data)
                    await admin.batch_list_callback(duplicate, self.ctx)
                    duplicate.callback_query.answer.assert_awaited_once_with(STALE_ACTION_NOTICE, show_alert=False)
                    release.set()
                    await asyncio.wait_for(opened, 2)
                nav = next(b for row in card.reply_markup.inline_keyboard for b in row if b.text == '➡️')
                moved = await lists.channel_vods_callback(self.update(card, nav.callback_data), self.ctx)
                self.assertEqual(1, moved.page)
                await admin.batch_list_callback(self.update(card, button.callback_data), self.ctx)
                await batch.summary('Старый итог', terminal=True)
                await batch.summary('Старый прогресс')
                self.assertEqual(['edit_message_caption' if photo else 'edit_message_text'] * 2,
                                 [name for name, _ in self.calls[before:]])

    async def test_batch_list_definite_edit_failure_preserves_final_card_for_retry(self):
        from src import channel_archive
        from src.handlers import handlers_vod as vod
        from telegram.error import BadRequest

        source = self.message(20, 'channel all')
        self.vods[:] = self.vods[:1]
        self.fetch.return_value = replace(self.user, profile_image_url='')
        with patch.dict(os.environ, {'TCD_ADMIN_USER_ID': '7'}), \
                patch.object(vod, 'fetch_vod_meta', AsyncMock(side_effect=TwitchAPIError('missing', status=404))):
            await inputs.text_input(self.update(source), self.ctx)
            batch = self.app.bot_data[admin.KEY]
            await asyncio.wait_for(batch.task, 2)
        card = batch.message
        self.assertEqual('<b>⚠️ Обработка завершена с ошибками</b>\n🟣 Channel', card.text)
        before = (card.text, card.reply_markup)
        payload = card.reply_markup.inline_keyboard[0][0].callback_data
        with patch.object(channel_archive, 'candidates', return_value=[]):
            self.bot.edit_message_text.side_effect = BadRequest('synthetic rejection')
            await admin.batch_list_callback(self.update(card, payload), self.ctx)
            self.assertEqual(before, (card.text, card.reply_markup))
            self.bot.edit_message_text.side_effect = lambda **kw: self.api('edit_message_text', **kw)
            await admin.batch_list_callback(self.update(card, payload), self.ctx)
        self.assertNotEqual(before, (card.text, card.reply_markup))
        self.assertEqual(1, sum(name.startswith('send_') for name, _ in self.calls))
        self.bot.delete_message.assert_not_awaited()

    async def test_avatar_card_edits_only_caption_and_reuses_worker_progress(self):
        from src.workers.worker_progress import _build_progress_card
        from src.ui.result_preview import caption_fits
        batch, card, source = await self.launch(self.vods[:1], photo=True)
        job = await self.entered('1')
        async with batch.render_lock:
            batch.last_summary = 0
        observed = asyncio.Event()

        async def edit(**kwargs):
            if '💬 123' in kwargs['caption']:
                observed.set()

        card.edit_caption.side_effect = edit
        text = _build_progress_card(dict(vars(self.meta), title='<&>'*150), None, 123, 7, True)
        job.participants.progress(text)
        await asyncio.wait_for(observed.wait(), 2)
        self.assertTrue(caption_fits(card.edit_caption.await_args.kwargs['caption']))
        self.assertNotIn('<&>', card.edit_caption.await_args.kwargs['caption'])
        self.assertNotIn('Обработано:', card.edit_caption.await_args.kwargs['caption'])
        await self.finish(job)
        await asyncio.wait_for(batch.task, 2)
        source.reply_photo.assert_awaited_once()
        source.reply_text.assert_not_awaited()
        card.edit_text.assert_not_awaited()
        self.assertEqual('<b>✅ Массовая обработка завершена</b>\n🟣 Channel', card.edit_caption.await_args.kwargs['caption'])
        counts = [call.kwargs['caption'].splitlines()[1] for call in card.edit_caption.await_args_list[:-1]]
        self.assertTrue(counts)
        self.assertEqual({'🎬 0 из 1'}, set(counts))
        self.assertEqual(1, batch.index)
        self.assertEqual('Открыть список', card.edit_caption.await_args.kwargs['reply_markup'].inline_keyboard[0][0].text)
        self.assertFalse(self.calls)

    async def test_batch_cancel_callback_owns_surface_and_never_sends_another_message(self):
        batch, card, source = await self.launch(self.vods[:3])
        await self.finish(await self.entered('3'))
        job = await self.entered('2')
        cancel = source.reply_text.await_args.kwargs['reply_markup'].inline_keyboard[0][0]
        self.assertEqual('❌ Отмена', cancel.text)
        payload = cancel.callback_data
        self.assertLessEqual(len(payload.encode()), 64)

        async def press(user, data=payload, message=card):
            query = NS(message=message, data=data, answer=AsyncMock())
            await admin.batch_cancel_callback(NS(callback_query=query, effective_user=NS(id=user)), self.ctx)
            query.answer.assert_awaited_once_with()

        await press(8)
        await press(7, admin.BATCH_CANCEL+'a'*32)
        await press(7, message=NS(chat_id=card.chat_id, message_id=card.message_id+1))
        self.assertFalse(batch.cancelled)
        await press(7)
        await press(7)
        self.assertTrue(batch.cancelled)
        self.gates[job.vod].set()
        await asyncio.wait_for(batch.task, 2)
        await press(7)
        source.reply_text.assert_awaited_once()
        source.reply_photo.assert_not_awaited()
        self.assertFalse(self.calls)
        self.assertEqual({'3'}, set(self.ready))
        self.assertEqual(1, batch.index)
        self.assertNotIn('1', self.started)
        self.assertTrue(any('Остановка обработки' in call.args[0] for call in card.edit_text.await_args_list))
        self.assertEqual('<b>⏹️ Обработка остановлена по запросу</b>\n🟣 Channel', card.edit_text.await_args.args[0])
        self.assertEqual('Открыть список', card.edit_text.await_args.kwargs['reply_markup'].inline_keyboard[0][0].text)
        next_batch, next_card, _ = await self.launch(self.vods[:1])
        next_job = await self.entered('1')
        await press(7, message=next_card)
        self.assertFalse(next_batch.cancelled)
        await self.finish(next_job)
        await asyncio.wait_for(next_batch.task, 2)

    async def test_batch_only_cancel_command_keeps_same_card(self):
        batch, card, source = await self.launch(self.vods[:2])
        job = await self.entered('2')
        cancel = asyncio.create_task(self.cancel_all())
        while not job.cancelled:
            await asyncio.sleep(0)
        self.gates['2'].set()
        notice = await asyncio.wait_for(cancel, 2)
        notice.reply_text.assert_not_awaited()
        source.reply_text.assert_awaited_once()
        self.assertTrue(batch.task.done())
        self.assertEqual('<b>⏹️ Обработка остановлена по запросу</b>\n🟣 Channel', card.edit_text.await_args.args[0])

    async def test_repeated_batch_is_rejected_while_fetching_and_cancel_stops_it(self):
        entered=asyncio.Event()
        async def slow(*args):entered.set();await asyncio.Event().wait()
        with patch.object(videos,'fetch_user_by_login',AsyncMock(side_effect=slow)):
            batch,_,_=await self.launch()
            await asyncio.wait_for(entered.wait(),1)
            msg=NS(text='channel new',reply_text=AsyncMock())
            with patch.dict(os.environ,{'TCD_ADMIN_USER_ID':'7'}):
                await inputs.text_input(NS(effective_user=NS(id=7),message=msg),self.context(7))
            msg.reply_text.assert_awaited_once_with('Массовая обработка уже выполняется')
            await self.cancel_all()
        self.assertTrue(batch.cancelled);self.assertTrue(batch.task.done());self.assertFalse(self.started)
        self.assertNotIn(admin.KEY,self.app.bot_data)

    async def test_batch_cancel_running_waits_for_cleanup_and_never_issues_next(self):
        batch,_,_=await self.launch(self.vods[:2])
        job=await self.entered('2')
        plan_task=asyncio.create_task(self.cancel_all())
        # Synchronize on cancellation request, never on elapsed wall time.
        while not job.cancelled:await asyncio.sleep(0)
        self.assertFalse(plan_task.done());self.assertTrue(batch.cancelled)
        self.gates['2'].set();await asyncio.wait_for(plan_task,2)
        self.assertEqual({'2'},set(self.started));self.assertEqual(0,batch.success)
        self.assertEqual(2,batch.cancelled_vods)
        self.assertFalse(scheduler(self.app).jobs)

    async def test_owner_cancel_all_cards_across_chats_leaves_other_participant_running(self):
        _,first=await self.request(7,'1');job=await self.entered('1')
        _,second=await self.request(7,'1');_,other=await self.request(8,'1')
        for part in job.participants.members.values():
            if part.owner==7:part.context.user_data['download_ui']['chat']=20+int(part.id[0],16)
        notice=await self.cancel_all(7)
        self.assertFalse(job.cancelled)
        self.assertTrue(any(p.active and p.owner==8 for p in job.participants.members.values()))
        self.assertFalse(any(p.active and p.owner==7 for p in job.participants.members.values()))
        self.assertIn('Ваши текущие',notice.reply_text.await_args.args[0])
        await self.finish(job)
        self.assertEqual(1,len(self.publications));self.publications[0].complete.assert_called_once()
        self.assertIn(self.publications[0].ref,str(other.reply_markup))

    async def test_multiple_waiting_and_running_jobs_cancel_only_owner(self):
        await self.request(7,'1');a=await self.entered('1')
        await self.request(8,'2');b=await self.entered('2')
        await self.request(7,'3');c=next(j for j in scheduler(self.app).jobs.values() if j.vod=='3')
        self.assertEqual('waiting',c.state)
        operation=asyncio.create_task(self.cancel_all(7))
        while not a.cancelled:await asyncio.sleep(0)
        self.assertTrue(c.finished.is_set());self.assertFalse(b.cancelled)
        self.gates['1'].set();await asyncio.wait_for(operation,2)
        self.assertNotIn('3',self.started);await self.finish(b)
        await self.cancel_all(7)  # Also consume the restored ready/Create offer.
        notice=await self.cancel_all(7);self.assertEqual('Нет активных обработок',notice.reply_text.await_args.args[0])

    async def test_missing_vod_counts_as_error_and_batch_continues(self):
        from src.handlers import handlers_vod as vod
        # A missing freshly fetched VOD never reserves a scheduler job.
        metadata=AsyncMock(side_effect=[NS(length_seconds=None),self.meta])
        with patch.object(vod,'fetch_vod_meta',metadata):
            batch,_,_=await self.launch(self.vods[:2]);job=await self.entered('1');await self.finish(job)
            await asyncio.wait_for(batch.task,2)
        self.assertEqual((1,1),(batch.errors,batch.success));self.assertNotIn('2',self.started)

    async def test_local_chat_error_continues_and_keeps_previous_ready_generation(self):
        from src.handlers import handlers_vod as vod
        old=self.ready['2']=self.saved('2')
        original=self.worker
        async def fail_one(**kwargs):
            if kwargs['vod_id']=='2':raise RuntimeError('CHAT_EMPTY')
            return await original(**kwargs)
        with patch.object(vod,'download_and_send',fail_one):
            batch,_,_=await self.launch(self.vods[:2]);job=await self.entered('1');await self.finish(job)
            await asyncio.wait_for(batch.task,2)
        self.assertEqual((1,1),(batch.errors,batch.success));self.assertIs(old,self.ready['2'])

    async def test_critical_worker_failure_stops_pending_batch_items(self):
        from src.handlers import handlers_vod as vod
        with patch.object(vod,'download_and_send',AsyncMock(side_effect=OSError('synthetic'))):
            batch,_,_=await self.launch(self.vods[:2]);await asyncio.wait_for(batch.task,2)
        self.assertEqual(1,len(self.publications));self.assertEqual((1,0),(batch.errors,batch.success))
        self.assertEqual(0,batch.index)

    async def test_shutdown_during_worker_drains_batch_and_does_not_restart(self):
        batch,_,_=await self.launch(self.vods[:2]);job=await self.entered('2')
        await asyncio.wait_for(stop_scheduler(self.app),2)
        self.assertTrue(batch.task.done());self.assertFalse(scheduler(self.app).jobs)
        self.assertNotIn(admin.KEY,self.app.bot_data);self.assertNotIn('1',self.started)

    async def test_commit_fence_is_not_cancelled_and_result_is_preserved(self):
        await self.request(7,'1');job=await self.entered('1')
        job.committing=True
        task=asyncio.create_task(self.cancel_all())
        while not self.context(7).application.bot_data.get('tdb_owner_cancellations'):await asyncio.sleep(0)
        self.assertFalse(job.cancelled);self.assertFalse(task.done())
        await self.finish(job);await asyncio.wait_for(task,2)
        self.publications[0].complete.assert_called_once()

    async def test_incomplete_lists_limits_and_api_errors_never_submit_jobs(self):
        from src.handlers import handlers_vod as vod
        for pages in (
            [([replace(self.vods[0],id=str(n+1)) for n in range(i*100,(i+1)*100)],str(i)) for i in range(5)],
            [([replace(self.vods[0],id=str(i+1))],str(i)) for i in range(10)],
            [([self.vods[0]],'C'),TwitchAPIError('rate',status=429,retry_after=10)],
            [TwitchAPIError('network',status=503)],
            [TimeoutError()],
            [([NS()],None)],
        ):
            with self.subTest(pages=len(pages)),patch.object(videos,'fetch_archive_vods',AsyncMock(side_effect=pages)),patch.object(vod,'fetch_vod_meta',AsyncMock()) as meta:
                batch,control,source=await self.launch()
                await asyncio.wait_for(batch.task,2)
                meta.assert_not_awaited();self.assertFalse(scheduler(self.app).jobs)
                rendered = control.edit_text.await_args or source.reply_text.await_args
                self.assertTrue(any(label in rendered.args[0] for label in ('полный список','остановлена из-за ошибки')))
        self.assertFalse(self.publications)

    async def test_empty_and_missing_channel_do_not_create_workers(self):
        for user,records in ((None,[]),(self.user,[])):
            with patch.object(videos,'fetch_user_by_login',AsyncMock(return_value=user)),patch.object(videos,'fetch_archive_vods',AsyncMock(return_value=(records,None))):
                batch,card,_=await self.launch();await asyncio.wait_for(batch.task,2)
                self.assertEqual(0,batch.total);self.assertFalse(self.publications)
                self.assertFalse(any('🎬' in call.args[0] for call in card.edit_text.await_args_list))

    async def test_fresh_metadata_error_is_local_but_authorization_failure_stops(self):
        from src.handlers import handlers_vod as vod
        with patch.object(vod,'fetch_vod_meta',AsyncMock(side_effect=[TwitchAPIError('missing',status=404),self.meta])):
            batch,card,_=await self.launch(self.vods[:2]);job=await self.entered('1')
            self.assertEqual(1,batch.index)
            await self.finish(job);await asyncio.wait_for(batch.task,2)
        self.assertEqual((1,1),(batch.errors,batch.success))
        self.assertEqual(['🎬 0 из 2', '🎬 1 из 2', '🎬 1 из 2'],
                         [call.args[0].splitlines()[1] for call in card.edit_text.await_args_list[:-1]])
        self.assertEqual('<b>⚠️ Обработка завершена с ошибками</b>\n🟣 Channel', card.edit_text.await_args.args[0])
        with patch.object(vod,'fetch_vod_meta',AsyncMock(side_effect=TwitchAPIError('auth',status=401))) as meta:
            batch,_,_=await self.launch(self.vods[:2]);await asyncio.wait_for(batch.task,2)
            self.assertEqual(1,meta.await_count);self.assertEqual(1,batch.errors)

    async def test_batch_joins_compatible_job_and_owner_cancel_leaves_other_user(self):
        await self.request(8,'1');job=await self.entered('1')
        batch,_,_=await self.launch(self.vods[:1])
        # Wait for the batch's participation without a timer-based race.
        while batch.current_part is None:await asyncio.sleep(0)
        self.assertIs(job,batch.current_job);self.assertEqual(1,self.worker_calls)
        await self.cancel_all(7)
        self.assertFalse(job.cancelled);self.assertTrue(any(p.active and p.owner==8 for p in job.participants.members.values()))
        await self.finish(job);self.publications[0].complete.assert_called_once()

    async def test_batch_can_own_job_that_later_gets_normal_participant(self):
        batch,_,_=await self.launch(self.vods[:1]);job=await self.entered('1')
        _,other=await self.request(8,'1')
        self.assertEqual(1,self.worker_calls);self.assertEqual(2,len(job.participants.members))
        await self.finish(job);await asyncio.wait_for(batch.task,2)
        self.assertEqual(1,batch.success);self.assertIn(self.publications[0].ref,str(other.reply_markup))

    async def test_batch_waits_behind_regular_jobs_with_one_reserved_vod(self):
        await self.request(8,'1');a=await self.entered('1')
        await self.request(8,'2');b=await self.entered('2')
        records=self.vods[2:4]
        batch,card,source=await self.launch(records)
        while batch.current_job is None:await asyncio.sleep(0)
        self.assertEqual('waiting',batch.current_job.state)
        self.assertEqual(1,sum(j.owner==7 for j in scheduler(self.app).jobs.values()))
        operation=asyncio.create_task(self.cancel_all(7))
        await asyncio.wait_for(operation,2)
        self.assertNotIn('3',self.started);self.assertNotIn('4',self.started)
        self.assertEqual(0, batch.index)
        self.assertTrue(any('🎬 0 из 2' in call.args[0] for call in card.edit_text.await_args_list))
        self.assertEqual('<b>⏹️ Обработка остановлена по запросу</b>\n🟣 Channel', card.edit_text.await_args.args[0])
        source.reply_text.assert_awaited_once()
        self.assertFalse(a.cancelled or b.cancelled);await self.finish(a);await self.finish(b)

    async def test_cancel_during_metadata_and_second_cancel_do_not_submit(self):
        from src.handlers import handlers_vod as vod
        entered=asyncio.Event()
        async def slow(*a,**k):entered.set();await asyncio.Event().wait()
        with patch.object(vod,'fetch_vod_meta',AsyncMock(side_effect=slow)):
            batch,_,_=await self.launch(self.vods[:1]);await asyncio.wait_for(entered.wait(),1)
            await self.cancel_all();await self.cancel_all()
        self.assertTrue(batch.task.done());self.assertFalse(self.publications)
        self.assertEqual(0,scheduler(self.app).preparing)

    async def test_cancel_after_commit_before_next_item_keeps_result_and_fences_issuance(self):
        batch,_,_=await self.launch(self.vods[:2]);job=await self.entered('2')
        job.committing=True
        cancelled=asyncio.create_task(self.cancel_all())
        while not batch.cancelled:await asyncio.sleep(0)
        self.assertFalse(job.cancelled)
        await self.finish(job);await asyncio.wait_for(cancelled,2)
        self.assertEqual(1,batch.success);self.assertNotIn('1',self.started)
        self.assertIn('2',self.ready)

    async def test_full_500_list_retains_only_one_participation_and_ignores_archived_registry(self):
        from src import channel_archive
        records=[replace(self.vods[0],id=str(n+1)) for n in range(500)]
        pages=[(records[i:i+50],str(i+50) if i+50<500 else None) for i in range(0,500,50)]
        self.ready['999']=self.saved('999')
        with patch.object(videos,'fetch_archive_vods',AsyncMock(side_effect=pages)) as fetch,patch.object(channel_archive,'candidates',side_effect=AssertionError('No merged archive source')):
            batch,_,_=await self.launch();job=await self.entered('500')
            self.assertEqual(10,fetch.await_count);self.assertEqual(500,batch.total)
            self.assertEqual(1,len(scheduler(self.app).jobs));self.assertEqual(1,len(job.participants.members))
            cancel=asyncio.create_task(self.cancel_all())
            while not job.cancelled:await asyncio.sleep(0)
            self.gates['500'].set();await asyncio.wait_for(cancel,2)
        self.assertEqual({'500'},set(self.started));self.assertIn('999',self.ready)

    async def test_failed_task_registration_does_not_leak_reservation_or_emit_vod_cards(self):
        from unittest.mock import Mock
        self.app.create_task=Mock(side_effect=RuntimeError('synthetic task refusal'))
        batch,_,_=await self.launch(self.vods[:1]);await asyncio.wait_for(batch.task,2)
        self.assertFalse(scheduler(self.app).jobs);self.assertFalse(self.publications)
        self.assertFalse(self.calls);self.assertEqual(1,batch.errors)
        self.assertFalse(self.context(7).user_data.get('vod_jobs'))

    async def test_repeated_cancel_waits_for_the_same_cleanup(self):
        await self.request(7,'1');job=await self.entered('1')
        first=asyncio.create_task(self.cancel_all());second=asyncio.create_task(self.cancel_all())
        while not job.cancelled:await asyncio.sleep(0)
        self.assertFalse(first.done() or second.done());self.gates['1'].set()
        await asyncio.wait_for(asyncio.gather(first,second),2)
        self.assertFalse(scheduler(self.app).jobs)

    async def test_cleanup_failure_never_claims_completed_cancellation(self):
        await self.request(7,'1');job=await self.entered('1')
        self.publications[0].close.side_effect=OSError('synthetic close refusal')
        cancel=asyncio.create_task(self.cancel_all())
        while not job.cancelled:await asyncio.sleep(0)
        self.gates['1'].set();msg=await asyncio.wait_for(cancel,2)
        self.assertEqual('Не удалось подтвердить штатное завершение отмены',msg.reply_text.await_args.args[0])
        self.assertTrue(job.critical)
