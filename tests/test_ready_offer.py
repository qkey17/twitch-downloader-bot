import asyncio
import html
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import test_stale_vod_lists as fixture
import test_channel_vods as cards
import test_result_registry as registry_fixture
from src.handlers import handlers_channel_vods as lists, handlers_chat as chat
from src.channel_browser import VodSelection
from src.vod_list import callback, build_list, VodListBrowser
from src.twitch_api import videos
from src.ui.ui_history import ready_offer_text, format_vod_summary, build_history_page


class ReadyOfferTests(unittest.IsolatedAsyncioTestCase):
    _setupAsyncioRunner=fixture.StaleListTests._setupAsyncioRunner
    setUp=fixture.StaleListTests.setUp
    asyncTearDown=fixture.StaleListTests.asyncTearDown
    context_for_user=fixture.StaleListTests.context_for_user
    task=fixture.StaleListTests.task
    message=fixture.StaleListTests.message
    update=fixture.StaleListTests.update
    ready_result=fixture.StaleListTests.ready_result
    open=fixture.StaleListTests.open

    async def setup_offer(self):
        thumbnail='https://static-cdn.jtvnw.net/cf_vods/example/thumb/thumb0-%{width}x%{height}.jpg'
        videos.fetch_archive_vods.return_value=([cards.vod(123456789,thumbnail_url=thumbnail),cards.vod(2,thumbnail_url=thumbnail)],None)
        source,view=await self.open(20)
        self.card=self.message(99)
        source.reply_photo=AsyncMock(return_value=self.card)
        self.context.bot.edit_message_media=AsyncMock()
        self.available={}
        self.ready.side_effect=lambda vod_id:self.available.get(vod_id)
        return source,view

    async def pick(self,source,view,index=0):
        data=build_list(view)[1].inline_keyboard[index-view.page*5][0].callback_data
        q=self.update(data,source)
        result=await lists.channel_vods_callback(q,self.context)
        q.callback_query.answer.assert_awaited_once()
        return result

    async def test_date_ready_offer_is_immediate_text_only_without_history_or_api(self):
        source,view=await self.setup_offer()
        ready=self.available['123456789']=self.ready_result()
        ready['meta'].update(channel='Channel',title='<Title&>',length_seconds=3723,created_at='2026-01-02T03:04:00Z')
        ready['stats'].update(messages=100,unique_users=20)
        with patch.object(chat.vod.db,'add_user_history') as history:
            selected=await self.pick(source,view)
        self.assertIsInstance(selected,VodSelection)
        self.assertEqual(ready_offer_text(ready),source.edit_text.await_args.args[0])
        rows=source.edit_text.await_args.kwargs['reply_markup'].inline_keyboard
        self.assertEqual(['Открыть HTML','Сгенерировать заново','Вернуться к списку'],[r[0].text for r in rows])
        self.assertEqual('HTML',source.edit_text.await_args.kwargs['parse_mode'])
        history.assert_not_called();self.metadata.assert_not_awaited();self.runner.assert_not_awaited()
        source.reply_photo.assert_not_awaited();source.reply_text.assert_not_awaited()
        self.context.bot.edit_message_media.assert_not_awaited()
        videos.fetch_archive_vods.assert_awaited_once()
        self.assertTrue(self.browser.is_current(selected.view))

    async def test_uncached_date_still_opens_information_without_starting_download(self):
        source,view=await self.setup_offer()
        await self.pick(source,view)
        self.assertIsNotNone(self.browser._sessions[view.session_id].info_card)
        info_message=source.reply_photo.await_args.kwargs['reply_markup']
        self.assertEqual(['Обработать чат','Вернуться к списку'],[r[0].text for r in info_message.inline_keyboard])
        self.assertNotIn('chat_choice',self.context.user_data)
        self.runner.assert_not_awaited();self.metadata.assert_not_awaited()

    async def test_back_choose_other_and_old_callbacks_do_not_open_wrong_result(self):
        source,view=await self.setup_offer()
        self.available['123456789']=self.ready_result()
        self.available['2']=self.ready_result() | {'vod_id':'2','meta':{'title':'Second'}}
        await self.pick(source,view)
        old_rows=source.edit_text.await_args.kwargs['reply_markup'].inline_keyboard
        back=await lists.channel_vods_callback(self.update(old_rows[2][0].callback_data,source),self.context)
        await self.pick(source,back,1)
        self.assertIn('Second',source.edit_text.await_args.args[0])
        self.assertEqual('2',self.context.user_data['chat_choice']['vod_id'])
        before=self.context.bot.edit_message_text.await_count
        for row in old_rows[:2]:await chat.chat_callback(self.update(row[0].callback_data,source),self.context)
        self.assertEqual(before,self.context.bot.edit_message_text.await_count)
        self.runner.assert_not_awaited()
        source.reply_text.assert_not_awaited()

    async def test_repeat_foreign_and_wrong_message_dates_are_rejected(self):
        source,view=await self.setup_offer()
        self.available['123456789']=self.ready_result()
        data=build_list(view)[1].inline_keyboard[0][0].callback_data
        for q in (self.update(data,source,owner=8),self.update(data,self.message(21))):
            await lists.channel_vods_callback(q,self.context)
            q.callback_query.answer.assert_awaited_once_with(lists.STALE_BUTTON)
        self.ready.assert_not_called()
        await self.pick(source,view)
        before=source.edit_text.await_count
        repeated=self.update(data,source)
        await lists.channel_vods_callback(repeated,self.context)
        repeated.callback_query.answer.assert_awaited_once_with(lists.STALE_BUTTON)
        self.assertEqual(before,source.edit_text.await_count)

    async def test_disappearing_ready_after_date_never_starts_generation(self):
        source,view=await self.setup_offer()
        self.ready.side_effect=[self.ready_result(),None]
        await self.pick(source,view)
        self.assertIn('недоступен',source.edit_text.await_args.args[0])
        self.assertEqual('Вернуться к списку',source.edit_text.await_args.kwargs['reply_markup'].inline_keyboard[0][0].text)
        self.runner.assert_not_awaited();self.metadata.assert_not_awaited()

    async def test_direct_ready_open_preserves_preview_order_and_terminal_fencing(self):
        source,view=await self.setup_offer()
        self.available['123456789']=self.ready_result()
        await self.pick(source,view)
        rows=source.edit_text.await_args.kwargs['reply_markup'].inline_keyboard
        self.context.bot.edit_message_media.side_effect=lambda **kw:self.context.bot.edit_message_text.assert_awaited_once()
        with patch.object(chat.vod.config,'HISTORY_ENABLED',True),patch.object(chat.vod.db,'add_user_history') as history:
            await chat.chat_callback(self.update(rows[0][0].callback_data,source),self.context)
        history.assert_called_once_with(7,1)
        self.context.bot.edit_message_media.assert_awaited_once()
        self.assertNotIn(view.session_id,self.browser._sessions)
        self.runner.assert_not_awaited();source.reply_text.assert_not_awaited()

    async def test_regenerate_uses_existing_runner_once(self):
        source,view=await self.setup_offer()
        self.available['123456789']=self.ready_result()
        await self.pick(source,view)
        rows=source.edit_text.await_args.kwargs['reply_markup'].inline_keyboard
        for _ in range(2):await chat.chat_callback(self.update(rows[1][0].callback_data,source),self.context)
        await asyncio.gather(*self.tasks)
        self.runner.assert_awaited_once()
        self.assertEqual(source.message_id,self.runner.await_args.kwargs['progress_message_id'])

    async def test_slow_offer_and_back_are_serialized_without_late_overwrite(self):
        source,view=await self.setup_offer()
        self.available['123456789']=self.ready_result()
        entered,release=asyncio.Event(),asyncio.Event()
        async def slow(text,**kwargs):
            if text.startswith('Готовый HTML доступен.'):
                entered.set();await release.wait()
        source.edit_text.side_effect=slow
        selecting=self.task(self.pick(source,view))
        try:
            await asyncio.wait_for(entered.wait(),1)
            selected=self.browser._view(self.browser._sessions[view.session_id])
            back=self.task(lists.channel_vods_callback(self.update(callback(selected,'back'),source),self.context))
            await asyncio.sleep(0);self.assertFalse(back.done())
            release.set();await asyncio.wait_for(asyncio.gather(selecting,back),1)
            self.assertIn('VODs канала',source.edit_text.await_args.args[0])
            self.assertNotIn('chat_choice',self.context.user_data)
        finally:release.set();await asyncio.wait_for(selecting,1)


class ReadyFormattingTests(unittest.TestCase):
    def test_summary_matches_existing_format_and_escapes(self):
        ready={'meta':dict(channel='C<&>',title='T<&>',created_at='2026-01-02T03:04:00Z',length_seconds=3723),
               'stats':dict(messages=1234,unique_users=56)}
        text=ready_offer_text(ready)
        self.assertEqual('Готовый HTML доступен.\n\n🟣 C&lt;&amp;&gt;\n🎬 T&lt;&amp;&gt;\n⏱ 01:02:03 • 💬 1234 • 👥 56\n🗓 02 Янв 2026 03:04 UTC',text)
        item=ready['meta'] | ready['stats']
        self.assertIn(format_vod_summary(item),build_history_page([item],0,1)[0][0][0])

    def test_missing_is_unknown_but_zero_is_real(self):
        self.assertIn('💬 — • 👥 —',ready_offer_text({}))
        self.assertIn('💬 0 • 👥 0',ready_offer_text({'stats':{'messages':0,'unique_users':0}}))
        self.assertIn('💬 — • 👥 —',ready_offer_text({'stats':{'messages':'<bad>','unique_users':-1}}))

    def test_long_card_stays_valid_within_text_limit(self):
        text=ready_offer_text({'meta':{'channel':'😀'*9999,'title':'<&>😀'*9999,'length_seconds':10**4000},'stats':{}})
        self.assertLess(len(html.unescape(text).encode('utf-16-le'))//2,4096)
        self.assertNotIn('<&>',text)


class RegistryReadyOfferTests(unittest.IsolatedAsyncioTestCase):
    _setupAsyncioRunner=fixture.StaleListTests._setupAsyncioRunner
    setUp=registry_fixture.ResultRegistryTestCase.setUp
    plan=registry_fixture.ResultRegistryTestCase.plan
    publish=registry_fixture.ResultRegistryTestCase.publish

    async def test_real_registry_local_public_missing_and_invalid_html(self):
        for mode in ('local','public'):
            for state in ('available','missing','invalid'):
                with self.subTest(mode=mode,state=state):
                    plan=self.plan(mode=mode)
                    self.publish(plan);plan.complete();plan.close()
                    if state=='missing':plan.path().unlink()
                    if state=='invalid':plan.path().write_text('broken',encoding='utf-8')
                    browser=VodListBrowser()
                    source=SimpleNamespace(chat_id=10,message_id=20,edit_text=AsyncMock(),reply_text=AsyncMock(return_value=SimpleNamespace(message_id=99)),reply_photo=AsyncMock())
                    context=SimpleNamespace(application=SimpleNamespace(bot_data={'aiohttp':object(),lists._STORE_KEY:browser}),user_data={})
                    with patch.object(videos,'fetch_user_by_login',AsyncMock(return_value=videos.TwitchUser('123','channel','Channel'))),patch.object(videos,'fetch_archive_vods',AsyncMock(return_value=([cards.vod(123456789)],None))):
                        view=await lists._open_and_show(source,context,7,'channel')
                    query=SimpleNamespace(data=build_list(view)[1].inline_keyboard[0][0].callback_data,message=source,answer=AsyncMock())
                    await lists.channel_vods_callback(SimpleNamespace(callback_query=query,effective_user=SimpleNamespace(id=7)),context)
                    if state=='available':
                        self.assertTrue(source.edit_text.await_args.args[0].startswith('Готовый HTML доступен.'))
                        source.reply_text.assert_not_awaited()
                    else:
                        self.assertNotIn('chat_choice',context.user_data)
                        source.reply_text.assert_awaited_once()
