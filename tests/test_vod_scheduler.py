import asyncio
from contextlib import contextmanager
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock,patch

from src.vod_scheduler import Scheduler,QUEUE_LIMIT
from src.maintenance import Coordinator,GATE_KEY
from src.job_context import JobContext
from src.handlers.handlers_state import begin_download,finish_download,is_busy,is_cancelled


class SchedulerTests(unittest.IsolatedAsyncioTestCase):
    def _setupAsyncioRunner(self):
        self._asyncioRunner=asyncio.Runner(debug=True,loop_factory=asyncio.SelectorEventLoop)

    async def asyncSetUp(self):
        self.allowed=True
        @contextmanager
        def admission():yield self.allowed
        self.app=SimpleNamespace(bot_data={},running=True,create_task=asyncio.create_task)
        self.scheduler=Scheduler(self.app,admission)
        self.app.bot_data['tdb_vod_scheduler']=self.scheduler
        self.starts=[];self.releases={};self.started={};self.running=0;self.peak=0

    async def asyncTearDown(self):
        await self.scheduler.close()

    def add(self,n,owner=1,fail=False):
        job,error=self.scheduler.reserve(owner,str(n));self.assertIsNone(error)
        release=self.releases[n]=asyncio.Event();started=self.started[n]=asyncio.Event()
        async def work():
            self.starts.append(n);self.running+=1;self.peak=max(self.peak,self.running);started.set()
            try:
                await release.wait()
                if fail:raise ValueError('isolated failure')
            finally:self.running-=1
        self.scheduler.arm(job,work)
        return job

    async def finish(self,job,n):
        self.releases[n].set();await job.task;await asyncio.sleep(0)

    async def test_two_slots_fifo_many_users_and_exception_isolation(self):
        a=self.add(1);b=self.add(2,owner=2);c=self.add(3,owner=3);d=self.add(4,owner=1)
        await self.started[1].wait();await self.started[2].wait()
        self.assertEqual([1,2],self.starts);self.assertIsNone(c.task);self.assertIsNone(d.task)
        await self.finish(a,1);await self.started[3].wait()
        await self.finish(b,2);await self.started[4].wait()
        self.assertEqual([1,2,3,4],self.starts);self.assertEqual(2,self.peak)
        await self.finish(c,3);await self.finish(d,4)
        self.assertEqual('success',c.state);self.assertEqual(0,self.scheduler.active)

    async def test_waiting_cancel_never_executes_and_active_cancel_drains(self):
        a=self.add(1);b=self.add(2);c=self.add(3);d=self.add(4)
        await self.started[1].wait();await self.started[2].wait()
        self.assertTrue(self.scheduler.cancel(c));self.assertFalse(self.scheduler.cancel(c))
        self.assertEqual('cancelled',c.state);self.assertNotIn(3,self.starts)
        self.scheduler.cancel(a)
        self.assertEqual(2,self.scheduler.active) # Accepted cooperative cancellation still owns its slot.
        await self.finish(a,1);await self.started[4].wait()
        self.assertFalse(b.task.done());self.assertEqual('cancelled',a.state)
        await self.finish(b,2);await self.finish(d,4)

    async def test_failure_frees_one_slot_and_keeps_other_running(self):
        a=self.add(1,fail=True);b=self.add(2);c=self.add(3)
        await self.started[1].wait();self.releases[1].set()
        with self.assertRaises(ValueError):await a.task
        await self.started[3].wait()
        self.assertEqual('error',a.state);self.assertFalse(b.task.done())
        await self.finish(b,2);await self.finish(c,3)

    async def test_hold_prepare_and_release_are_atomic_with_pump(self):
        self.allowed=False;a=self.add(1);b=self.add(2)
        self.assertEqual(0,self.scheduler.active)
        gate=Coordinator(self.app,AsyncMock(),AsyncMock())
        self.assertEqual('ready',(await gate.command('prepare','a'*32,self))['status'])
        self.allowed=True;self.scheduler.pump();self.assertEqual(0,self.scheduler.active)
        await gate.command('release','a'*32,self)
        await self.started[1].wait();await self.started[2].wait()
        self.assertEqual('busy',(await gate.command('prepare','b'*32,self))['status'])
        await self.finish(a,1)
        self.assertEqual('busy',(await gate.command('prepare','b'*32,self))['status'])
        await self.finish(b,2)
        self.assertEqual('ready',(await gate.command('prepare','b'*32,self))['status'])

    async def test_capacity_duplicate_and_shutdown(self):
        a=self.add(1);b=self.add(2)
        for n in range(3,QUEUE_LIMIT+3):self.add(n)
        self.assertEqual(QUEUE_LIMIT,len(self.scheduler.queue))
        self.assertIsNone(self.scheduler.reserve(2,'999')[0])
        self.assertIsNone(self.scheduler.reserve(2,'1')[0])
        await self.started[1].wait();await self.scheduler.close()
        self.assertEqual({},self.scheduler.jobs);self.assertEqual(0,self.running)
        self.assertEqual('cancelled',a.state);self.assertEqual('cancelled',b.state)
        self.assertIsNone(self.scheduler.reserve(1,'1000')[0])

    async def test_cancel_completion_commit_and_startup_failure(self):
        a=self.add(1);await self.started[1].wait();a.committing=True
        self.assertFalse(self.scheduler.cancel(a))
        await self.finish(a,1);self.assertEqual('success',a.state)
        b=self.add(2);await self.started[2].wait()
        self.releases[2].set();self.assertTrue(self.scheduler.cancel(b));await b.task;await asyncio.sleep(0)
        self.assertEqual('cancelled',b.state)
        self.app.create_task=lambda coro: (_ for _ in ()).throw(RuntimeError('task unavailable'))
        with self.assertRaises(RuntimeError):self.add(3)
        self.assertEqual(0,self.scheduler.active);self.assertEqual({},self.scheduler.jobs)

    async def test_job_context_cancel_and_navigation_are_independent(self):
        shared={'chat_request':object()};root=SimpleNamespace(user_data=shared,application=self.app)
        a,_=self.scheduler.reserve(1,'1');b,_=self.scheduler.reserve(1,'2')
        ca,cb=JobContext(root,a),JobContext(root,b);a.context=ca;b.context=cb
        oa,ob=begin_download(ca),begin_download(cb)
        ca.user_data['download_ui']={'operation':oa};cb.user_data['download_ui']={'operation':ob}
        self.scheduler.cancel(a)
        self.assertTrue(is_cancelled(ca,oa));self.assertFalse(is_cancelled(cb,ob));self.assertTrue(is_busy(cb))
        self.assertEqual(ob,cb.user_data['download_ui']['operation'])
        changed=object();shared['chat_request']=changed
        self.assertIs(ca.user_data['chat_request'],changed)
        self.assertFalse(finish_download(cb,oa));self.assertTrue(finish_download(cb,ob))

    async def test_cancelled_shutdown_caller_still_drains_committing_job(self):
        a=self.add(1);b=self.add(2)
        await self.started[1].wait();await self.started[2].wait();a.committing=True
        shutdown=asyncio.create_task(self.scheduler.close())
        await asyncio.sleep(0);await asyncio.sleep(0)
        shutdown.cancel();await asyncio.sleep(0)
        self.assertFalse(a.task.done());self.assertFalse(shutdown.done())
        self.releases[1].set()
        with self.assertRaises(asyncio.CancelledError):await shutdown
        self.assertTrue(a.task.done());self.assertTrue(b.task.done());self.assertEqual({},self.scheduler.jobs)

    async def test_preparing_inputs_share_queue_bound_and_transfer_place_once(self):
        from src.vod_scheduler import INPUT_TICKET
        from src.handlers.handlers_state import is_globally_busy
        tickets=[self.scheduler.prepare_input() for _ in range(QUEUE_LIMIT)]
        self.assertTrue(all(tickets));self.assertIsNone(self.scheduler.prepare_input())
        token=INPUT_TICKET.set(tickets[0])
        try:
            self.assertFalse(is_globally_busy(SimpleNamespace(application=self.app)))
            job,error=self.scheduler.reserve(1,'123')
            self.assertIsNone(error);self.assertEqual(1,len(self.scheduler.queue))
            self.scheduler.release_input(tickets[0])
            self.assertEqual(QUEUE_LIMIT-1,self.scheduler.preparing)
        finally:INPUT_TICKET.reset(token)
        self.scheduler.cancel(job)
        for ticket in tickets:self.scheduler.release_input(ticket)
        self.assertEqual(0,self.scheduler.preparing)
