import asyncio
import errno
import sqlite3
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import test_result_registry as registry_fixture
from src import config, html_publisher
from src.database import database as db, db_results as results
from src.handlers import handlers_vod
from src.handlers.handlers_state import begin_download,finish_download,is_busy
from src.workers import download_worker,worker_html
from src.twitch_api.client import TwitchAPIError


class ResultCallbacksTestCase(unittest.IsolatedAsyncioTestCase):
    plan=registry_fixture.ResultRegistryTestCase.plan
    publish=registry_fixture.ResultRegistryTestCase.publish
    ready=registry_fixture.ResultRegistryTestCase.ready
    rows=registry_fixture.ResultRegistryTestCase.rows

    def setUp(self):
        registry_fixture.ResultRegistryTestCase.setUp(self)
        self.tasks=[]
        self.application=SimpleNamespace(bot_data={'aiohttp':object()})

        def create_task(coro):
            task=asyncio.create_task(coro)
            self.tasks.append(task)
            return task

        self.application.create_task=Mock(side_effect=create_task)
        meta=SimpleNamespace(title='new VOD',channel='channel',channel_id='id',length_seconds=30,
                             created_at=None,thumbnail_preview_url=None,thumbnail_full_url=None)
        for module,name,value in (
            (handlers_vod,'HTML_PUBLISH_MODE','local'),(worker_html,'HTML_PUBLISH_MODE','local'),
            (config,'HISTORY_ENABLED',False),(download_worker,'OUT_DIR',str(self.root/'html')),
            (download_worker,'SPOOL_DIR',str(self.root/'work')),
            (download_worker,'get_client_id',lambda:'test-client'),
            (download_worker,'fetch_vod_meta',AsyncMock(return_value=meta)),
            (download_worker,'make_progress_updater',lambda *args:AsyncMock()),
            (download_worker,'gql_fetch_comments',self.source),
        ):
            self.stack.enter_context(patch.object(module,name,value))
        for name in ('fetch_7tv_emote_map','fetch_bttv_emote_map','fetch_ffz_emote_map',
                     'fetch_twitch_global_emote_map','fetch_twitch_global_badge_map','fetch_twitch_channel_badge_map'):
            self.stack.enter_context(patch.object(worker_html,name,AsyncMock(return_value={})))
        self.stack.enter_context(patch.object(worker_html,'fetch_twitch_channel_emote_maps',AsyncMock(return_value=({},{}))))

    async def asyncTearDown(self):
        for task in self.tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*self.tasks,return_exceptions=True)

    async def source(self,*args,**kwargs):
        for i in range(3):
            yield (i,'','user',f'message {i}',[{'text':'hello'}],None,[])

    def request(self,user_id=1):
        message=SimpleNamespace(chat_id=user_id,message_id=9,text='https://www.twitch.tv/videos/123456789',
                                reply_text=AsyncMock(),edit_text=AsyncMock())
        query=SimpleNamespace(data='',message=message,answer=AsyncMock())
        update=SimpleNamespace(message=message,effective_message=message,callback_query=query,
                               effective_user=SimpleNamespace(id=user_id))
        context=SimpleNamespace(application=self.application,user_data={},
                                bot=SimpleNamespace(send_message=AsyncMock(),edit_message_text=AsyncMock()))
        return update,context

    async def choose(self,update,context):
        await handlers_vod.vod_link_entry(update,context)
        keyboard=update.message.reply_text.await_args.kwargs['reply_markup']
        self.assertEqual(['Открыть готовую страницу','Сгенерировать заново'],
                         [row[0].text for row in keyboard.inline_keyboard])
        return [row[0].callback_data for row in keyboard.inline_keyboard]

    async def finish(self):
        await asyncio.gather(*self.tasks)
        await asyncio.sleep(0)

    async def test_ready_request_and_open_do_not_call_twitch(self):
        old=self.ready()
        update,context=self.request()
        with patch.object(handlers_vod,'fetch_vod_meta',AsyncMock(side_effect=AssertionError('Twitch called'))), \
             patch.object(handlers_vod,'get_client_id',side_effect=AssertionError('credentials read')):
            open_data,regen=await self.choose(update,context)
            update.callback_query.data=open_data
            await handlers_vod.vod_result_callback(update,context)
        self.assertIn(old.filename,context.bot.send_message.await_args.kwargs['text'])
        self.assertTrue(all(len(data.encode('utf-8'))<=64 for data in (open_data,regen)))
        self.application.create_task.assert_not_called()

    async def test_new_vod_keeps_original_format_prompt(self):
        update,context=self.request()
        with patch.object(handlers_vod,'get_client_id',return_value='client'), \
             patch.object(handlers_vod,'fetch_vod_meta',AsyncMock(return_value=SimpleNamespace(length_seconds=30))):
            await handlers_vod.vod_link_entry(update,context)
        self.assertEqual(handlers_vod._FORMAT_PROMPT_TEXT,update.message.reply_text.await_args.args[0])
        self.assertEqual('123456789',context.user_data['vod_pending']['vod_id'])

    async def test_successful_regeneration_keeps_old_file_and_switches_current(self):
        old=self.ready()
        update,context=self.request()
        _,regen=await self.choose(update,context)
        update.callback_query.data=regen
        await handlers_vod.vod_result_callback(update,context)
        await self.finish()
        current=results.get_ready_result('123456789')
        self.assertNotEqual(old.id,current['id'])
        self.assertTrue(old.path().exists())
        self.assertEqual(2,len(self.rows()))
        self.assertEqual(3,current['stats']['messages'])
        self.assertFalse(is_busy(context))
        self.assertEqual(0,db.get_history_count(1))

    async def test_old_open_callback_resolves_latest_generation(self):
        old=self.ready()
        update,context=self.request()
        open_data,_=await self.choose(update,context)
        latest=self.ready('latest')
        update.callback_query.data=open_data
        await handlers_vod.vod_result_callback(update,context)
        text=context.bot.send_message.await_args.kwargs['text']
        self.assertIn(latest.filename,text)
        self.assertNotIn(old.filename,text)
        self.assertTrue(old.path().exists())

    async def test_missing_ready_file_offers_creation_without_broken_url(self):
        old=self.ready()
        update,context=self.request()
        open_data,_=await self.choose(update,context)
        old.path().unlink()
        update.callback_query.data=open_data
        await handlers_vod.vod_result_callback(update,context)
        self.assertIn('недоступна',update.message.reply_text.await_args.args[0])
        self.assertEqual('123456789',context.user_data['vod_pending']['vod_id'])
        context.bot.send_message.assert_not_awaited()
        self.assertEqual('ready',self.rows()[0]['state'])

    async def test_ready_page_opens_while_another_user_owns_slot(self):
        old=self.ready()
        update,context=self.request()
        other_update,other=self.request(2)
        operation=begin_download(other)
        try:
            open_data,_=await self.choose(update,context)
            update.callback_query.data=open_data
            await handlers_vod.vod_result_callback(update,context)
            self.assertIn(old.filename,context.bot.send_message.await_args.kwargs['text'])
            self.assertTrue(is_busy(other))
        finally:
            finish_download(other,operation)

    async def test_busy_regeneration_has_no_queue_and_keeps_choice(self):
        old=self.ready()
        update,context=self.request()
        _,regen=await self.choose(update,context)
        _,other=self.request(2)
        operation=begin_download(other)
        update.callback_query.data=regen
        await handlers_vod.vod_result_callback(update,context)
        self.assertIn('Бот занят',update.message.reply_text.await_args.args[0])
        self.application.create_task.assert_not_called()
        self.assertIn('vod_result_choice',context.user_data)
        finish_download(other,operation)
        await handlers_vod.vod_result_callback(update,context)
        await self.finish()
        self.assertEqual(2,len(self.rows()))
        self.assertTrue(old.path().exists())

    async def test_double_click_starts_only_one_generation(self):
        self.ready()
        update,context=self.request()
        _,regen=await self.choose(update,context)
        update.callback_query.data=regen
        await asyncio.gather(handlers_vod.vod_result_callback(update,context),handlers_vod.vod_result_callback(update,context))
        await self.finish()
        self.assertEqual(1,self.application.create_task.call_count)
        self.assertEqual(2,len(self.rows()))
        # A delayed duplicate must not start again after completion either.
        await handlers_vod.vod_result_callback(update,context)
        self.assertEqual(1,self.application.create_task.call_count)

    async def test_two_users_racing_start_only_one_regeneration(self):
        self.ready()
        first,context1=self.request(1)
        second,context2=self.request(2)
        _,first.callback_query.data=await self.choose(first,context1)
        _,second.callback_query.data=await self.choose(second,context2)
        await asyncio.gather(handlers_vod.vod_result_callback(first,context1),handlers_vod.vod_result_callback(second,context2))
        await self.finish()
        self.assertEqual(1,self.application.create_task.call_count)
        self.assertEqual(2,len(self.rows()))
        self.assertFalse(is_busy(context1))
        self.assertFalse(is_busy(context2))

    async def test_stale_and_foreign_user_callbacks_cannot_start_regeneration(self):
        self.ready()
        update,context=self.request()
        _,regen=await self.choose(update,context)
        foreign,other=self.request(2)
        foreign.callback_query.data=regen
        await handlers_vod.vod_result_callback(foreign,other)
        self.application.create_task.assert_not_called()
        self.assertEqual('Ссылка устарела.',foreign.message.reply_text.await_args_list[0].args[0])

    async def test_invalid_vod_links_and_callbacks_do_not_start_work(self):
        update,context=self.request()
        with patch.object(handlers_vod,'fetch_vod_meta',AsyncMock(side_effect=AssertionError('Twitch called'))):
            for text in ('abc 123456789','https://evil.invalid/videos/123456789','https://www.twitch.tv/videos/123abc','0','9'*21):
                update.message.text=text
                await handlers_vod.vod_link_entry(update,context)
        for data in ('ui:result:open:../file','ui:result:regen:0:1234567890123456','ui:result:open:bad'):
            update.callback_query.data=data
            await handlers_vod.vod_result_callback(update,context)
        self.application.create_task.assert_not_called()

    async def test_failures_do_not_change_old_head_or_file(self):
        old=self.ready()
        for stage in ('api','sqlite','serialize','publish'):
            with self.subTest(stage=stage):
                update,context=self.request()
                _,regen=await self.choose(update,context)
                update.callback_query.data=regen
                if stage=='api':
                    target=patch.object(download_worker,'fetch_vod_meta',AsyncMock(side_effect=TwitchAPIError('test failure')))
                elif stage=='sqlite':
                    target=patch.object(results.Publication,'complete',side_effect=sqlite3.OperationalError('test failure'))
                elif stage=='serialize':
                    target=patch.object(worker_html,'write_viewer_html',side_effect=TypeError('test failure'))
                else:
                    target=patch.object(html_publisher.os,'link',side_effect=OSError(errno.ENOSPC,'test disk full'))
                with target:
                    await handlers_vod.vod_result_callback(update,context)
                    await self.finish()
                self.assertEqual(old.id,results.get_ready_result('123456789')['id'])
                self.assertTrue(old.path().exists())
                self.assertFalse(is_busy(context))

    async def test_cancelled_regeneration_keeps_old_head_and_releases_slot(self):
        old=self.ready()
        update,context=self.request()
        _,regen=await self.choose(update,context)
        entered,release=asyncio.Event(),asyncio.Event()

        async def source(*args,**kwargs):
            entered.set()
            await release.wait()
            yield (1,'','user','message',[],None,[])

        with patch.object(download_worker,'gql_fetch_comments',source):
            update.callback_query.data=regen
            await handlers_vod.vod_result_callback(update,context)
            try:
                await asyncio.wait_for(entered.wait(),2)
                await handlers_vod.pending_cancel_callback(update,context)
                self.assertTrue(is_busy(context))
            finally:
                release.set()
                await self.finish()
        self.assertEqual(old.id,results.get_ready_result('123456789')['id'])
        self.assertEqual('cancelled',self.rows()[-1]['state'])
        self.assertFalse(is_busy(context))

    async def test_notification_failure_does_not_undo_committed_generation(self):
        old=self.ready()
        update,context=self.request()
        _,update.callback_query.data=await self.choose(update,context)
        with patch.object(handlers_vod,'_edit_progress_message_with_card',AsyncMock(side_effect=OSError('Telegram unavailable'))):
            await handlers_vod.vod_result_callback(update,context)
            await self.finish()
        current=results.get_ready_result('123456789')
        self.assertNotEqual(old.id,current['id'])
        self.assertTrue(old.path().exists())
        self.assertEqual('ready',self.rows()[-1]['state'])
        self.assertFalse(is_busy(context))


if __name__=='__main__':
    unittest.main()
