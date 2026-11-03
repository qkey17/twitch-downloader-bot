import asyncio
from unittest.mock import patch
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock
import test_processing_control as fixture
from src.handlers import handlers_vod as vod
from src.handlers import handlers_input as inputs
from src.handlers.handlers_state import is_cancelled,CANCELLED_MESSAGE
from src.vod_scheduler import stop_scheduler


class QueueIntegration(unittest.IsolatedAsyncioTestCase):
    _setupAsyncioRunner=fixture.ProcessingControlTestCase._setupAsyncioRunner
    setUp=fixture.ProcessingControlTestCase.setUp
    request=fixture.ProcessingControlTestCase.request
    result=fixture.ProcessingControlTestCase.result

    async def asyncTearDown(self):
        await stop_scheduler(self.application)
        await fixture.ProcessingControlTestCase.asyncTearDown(self)

    async def test_one_user_three_cards_owners_cancel_and_late_callback(self):
        ctx,u1=self.request(1,'123456789');other,u2=self.request(1,'234567890')
        third,u3=self.request(1,'345678901')
        other.user_data=ctx.user_data;third.user_data=ctx.user_data
        u2.callback_query.message.message_id=21;u3.callback_query.message.message_id=22
        started={v:asyncio.Event() for v in ('123456789','234567890','345678901')}
        release={v:asyncio.Event() for v in started};workers={}
        async def work(**kw):
            v=kw['vod_id'];workers[v]=kw['context'];started[v].set();await release[v].wait()
            if is_cancelled(kw['context'],kw['operation_id']):raise RuntimeError(CANCELLED_MESSAGE)
            return self.result()
        with patch.object(vod,'download_and_send',side_effect=work):
            await vod._start_download(u1,ctx,'123456789')
            await vod._start_download(u2,other,'234567890')
            await vod._start_download(u3,third,'345678901')
            await started['123456789'].wait();await started['234567890'].wait()
            jobs=list(ctx.user_data['vod_jobs'].values());a,b,c=jobs
            self.assertEqual('waiting',c.state);self.assertFalse(started[c.vod].is_set())
            self.assertIn(c.vod,u3.message.edit_text.await_args.kwargs['text'])
            self.assertIsNot(workers[a.vod].user_data,workers[b.vod].user_data)
            self.assertEqual(a.id,workers[a.vod].user_data['download_ui']['operation'])
            u1.callback_query.data=vod.CB_PENDING_CANCEL+':'+a.id
            u1.effective_user.id=999
            await vod.pending_cancel_callback(u1,ctx);self.assertFalse(a.cancelled)
            u1.effective_user.id=1
            await vod.pending_cancel_callback(u1,ctx);self.assertTrue(a.cancelled)
            self.assertFalse(b.cancelled);self.assertFalse(c.cancelled)
            release[a.vod].set();await a.task;await started[c.vod].wait()
            self.assertEqual('cancelled',a.state);self.assertFalse(b.task.done())
            await vod.pending_cancel_callback(u1,ctx)
            self.assertFalse(b.cancelled);self.assertFalse(c.cancelled)
            release[b.vod].set();release[c.vod].set()
            await asyncio.gather(b.task,c.task);await asyncio.sleep(0)
            self.assertEqual('success',b.state);self.assertEqual('success',c.state)
            self.assertEqual({},ctx.user_data['vod_jobs'])

    async def test_waiting_cancel_does_not_create_worker_or_publication(self):
        requests=[self.request(i+1,str(123456789+i)) for i in range(3)]
        entered=asyncio.Event();release=asyncio.Event();calls=[]
        async def work(**kw):
            calls.append(kw['vod_id']);entered.set();await release.wait();return self.result()
        with patch.object(vod,'download_and_send',side_effect=work):
            for ctx,u in requests:await vod._start_download(u,ctx,u.message.text.rsplit('/',1)[-1])
            await entered.wait()
            ctx,u=requests[2];job=next(iter(ctx.user_data['vod_jobs'].values()))
            u.callback_query.data=vod.CB_PENDING_CANCEL+':'+job.id
            await vod.pending_cancel_callback(u,ctx)
            self.assertEqual('cancelled',job.state);self.assertEqual(2,len(calls))
            release.set();await asyncio.gather(*self.tasks);await asyncio.sleep(0)
            self.assertEqual(2,len(calls))

    async def test_cancel_while_initial_card_is_in_flight_never_starts_worker(self):
        ctx,u=self.request();entered=asyncio.Event();release=asyncio.Event()
        async def edit(**kw):entered.set();await release.wait()
        u.callback_query.message.edit_text.side_effect=edit
        with patch.object(vod,'download_and_send') as worker:
            starter=asyncio.create_task(vod._start_download(u,ctx,'123456789'))
            await entered.wait();job=next(iter(ctx.user_data['vod_jobs'].values()))
            u.callback_query.data=vod.CB_PENDING_CANCEL+':'+job.id
            cancelling=asyncio.create_task(vod.pending_cancel_callback(u,ctx));await asyncio.sleep(0)
            self.assertFalse(cancelling.done());self.assertTrue(job.cancelled)
            release.set();await starter;await cancelling
            worker.assert_not_called();self.assertEqual('cancelled',job.state)

    async def test_cancel_before_running_coroutine_enters_never_reserves_publication(self):
        ctx,u=self.request()
        with patch.object(vod,'download_and_send') as worker,patch.object(vod.results,'Publication') as publication:
            await vod._start_download(u,ctx,'123456789')
            job=next(iter(ctx.user_data['vod_jobs'].values()))
            u.callback_query.data=vod.CB_PENDING_CANCEL+':'+job.id
            await vod.pending_cancel_callback(u,ctx)
            await job.task;await asyncio.sleep(0)
            publication.assert_not_called();worker.assert_not_called()
            self.assertEqual('cancelled',job.state)

    async def test_resource_waiting_card_and_queued_direct_cancel_do_not_lock_recursively(self):
        from contextlib import contextmanager
        from src.handlers import handlers_chat as chat
        from src.vod_scheduler import scheduler
        @contextmanager
        def blocked():yield False
        ctx,u=self.request();u.callback_query.message.direct_vod=True
        manager=scheduler(self.application);manager.admission=blocked
        async def restore(context,record):
            async with record['ui_lock']:return True
        with patch.object(vod,'download_and_send') as worker,patch.object(chat,'restore_direct_cancelled',side_effect=restore):
            await vod._start_download(u,ctx,'123456789')
            job=next(iter(ctx.user_data['vod_jobs'].values()))
            self.assertEqual('waiting',job.state)
            self.assertIn('ожидает в очереди',ctx.bot.edit_message_text.await_args.kwargs['text'])
            u.callback_query.data=vod.CB_PENDING_CANCEL+':'+job.id
            await asyncio.wait_for(vod.pending_cancel_callback(u,ctx),1)
            worker.assert_not_called();self.assertEqual('cancelled',job.state)

    async def test_failed_queued_registration_notifies_own_card_and_keeps_first_slot(self):
        from src.vod_scheduler import scheduler
        first,u1=self.request(1,'123456789');second,u2=self.request(2,'234567890')
        editing=asyncio.Event();delivery=asyncio.Event();release=asyncio.Event()
        async def slow(**kw):editing.set();await delivery.wait()
        u1.message.edit_text.side_effect=slow
        create=self.application.create_task.side_effect;calls=0
        def register(coro):
            nonlocal calls
            calls+=1
            if calls==2:raise RuntimeError('synthetic registration failure')
            return create(coro)
        self.application.create_task.side_effect=register
        async def work(**kw):await release.wait();return self.result()
        with patch.object(vod,'download_and_send',side_effect=work):
            starting=asyncio.create_task(vod._start_download(u1,first,'123456789'))
            await asyncio.wait_for(editing.wait(),1)
            await vod._start_download(u2,second,'234567890')
            manager=scheduler(self.application);a,b=list(manager.jobs.values())
            with self.assertLogs('src.vod_scheduler',level='ERROR'):
                delivery.set();await starting
            await asyncio.gather(*manager.notifications)
            self.assertEqual(1,manager.active);self.assertEqual('error',b.state)
            self.assertIn('Не удалось начать',second.bot.edit_message_text.await_args.kwargs['text'])
            self.assertIsNone(second.bot.edit_message_text.await_args.kwargs['reply_markup'])
            release.set();await a.task;await asyncio.sleep(0)
            self.assertEqual('success',a.state)

    async def test_rapid_direct_links_are_not_dropped_during_metadata(self):
        ctx,u1=self.request(1,'123456789');_,u2=self.request(1,'234567890')
        ctx.user_data.pop('vod_pending',None)  # Direct input has no legacy format prompt.
        for n,u in enumerate((u1,u2),50):
            shown=SimpleNamespace(chat_id=1,message_id=n,edit_text=AsyncMock(),reply_text=AsyncMock())
            u.message.reply_text=AsyncMock(return_value=shown)
        metadata_entered=asyncio.Event();metadata_release=asyncio.Event();work_release=asyncio.Event();both=asyncio.Event();count=0
        async def meta(*a,**k):
            if not metadata_entered.is_set():metadata_entered.set();await metadata_release.wait()
            return SimpleNamespace(title='VOD',channel='test',length_seconds=60)
        async def work(**kw):
            nonlocal count
            count+=1
            if count==2:both.set()
            await work_release.wait();return self.result()
        with patch.object(vod,'fetch_vod_meta',side_effect=meta),patch.object(vod,'get_client_id',return_value='synthetic'),patch.object(vod.results,'get_ready_result',return_value=None),patch.object(vod,'download_and_send',side_effect=work):
            first=asyncio.create_task(inputs.text_input(u1,ctx));await asyncio.wait_for(metadata_entered.wait(),2)
            second=asyncio.create_task(inputs.text_input(u2,ctx));await asyncio.sleep(0)
            self.assertFalse(second.done())
            metadata_release.set();await asyncio.gather(first,second)
            await asyncio.wait_for(both.wait(),2)
            self.assertEqual(2,count)
            work_release.set();await asyncio.gather(*self.tasks)
