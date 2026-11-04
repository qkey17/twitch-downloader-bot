import asyncio
import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch
from src.handlers import handlers_chat as chat, handlers_vod as vod
from src.ui.card_context import for_message
from src.ui.callbacks import CallbackReply
from src.ui.ui_labels import VOD_BUSY_NOTICE, STALE_ACTION_NOTICE
from src.vod_scheduler import scheduler
import test_shared_vods as fixture


class BusyNotice(unittest.IsolatedAsyncioTestCase):
    for name in ('_setupAsyncioRunner','setUp','asyncTearDown','task','api','message','update','saved',
                 'publication','worker','context','request','entered','finish'):
        locals()[name]=getattr(fixture.SharedCards,name)

    async def offer(self, owner=7, v='1'):
        ctx=self.context(owner)
        message=self.message(3000+len(self.calls),v)
        scope=for_message(ctx,owner,10,message.message_id)
        await chat.offer_chat(message,scope,owner,self.saved(v),lambda:True,None)
        choice=scope.user_data['chat_choice']
        m=self.messages[choice['message']]
        return ctx,m,'ui:chat:regen:'+choice['token']

    async def stale_while_waiting(self, action, acquisition):
        ctx,message,data=await self.offer()
        scope=for_message(ctx,7,10,message.message_id)
        choice=scope.user_data['chat_choice']
        choice['create']=action=='create'
        waiting=asyncio.Event()

        class ContendedLock:
            def __init__(self):
                self.lock=asyncio.Lock()
                self.entries=0

            async def __aenter__(self):
                self.entries+=1
                if self.entries==acquisition:
                    waiting.set()
                await self.lock.acquire()

            async def __aexit__(self,*args):
                self.lock.release()
                if self.entries==acquisition-1:
                    # A competing UI owner holds the lock before the next entry.
                    await self.lock.acquire()

        lock=choice['ui_lock']=ContendedLock()
        if acquisition==1:
            await lock.lock.acquire()
        query=self.update(message,data.replace(':regen:',':'+action+':'))
        before=list(self.calls)
        with patch.object(vod,'_start_download',new_callable=AsyncMock) as start:
            task=asyncio.create_task(chat.chat_callback(query,ctx))
            try:
                await asyncio.wait_for(waiting.wait(),2)
                choice['current']=lambda:False
                lock.lock.release()
                await asyncio.wait_for(task,2)
            finally:
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task,return_exceptions=True)
            start.assert_not_awaited()
        if action=='regen':
            query.callback_query.answer.assert_awaited_once_with(STALE_ACTION_NOTICE,show_alert=False)
        else:
            query.callback_query.answer.assert_awaited_once_with()
        self.assertEqual(before,self.calls)
        self.assertFalse(self.publications)
        self.assertFalse(scheduler(self.app).jobs)

    async def test_regen_stale_during_first_lock_answers(self):
        await self.stale_while_waiting('regen',1)

    async def test_regen_stale_during_second_lock_answers(self):
        await self.stale_while_waiting('regen',2)

    async def test_acknowledged_open_create_do_not_answer_twice(self):
        for action in ('open','create'):
            with self.subTest(action=action):
                await self.stale_while_waiting(action,1)

    async def test_running_and_queued_conflicts_keep_both_cards_and_job(self):
        for queued in (False,True):
            with self.subTest(queued=queued):
                if queued:
                    await self.request(8,'2');await self.entered('2')
                    await self.request(9,'3');await self.entered('3')
                ctx,m,data=await self.offer()
                first=self.update(m,data)
                await chat.chat_callback(first,ctx)
                job=next(j for j in scheduler(self.app).jobs.values() if j.vod=='1')
                if not queued:await self.entered()
                self.assertEqual('waiting' if queued else 'running',job.state)
                ctx,other,data=await self.offer()
                before=list(self.calls);publications=len(self.publications);jobs=dict(scheduler(self.app).jobs)
                q=self.update(other,data)
                await chat.chat_callback(q,ctx)
                q.callback_query.answer.assert_awaited_once_with('Этот VOD уже в работе',show_alert=False)
                self.assertEqual(before,self.calls)
                self.assertEqual(jobs,scheduler(self.app).jobs)
                self.assertFalse(job.cancelled)
                self.assertEqual(publications,len(self.publications))
                first.callback_query.answer.assert_awaited_once_with()
                # Drain this scenario without initiating further work.
                for j in list(scheduler(self.app).jobs.values()):
                    self.gates.setdefault(j.vod,asyncio.Event()).set()
                while scheduler(self.app).jobs:await asyncio.sleep(.01)
                self.started.clear();self.gates.clear()

    async def test_common_starter_conflict_answers_once_without_message(self):
        await self.request(regen=True);job=await self.entered()
        ctx=self.context(8);m=self.message(4000,'1');u=self.update(m)
        reply=CallbackReply(u.callback_query);before=list(self.calls)
        self.assertFalse(await vod._start_download(u,ctx,'1',True,modern=True,callback_reply=reply))
        u.callback_query.answer.assert_awaited_once_with(VOD_BUSY_NOTICE,show_alert=False)
        self.assertEqual(before,self.calls);self.assertFalse(job.cancelled)
        await self.finish(job)

    async def test_compatible_join_and_plain_text_refusal_unchanged(self):
        await self.request();job=await self.entered()
        self.assertFalse(vod.vod_conflict(self.context(8),'1'))
        await self.request(8)
        self.assertEqual(2,len(job.participants.members));self.assertEqual(1,self.worker_calls)
        ctx=self.context(9);m=self.message(4000,'1');u=NS(callback_query=NS(message=m),effective_user=NS(id=9))
        self.assertFalse(await vod._start_download(u,ctx,'1',True,modern=True))
        self.assertIn('Этот VOD уже обрабатывается или ожидает в очереди.',str(self.calls))
        await self.finish(job)


import test_vod_info as info_fixture
from src.handlers import handlers_vod_info as info


class EarlyNotice(unittest.IsolatedAsyncioTestCase):
    for name in ('_setupAsyncioRunner','setUp','asyncTearDown','context_for_user','task',
                 'update','ready_result','open','message','setup_list','show','record'):
        locals()[name]=getattr(info_fixture.InfoTests,name)

    async def test_info_process_conflict_does_not_mutate_card(self):
        source,view=await self.setup_list()
        await self.show(source,view)
        session=self.browser._sessions[view.session_id];card=session.info_card
        self.context.application.bot_data['tdb_vod_scheduler']=NS(jobs={'one':NS(
            vod='123456789',processing_done=False,compatibility=None,participants=None)})
        before=list(self.events)
        q=self.update(f'ui:vi:{session.token}:{card.nonce}:process',card.message)
        await info.info_callback(q,self.context)
        q.callback_query.answer.assert_awaited_once_with(VOD_BUSY_NOTICE,show_alert=False)
        self.assertEqual(before,self.events)
