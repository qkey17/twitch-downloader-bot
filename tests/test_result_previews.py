"""Synthetic Telegram/Helix delivery and UI ownership; no image downloads."""
import asyncio
from types import SimpleNamespace
import unittest
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch
from telegram.error import BadRequest
from src.handlers import handlers_chat as chat, handlers_vod as vod
from src.history import history_view as history
from src.history.history_callbacks import history_page_callback
from src.ui import result_preview as preview
from src.twitch_api import videos
import test_vod_info as info_fixture


URL = 'https://static-cdn.jtvnw.net/cf_vods/example/thumb/custom-480x270.jpeg'
HTML = 'https://example.invalid/public_html/chat_'+'a'*32+'.html'


def make_context():
    counter = iter(range(100, 1000))
    bot = SimpleNamespace(**{name:AsyncMock() for name in ('send_message','edit_message_text','send_photo',
                          'edit_message_media','delete_message','edit_message_caption')})
    bot.send_message.side_effect = lambda **kw: SimpleNamespace(message_id=next(counter))
    bot.send_photo.side_effect = lambda **kw: SimpleNamespace(message_id=next(counter))
    return SimpleNamespace(bot=bot, user_data={}, application=SimpleNamespace(bot_data={'aiohttp':object()}))


class PreviewTests(unittest.IsolatedAsyncioTestCase):
    def _setupAsyncioRunner(self):
        self._asyncioRunner = asyncio.Runner(loop_factory=asyncio.SelectorEventLoop)

    def setUp(self):
        self.context = make_context()
        self.lookup = AsyncMock(return_value=URL)
        p = patch.object(preview, 'fetch_video_thumbnail', self.lookup)
        p.start(); self.addCleanup(p.stop)
        p = patch.object(vod, 'HTML_PUBLISH_MODE', 'public')
        p.start(); self.addCleanup(p.stop)
        self.rows = [dict(vod_id=str(i), title=f'VOD {i}', channel='Channel', fmt='html_online',
                         html_url=HTML, vod_url=f'https://www.twitch.tv/videos/{i}',history_id=i) for i in range(1,4)]
        for name, value in [('get_history_count', Mock(return_value=3)),
                            ('get_history_for_user', Mock(side_effect=lambda owner,limit,offset:self.rows[offset:offset+limit]))]:
            p = patch('src.database.database.'+name, value)
            p.start(); self.addCleanup(p.stop)
        p=patch('src.config.HISTORY_ENABLED', True);p.start();self.addCleanup(p.stop)
        self.ready = dict(vod_id='1',cache_id=42,meta=dict(title='Title',channel='Channel',thumbnail_preview_url=URL),
                          stats={'messages':3,'unique_users':2},html_ref=HTML)

    def query(self, page, message_id=None, owner=7):
        state = self.context.user_data.get('history_view', {})
        return SimpleNamespace(effective_user=SimpleNamespace(id=owner), callback_query=SimpleNamespace(
            data=f'ui:histpage:{page}', answer=AsyncMock(), message=SimpleNamespace(
                chat_id=10,message_id=message_id if message_id is not None else state.get('message_id',20))))

    async def open_history(self):
        await history._send_history_cards(10,self.context,self.rows[:1],0,3,owner_id=7)
        return self.context.user_data['history_view']

    async def test_ready_uses_metadata_preserves_links_and_history(self):
        message=SimpleNamespace(chat_id=10,message_id=20,photo=[],edit_text=AsyncMock())
        with patch.object(vod.results,'get_ready_result',return_value=self.ready), patch.object(vod.db,'add_user_history') as record:
            await chat.show_ready(message,self.context,7,'1')
        record.assert_called_once_with(7,42)
        self.lookup.assert_not_awaited()
        bot=self.context.bot
        bot.edit_message_text.assert_awaited_once()  # Links first.
        options=bot.edit_message_media.await_args.kwargs
        self.assertEqual(20,options['message_id'])
        self.assertEqual(URL,options['media'].media)
        self.assertEqual([HTML,'https://www.twitch.tv/videos/1'],[b.url for b in options['reply_markup'].inline_keyboard[0]])
        bot.send_message.assert_not_awaited()

    async def test_legacy_standalone_result_reuses_new_message_for_photo(self):
        item=vod._make_item(self.ready['meta'],{},'https://www.twitch.tv/videos/1','html_online')
        await vod._send_card_with_buttons(self.context,10,item,HTML)
        self.context.bot.send_message.assert_awaited_once()
        self.context.bot.edit_message_media.assert_awaited_once()
        self.assertEqual(100,self.context.bot.edit_message_media.await_args.kwargs['message_id'])
        self.lookup.assert_not_awaited()

    async def test_standalone_history_actions_bind_before_preview_and_expire(self):
        from src.history.history_files import _send_file_card
        state=await self.open_history()
        item=self.rows[0] | {'thumbnail_preview_url':URL}
        await _send_file_card(self.context,10,'file card',None,item,7)
        record=self.context.user_data['history_file_card']
        message=SimpleNamespace(chat_id=10,message_id=record['message_id'])
        self.assertTrue(history._current_message(self.context,message,7,1))
        self.assertFalse(history._current_message(self.context,message,8,1))
        await self.open_history()
        self.assertFalse(history._current_message(self.context,message,7,1))

    async def test_old_ready_looks_up_one_vod_and_failure_never_hides_html(self):
        item=vod._make_item({'title':'Old'}, {},'https://www.twitch.tv/videos/1','html_online')
        for error in (None,RuntimeError('SECRET')):
            with self.subTest(error=bool(error)):
                self.context=make_context();self.lookup.reset_mock();self.lookup.side_effect=error
                await vod._edit_progress_message_with_card(self.context,10,20,item,HTML)
                self.lookup.assert_awaited_once_with(self.context.application.bot_data['aiohttp'],'1')
                self.assertEqual(HTML,self.context.bot.edit_message_text.await_args.kwargs['reply_markup'].inline_keyboard[0][0].url)
                self.context.bot.send_message.assert_not_awaited()

    async def test_known_missing_or_unsafe_preview_does_not_fetch_or_hide_result(self):
        for url in (None,'','https://evil.invalid/private.jpg','https://vod-secure.twitch.tv/_404/404_processing_480x270.png'):
            with self.subTest(url=url):
                self.context=make_context()
                item={'vod_url':'https://www.twitch.tv/videos/1','thumbnail_preview_url':url}
                await vod._edit_progress_message_with_card(self.context,10,20,item,HTML)
                self.context.bot.edit_message_text.assert_awaited_once()
                self.context.bot.edit_message_media.assert_not_awaited()
        self.lookup.assert_not_awaited()

    async def test_photo_rejection_keeps_text_buttons_without_generation_or_extra_card(self):
        self.context.bot.edit_message_media.side_effect=BadRequest('SECRET')
        with patch.object(vod.results,'get_ready_result',return_value=self.ready),patch.object(vod.db,'add_user_history'),patch.object(vod,'_start_download',AsyncMock()) as start:
            await chat.show_ready(SimpleNamespace(chat_id=10,message_id=20,photo=[]),self.context,7,'1')
        start.assert_not_awaited()
        self.context.bot.edit_message_text.assert_awaited_once()
        self.context.bot.send_message.assert_not_awaited()
        self.assertEqual(HTML,self.context.bot.edit_message_text.await_args.kwargs['reply_markup'].inline_keyboard[0][0].url)

    async def test_stale_result_probe_never_applies_photo(self):
        entered,release=asyncio.Event(),asyncio.Event()
        async def slow(*args):
            entered.set();await release.wait();return URL
        self.lookup.side_effect=slow
        current=[True]
        task=asyncio.create_task(vod._edit_progress_message_with_card(self.context,10,20,{'vod_url':'https://www.twitch.tv/videos/1'},HTML,current=lambda:current[0]))
        try:
            await asyncio.wait_for(entered.wait(),1)
            self.context.bot.edit_message_text.assert_awaited_once()
            current[0]=False;release.set()
            await asyncio.wait_for(task,1)
            self.context.bot.edit_message_media.assert_not_awaited()
        finally:
            release.set();await asyncio.wait_for(task,1)

    async def test_caption_limit_preserves_full_text_and_skips_lookup(self):
        for text in ('x'*1025,'😀'*513,'&amp;'*300):
            self.context=make_context()
            with patch.object(vod,'_build_card',return_value=(text,None)):
                await vod._edit_progress_message_with_card(self.context,10,20,{'vod_url':'https://www.twitch.tv/videos/1'},HTML)
            self.assertEqual(text,self.context.bot.edit_message_text.await_args.kwargs['text'])
            self.context.bot.edit_message_media.assert_not_awaited()
        self.lookup.assert_not_awaited()
        self.assertTrue(preview.caption_fits('x'*1024))

    async def test_history_fetches_only_current_entry_and_photo_to_photo_keeps_id(self):
        state=await self.open_history();message_id=state['message_id']
        await history_page_callback(self.query(1),self.context)
        self.assertEqual(message_id,state['message_id'])
        self.assertTrue(state['photo'])
        self.assertEqual(['1','2'],[call.args[1] for call in self.lookup.await_args_list])
        self.assertIn('VOD 2',self.context.bot.edit_message_media.await_args.kwargs['media'].caption)
        self.context.bot.send_message.assert_awaited_once()

    async def test_history_photo_to_text_transfers_id_even_if_delete_fails_and_back(self):
        state=await self.open_history();old=state['message_id']
        self.lookup.return_value=''
        self.context.bot.delete_message.side_effect=BadRequest('cannot delete')
        await history_page_callback(self.query(1),self.context)
        self.assertNotEqual(old,state['message_id']);self.assertFalse(state['photo'])
        before=self.lookup.await_count
        q=self.query(2,message_id=old)
        await history_page_callback(q,self.context)
        q.callback_query.answer.assert_awaited_once()
        await history_page_callback(self.query(2,owner=8),self.context)
        self.assertEqual(before,self.lookup.await_count)
        self.lookup.return_value=URL
        await history_page_callback(self.query(0),self.context)
        self.assertTrue(state['photo'])
        self.assertEqual(1,state['history_id'])

    async def test_history_text_to_text_uses_same_message_and_keeps_navigation(self):
        self.lookup.return_value=''
        state=await self.open_history();old=state['message_id']
        await history_page_callback(self.query(1),self.context)
        self.assertEqual(old,state['message_id']);self.assertFalse(state['photo'])
        self.context.bot.send_message.assert_awaited_once()
        labels=[b.text for row in self.context.bot.edit_message_text.await_args.kwargs['reply_markup'].inline_keyboard for b in row]
        self.assertIn('2/3',labels);self.assertIn('🔗 Ссылка на VOD',labels)

    async def test_slow_history_probe_cannot_overwrite_newer_page(self):
        self.lookup.return_value=''
        state=await self.open_history()
        entered,release=asyncio.Event(),asyncio.Event()
        async def slow(http,vod_id):
            if vod_id=='2':
                entered.set();await release.wait()
            return URL
        self.lookup.side_effect=slow
        task=asyncio.create_task(history_page_callback(self.query(1),self.context))
        try:
            await asyncio.wait_for(entered.wait(),1)
            self.assertFalse(state['lock'].locked())
            await asyncio.wait_for(history_page_callback(self.query(2),self.context),1)
            edits=self.context.bot.edit_message_media.await_count
            release.set();await asyncio.wait_for(task,1)
            self.assertEqual(edits,self.context.bot.edit_message_media.await_count)
            self.assertEqual(3,state['history_id'])
            self.assertIn('VOD 3',self.context.bot.edit_message_media.await_args.kwargs['media'].caption)
        finally:
            release.set();await asyncio.wait_for(task,1)

    async def test_new_history_request_retires_pending_old_view(self):
        self.lookup.return_value=''
        old=await self.open_history()
        self.lookup.return_value=URL
        await self.open_history()
        before=self.lookup.await_count
        await history_page_callback(self.query(1,message_id=old['message_id']),self.context)
        self.assertEqual(before,self.lookup.await_count)

    async def test_cleanup_releases_lock_and_new_message_works_during_delay(self):
        state=await self.open_history()
        self.lookup.return_value=''
        entered,release=asyncio.Event(),asyncio.Event()
        old=state['message_id']
        async def slow_delete(**kwargs):
            self.assertNotEqual(old,state['message_id'])
            self.assertFalse(state['lock'].locked())
            entered.set();await release.wait()
        self.context.bot.delete_message.side_effect=slow_delete
        task=asyncio.create_task(history_page_callback(self.query(1),self.context))
        try:
            await asyncio.wait_for(entered.wait(),1)
            await asyncio.wait_for(history_page_callback(self.query(2),self.context),1)
            edits=self.context.bot.edit_message_text.await_count
            release.set();await asyncio.wait_for(task,1)
            self.assertEqual(3,state['history_id'])
            self.assertEqual(edits,self.context.bot.edit_message_text.await_count)
        finally:
            release.set();await asyncio.wait_for(task,1)

    async def test_history_photo_edit_failure_falls_back_and_rebinds(self):
        state=await self.open_history();old=state['message_id']
        self.context.bot.edit_message_media.side_effect=BadRequest('SECRET')
        await history_page_callback(self.query(1),self.context)
        self.assertFalse(state['photo']);self.assertNotEqual(old,state['message_id'])
        self.assertIn('VOD 2',self.context.bot.send_message.await_args.kwargs['text'])
        self.assertEqual(2,state['history_id'])

    async def test_failed_text_replacement_preserves_old_history_binding(self):
        state=await self.open_history();old=state['message_id']
        self.lookup.return_value=''
        self.context.bot.send_message.side_effect=BadRequest('SECRET')
        await history_page_callback(self.query(1),self.context)
        self.assertEqual(old,state['message_id']);self.assertTrue(state['photo'])
        self.assertEqual(1,state['history_id'])
        self.context.bot.delete_message.assert_not_awaited()

    async def test_cancelled_deletion_keeps_new_binding_and_navigation(self):
        state=await self.open_history();old=state['message_id']
        self.lookup.return_value=''
        entered=asyncio.Event()
        async def stuck(**kwargs):entered.set();await asyncio.Event().wait()
        self.context.bot.delete_message.side_effect=stuck
        task=asyncio.create_task(history_page_callback(self.query(1),self.context))
        await asyncio.wait_for(entered.wait(),1);task.cancel()
        with self.assertRaises(asyncio.CancelledError):await asyncio.wait_for(task,1)
        self.assertNotEqual(old,state['message_id'])
        self.assertFalse(state['lock'].locked())
        await asyncio.wait_for(history_page_callback(self.query(2),self.context),1)
        self.assertEqual(3,state['history_id'])

    async def test_metadata_timeout_is_bounded_and_cancellation_leaves_text(self):
        async def stuck(*args):await asyncio.Event().wait()
        self.lookup.side_effect=stuck
        real_timeout=asyncio.timeout
        with patch.object(preview.asyncio,'timeout',side_effect=lambda n:real_timeout(min(n,0.02))):
            await asyncio.wait_for(self.open_history(),1)
        self.assertFalse(self.context.user_data['history_view']['photo'])
        self.context.bot.send_message.assert_awaited_once()
        self.lookup.assert_awaited_once()

    async def test_initial_history_photo_failure_still_has_text_and_navigation(self):
        self.context.bot.edit_message_media.side_effect=BadRequest('SECRET')
        state=await self.open_history()
        self.assertFalse(state['photo'])
        self.context.bot.send_message.assert_awaited_once()
        self.context.bot.edit_message_text.assert_not_awaited()
        self.assertIn('1/3',str(self.context.bot.send_message.await_args.kwargs['reply_markup']))

    async def test_history_receives_full_list_uses_matching_page_metadata(self):
        await history._send_history_cards(10,self.context,self.rows,2,owner_id=7)
        self.lookup.assert_awaited_once_with(self.context.application.bot_data['aiohttp'],'3')
        self.assertIn('VOD 3',self.context.bot.edit_message_media.await_args.kwargs['media'].caption)

    async def test_metadata_cancellation_has_no_background_work_and_keeps_links(self):
        entered,stopped=asyncio.Event(),asyncio.Event()
        async def stuck(*args):
            entered.set()
            try:await asyncio.Event().wait()
            finally:stopped.set()
        self.lookup.side_effect=stuck
        task=asyncio.create_task(self.open_history())
        await asyncio.wait_for(entered.wait(),1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):await asyncio.wait_for(task,1)
        self.assertTrue(stopped.is_set())
        self.assertFalse(self.context.user_data['history_view']['photo'])
        self.context.bot.send_message.assert_awaited_once()

    async def test_missing_ready_photo_updates_caption_without_recreating_result(self):
        message=SimpleNamespace(chat_id=10,message_id=20,photo=[object()],edit_caption=AsyncMock(),edit_text=AsyncMock())
        with patch.object(vod.results,'get_ready_result',return_value=None),patch.object(vod,'_start_download',AsyncMock()) as start:
            await chat.show_ready(message,self.context,7,'1')
        message.edit_caption.assert_awaited_once()
        message.edit_text.assert_not_awaited();start.assert_not_awaited()
        self.lookup.assert_not_awaited()

    async def test_preview_failures_are_safe_diagnostic_categories(self):
        self.lookup.side_effect=RuntimeError('SECRET https://private.invalid user=123')
        with self.assertLogs('src.handlers.handlers_vod_info',level='INFO') as logs:
            self.assertIsNone(await preview.resolve_preview(self.context,{'vod_id':'1'}))
        self.assertEqual(['metadata_failed'],[r.args[0] for r in logs.records])
        self.assertNotIn('SECRET',str(logs.output))
        self.context.bot.edit_message_media.side_effect=BadRequest('SECRET')
        self.context.bot.edit_message_text.side_effect=BadRequest('SECRET')
        with self.assertLogs('src.handlers.handlers_vod_info',level='INFO') as logs:
            shown=await preview.render_card(self.context.bot,10,'card',None,URL,message_id=20)
        self.assertEqual(['photo_edit_failed','text_fallback'],[r.args[0] for r in logs.records])
        self.assertNotIn('SECRET',str(logs.output))
        self.assertFalse(shown.photo)

    async def test_single_vod_helix_identity_and_empty_response(self):
        for rows,expected in [([],''),([{'id':'1','thumbnail_url':URL}],URL)]:
            with patch.object(videos,'_get_json',AsyncMock(return_value={'data':rows})) as get:
                self.assertEqual(expected,await videos.fetch_video_thumbnail(object(),'1'))
                get.assert_awaited_once()
                self.assertEqual({'id':'1'},get.await_args.args[2])
        with patch.object(videos,'_get_json',AsyncMock(return_value={'data':[{'id':'2','thumbnail_url':URL}]})):
            with self.assertRaises(videos.TwitchAPIError):await videos.fetch_video_thumbnail(object(),'1')

    async def test_new_generation_uses_existing_completion_and_result_photo_path(self):
        publication=SimpleNamespace(ref=HTML,reserve=Mock(),complete=Mock(return_value=42),close=Mock(),fail=Mock())
        meta=self.ready['meta'].copy()
        with patch.object(vod.results,'Publication',return_value=publication),patch.object(vod,'download_and_send',AsyncMock(return_value=(meta,{},[],HTML))),patch.object(vod.db,'add_user_history') as record:
            await asyncio.wait_for(vod._runner(self.context,10,20,'https://www.twitch.tv/videos/1','1','html_online',7),2)
        publication.complete.assert_called_once();publication.fail.assert_not_called()
        record.assert_called_once_with(7,42)
        self.context.bot.edit_message_media.assert_awaited_once()
        self.lookup.assert_not_awaited()


    async def test_standalone_double_failure_keeps_original_text_and_both_links(self):
        item=vod._make_item(self.ready['meta'],{},'https://www.twitch.tv/videos/1','html_online')
        self.context.bot.edit_message_media.side_effect=BadRequest('photo rejected')
        self.context.bot.edit_message_text.side_effect=BadRequest('text rejected')
        await vod._send_card_with_buttons(self.context,10,item,HTML)
        self.context.bot.send_message.assert_awaited_once()
        self.context.bot.edit_message_media.assert_awaited_once()
        self.context.bot.edit_message_text.assert_not_awaited()
        self.context.bot.delete_message.assert_not_awaited()
        rows=self.context.bot.send_message.await_args.kwargs['reply_markup'].inline_keyboard
        self.assertEqual([HTML,'https://www.twitch.tv/videos/1'],[b.url for b in rows[0]])

    async def test_file_card_double_failure_preserves_bound_id_and_rejects_old_card(self):
        from src.history.history_files import _send_file_card
        await self.open_history()
        item=self.rows[0] | {'thumbnail_preview_url':URL}
        await _send_file_card(self.context,10,'first',None,item,7)
        old=self.context.user_data['history_file_card']['message_id']
        self.context.bot.send_message.reset_mock()
        self.context.bot.edit_message_media.side_effect=BadRequest('photo rejected')
        self.context.bot.edit_message_text.side_effect=BadRequest('text rejected')
        await _send_file_card(self.context,10,'second',None,item,7)
        record=self.context.user_data['history_file_card']
        self.assertNotEqual(old,record['message_id'])
        self.context.bot.send_message.assert_awaited_once()  # Only the intended new card.
        self.context.bot.edit_message_text.assert_not_awaited()
        self.context.bot.delete_message.assert_not_awaited()
        self.assertTrue(history._current_message(self.context,SimpleNamespace(chat_id=10,message_id=record['message_id']),7,1))
        self.assertFalse(history._current_message(self.context,SimpleNamespace(chat_id=10,message_id=old),7,1))

    async def test_delayed_failed_standalone_media_does_not_rebind_new_file_card(self):
        from src.history.history_files import _send_file_card
        entered,release=asyncio.Event(),asyncio.Event()
        async def slow(**kwargs):
            entered.set();await release.wait();raise BadRequest('photo rejected')
        self.context.bot.edit_message_media.side_effect=slow
        item=self.rows[0] | {'thumbnail_preview_url':URL}
        task=asyncio.create_task(_send_file_card(self.context,10,'old',None,item,7))
        try:
            await asyncio.wait_for(entered.wait(),1)
            await _send_file_card(self.context,10,'new',None,self.rows[1] | {'thumbnail_preview_url':''},7)
            newer=self.context.user_data['history_file_card'].copy()
            release.set();await asyncio.wait_for(task,1)
            self.assertEqual(newer,self.context.user_data['history_file_card'])
            self.assertEqual(2,self.context.bot.send_message.await_count)
            self.context.bot.edit_message_text.assert_not_awaited()
            self.context.bot.delete_message.assert_not_awaited()
        finally:
            release.set();await asyncio.wait_for(task,1)

    async def test_new_history_while_old_file_media_fails_does_not_restore_old_binding(self):
        from src.history.history_files import _send_file_card
        entered,release=asyncio.Event(),asyncio.Event()
        async def slow(**kwargs):
            entered.set();await release.wait();raise BadRequest('photo rejected')
        self.context.bot.edit_message_media.side_effect=slow
        task=asyncio.create_task(_send_file_card(self.context,10,'old',None,self.rows[0] | {'thumbnail_preview_url':URL},7))
        try:
            await asyncio.wait_for(entered.wait(),1)
            self.lookup.return_value=''
            state=await self.open_history()
            newer=state['message_id']
            release.set();await asyncio.wait_for(task,1)
            self.assertNotIn('history_file_card',self.context.user_data)
            self.assertEqual(newer,state['message_id'])
            self.context.bot.edit_message_text.assert_not_awaited()
        finally:
            release.set();await asyncio.wait_for(task,1)

    async def test_history_optional_media_double_failure_keeps_primary_page(self):
        self.context.bot.edit_message_media.side_effect=BadRequest('photo rejected')
        self.context.bot.edit_message_text.side_effect=BadRequest('text rejected')
        state=await self.open_history()
        self.assertEqual(100,state['message_id']);self.assertFalse(state['photo'])
        self.context.bot.send_message.assert_awaited_once()
        self.context.bot.edit_message_text.assert_not_awaited()
        self.context.bot.delete_message.assert_not_awaited()
        self.assertEqual(1,state['history_id'])

    async def test_cancelled_optional_media_keeps_standalone_binding(self):
        from src.history.history_files import _send_file_card
        entered=asyncio.Event()
        async def stuck(**kwargs):entered.set();await asyncio.Event().wait()
        self.context.bot.edit_message_media.side_effect=stuck
        task=asyncio.create_task(_send_file_card(self.context,10,'card',None,self.rows[0] | {'thumbnail_preview_url':URL},7))
        await asyncio.wait_for(entered.wait(),1)
        record=self.context.user_data['history_file_card'].copy()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):await asyncio.wait_for(task,1)
        self.assertEqual(record,self.context.user_data['history_file_card'])
        self.context.bot.send_message.assert_awaited_once()
        self.context.bot.delete_message.assert_not_awaited()

    async def test_history_deletion_timeout_keeps_replacement_binding(self):
        state=await self.open_history();old=state['message_id']
        self.lookup.return_value=''
        async def stuck(**kwargs):await asyncio.Event().wait()
        self.context.bot.delete_message.side_effect=stuck
        real_timeout=asyncio.timeout
        with patch.object(preview.asyncio,'timeout',side_effect=lambda n:real_timeout(min(n,0.02))):
            await asyncio.wait_for(history_page_callback(self.query(1),self.context),1)
        self.assertNotEqual(old,state['message_id'])
        self.assertFalse(state['photo']);self.assertEqual(2,state['history_id'])
        self.assertEqual(old,self.context.bot.delete_message.await_args.kwargs['message_id'])
        self.assertFalse(state['lock'].locked())


class HistoryPersistenceTests(unittest.IsolatedAsyncioTestCase):
    _setupAsyncioRunner=PreviewTests._setupAsyncioRunner

    async def test_real_sqlite_history_and_cache_unchanged_after_photo_navigation(self):
        from src.database import database as db, db_core
        context=make_context()
        with tempfile.TemporaryDirectory() as directory, patch.object(db_core,'DB_PATH',str(Path(directory)/'history.db')), patch('src.config.HISTORY_ENABLED',True):
            db.init_db()
            for n in (1,2):
                cache=db.upsert_cache(str(n),'html_online',f'https://www.twitch.tv/videos/{n}',{'title':f'VOD {n}','html_url':HTML},{'messages':3},[])
                db.add_user_history(7,cache)
            before=Path(db_core.DB_PATH).read_bytes()
            items=db.get_history_for_user(7,limit=1)
            with patch.object(preview,'fetch_video_thumbnail',AsyncMock(return_value=URL)):
                await history._send_history_cards(10,context,items,0,2,owner_id=7)
                state=context.user_data['history_view']
                q=SimpleNamespace(effective_user=SimpleNamespace(id=7),callback_query=SimpleNamespace(data='ui:histpage:1',answer=AsyncMock(),message=SimpleNamespace(chat_id=10,message_id=state['message_id'])))
                await history_page_callback(q,context)
            self.assertEqual(before,Path(db_core.DB_PATH).read_bytes())
            self.assertEqual(2,db.get_history_count(7))


class ChannelResultPreviewTests(unittest.IsolatedAsyncioTestCase):
    _setupAsyncioRunner=info_fixture.InfoTests._setupAsyncioRunner
    setUp=info_fixture.InfoTests.setUp
    asyncTearDown=info_fixture.InfoTests.asyncTearDown
    context_for_user=info_fixture.InfoTests.context_for_user
    task=info_fixture.InfoTests.task
    message=info_fixture.InfoTests.message
    update=info_fixture.InfoTests.update
    ready_result=info_fixture.InfoTests.ready_result
    open=info_fixture.InfoTests.open
    setup_list=info_fixture.InfoTests.setup_list
    show=info_fixture.InfoTests.show
    record=info_fixture.InfoTests.record
    process_update=info_fixture.InfoTests.process_update

    async def offer(self):
        from src.handlers import handlers_vod_info as info
        source,view=await self.setup_list()
        await self.show(source,view)
        self.ready.return_value=self.ready_result()
        self.ready.return_value['meta']['thumbnail_preview_url']=URL
        self.context.bot.edit_message_media=AsyncMock()
        await info.info_callback(self.process_update(view),self.context)
        rows=source.edit_text.await_args.kwargs['reply_markup'].inline_keyboard
        return source,view,rows

    async def test_info_to_ready_photo_preserves_html_and_rejects_old_back_and_regen(self):
        from src.handlers import handlers_channel_vods as lists
        source,view,rows=await self.offer()
        opening=self.update(rows[0][0].callback_data,source)
        await chat.chat_callback(opening,self.context)
        opening.callback_query.answer.assert_awaited_once()
        self.context.bot.edit_message_media.assert_awaited_once()
        self.assertNotIn(view.session_id,self.browser._sessions)
        before=source.edit_text.await_count
        back=self.update(rows[2][0].callback_data,source)
        await lists.channel_vods_callback(back,self.context)
        back.callback_query.answer.assert_awaited_once_with(lists.STALE_BUTTON)
        await chat.chat_callback(self.update(rows[1][0].callback_data,source),self.context)
        await chat.chat_callback(self.update(rows[0][0].callback_data,source),self.context)
        self.assertEqual(before,source.edit_text.await_count)
        self.runner.assert_not_awaited()
        source.reply_text.assert_not_awaited()

    async def test_back_to_list_during_preview_metadata_prevents_late_media_edit(self):
        from src.handlers import handlers_channel_vods as lists
        source,view,rows=await self.offer()
        self.ready.return_value['meta'].pop('thumbnail_preview_url')
        self.context.user_data['chat_choice']['thumbnail_url']=None  # Legacy offer without cached Helix metadata.
        entered,release=asyncio.Event(),asyncio.Event()
        async def slow(*args):entered.set();await release.wait();return URL
        with patch.object(preview,'fetch_video_thumbnail',AsyncMock(side_effect=slow)):
            task=self.task(chat.chat_callback(self.update(rows[0][0].callback_data,source),self.context))
            try:
                await asyncio.wait_for(entered.wait(),1)
                await asyncio.wait_for(lists.channel_vods_callback(self.update(rows[2][0].callback_data,source),self.context),1)
                release.set();await asyncio.wait_for(task,1)

                self.context.bot.edit_message_media.assert_not_awaited()
                self.assertIn('VODs канала',source.edit_text.await_args.args[0])
                source.reply_text.assert_not_awaited()
            finally:
                release.set();await asyncio.wait_for(task,1)

    async def test_old_ready_from_info_reuses_cached_thumbnail_without_helix(self):
        source,view,rows=await self.offer()
        self.ready.return_value['meta'].pop('thumbnail_preview_url')
        with patch.object(preview,'fetch_video_thumbnail',AsyncMock()) as lookup:
            await chat.chat_callback(self.update(rows[0][0].callback_data,source),self.context)
        lookup.assert_not_awaited()
        self.assertIn('480x270',self.context.bot.edit_message_media.await_args.kwargs['media'].media)

    async def test_primary_result_text_replacement_retires_callbacks_before_cleanup(self):
        from src.handlers import handlers_channel_vods as lists
        source,view,rows=await self.offer()
        self.context.bot.edit_message_text.side_effect=BadRequest('edit rejected')
        self.context.bot.send_message.return_value=SimpleNamespace(message_id=199)
        self.context.bot.delete_message=AsyncMock()
        async def deleted(**kwargs):
            self.assertEqual(source.message_id,kwargs['message_id'])
            self.assertNotIn(view.session_id,self.browser._sessions)
            self.assertTrue(self.context.user_data['chat_choice']['media_final'])
        self.context.bot.delete_message.side_effect=deleted
        await chat.chat_callback(self.update(rows[0][0].callback_data,source),self.context)
        self.context.bot.send_message.assert_awaited_once()
        self.context.bot.delete_message.assert_awaited_once()
        self.context.bot.edit_message_media.assert_not_awaited()
        old=self.update(rows[2][0].callback_data,source)
        await lists.channel_vods_callback(old,self.context)
        old.callback_query.answer.assert_awaited_once_with(lists.STALE_BUTTON)
        self.runner.assert_not_awaited()
