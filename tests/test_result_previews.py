from callback_assertions import assert_stale
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


    async def test_caption_limit_preserves_full_text_and_skips_lookup(self):
        for text in ('x'*1025,'😀'*513,'&amp;'*300):
            self.context=make_context()
            with patch.object(vod,'_build_card',return_value=(text,None)):
                await vod._edit_progress_message_with_card(self.context,10,20,{'vod_url':'https://www.twitch.tv/videos/1'},HTML)
            self.assertEqual(text,self.context.bot.edit_message_text.await_args.kwargs['text'])
            self.context.bot.edit_message_media.assert_not_awaited()
        self.lookup.assert_not_awaited()
        self.assertTrue(preview.caption_fits('x'*1024))


    async def test_missing_ready_photo_updates_caption_without_recreating_result(self):
        message=SimpleNamespace(chat_id=10,message_id=20,photo=[object()],edit_caption=AsyncMock(),edit_text=AsyncMock())
        with patch.object(vod.results,'get_ready_result',return_value=None),patch.object(vod,'_start_download',AsyncMock()) as start:
            await chat.show_ready(message,self.context,7,'1')
        message.edit_caption.assert_awaited_once()
        message.edit_text.assert_not_awaited();start.assert_not_awaited()
        self.lookup.assert_not_awaited()


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


    async def test_ready_result_edits_original_media_and_keeps_links_history(self):
        with patch.object(vod.results,'get_ready_result',return_value=self.ready),patch.object(vod.db,'add_user_history') as record:
            await chat.show_ready(SimpleNamespace(chat_id=10,message_id=20,photo=[]),self.context,7,'1')
        record.assert_called_once_with(7,42)
        args=self.context.bot.edit_message_media.await_args.kwargs
        self.assertEqual(20,args['message_id']);self.assertEqual(URL,args['media'].media)
        self.assertEqual([HTML,'https://www.twitch.tv/videos/1'],[b.url for row in args['reply_markup'].inline_keyboard for b in row])
        self.context.bot.send_photo.assert_not_awaited();self.context.bot.send_message.assert_not_awaited();self.context.bot.delete_message.assert_not_awaited()

    async def test_history_missing_preview_uses_cover_and_keeps_same_message_navigation(self):
        state=await self.open_history();mid=state['message_id'];self.lookup.return_value=''
        await history_page_callback(self.query(1),self.context)
        self.assertEqual(mid,state['message_id']);self.assertTrue(state['photo'])
        args=self.context.bot.edit_message_media.await_args.kwargs
        self.assertEqual('vod.png',args['media'].media.filename)
        self.assertIn('VOD 2',args['media'].caption);self.assertIn('2/3',str(args['reply_markup']))
        self.context.bot.send_photo.assert_awaited_once();self.context.bot.delete_message.assert_not_awaited()

    async def test_history_pages_reuse_photo_and_reject_foreign_owners(self):
        state=await self.open_history();mid=state['message_id']
        await history_page_callback(self.query(1),self.context)
        self.assertEqual(mid,state['message_id']);self.assertEqual(2,state['history_id'])
        before=self.lookup.await_count
        await history_page_callback(self.query(2,owner=8),self.context)
        self.assertEqual(before,self.lookup.await_count)
        self.context.bot.send_photo.assert_awaited_once();self.context.bot.send_message.assert_not_awaited()

    async def test_slow_history_lookup_is_fenced_by_newer_page(self):
        state=await self.open_history();entered,release=asyncio.Event(),asyncio.Event()
        async def lookup(http,v):
            if v=='2':entered.set();await release.wait()
            return URL
        self.lookup.side_effect=lookup
        old=asyncio.create_task(history_page_callback(self.query(1),self.context))
        try:
            await asyncio.wait_for(entered.wait(),1)
            await history_page_callback(self.query(2),self.context)
            before=self.context.bot.edit_message_media.await_count+self.context.bot.edit_message_caption.await_count
            release.set();await old
            self.assertEqual(before,self.context.bot.edit_message_media.await_count+self.context.bot.edit_message_caption.await_count)
            self.assertEqual(3,state['history_id'])
        finally:release.set();await old

    async def test_independent_history_cards_keep_own_navigation(self):
        a=await self.open_history();b=await self.open_history()
        await history_page_callback(self.query(1,message_id=a['message_id']),self.context)
        self.assertEqual(2,a['history_id']);self.assertEqual(1,b['history_id'])

    async def test_metadata_timeout_uses_cover_and_never_leaks_sensitive_errors(self):
        self.lookup.side_effect=TimeoutError('SYNTHETIC_SECRET')
        with self.assertLogs('src.handlers.handlers_vod_info',level='INFO') as logs:
            state=await self.open_history()
        self.assertTrue(state['photo']);self.assertNotIn('SYNTHETIC_SECRET',str(logs.output))
        self.assertEqual('vod.png',self.context.bot.send_photo.await_args.kwargs['photo'].filename)

    async def test_definite_image_refusal_uses_text_once_with_links(self):
        self.context.bot.edit_message_media.side_effect=BadRequest('Unsupported image')
        await vod._edit_progress_message_with_card(self.context,10,20,self.rows[0]|{'thumbnail_preview_url':URL},HTML)
        self.assertEqual(2,self.context.bot.edit_message_media.await_count)
        self.context.bot.edit_message_text.assert_awaited_once();self.context.bot.send_message.assert_not_awaited()
        self.assertIn(HTML,str(self.context.bot.edit_message_text.await_args.kwargs['reply_markup']))

    async def test_ambiguous_edit_does_not_replace_or_delete(self):
        self.context.bot.edit_message_media.side_effect=TimeoutError()
        self.assertFalse(await vod._edit_progress_message_with_card(self.context,10,20,self.rows[0]|{'thumbnail_preview_url':URL},HTML))
        self.context.bot.edit_message_media.assert_awaited_once();self.context.bot.send_message.assert_not_awaited();self.context.bot.delete_message.assert_not_awaited()

    async def test_file_action_from_history_reuses_source_message(self):
        from src.history import history_files
        item=self.rows[0]|{'thumbnail_preview_url':URL}
        await history_files._send_file_card(self.context,10,'card',None,item,7,message_id=20,photo=True)
        self.assertEqual(20,self.context.user_data['history_file_card']['message_id'])
        self.context.bot.send_message.assert_not_awaited();self.context.bot.send_photo.assert_not_awaited()

    async def test_file_lookup_cannot_overwrite_newer_history_page(self):
        from src.history import history_files
        from src.ui.card_context import for_message
        state=await self.open_history();entered,release=asyncio.Event(),asyncio.Event()
        scoped=for_message(self.context,7,10,state['message_id'])
        async def lookup(http,v):
            if v=='1':entered.set();await release.wait()
            return URL
        self.lookup.side_effect=lookup
        task=asyncio.create_task(history_files._send_file_card(scoped,10,'old file card',None,self.rows[0],7,
                                                              message_id=state['message_id'],photo=True))
        try:
            await asyncio.wait_for(entered.wait(),1)
            await history_page_callback(self.query(1),self.context)
            before=self.context.bot.edit_message_caption.await_count+self.context.bot.edit_message_media.await_count
            release.set();await task
            self.assertEqual(before,self.context.bot.edit_message_caption.await_count+self.context.bot.edit_message_media.await_count)
            self.assertEqual(2,state['history_id'])
            self.assertNotIn('history_file_card',scoped.user_data)
        finally:release.set();await task

    async def test_saved_live_history_reuses_metadata_and_cover_without_live_lookup(self):
        saved=self.ready | {'meta':{'captured_during_live':True,'thumbnail_preview_url':''}}
        with patch('src.ui.html_file_action.lookup',return_value=saved):
            state=await self.open_history()
        self.assertTrue(state['photo'])
        self.assertEqual('live-vod.png',self.context.bot.send_photo.await_args.kwargs['photo'].filename)
        self.lookup.assert_not_awaited()


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

    async def offer(self, photo=True):
        from src.handlers import handlers_vod_info as info
        source,view=await self.setup_list()
        if photo:await self.show(source,view)
        else:
            original_media=self.context.bot.edit_message_media.side_effect
            self.context.bot.edit_message_media.side_effect=BadRequest('Unsupported fixture preview')
            await self.show(source,view)
            self.context.bot.edit_message_media.side_effect=original_media
        self.ready.side_effect=None
        self.ready.return_value=self.ready_result()
        self.ready.return_value['meta']['thumbnail_preview_url']=URL
        self.context.bot.edit_message_media=AsyncMock(side_effect=None if photo else BadRequest('Synthetic text fallback'))
        await info.info_callback(self.process_update(view),self.context)
        source=self.by_id[self.context.user_data['chat_choice']['message']]
        rows=source.keyboard.inline_keyboard
        self.assertEqual(photo, self.context.user_data['chat_choice']['photo'])
        self.context.bot.edit_message_media.reset_mock(side_effect=True)
        return source,view,rows

    async def test_info_to_ready_photo_preserves_html_and_rejects_old_back_and_regen(self):
        from src.handlers import handlers_channel_vods as lists
        source,view,rows=await self.offer()
        opening=self.update(rows[0][0].callback_data,source)
        await chat.chat_callback(opening,self.context)
        opening.callback_query.answer.assert_awaited_once()
        self.context.bot.send_photo.assert_not_awaited()
        self.assertNotIn(view.session_id,self.browser._sessions)
        before=source.edit_text.await_count
        back=self.update(rows[2][0].callback_data,source)
        await lists.channel_vods_callback(back,self.context)
        assert_stale(self, back)
        await chat.chat_callback(self.update(rows[1][0].callback_data,source),self.context)
        await chat.chat_callback(self.update(rows[0][0].callback_data,source),self.context)
        self.assertEqual(before,source.edit_text.await_count)
        self.runner.assert_not_awaited()
        source.reply_text.assert_not_awaited()

    async def test_back_to_list_during_preview_metadata_prevents_late_media_edit(self):
        from src.handlers import handlers_channel_vods as lists
        source,view,rows=await self.offer(photo=False)
        self.ready.return_value['meta'].pop('thumbnail_preview_url')
        self.context.user_data['chat_choice']['thumbnail_url']=None  # Legacy offer without cached Helix metadata.
        entered,release=asyncio.Event(),asyncio.Event()
        self.context.bot.edit_message_media.reset_mock()
        async def slow(*args):entered.set();await release.wait();return URL
        with patch.object(preview,'fetch_video_thumbnail',AsyncMock(side_effect=slow)):
            task=self.task(chat.chat_callback(self.update(rows[0][0].callback_data,source),self.context))
            try:
                await asyncio.wait_for(entered.wait(),1)
                await asyncio.wait_for(lists.channel_vods_callback(self.update(rows[2][0].callback_data,source),self.context),1)
                before=self.context.bot.edit_message_media.await_count
                release.set();await asyncio.wait_for(task,1)

                self.assertEqual(before,self.context.bot.edit_message_media.await_count)
                self.assertNotIn('Время указано по UTC',self.context.bot.edit_message_media.await_args.kwargs['media'].caption)
                source.reply_text.assert_not_awaited()
            finally:
                release.set();await asyncio.wait_for(task,1)

    async def test_old_ready_from_info_reuses_cached_thumbnail_without_helix(self):
        source,view,rows=await self.offer()
        self.ready.return_value['meta'].pop('thumbnail_preview_url')
        with patch.object(preview,'fetch_video_thumbnail',AsyncMock()) as lookup:
            await chat.chat_callback(self.update(rows[0][0].callback_data,source),self.context)
        lookup.assert_not_awaited()
        self.assertEqual(source.message_id,self.context.user_data['chat_choice']['message'])
