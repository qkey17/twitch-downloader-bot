"""Controlled regeneration joins via registered callbacks and existing lifecycle fixtures."""
import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch
from telegram import Update
from telegram.error import Forbidden
from telegram.ext import Application, CallbackQueryHandler
from src import main
from src.handlers import handlers_chat as chat, handlers_vod as vod
from src.ui.card_context import for_message
from src.ui import html_file_action as files
from src.ui.ui_labels import VOD_BUSY_NOTICE
from src.vod_scheduler import scheduler
from src.vod_participants import PARTICIPANT_LIMIT
from src.database import database as db, db_core
import test_shared_vods as fixture


class SharedRegeneration(unittest.IsolatedAsyncioTestCase):
    for name in ('_setupAsyncioRunner','setUp','asyncTearDown','task','api','message','update','saved',
                 'publication','worker','context','entered','finish','cancel'):
        locals()[name]=getattr(fixture.SharedCards,name)

    async def request(self,owner=7,v='1',regen=True):
        return await fixture.SharedCards.request(self,owner,v,regen)

    async def dispatch(self,query,context):
        if not hasattr(self,'callbacks'):
            app=Application.builder().token('123456:synthetic').build()
            with patch.object(main.Application,'builder') as builder, \
                 patch.object(main,'get_telegram_bot_token',return_value='test'), \
                 patch.object(main,'validate_credentials'),patch.object(main,'setup_logging'), \
                 patch.object(Application,'run_polling'):
                builder.return_value.application_class.return_value.token.return_value.post_init.return_value.post_stop.return_value.post_shutdown.return_value.build.return_value=app
                main.main()
            self.callbacks=[h for group in app.handlers.values() for h in group if isinstance(h,CallbackQueryHandler)]
        query.callback_query.game_short_name=None
        query.callback_query.from_user=query.effective_user
        routed=Update(1,callback_query=query.callback_query)
        handler=next(h for h in self.callbacks if h.check_update(routed))
        self.assertIs(chat.chat_callback,handler.callback)
        await handler.callback(routed,context)

    async def offer(self,owner=7,v='1'):
        ctx=self.context(owner);m=self.message(5000+len(self.calls),v)
        scoped=for_message(ctx,owner,10,m.message_id)
        await chat.offer_chat(m,scoped,owner,self.saved(v),lambda:True,None)
        choice=scoped.user_data['chat_choice']
        m=self.messages[choice['message']]
        q=self.update(m,'ui:chat:regen:'+choice['token'],owner=owner)
        return ctx,m,q

    async def join(self,owner=7):
        ctx,m,q=await self.offer(owner)
        await self.dispatch(q,ctx)
        q.callback_query.answer.assert_awaited_once_with()
        return ctx,m,q

    def part(self,job,message):
        return next(p for p in job.participants.members.values() if p.context is not None
                    and p.context.user_data['download_ui']['message']==message.message_id)

    async def cancel_card(self,owner,message,token):
        q=self.update(message,'ui:pending_cancel:'+token,owner=owner)
        await vod.pending_cancel_callback(q,self.context(owner))
        return q

    def assert_result(self,owner,message,publication):
        self.assertIn(publication.ref,str(message.reply_markup))
        data=next(b.callback_data for row in message.reply_markup.inline_keyboard for b in row
                  if (b.callback_data or '').startswith('ui:htmlfile:'))
        self.assertEqual(('1',publication.id),files._selection(self.update(message,data,owner=owner),self.context(owner)))

    async def test_registered_regeneration_for_two_owners_and_two_cards_one_owner(self):
        for owner in (8,7):
            with self.subTest(owner=owner):
                _,a,_=await self.join();job=await self.entered()
                job.participants.progress('current checkpoint')
                before=len(self.calls)
                _,b,_=await self.join(owner)
                self.assertEqual('current checkpoint',b.caption)
                self.assertFalse(any(n.startswith('send_') for n,_ in self.calls[before:]))
                self.assertEqual(2,len(job.participants.members))
                self.assertEqual(1,scheduler(self.app).active)
                p,q=self.part(job,a),self.part(job,b)
                self.assertNotEqual(p.id,q.id)
                self.assertIsNot(p.context,q.context)
                self.assertEqual(1,len(self.publications))
                await self.finish(job)
                for o,m in ((7,a),(owner,b)):self.assert_result(o,m,self.publications[0])
                self.publications[0].complete.assert_called_once()
                self.assertFalse(self.context(7).user_data.get('vod_jobs'))
                self.started.clear();self.gates.clear();self.publications.clear()

    async def test_same_owner_cancel_is_message_bound_and_retains_other_participation(self):
        _,a,_=await self.join();job=await self.entered();_,b,_=await self.join()
        p,q=self.part(job,a),self.part(job,b)
        before=list(self.calls)
        await self.cancel_card(7,b,p.id)  # Correct owner, wrong source message.
        await self.cancel_card(8,a,p.id)  # Wrong owner.
        self.assertEqual(before,self.calls);self.assertTrue(p.active and q.active)
        await self.cancel_card(7,a,p.id)
        cancelled=(a.caption,a.reply_markup)
        self.assertFalse(job.cancelled);self.assertTrue(q.active)
        self.assertIn(job.id,self.context(7).user_data['vod_jobs'])
        await self.finish(job)
        self.assertEqual(cancelled,(a.caption,a.reply_markup))
        self.assert_result(7,b,self.publications[0])

    async def test_same_owner_last_cancel_waits_for_worker_cleanup(self):
        _,a,_=await self.join();job=await self.entered();_,b,_=await self.join()
        p,q=self.part(job,a),self.part(job,b)
        await self.cancel_card(7,a,p.id)
        await self.cancel_card(7,b,q.id)
        self.assertTrue(job.cancelled);self.assertEqual(1,scheduler(self.app).active)
        self.assertIsNone(scheduler(self.app).reserve(8,'1',compatibility=job.compatibility)[0])
        self.publications[0].close.assert_not_called()
        await self.finish(job)
        self.assertEqual(0,scheduler(self.app).active)
        self.publications[0].close.assert_called_once();self.publications[0].complete.assert_not_called()
        self.assertFalse(self.context(7).user_data.get('vod_jobs'))

    async def test_join_queued_regeneration_at_full_capacity_preserves_fifo(self):
        manager=scheduler(self.app)
        await self.request(8,'2');await self.entered('2')
        await self.request(9,'3');await self.entered('3')
        _,a,_=await self.join();job=next(j for j in manager.jobs.values() if j.vod=='1')
        for n in range(19):manager.reserve(100+n,str(100+n))
        queue=tuple(manager.queue)
        _,b,_=await self.join()
        self.assertEqual(queue,tuple(manager.queue));self.assertEqual(20,len(manager.queue))
        self.assertEqual(2,manager.active);self.assertEqual(2,len(job.participants.members))
        self.assertIsNone(job.task)
        for m in (a,b):await self.cancel_card(7,m,self.part(job,m).id)
        self.assertNotIn(job,manager.queue)

    async def test_history_deduplicates_owner_but_delivers_all_cards(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as temp, \
             patch.object(db_core,'DB_PATH',str(Path(temp)/'history.sqlite3')), \
             patch.object(vod.config,'HISTORY_ENABLED',True):
            db.init_db();db.upsert_cache('1','html_online','https://www.twitch.tv/videos/1',vars(self.meta),{},[])
            _,a,_=await self.join();job=await self.entered();_,b,_=await self.join();_,c,_=await self.join(8)
            with patch.object(db,'add_user_history',wraps=db.add_user_history) as history:
                await self.finish(job)
                self.assertEqual([7,8],[x.args[0] for x in history.call_args_list])
            self.assertEqual((1,1),(db.get_history_count(7),db.get_history_count(8)))
            for o,m in ((7,a),(7,b),(8,c)):self.assert_result(o,m,self.publications[0])

    async def test_ready_a_during_regeneration_b_remains_pinned(self):
        old=self.saved();self.ready['1']=old
        _,a,_=await self.join();job=await self.entered();_,b,_=await self.join(8)
        ctx,old_card=await fixture.SharedCards.request(self,9,'1',False)
        choice=for_message(ctx,9,10,old_card.message_id).user_data['chat_choice']
        self.assertEqual(old['id'],choice['generation'])
        await self.finish(job)
        for o,m in ((7,a),(8,b)):self.assert_result(o,m,self.publications[0])
        with patch.object(vod,'ready_generation',return_value=old) as lookup:
            await self.dispatch(self.update(old_card,'ui:chat:open:'+choice['token'],owner=9),ctx)
        lookup.assert_called_once_with('1',old['id'])
        self.assertIn(old['html_ref'],str(old_card.reply_markup))
        self.assertNotIn(self.publications[0].ref,str(old_card.reply_markup))

    async def test_limit_counts_cards_and_incompatible_conflict_is_popup_only(self):
        _,a,_=await self.join();job=await self.entered()
        for n in range(PARTICIPANT_LIMIT-1):await self.join()
        self.assertEqual(20,len(job.participants.members))
        ctx,m,q=await self.offer();before=list(self.calls)
        await self.dispatch(q,ctx)
        q.callback_query.answer.assert_awaited_once_with('Чат уже находится в обработке',show_alert=False)
        self.assertEqual(before,self.calls);self.assertEqual(1,self.worker_calls)
        self.assertEqual(1,len(self.publications));await self.finish(job)
        self.started.clear();self.gates.clear()
        await fixture.SharedCards.request(self,7,'1',False)  # Ready lookup, no worker.
        # A first-request worker is incompatible with explicit regeneration.
        self.ready.clear()
        await fixture.SharedCards.request(self,8,'1',False);job=await self.entered()
        ctx,m,q=await self.offer();before=list(self.calls)
        await self.dispatch(q,ctx)
        q.callback_query.answer.assert_awaited_once_with(VOD_BUSY_NOTICE,show_alert=False)
        self.assertEqual(before,self.calls);await self.finish(job)

    async def test_slow_or_failed_ack_does_not_drop_other_participants(self):
        ctx,a,q=await self.offer()
        entered=asyncio.Event();release=asyncio.Event()
        async def acknowledge():
            entered.set();await release.wait();raise Forbidden('synthetic')
        q.callback_query.answer.side_effect=acknowledge
        first=asyncio.create_task(self.dispatch(q,ctx))
        try:
            await asyncio.wait_for(entered.wait(),2)
            _,b,_=await self.join(8);job=await self.entered()
            release.set();await first
            self.assertFalse(job.cancelled);self.assertEqual(1,len(job.participants.members))
            await self.finish(job)
            self.assert_result(8,b,self.publications[0]);self.assertEqual(1,self.worker_calls)
        finally:
            release.set();await asyncio.gather(first,return_exceptions=True)

    async def test_commit_join_same_owner_is_included_once_and_cannot_cancel(self):
        _,a,_=await self.join();job=await self.entered()
        entered=asyncio.Event();release=asyncio.Event();original=vod.run_blocking
        async def io(fn,*args,**kw):
            if fn is self.publications[0].complete:
                entered.set();await release.wait()
            return await original(fn,*args,**kw)
        with patch.object(vod,'run_blocking',io),patch.object(vod.config,'HISTORY_ENABLED',True),patch.object(db,'add_user_history') as history:
            self.gates['1'].set();await asyncio.wait_for(entered.wait(),2)
            try:
                _,b,_=await self.join();p=self.part(job,b)
                await self.cancel_card(7,b,p.id)
                self.assertTrue(p.active);self.assertFalse(job.cancelled)
            finally:release.set()
            await self.finish(job)
            history.assert_called_once_with(7,1)
        for m in (a,b):self.assert_result(7,m,self.publications[0])

    async def test_closed_group_refuses_join_until_publication_cleanup(self):
        _,a,_=await self.join();job=await self.entered()
        entered=asyncio.Event();release=asyncio.Event()
        original=self.bot.edit_message_caption.side_effect
        async def caption(**kw):
            if kw['message_id']==a.message_id and self.part(job,a).context.user_data['download_ui']['phase']=='ready':
                entered.set();await release.wait()
            return original(**kw)
        self.bot.edit_message_caption.side_effect=caption
        self.gates['1'].set();await asyncio.wait_for(entered.wait(),2)
        try:
            self.assertFalse(job.participants.accepting)
            ctx,m,q=await self.offer(8);before=list(self.calls)
            await self.dispatch(q,ctx)
            q.callback_query.answer.assert_awaited_once_with(VOD_BUSY_NOTICE,show_alert=False)
            self.assertEqual(before,self.calls)
            self.assertEqual(1,self.worker_calls);self.assertEqual(1,len(self.publications))
        finally:release.set()
        await self.finish(job)

    async def test_shutdown_or_stale_card_during_ack_does_not_write_late_progress(self):
        for shutdown in (False,True):
            with self.subTest(shutdown=shutdown):
                ctx,m,q=await self.offer()
                scoped=for_message(ctx,7,10,m.message_id)
                entered=asyncio.Event();release=asyncio.Event()
                async def acknowledge():entered.set();await release.wait()
                q.callback_query.answer.side_effect=acknowledge
                task=asyncio.create_task(self.dispatch(q,ctx))
                try:
                    await asyncio.wait_for(entered.wait(),2)
                    before=list(self.calls)
                    if shutdown:await scheduler(self.app).close()
                    else:scoped.user_data['chat_choice']['current']=lambda:False
                    release.set();await task
                    self.assertEqual(before,self.calls)
                    self.assertEqual(0,self.worker_calls)
                    self.assertFalse(scheduler(self.app).jobs)
                finally:
                    release.set();await asyncio.gather(task,return_exceptions=True)

    async def test_same_owner_join_and_last_cancel_linearize_in_either_order(self):
        manager=scheduler(self.app)
        for join_first in (False,True):
            key=vod.compatibility_key('1',True)
            job,_=manager.reserve(7,'1',compatibility=key,participation_id='a'*32)
            if join_first:
                other,_=manager.reserve(7,'1',compatibility=key,participation_id='b'*32)
                self.assertIs(job,other)
                manager.cancel_participant(job,'a'*32)
                self.assertFalse(job.cancelled)
                manager.cancel_participant(job,'b'*32)
            else:
                manager.cancel_participant(job,'a'*32)
                other,_=manager.reserve(7,'1',compatibility=key,participation_id='b'*32)
                self.assertIsNot(job,other)
                manager.cancel_participant(other,'b'*32)
            self.assertFalse(manager.jobs)

    async def test_history_failure_is_shared_only_by_cards_of_that_owner(self):
        _,a,_=await self.join();job=await self.entered();_,b,_=await self.join();_,c,_=await self.join(8)
        def history(owner,cache):
            if owner==7:raise RuntimeError('synthetic history failure')
        with patch.object(vod.config,'HISTORY_ENABLED',True),patch.object(db,'add_user_history',side_effect=history) as save:
            await self.finish(job)
            self.assertEqual([7,8],[x.args[0] for x in save.call_args_list])
        for m in (a,b):self.assertNotIn(self.publications[0].ref,str(m.reply_markup))
        self.assert_result(8,c,self.publications[0])
        self.publications[0].complete.assert_called_once()

    # Exercise the established safety matrix against regeneration's new key/group.
    for name in (
        'test_join_during_commit_is_included_and_cancellation_is_declined',
        'test_last_active_cancellation_holds_slot_until_cleanup_and_allows_retry',
        'test_late_join_sees_current_progress_and_one_cancel_does_not_stop_worker',
        'test_join_waiting_job_does_not_consume_queue_and_last_waiter_never_runs',
        'test_shutdown_drains_worker_and_all_consumers_then_refuses_joins',
        'test_failing_one_terminal_card_does_not_fail_publication_or_other_history',
        'test_worker_completion_does_not_invalidate_detached_members_cancel_restore',
        'test_slow_progress_on_one_card_never_blocks_other_card_or_publication',
        'test_slow_first_initial_card_does_not_prevent_other_member_starting_worker',
        'test_first_initial_delivery_failure_keeps_joined_worker_and_other_card',
    ):
        locals()[name]=getattr(fixture.SharedCards,name)
