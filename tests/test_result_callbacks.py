import asyncio
import errno
import sqlite3
import unittest
from types import SimpleNamespace
from telegram import Update
from telegram.ext import Application, CallbackQueryHandler
from src import main
from src.handlers import handlers_chat
from src.ui.ready_open_action import selection
from src.ui.html_file_action import _selection as file_selection
from src.ui.ui_labels import READY_UNAVAILABLE, STALE_ACTION_NOTICE
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
        self.callbacks = None
        self.offer_number = 100

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
                                bot=SimpleNamespace(id=42,token='123:synthetic-offer-key',send_message=AsyncMock(),edit_message_text=AsyncMock(),
                                    send_photo=AsyncMock(return_value=SimpleNamespace(message_id=100)),
                                    edit_message_media=AsyncMock(),edit_message_caption=AsyncMock()))
        return update,context

    async def choose(self,update,context,label='Получить готовый чат'):
        await handlers_vod.vod_link_entry(update,context)
        keyboard=update.message.reply_text.await_args.kwargs['reply_markup']
        self.assertEqual([label,'Обработать чат заново'],
                         [row[0].text for row in keyboard.inline_keyboard])
        self.offer_number += 1
        update.callback_query.message = SimpleNamespace(chat_id=update.message.chat_id,
            message_id=self.offer_number, from_user=SimpleNamespace(id=context.bot.id),
            reply_markup=keyboard, photo=(), edit_text=AsyncMock(), edit_caption=AsyncMock(),
            reply_text=update.message.reply_text)
        return [row[0].callback_data for row in keyboard.inline_keyboard]

    async def dispatch(self, update, context):
        # Use the production registration and PTB's actual callback matching.
        if self.callbacks is None:
            app = Application.builder().token('123456:test-token').build()
            with patch.object(main.Application, 'builder') as builder, \
                 patch.object(main, 'get_telegram_bot_token', return_value='test'), \
                 patch.object(main, 'validate_credentials'), patch.object(main, 'setup_logging'), \
                 patch.object(Application, 'run_polling'):
                builder.return_value.application_class.return_value.token.return_value.post_init.return_value.post_stop.return_value.post_shutdown.return_value.build.return_value = app
                main.main()
            self.callbacks = [h for group in app.handlers.values() for h in group
                              if isinstance(h, CallbackQueryHandler)]
        update.callback_query.game_short_name = None
        update.callback_query.from_user = update.effective_user
        routed = Update(1, callback_query=update.callback_query)
        handler = next(h for h in self.callbacks if h.check_update(routed))
        expected = handlers_chat.ready_open_callback if update.callback_query.data.startswith('ui:ready:') else handlers_chat.legacy_open_callback
        self.assertIs(expected, handler.callback)
        await handler.callback(routed, context)

    def final_card(self, context, expected):
        call = context.bot.edit_message_media.await_args
        self.assertIsNotNone(call)
        from src.ui.live_status import WARNING
        self.assertEqual(expected.meta.get('captured_during_live') is True, WARNING in call.kwargs['media'].caption)
        if expected.mode == 'local':
            self.assertIn(expected.filename, call.kwargs['media'].caption)
        buttons = [b for row in call.kwargs['reply_markup'].inline_keyboard for b in row]
        if expected.mode == 'public':
            self.assertTrue(any(b.url == expected.ref for b in buttons))
        else:
            self.assertFalse(any((b.url or '').startswith('local:') for b in buttons))
        data = next(b.callback_data for b in buttons if (b.callback_data or '').startswith('ui:htmlfile:'))
        source = SimpleNamespace(chat_id=1, from_user=SimpleNamespace(id=context.bot.id),
                                 reply_markup=call.kwargs['reply_markup'])
        q = SimpleNamespace(message=source, data=data)
        self.assertEqual((expected.vod_id, expected.id), file_selection(
            SimpleNamespace(callback_query=q, effective_user=SimpleNamespace(id=1)), context))


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
            await self.dispatch(update,context)
        self.final_card(context,old)
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

    def public_ready(self, text='old'):
        plan=self.plan(mode='public')
        plan.meta['captured_during_live'] = text=='old'
        self.publish(plan,text)
        plan.complete()
        plan.close()
        return plan

    async def test_offered_open_callback_keeps_selected_generation(self):
        from src.ui import ui_history
        self.stack.enter_context(patch.object(handlers_vod,'HTML_PUBLISH_MODE','public'))
        self.stack.enter_context(patch.object(ui_history,'HTML_PUBLISH_MODE','public'))
        for order in ((0,1), (1,0)):
            with self.subTest(order=order):
                old=self.public_ready()
                first,context=self.request()
                open_a,_=await self.choose(first,context,label='Получить готовый неполный чат')
                latest=self.public_ready('latest')
                second,_=self.request()
                open_b,_=await self.choose(second,context)
                self.assertNotEqual(open_a,open_b)
                first.callback_query.data=open_a
                second.callback_query.data=open_b
                for index in order:
                    update=(first,second)[index]
                    expected=(old,latest)[index]
                    context.bot.edit_message_media.reset_mock()
                    await self.dispatch(update,context)
                    self.final_card(context,expected)
                self.assertTrue(old.path().exists())

    async def test_unavailable_old_generation_never_opens_new_current(self):
        for failure in ('missing','deleted','state','format','deleting','reference'):
            with self.subTest(failure=failure):
                old=self.ready()
                update,context=self.request()
                update.callback_query.data,_=await self.choose(update,context)
                self.ready('latest')
                con=results._connect()
                try:
                    with con:
                        if failure=='missing':old.path().unlink()
                        elif failure=='deleted':con.execute('DELETE FROM html_results WHERE id=?',(old.id,))
                        elif failure=='state':con.execute("UPDATE html_results SET state='failed' WHERE id=?",(old.id,))
                        elif failure=='format':con.execute("UPDATE html_results SET fmt='html_offline' WHERE id=?",(old.id,))
                        elif failure=='deleting':con.execute("UPDATE html_results SET managed=0 WHERE id=?",(old.id,))
                        else:con.execute("UPDATE html_results SET html_ref='local:other.html' WHERE id=?",(old.id,))
                finally:con.close()
                with patch.object(config,'HISTORY_ENABLED',True), patch.object(db,'add_user_history') as history:
                    await self.dispatch(update,context)
                update.callback_query.answer.assert_awaited_once_with(READY_UNAVAILABLE)
                context.bot.edit_message_media.assert_not_awaited()
                history.assert_not_called()

    async def test_legacy_vod_callback_keeps_current_result_compatibility(self):
        old=self.ready()
        update,context=self.request()
        await self.choose(update,context)
        latest=self.ready('latest')
        update.callback_query.data='ui:result:open:123456789'
        await self.dispatch(update,context)
        self.final_card(context,latest)
        self.assertNotEqual(old.id,latest.id)

    async def test_signed_offer_rejects_foreign_owner_chat_message_and_tampering(self):
        for invalid in ('owner','chat','sender','markup','signature','copied_file_action'):
            with self.subTest(invalid=invalid):
                self.ready()
                update,context=self.request()
                data,_=await self.choose(update,context)
                q=update.callback_query
                q.data=data
                if invalid=='owner':update.effective_user=SimpleNamespace(id=2)
                elif invalid=='chat':q.message.chat_id=2
                elif invalid=='sender':q.message.from_user=SimpleNamespace(id=77)
                elif invalid=='markup':q.message.reply_markup=None
                else:
                    from telegram import InlineKeyboardMarkup, InlineKeyboardButton
                    q.data=(data[:-2]+('A' if data[-2]!='A' else 'B')+data[-1]) if invalid=='signature' else data.replace('ui:ready:1','ui:htmlfile:2')
                    if invalid=='copied_file_action':
                        # A valid file-delivery signature cannot authorize an open action.
                        from src.ui.html_file_action import keyboard
                        kb=keyboard(context,1,1,results.get_ready_result('123456789'),None)
                        q.data=kb.inline_keyboard[0][0].callback_data.replace('ui:htmlfile:2','ui:ready:1')
                    q.message.reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('open',callback_data=q.data)]])
                with patch.object(db,'add_user_history') as history:
                    await self.dispatch(update,context)
                q.answer.assert_awaited_once_with(STALE_ACTION_NOTICE, show_alert=False)
                history.assert_not_called()
                context.bot.edit_message_media.assert_not_awaited()

    async def test_new_offer_history_download_owner_and_duplicate_action(self):
        old=self.ready()
        update,context=self.request()
        update.callback_query.data,_=await self.choose(update,context)
        self.ready('latest')
        with patch.object(config,'HISTORY_ENABLED',True), patch.object(db,'add_user_history') as history:
            await self.dispatch(update,context)
            self.final_card(context,old)
            history.assert_called_once_with(1,handlers_vod.ready_generation(old.vod_id,old.id)['cache_id'])
            await self.dispatch(update,context)
            self.assertEqual(1,context.bot.edit_message_media.await_count)
            self.assertEqual(1,history.call_count)

    async def test_open_checks_active_message_and_current_request(self):
        old=self.ready()
        update,context=self.request()
        update.callback_query.data,_=await self.choose(update,context)
        q=update.callback_query
        context.user_data['download_ui']={'chat':q.message.chat_id,'message':q.message.message_id}
        await self.dispatch(update,context)
        q.answer.assert_awaited_once_with(STALE_ACTION_NOTICE, show_alert=False)
        context.bot.edit_message_media.assert_not_awaited()
        context.user_data.pop('download_ui')
        async def invalidate(*args):
            from src.ui.card_context import for_message
            scoped=for_message(context,1,q.message.chat_id,q.message.message_id)
            scoped.user_data['legacy_open']={}
            return None
        with patch('src.ui.result_preview.resolve_preview',side_effect=invalidate):
            await self.dispatch(update,context)
        context.bot.edit_message_media.assert_not_awaited()

    async def test_callback_limit_including_max_vod_and_pinned_generation(self):
        old=self.ready(vod_id='9'*20)
        update,context=self.request()
        update.message.text='https://www.twitch.tv/videos/'+'9'*20
        data,regen=await self.choose(update,context)
        update.callback_query.data=data
        self.assertEqual(61,len(data.encode('utf-8')))
        self.assertLessEqual(len(regen.encode('utf-8')),64)
        self.assertEqual(('9'*20,old.id),selection(update,context))
        await self.dispatch(update,context)
        self.assertIn(old.filename,context.bot.edit_message_media.await_args.kwargs['media'].caption)

    async def test_missing_ready_file_refuses_without_reviving_format_choice(self):
        old=self.ready()
        update,context=self.request()
        open_data,_=await self.choose(update,context)
        old.path().unlink()
        update.callback_query.data=open_data
        await self.dispatch(update,context)
        update.callback_query.answer.assert_awaited_once_with(READY_UNAVAILABLE)
        self.assertNotIn('vod_pending',context.user_data)
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
            await self.dispatch(update,context)
            self.final_card(context,old)
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
        self.assertIn('Сейчас обрабатывается чат VOD',update.message.reply_text.await_args.args[0])
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
        self.assertEqual('Действие устарело. Выберите VOD заново.',foreign.message.reply_text.await_args_list[0].args[0])

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
                update.callback_query.data = 'ui:pending_cancel:' + context.user_data['download_ui']['cancel_token']
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
