"""Disposable registry/files and local bot doubles; no Telegram/Twitch requests."""
import asyncio
import hashlib
import os
from pathlib import Path
import sqlite3
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import test_result_registry as fixture
from src import html_sender as sender, html_cleanup as cleanup, ready_html, maintenance, main
from src.database import db_results as results, db_core


class HtmlSenderTests(unittest.IsolatedAsyncioTestCase):
    plan = fixture.ResultRegistryTestCase.plan
    publish = fixture.ResultRegistryTestCase.publish
    ready = fixture.ResultRegistryTestCase.ready
    rows = fixture.ResultRegistryTestCase.rows
    change = fixture.ResultRegistryTestCase.change

    def setUp(self):
        fixture.ResultRegistryTestCase.setUp(self)
        self.bot = SimpleNamespace(send_document=AsyncMock())
        self.app = SimpleNamespace(running=True, bot_data={}, bot=self.bot)
        sender.start_sender(self.app)
        self.service = self.app.bot_data[sender.SENDER_KEY]

    async def asyncTearDown(self):
        await self.service.close()

    def send(self, user=7, vod='123456789', **kwargs):
        return self.service.send(user, 1, vod, **kwargs)

    async def test_streams_pinned_generation_with_no_writer_or_live_transaction(self):
        old = self.ready()
        current = self.ready('new')
        captured = []
        async def upload(**kwargs):
            file = kwargs['document']; handle = file.input_file_content
            captured.append(handle)
            self.assertFalse(isinstance(handle, bytes))
            self.assertEqual(0, handle.tell())
            self.assertEqual(old.path().read_bytes(), handle.read(65536))
            self.assertEqual(f'chat_{old.vod_id}_{old.id}.html', file.filename)
            self.assertEqual(dict(connect_timeout=5,pool_timeout=2,write_timeout=60,read_timeout=30),
                             {k:v for k,v in kwargs.items() if k.endswith('_timeout')})
            # A new writer can commit while upload is in flight.
            con = db_core._connect()
            try:
                con.execute('BEGIN EXCLUSIVE');con.commit()
            finally:con.close()
            self.assertIsNone(results._recovery_lease(old.id))
        self.bot.send_document.side_effect = upload
        before = self.rows()
        self.assertEqual(sender.SendStatus.SENT, (await self.send(generation_id=old.id)).status)
        self.assertTrue(captured[0].closed)
        self.assertEqual(before, self.rows())
        self.assertEqual(current.id,results.get_ready_result(old.vod_id)['id'])
        lease = results._recovery_lease(old.id);self.assertIsNotNone(lease);lease.close()

    async def test_real_size_boundary_and_no_unbounded_file_reads(self):
        plan = self.ready()
        size = ready_html.MAX_CLOUD_HTML_BYTES
        with plan.path().open('wb') as f:f.truncate(size)
        digest = hashlib.sha256()
        with plan.path().open('rb') as f:
            while b:=f.read(65536):digest.update(b)
        self.change(plan.id,size_bytes=size,sha256=digest.hexdigest())
        async def upload(**kwargs):
            handle = kwargs['document'].input_file_content
            total = 0
            while b:=handle.read(65536):total+=len(b)
            self.assertEqual(size,total)
        self.bot.send_document.side_effect=upload
        fdopen = os.fdopen
        reads = []
        class BoundedFile:
            def __init__(self, handle): self.handle = handle
            def __getattr__(self, name): return getattr(self.handle, name)
            def read(self, n=-1):
                if n < 0 or n > 65536: raise AssertionError('Unbounded read')
                reads.append(n)
                return self.handle.read(n)
        with patch.object(ready_html.os, 'fdopen', side_effect=lambda *a,**k:BoundedFile(fdopen(*a,**k))):
            self.assertEqual(sender.SendStatus.SENT,(await self.send()).status)
        self.assertEqual(65536,max(reads))
        with plan.path().open('ab') as f:f.write(b'x')
        self.change(plan.id,size_bytes=size+1)
        self.assertEqual(sender.SendStatus.TOO_LARGE,(await self.send()).status)
        self.assertEqual(1,self.bot.send_document.await_count)

    async def test_rejects_missing_changed_unsafe_and_nonready_without_upload(self):
        plan=self.ready();original=self.rows()[0];data=plan.path().read_bytes()
        for fields in ({'managed':0},{'root':str(self.root)},{'filename':'../other.html'},
                       {'sha256':'0'*64},{'state':'unverified'},{'html_ref':'https://evil.invalid/chat.html'}):
            with self.subTest(fields=fields):
                # Remove current first to allow testing a non-ready historical record.
                con=db_core._connect()
                try:
                    with con:con.execute('DELETE FROM html_current')
                finally:con.close()
                self.change(plan.id,**fields)
                self.assertEqual(sender.SendStatus.UNAVAILABLE,
                                 (await self.send(generation_id=plan.id)).status)
                self.change(plan.id,**{k:original[k] for k in fields})
        plan.path().write_bytes(b'X'*len(data))
        self.assertEqual(sender.SendStatus.UNAVAILABLE,(await self.send(generation_id=plan.id)).status)
        plan.path().unlink()
        self.assertEqual(sender.SendStatus.UNAVAILABLE,(await self.send(generation_id=plan.id)).status)
        self.bot.send_document.assert_not_awaited()

    async def test_legacy_without_hash_requires_complete_html_envelope(self):
        plan=self.ready()
        self.change(plan.id,origin='legacy',sha256=None)
        self.assertEqual(sender.SendStatus.SENT,(await self.send()).status)
        plan.path().write_bytes(b'X'*plan.path().stat().st_size)
        self.assertEqual(sender.SendStatus.UNAVAILABLE,(await self.send()).status)

    async def test_replacement_between_stat_and_open_is_not_sent(self):
        plan=self.ready();replacement=self.root/'replacement'
        replacement.write_bytes(plan.path().read_bytes())
        original_open=os.open
        replaced=False
        def racing_open(path,*args,**kwargs):
            nonlocal replaced
            if Path(path).name==plan.filename and not replaced:
                replaced=True
                os.replace(replacement,plan.path())
            return original_open(path,*args,**kwargs)
        with patch.object(ready_html.os,'open',side_effect=racing_open):
            self.assertEqual(sender.SendStatus.UNAVAILABLE,(await self.send()).status)
        self.assertTrue(replaced);self.bot.send_document.assert_not_awaited()

    async def test_symlink_is_not_sent(self):
        plan=self.ready()
        plan.path().unlink()
        try:plan.path().symlink_to(self.root/'outside')
        except OSError:self.skipTest('OS does not permit symlinks')
        self.assertEqual(sender.SendStatus.UNAVAILABLE,(await self.send()).status)

    async def test_sender_wins_cleanup_skips_then_can_delete_after_release(self):
        plan=self.plan(mode='public')
        self.publish(plan);plan.complete();plan.close()
        entered,release=asyncio.Event(),asyncio.Event()
        async def upload(**kwargs):entered.set();await release.wait()
        self.bot.send_document.side_effect=upload
        send=asyncio.create_task(self.send());await entered.wait()
        row=self.rows()[0]
        self.assertFalse(await asyncio.to_thread(cleanup._attempt,row))
        self.assertTrue(plan.path().exists())
        release.set();await send
        self.assertTrue(await asyncio.to_thread(cleanup._attempt,row))
        self.assertFalse(plan.path().exists())

    async def test_cleanup_wins_and_pending_intent_always_prevents_send(self):
        plan=self.plan(mode='public');self.publish(plan);plan.complete();plan.close()
        row=self.rows()[0]
        with cleanup._result_lease(row) as lease:
            self.assertIsNotNone(lease)
            self.assertEqual(sender.SendStatus.UNAVAILABLE,(await self.send()).status)
            intent=cleanup._claim(row,cleanup._sample(row,None))
            self.assertEqual('pending',intent['phase'])
        self.assertEqual(sender.SendStatus.UNAVAILABLE,(await self.send()).status)
        self.bot.send_document.assert_not_awaited()

    async def test_limits_are_atomic_and_do_not_reserve_vod_slot(self):
        self.ready(vod_id='101');self.ready(vod_id='102');self.ready(vod_id='103')
        started=asyncio.Queue();release=asyncio.Event()
        async def upload(**kwargs):await started.put(True);await release.wait()
        self.bot.send_document.side_effect=upload
        a=asyncio.create_task(self.send(1,'101'));await started.get()
        self.assertEqual(sender.SendStatus.USER_BUSY,(await self.send(1,'103')).status)
        b=asyncio.create_task(self.send(2,'102'));await started.get()
        self.assertEqual(sender.SendStatus.SENDER_BUSY,(await self.send(3,'103')).status)
        self.assertNotIn('vod_global_operation',self.app.bot_data)
        release.set();await asyncio.gather(a,b)
        self.assertFalse(self.service.active);self.assertFalse(self.service.users)

    async def test_old_waiter_cannot_clear_a_new_send_reservation(self):
        self.ready(vod_id='101');self.ready(vod_id='102')
        old_done=asyncio.Event();finish_old=asyncio.Event();new_started=asyncio.Event();finish_new=asyncio.Event()
        original_shield=asyncio.shield
        first=True
        async def delayed_waiter(task):
            nonlocal first
            delay=task.get_name()=='tdb-html-upload' and first
            if delay:first=False
            result=await original_shield(task)
            if delay:old_done.set();await finish_old.wait()
            return result
        async def upload(**kwargs):
            if 'chat_102_' in kwargs['document'].filename:
                new_started.set();await finish_new.wait()
        self.bot.send_document.side_effect=upload
        with patch.object(sender.asyncio,'shield',side_effect=delayed_waiter):
            old=asyncio.create_task(self.send(7,'101'));await old_done.wait()
            new=asyncio.create_task(self.send(7,'102'));await new_started.wait()
            finish_old.set();await old
            self.assertEqual(sender.SendStatus.USER_BUSY,(await self.send(7,'101')).status)
            finish_new.set();await new

    async def test_error_timeout_deadline_and_cancel_release_without_retry(self):
        from telegram.error import BadRequest, NetworkError, TimedOut
        plan=self.ready();handles=[]
        for error,status,uncertain in [(BadRequest('synthetic'),sender.SendStatus.TELEGRAM_ERROR,False),
                                       (NetworkError('synthetic'),sender.SendStatus.TELEGRAM_ERROR,True),
                                       (TimedOut(),sender.SendStatus.TIMEOUT,True)]:
            async def upload(**kwargs):
                handles.append(kwargs['document'].input_file_content);raise error
            self.bot.send_document.side_effect=upload
            outcome=await self.send()
            self.assertEqual((status,uncertain),(outcome.status,outcome.uncertain))
        entered=asyncio.Event();unwound=asyncio.Event()
        async def wait(**kwargs):
            handle=kwargs['document'].input_file_content;handles.append(handle);entered.set()
            try:await asyncio.Event().wait()
            finally:
                self.assertFalse(handle.closed)
                await asyncio.sleep(.01);unwound.set()
        self.bot.send_document.side_effect=wait
        with patch.object(sender,'SEND_DEADLINE',.25):
            self.assertEqual(sender.SendStatus.TIMEOUT,(await self.send()).status)
        self.assertTrue(unwound.is_set());entered.clear();unwound.clear()
        task=asyncio.create_task(self.send());await entered.wait();task.cancel()
        await asyncio.sleep(.001);task.cancel()  # Repeated waiter cancellation must still drain.
        result=await task
        self.assertEqual(sender.SendStatus.CANCELLED,result.status);self.assertTrue(result.uncertain)
        self.assertTrue(unwound.is_set());self.assertTrue(all(h.closed for h in handles))
        self.assertEqual(5,self.bot.send_document.await_count)
        lease=results._recovery_lease(plan.id);self.assertIsNotNone(lease);lease.close()

    async def test_cancellation_joins_preparation_before_closing_owned_resources(self):
        self.ready();entered=threading.Event();release=threading.Event();exited=threading.Event()
        original=ready_html.ReadyHtml.open
        def prepare(owned,stop):
            original(owned,stop);entered.set()
            try:release.wait(3)
            finally:exited.set()
        with patch.object(ready_html.ReadyHtml,'open',prepare):
            task=asyncio.create_task(self.send())
            self.assertTrue(await asyncio.to_thread(entered.wait,3))
            task.cancel();await asyncio.sleep(.01)
            self.assertFalse(task.done());self.assertTrue(self.service.active)
            release.set();self.assertEqual(sender.SendStatus.CANCELLED,(await task).status)
        self.assertTrue(exited.is_set());self.bot.send_document.assert_not_awaited()

    async def test_maintenance_and_shutdown_join_uploads_before_ready(self):
        plan=self.ready();entered=asyncio.Event();exited=asyncio.Event();handles=[]
        async def upload(**kwargs):
            handle=kwargs['document'].input_file_content;handles.append(handle);entered.set()
            try:await asyncio.Event().wait()
            finally:await asyncio.sleep(.01);exited.set()
        self.bot.send_document.side_effect=upload
        task=asyncio.create_task(self.send());await entered.wait()
        gate=maintenance.Coordinator(self.app,AsyncMock(),AsyncMock())
        self.assertEqual('busy',(await gate.command('prepare','a'*32,object()))['status'])
        with patch.object(main,'stop_maintenance',AsyncMock()),patch.object(main,'stop_cleanup',AsyncMock()):
            self.app.running=False
            await main.post_stop(self.app)
        result=await task
        self.assertEqual(sender.SendStatus.SHUTDOWN,result.status)
        self.assertTrue(exited.is_set());self.assertTrue(handles[0].closed)
        self.assertFalse(self.service.active)
        self.assertEqual(sender.SendStatus.ADMISSION_CLOSED,(await self.send()).status)
        await self.service.close()  # Idempotent.
        self.app.running=True
        self.assertEqual('ready',(await gate.command('prepare','a'*32,object()))['status'])

    async def test_closed_admission_and_stopped_app_do_not_start_work(self):
        self.app.bot_data[maintenance.GATE_KEY]='fence'
        self.assertEqual(sender.SendStatus.ADMISSION_CLOSED,(await self.send()).status)
        self.app.bot_data.clear();self.app.running=False
        self.assertEqual(sender.SendStatus.ADMISSION_CLOSED,(await self.send()).status)
        self.bot.send_document.assert_not_awaited()

    async def test_real_ptb_stop_cancels_upload_even_when_tracked_handler_awaits_it(self):
        self.ready()
        app=main.Application.builder().application_class(main.TcdApplication).token('123:synthetic').build()
        app._running=True
        sender.start_sender(app)
        service=app.bot_data[sender.SENDER_KEY]
        entered=asyncio.Event();exited=asyncio.Event();handles=[]
        async def upload(_bot,**kwargs):
            handle=kwargs['document'].input_file_content;handles.append(handle);entered.set()
            try:await asyncio.Event().wait()
            finally:await asyncio.sleep(.01);exited.set()
        try:
            with patch.object(type(app.bot),'send_document',new=upload):
                handler=app.create_task(service.send(7,1,'123456789'))
                await entered.wait()
                await asyncio.wait_for(app.stop(),2)
                self.assertEqual(sender.SendStatus.SHUTDOWN,(await handler).status)
                self.assertTrue(exited.is_set());self.assertTrue(handles[0].closed)
                self.assertFalse(app.running);self.assertFalse(service.active)
        finally:
            await service.close()
            for request in app.bot._request:await request.shutdown()
