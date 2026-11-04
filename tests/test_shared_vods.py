"""Controlled joins/cancellation/commit and independent synthetic Telegram cards."""
import asyncio
import base64
import uuid
import unittest
from contextlib import contextmanager
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock, patch

from src.handlers import handlers_input as inputs, handlers_vod as vod, handlers_chat as chat
from src.ui import html_file_action as files
from src.ui.card_context import for_message
from src.vod_scheduler import Scheduler, scheduler
from src.vod_participants import PARTICIPANT_LIMIT
from src.maintenance import GATE_KEY, Coordinator
import test_single_message_media as media
import test_result_registry as registry


class SharedCards(unittest.IsolatedAsyncioTestCase):
    _setupAsyncioRunner=media.MediaCards._setupAsyncioRunner
    task=media.MediaCards.task
    api=media.MediaCards.api
    message=media.MediaCards.message
    update=media.MediaCards.update
    saved=media.MediaCards.saved
    asyncTearDown=media.MediaCards.asyncTearDown

    def setUp(self):
        self.publications=[];self.worker_calls=0;self.error=None
        media.MediaCards.setUp(self)
        self.contexts={7:self.ctx}

    def publication(self,v,fmt,**kw):
        key=uuid.uuid4().hex
        p=NS(id=key,ref='https://public.invalid/public_html/chat_'+key+'.html',reserve=Mock(),close=Mock(),fail=Mock())
        def complete():
            self.ready[v]=dict(id=key,vod_id=v,cache_id=1,meta=p.meta,stats=p.stats,html_ref=p.ref)
            return 1
        p.complete=Mock(side_effect=complete);self.publications.append(p);return p

    async def worker(self,**kw):
        self.worker_calls+=1
        value=await media.MediaCards.worker(self,**kw)
        if self.error:raise self.error
        return value

    def context(self,owner):
        if owner not in self.contexts:self.contexts[owner]=NS(bot=self.bot,user_data={},application=self.app,args=[])
        return self.contexts[owner]

    async def request(self,owner=7,v='1',regen=False):
        ctx=self.context(owner);message=self.message(1000+owner*10+len(self.calls),v)
        update=self.update(message,owner=owner)
        if regen:
            scope=for_message(ctx,owner,10,message.message_id)
            update.callback_query=NS(message=message)
            await vod._start_download(update,scope,v,True,modern=True,initial_meta=vars(self.meta))
        else:await inputs.text_input(update,ctx)
        ui=ctx.user_data.get('download_ui') or ctx.user_data.get('chat_choice')
        return ctx,self.messages[ui['message']] if ui else message

    async def entered(self,v='1'):
        await asyncio.wait_for(self.started.setdefault(v,asyncio.Event()).wait(),2)
        return next(j for j in scheduler(self.app).jobs.values() if j.vod==v)

    async def finish(self,job):
        self.gates.setdefault(job.vod,asyncio.Event()).set()
        await asyncio.wait_for(job.task,2)
        await asyncio.sleep(0)  # Run the registered task-done callback.

    async def cancel(self,owner,message,token=None):
        ctx=self.context(owner);ui=ctx.user_data['download_ui']
        data='ui:pending_cancel:'+(token or ui.get('cancel_token',ui['operation']))
        await vod.pending_cancel_callback(self.update(message,data,owner=owner),ctx)

    async def test_simultaneous_first_requests_share_worker_publication_and_generation(self):
        (a,ma),(b,mb)=await asyncio.gather(self.request(),self.request(8))
        job=await self.entered()
        self.assertEqual(1,self.worker_calls);self.assertEqual(1,scheduler(self.app).active)
        self.assertEqual(2,len(job.participants.members));self.assertEqual(1,len(self.publications))
        self.assertNotEqual(ma.message_id,mb.message_id)
        self.assertNotEqual(a.user_data['download_ui']['cancel_token'],b.user_data['download_ui']['cancel_token'])
        await self.finish(job)
        self.publications[0].complete.assert_called_once();self.publications[0].close.assert_called_once()
        callbacks=[]
        for owner,m in ((7,ma),(8,mb)):
            data=next(b.callback_data for row in m.reply_markup.inline_keyboard for b in row if (b.callback_data or '').startswith(files.PREFIX))
            callbacks.append(data)
            raw=base64.urlsafe_b64decode(data[len(files.PREFIX)+1:]+'=')
            self.assertEqual(self.publications[0].id,raw[:16].hex())
            self.assertIsNotNone(files._selection(self.update(m,data,owner=owner),self.context(owner)))
            self.assertIsNone(files._selection(self.update(m,data,owner=99),self.context(99)))
        self.assertNotEqual(*callbacks)
        self.assertFalse(scheduler(self.app).jobs)

    async def test_late_join_sees_current_progress_and_one_cancel_does_not_stop_worker(self):
        a,ma=await self.request();job=await self.entered()
        job.participants.progress('checkpoint 12345')
        b,mb=await self.request(8)
        self.assertEqual('checkpoint 12345',mb.caption)
        token=a.user_data['download_ui']['cancel_token']
        await self.cancel(7,ma)
        self.assertFalse(job.cancelled);self.assertEqual(1,len(job.participants.members))
        cancelled=(ma.caption,ma.reply_markup)
        await self.finish(job)
        self.assertEqual(cancelled,(ma.caption,ma.reply_markup))
        self.assertIn(self.publications[0].ref,str(mb.reply_markup))
        # Old cancellation cannot detach a later participation on the same card/job.
        self.assertNotIn('download_ui',a.user_data)

    async def test_worker_completion_does_not_invalidate_detached_members_cancel_restore(self):
        a,ma=await self.request();job=await self.entered();await self.request(8)
        entered=asyncio.Event();release=asyncio.Event();original=chat.restore_cancelled_card
        async def restore(context,record):
            entered.set();await release.wait()
            self.assertIs(record,context.user_data.get('download_ui'))
            return await original(context,record)
        with patch.object(chat,'restore_cancelled_card',restore):
            cancelling=asyncio.create_task(self.cancel(7,ma))
            try:
                await asyncio.wait_for(entered.wait(),2)
                await self.finish(job)
                release.set();await cancelling
                self.assertIsNone(a.user_data.get('download_ui'))
                self.assertNotIn(self.publications[0].ref,str(ma.reply_markup))
                self.assertIn('Обработать чат',str(ma.reply_markup))
            finally:
                release.set();await asyncio.gather(cancelling,return_exceptions=True)

    async def test_join_waiting_job_does_not_consume_queue_and_last_waiter_never_runs(self):
        self.app.bot_data[GATE_KEY]='hold'
        self.app.bot_data.pop(GATE_KEY)
        manager=scheduler(self.app)
        @contextmanager
        def blocked():yield False
        manager.admission=blocked
        a,ma=await self.request();job=next(iter(manager.jobs.values()))
        await self.request(8);self.assertEqual(1,len(manager.queue));self.assertIsNone(job.task)
        await self.cancel(7,ma);self.assertEqual(1,len(manager.queue))
        b=self.context(8);mb=self.messages[b.user_data['download_ui']['message']]
        await self.cancel(8,mb)
        self.assertEqual(0,len(manager.queue));self.assertEqual(0,self.worker_calls)
        self.assertFalse(manager.jobs)

    async def test_last_active_cancellation_holds_slot_until_cleanup_and_allows_retry(self):
        a,ma=await self.request();job=await self.entered()
        await self.cancel(7,ma)
        self.assertTrue(job.cancelled);self.assertEqual(1,scheduler(self.app).active)
        other,error=scheduler(self.app).reserve(8,'1',compatibility=job.compatibility)
        self.assertIsNone(other);self.assertIsNotNone(error)
        await self.finish(job)
        self.assertEqual(0,scheduler(self.app).active);self.publications[0].complete.assert_not_called()
        self.started.clear();self.gates.clear()
        await self.request(8);new=await self.entered();self.assertNotEqual(job.id,new.id)
        await self.finish(new)

    async def test_join_during_commit_is_included_and_cancellation_is_declined(self):
        a,ma=await self.request();job=await self.entered()
        entered=asyncio.Event();release=asyncio.Event();original=vod.run_blocking
        async def io(fn,*args,**kw):
            if fn is self.publications[0].complete:
                entered.set();await release.wait()
            return await original(fn,*args,**kw)
        with patch.object(vod,'run_blocking',io),patch.object(vod.config,'HISTORY_ENABLED',True),patch.object(vod.db,'add_user_history') as history:
            self.gates['1'].set();await asyncio.wait_for(entered.wait(),2)
            b,mb=await self.request(8)
            await self.cancel(8,mb)
            self.assertFalse(job.cancelled);self.assertTrue(job.participants.members[8].active)
            release.set();await self.finish(job)
            self.assertEqual({7,8},{c.args[0] for c in history.call_args_list})
            self.assertIn(self.publications[0].ref,str(mb.reply_markup))

    async def test_publication_finishing_during_join_metadata_returns_ready_without_second_worker(self):
        await self.request();job=await self.entered()
        entered=asyncio.Event();release=asyncio.Event()
        async def metadata(*args,**kw):
            entered.set();await release.wait();return self.meta
        with patch.object(vod,'fetch_vod_meta',metadata):
            joining=asyncio.create_task(self.request(8))
            try:
                await asyncio.wait_for(entered.wait(),2)
                await self.finish(job);release.set();b,mb=await joining
                self.assertEqual(self.publications[0].id,b.user_data['chat_choice']['generation'])
                self.assertEqual(1,self.worker_calls)
                self.assertFalse(scheduler(self.app).jobs)
                self.assertEqual(0,scheduler(self.app).join_preparing)
            finally:
                release.set();await asyncio.gather(joining,return_exceptions=True)

    async def test_slow_progress_on_one_card_never_blocks_other_card_or_publication(self):
        a,ma=await self.request();job=await self.entered();b,mb=await self.request(8)
        slow=asyncio.Event();release=asyncio.Event();other_final=asyncio.Event()
        original=self.bot.edit_message_caption.side_effect
        async def caption(**kw):
            if kw['message_id']==ma.message_id and kw.get('caption')=='checkpoint':
                slow.set();await release.wait()
            if kw['message_id']==mb.message_id and job.participants.members[8].context.user_data['download_ui']['phase']=='ready':other_final.set()
            return original(**kw)
        self.bot.edit_message_caption.side_effect=caption
        job.participants.progress('checkpoint');await asyncio.wait_for(slow.wait(),2)
        self.gates['1'].set();await asyncio.wait_for(other_final.wait(),2)
        self.publications[0].complete.assert_called_once()
        release.set();await self.finish(job)

    async def test_worker_error_notifies_every_member_and_next_request_can_start(self):
        a,ma=await self.request();job=await self.entered();b,mb=await self.request(8)
        # A controlled failure in the already-running worker.
        self.error=ValueError('synthetic worker failure');self.gates['1'].set();await self.finish(job)
        self.assertIn('Не удалось',ma.caption);self.assertIn('Не удалось',mb.caption)
        self.error=None
        self.publications[0].complete.assert_not_called()
        self.assertFalse(scheduler(self.app).jobs)
        self.started.clear();self.gates.clear()
        await self.request(9);new=await self.entered();await self.finish(new)

    async def test_ready_result_bypasses_full_queue_during_explicit_regeneration_and_pins_offer(self):
        old=self.saved();self.ready['1']=old
        a,ma=await self.request(regen=True);job=await self.entered()
        manager=scheduler(self.app)
        for i in range(20):self.assertIsNotNone(manager.reserve(30+i,str(20+i))[0])
        b,mb=await self.request(8)
        choice=b.user_data['chat_choice'];self.assertEqual(old['id'],choice['generation'])
        self.assertEqual(1,self.worker_calls)
        await self.finish(job)
        self.assertNotEqual(old['id'],self.ready['1']['id'])
        with patch.object(vod,'ready_generation',return_value=old) as resolve:
            await chat.chat_callback(self.update(mb,'ui:chat:open:'+choice['token'],owner=8),b)
            resolve.assert_called_once_with('1',old['id'])
        self.assertIn(old['html_ref'],str(mb.reply_markup))

    async def test_history_is_individual_and_cancelled_member_is_excluded(self):
        with patch.object(vod.config,'HISTORY_ENABLED',True),patch.object(vod.db,'add_user_history') as history:
            a,ma=await self.request();job=await self.entered();b,mb=await self.request(8);await self.request(9)
            await self.cancel(8,mb);await self.finish(job)
            self.assertEqual({7,9},{c.args[0] for c in history.call_args_list})

    async def test_shutdown_drains_worker_and_all_consumers_then_refuses_joins(self):
        await self.request();job=await self.entered();await self.request(8)
        consumers=[p.task for p in job.participants.members.values()]
        await scheduler(self.app).close()
        self.assertFalse(scheduler(self.app).jobs)
        self.assertTrue(all(t.done() for t in consumers));self.publications[0].complete.assert_not_called()
        self.assertIsNone(scheduler(self.app).reserve(9,'1',compatibility=job.compatibility)[0])

    async def test_failing_one_terminal_card_does_not_fail_publication_or_other_history(self):
        a,ma=await self.request();job=await self.entered();b,mb=await self.request(8)
        original=self.bot.edit_message_caption.side_effect
        async def caption(**kw):
            if kw['message_id']==ma.message_id:raise RuntimeError('synthetic blocked recipient')
            return original(**kw)
        self.bot.edit_message_caption.side_effect=caption
        with patch.object(vod.config,'HISTORY_ENABLED',True),patch.object(vod.db,'add_user_history') as history:
            await self.finish(job)
            self.assertEqual({7,8},{c.args[0] for c in history.call_args_list})
        self.assertIn(self.publications[0].ref,str(mb.reply_markup))
        self.publications[0].complete.assert_called_once();self.publications[0].fail.assert_not_called()

    async def test_cancel_and_rejoin_has_new_token_and_old_callback_cannot_detach_it(self):
        a,ma=await self.request();job=await self.entered();await self.request(8)
        old=a.user_data['download_ui']['cancel_token'];await self.cancel(7,ma)
        a,new=await self.request()
        token=a.user_data['download_ui']['cancel_token'];self.assertNotEqual(old,token)
        await self.cancel(7,new,old)
        self.assertTrue(job.participants.members[7].active);await self.finish(job)

    async def test_slow_first_initial_card_does_not_prevent_other_member_starting_worker(self):
        blocked=asyncio.Event();release=asyncio.Event();original=self.bot.edit_message_media.side_effect
        async def edit(**kw):
            if not blocked.is_set():
                blocked.set();await release.wait()
            return original(**kw)
        self.bot.edit_message_media.side_effect=edit
        first=asyncio.create_task(self.request())
        try:
            await asyncio.wait_for(blocked.wait(),2)
            await self.request(8);job=await self.entered()
            self.assertEqual(1,self.worker_calls)
            release.set();await first;await self.finish(job)
        finally:
            release.set();await asyncio.gather(first,return_exceptions=True)

    async def test_first_initial_delivery_failure_keeps_joined_worker_and_other_card(self):
        blocked=asyncio.Event();release=asyncio.Event();original=self.bot.edit_message_media.side_effect
        async def edit(**kw):
            if not blocked.is_set():
                blocked.set();await release.wait();raise RuntimeError('synthetic first-card failure')
            return original(**kw)
        self.bot.edit_message_media.side_effect=edit
        first=asyncio.create_task(self.request())
        try:
            await asyncio.wait_for(blocked.wait(),2)
            b,mb=await self.request(8);job=await self.entered()
            release.set()
            with self.assertRaises(RuntimeError):await first
            self.assertFalse(job.cancelled)
            await self.finish(job)
            self.assertIn(self.publications[0].ref,str(mb.reply_markup))
        finally:
            release.set();await asyncio.gather(first,return_exceptions=True)

    async def test_membership_history_uses_real_existing_sqlite_history_table(self):
        import tempfile
        from pathlib import Path
        from src.database import db_core, database as db
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as temp, \
             patch.object(db_core,'DB_PATH',str(Path(temp)/'app.sqlite3')), \
             patch.object(vod.config,'HISTORY_ENABLED',True):
            db.init_db()
            self.assertEqual(1,db.upsert_cache('1','html_online','https://www.twitch.tv/videos/1',
                                             vars(self.meta),{},[]))
            await self.request();job=await self.entered();await self.request(8);c,mc=await self.request(9)
            await self.cancel(9,mc);await self.finish(job)
            self.assertEqual((1,1,0),tuple(db.get_history_count(o) for o in (7,8,9)))
            self.assertEqual('1',db.get_history_for_user(8)[0]['vod_id'])

    async def test_information_card_can_join_existing_worker_even_when_work_queue_is_full(self):
        from src.handlers import handlers_channel_vods as lists
        from src.vod_list import callback
        await self.request();job=await self.entered();manager=scheduler(self.app)
        for n in range(20):self.assertIsNotNone(manager.reserve(100+n,str(100+n))[0])
        ctx=self.context(8)
        view=await inputs.text_input(self.update(self.message(2000,'channel'),owner=8),ctx)
        m=self.messages[view.message_id]
        await lists.channel_vods_callback(self.update(m,callback(view,'info',0),owner=8),ctx)
        from src.handlers import handlers_vod_info as info
        session=lists.get_channel_browser(self.app)._sessions[view.session_id]
        card=session.info_card
        await info.info_callback(self.update(m,f'ui:vi:{session.token}:{card.nonce}:process',owner=8),ctx)
        self.assertIn(8,job.participants.members)
        self.assertEqual(20,len(manager.queue));self.assertEqual(1,self.worker_calls)
        await self.finish(job)

    async def test_shutdown_before_worker_enters_drains_cancelled_consumers(self):
        # No timing sleeps: suppress pump until the first card is installed.
        manager=scheduler(self.app)
        with patch.object(manager,'pump'):
            await self.request()
        job=next(iter(manager.jobs.values()));consumer=job.participants.members[7].task
        await manager.close()
        self.assertTrue(consumer.done());self.assertFalse(manager.jobs)
        self.assertFalse(manager.notifications);self.assertEqual(0,self.worker_calls)


class AtomicScheduler(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.app=NS(bot_data={},running=True,create_task=asyncio.create_task)
        @contextmanager
        def allowed():yield True
        self.manager=Scheduler(self.app,allowed);self.app.bot_data['tdb_vod_scheduler']=self.manager
        self.key=('1','html_online','local')
    async def asyncTearDown(self):await self.manager.close()

    async def test_join_and_last_cancel_linearize_in_either_order(self):
        for join_first in (False,True):
            job,_=self.manager.reserve(1,'1',compatibility=self.key)
            if join_first:
                other,_=self.manager.reserve(2,'1',compatibility=self.key);self.assertIs(job,other)
                self.manager.cancel_participant(job,1);self.assertFalse(job.cancelled)
                self.manager.cancel_participant(job,2)
            else:
                self.manager.cancel_participant(job,1)
                other,_=self.manager.reserve(2,'1',compatibility=self.key);self.assertIsNot(job,other)
                self.manager.cancel_participant(other,2)
            self.assertFalse(self.manager.jobs)

    async def test_limit_compatibility_regeneration_and_admission(self):
        job,_=self.manager.reserve(1,'1',compatibility=self.key)
        for owner in range(2,PARTICIPANT_LIMIT+1):self.assertIs(job,self.manager.reserve(owner,'1',compatibility=self.key)[0])
        self.assertIsNone(self.manager.reserve(21,'1',compatibility=self.key)[0])
        for key in (None,('1','html_online','public'),('1','other','local')):
            self.assertIsNone(self.manager.reserve(99,'1',compatibility=key)[0])
        self.app.bot_data[GATE_KEY]='maintenance'
        self.assertIsNone(self.manager.reserve(22,'1',compatibility=self.key)[0])
        self.app.bot_data.pop(GATE_KEY);await self.manager.close()
        self.assertIsNone(self.manager.reserve(22,'1',compatibility=self.key)[0])
        self.assertFalse(self.manager.jobs)

    async def test_two_workers_twenty_waiters_fifo_and_joins_use_neither_capacity(self):
        starts=[];events={};entered={}
        for n in range(1,23):
            job,error=self.manager.reserve(n,str(n),compatibility=(str(n),'html_online','local'))
            self.assertIsNone(error);events[n]=asyncio.Event();entered[n]=asyncio.Event()
            job.participants.members[n].ready.set()
            async def work(n=n):
                starts.append(n);entered[n].set();await events[n].wait()
            self.manager.arm(job,work)
        self.assertEqual(2,self.manager.active);self.assertEqual(20,len(self.manager.queue))
        for n in (1,3):
            job=next(j for j in self.manager.jobs.values() if j.vod==str(n))
            self.assertIs(job,self.manager.reserve(99,str(n),compatibility=job.compatibility)[0])
            ticket=self.manager.prepare_input(str(n));self.assertIsNotNone(ticket)
            self.assertEqual(0,self.manager.preparing);self.manager.release_input(ticket)
        self.assertIsNone(self.manager.reserve(100,'100')[0])
        gate=Coordinator(self.app,AsyncMock(),AsyncMock())
        self.assertEqual('busy',(await gate.command('prepare','a'*32,self))['status'])
        await entered[1].wait();events[1].set()
        await asyncio.wait_for(entered[3].wait(),2)
        self.assertEqual([1,2,3],starts)


class PinnedGeneration(unittest.TestCase):
    setUp=registry.ResultRegistryTestCase.setUp
    plan=registry.ResultRegistryTestCase.plan
    publish=registry.ResultRegistryTestCase.publish
    ready=registry.ResultRegistryTestCase.ready

    def test_old_generation_remains_resolvable_and_individual_history_needs_no_schema_change(self):
        from src.database import database as db
        old=self.ready();cache=vod.results.get_ready_result('123456789')['cache_id']
        db.add_user_history(7,cache);db.add_user_history(8,cache)
        new=self.ready('new')
        self.assertNotEqual(old.id,new.id)
        self.assertEqual(old.id,vod.ready_generation('123456789',old.id)['id'])
        self.assertEqual(new.id,vod.ready_generation('123456789')['id'])
        self.assertEqual(1,db.get_history_count(7));self.assertEqual(1,db.get_history_count(8))
