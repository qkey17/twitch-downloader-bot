"""Synthetic admin batches and owner-wide cancellation on the real scheduler/runner."""
import asyncio
import os
from dataclasses import replace
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch
import unittest
from src import admin_batch as admin, config
from src.vod_scheduler import scheduler, stop_scheduler
from src.handlers import handlers_cmd as commands
from src.twitch_api import videos
from src.twitch_api.client import TwitchAPIError
from src.vod_cancellation import request_owner, finish_owner
import test_shared_vods as shared


class Authorization(unittest.IsolatedAsyncioTestCase):
    async def test_missing_invalid_and_other_ids_fail_before_any_side_effect(self):
        for value,user in ((None,7),('',7),('0',7),('-7',7),('7.0',7),(' 7',7),('٧',7),('9'*30,7),('8',7),('7',True)):
            with self.subTest(value=value,user=user),patch.dict(os.environ,{},clear=True):
                if value is not None:os.environ['TCD_ADMIN_USER_ID']=value
                message=NS(reply_text=AsyncMock())
                app=NS(bot_data={});ctx=NS(application=app,args=['channel'])
                with patch.object(videos,'fetch_user_by_login',AsyncMock()) as api:
                    await admin.allvods_command(NS(effective_user=NS(id=user),effective_message=message),ctx)
                message.reply_text.assert_awaited_once_with('Команда недоступна');api.assert_not_awaited()
                self.assertEqual({},app.bot_data)

    async def test_authorized_invalid_arguments_do_not_create_batch(self):
        for args in ([],['channel','extra'],['https://evil.invalid/x'],['https://twitch.tv/videos/123']):
            ctx=NS(application=NS(bot_data={}),args=args);msg=NS(reply_text=AsyncMock())
            with patch.dict(os.environ,{'TCD_ADMIN_USER_ID':'7'}):
                await admin.allvods_command(NS(effective_user=NS(id=7),effective_message=msg),ctx)
            self.assertIn('/allvods',msg.reply_text.await_args.args[0]);self.assertFalse(ctx.application.bot_data)


class Lifecycle(unittest.IsolatedAsyncioTestCase):
    for name in ('_setupAsyncioRunner','setUp','task','api','message','update','saved','publication','worker','context','request','entered','finish','cancel'):
        locals()[name]=getattr(shared.SharedCards,name)

    async def asyncTearDown(self):
        for gate in self.gates.values():gate.set()
        await stop_scheduler(self.app)
        await shared.SharedCards.asyncTearDown(self)

    async def launch(self, records=None, argument='channel'):
        if records is not None:self.vods[:]=records
        source=NS(reply_text=AsyncMock())
        control=NS(chat_id=10,message_id=9000,edit_text=AsyncMock())
        source.reply_text.return_value=control
        ctx=self.context(7);ctx.args=[argument]
        with patch.dict(os.environ,{'TCD_ADMIN_USER_ID':'7'}):
            await admin.allvods_command(NS(effective_user=NS(id=7),effective_message=source),ctx)
        batch=self.app.bot_data[admin.KEY]
        return batch,control,source

    async def cancel_all(self,owner=7):
        msg=NS(chat_id=99,reply_text=AsyncMock())
        await commands.cancel_command(NS(effective_user=NS(id=owner),effective_message=msg),self.context(owner))
        return msg

    async def test_one_at_a_time_oldest_first_including_regeneration_and_one_control_card(self):
        records=[replace(self.vods[i-1],created_at=f'2026-01-0{4-i}T00:00:00Z') for i in (1,2,3)]
        self.ready['2']=self.saved('2');old=self.ready['2']
        batch,control,source=await self.launch(records+[records[0]],'https://www.twitch.tv/channel')
        for v,regen in (('3',False),('2',True),('1',False)):
            job=await self.entered(v)
            self.assertEqual(v,batch.current_job.vod);self.assertEqual(1,len(scheduler(self.app).jobs))
            self.assertEqual(regen,job.compatibility[-1]=='regeneration')
            self.assertLessEqual(batch.success,batch.index-1)
            if v=='2':self.assertIs(old,self.ready['2'])
            await self.finish(job)
        await asyncio.wait_for(batch.task,2)
        self.assertEqual((3,3,0,0),(batch.total,batch.success,batch.skipped,batch.errors))
        source.reply_text.assert_awaited_once();self.assertFalse(self.calls) # all per-VOD Telegram UI suppressed
        self.assertNotIn(admin.KEY,self.app.bot_data)
        self.assertIn('Успешно: 3',control.edit_text.await_args.args[0])
        self.assertEqual(3,len(self.publications))

    async def test_repeated_batch_is_rejected_while_fetching_and_cancel_stops_it(self):
        entered=asyncio.Event()
        async def slow(*args):entered.set();await asyncio.Event().wait()
        with patch.object(videos,'fetch_user_by_login',AsyncMock(side_effect=slow)):
            batch,_,_=await self.launch()
            await asyncio.wait_for(entered.wait(),1)
            msg=NS(reply_text=AsyncMock())
            with patch.dict(os.environ,{'TCD_ADMIN_USER_ID':'7'}):
                await admin.allvods_command(NS(effective_user=NS(id=7),effective_message=msg),self.context(7))
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

    async def test_missing_vod_is_skipped_local_error_continues_critical_error_stops(self):
        from src.handlers import handlers_vod as vod
        # A missing freshly fetched VOD never reserves a scheduler job.
        metadata=AsyncMock(side_effect=[NS(length_seconds=None),self.meta])
        with patch.object(vod,'fetch_vod_meta',metadata):
            batch,_,_=await self.launch(self.vods[:2]);job=await self.entered('1');await self.finish(job)
            await asyncio.wait_for(batch.task,2)
        self.assertEqual((1,1),(batch.skipped,batch.success));self.assertNotIn('2',self.started)

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
        self.assertEqual(1,batch.index)

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
                batch,control,_=await self.launch()
                await asyncio.wait_for(batch.task,2)
                meta.assert_not_awaited();self.assertFalse(scheduler(self.app).jobs)
                self.assertTrue(any(label in control.edit_text.await_args.args[0] for label in ('Полный список','остановлена из-за ошибки')))
        self.assertFalse(self.publications)

    async def test_empty_and_missing_channel_do_not_create_workers(self):
        for user,records in ((None,[]),(self.user,[])):
            with patch.object(videos,'fetch_user_by_login',AsyncMock(return_value=user)),patch.object(videos,'fetch_archive_vods',AsyncMock(return_value=(records,None))):
                batch,_,_=await self.launch();await asyncio.wait_for(batch.task,2)
                self.assertEqual(0,batch.total);self.assertFalse(self.publications)

    async def test_fresh_metadata_error_is_local_but_authorization_failure_stops(self):
        from src.handlers import handlers_vod as vod
        with patch.object(vod,'fetch_vod_meta',AsyncMock(side_effect=[TwitchAPIError('missing',status=404),self.meta])):
            batch,_,_=await self.launch(self.vods[:2]);job=await self.entered('1');await self.finish(job);await asyncio.wait_for(batch.task,2)
        self.assertEqual((1,1),(batch.errors,batch.success))
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
        batch,_,_=await self.launch(records)
        while batch.current_job is None:await asyncio.sleep(0)
        self.assertEqual('waiting',batch.current_job.state)
        self.assertEqual(1,sum(j.owner==7 for j in scheduler(self.app).jobs.values()))
        operation=asyncio.create_task(self.cancel_all(7))
        await asyncio.wait_for(operation,2)
        self.assertNotIn('3',self.started);self.assertNotIn('4',self.started)
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
