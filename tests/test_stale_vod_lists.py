from callback_assertions import assert_stale
"""Independent bounded lists, no Telegram/Twitch or database access."""
import asyncio
from unittest.mock import AsyncMock, patch
import unittest
import test_channel_selection as fixture
from src.handlers import handlers_channel_vods as h
from src.vod_list import VodListBrowser, callback, build_list
from src.ui.ui_history import ready_offer_text


class StaleListTests(unittest.IsolatedAsyncioTestCase):
    _setupAsyncioRunner = fixture.ChannelSelectionTests._setupAsyncioRunner
    asyncTearDown = fixture.ChannelSelectionTests.asyncTearDown
    context_for_user = fixture.ChannelSelectionTests.context_for_user
    task = fixture.ChannelSelectionTests.task
    message = fixture.ChannelSelectionTests.message
    update = fixture.ChannelSelectionTests.update
    ready_result = fixture.ChannelSelectionTests.ready_result

    def setUp(self):
        fixture.ChannelSelectionTests.setUp(self)
        self.browser = VodListBrowser(clock=lambda: self.now)
        self.app.bot_data[h._STORE_KEY] = self.browser

    async def open(self, number, login='channel', owner=7):
        message = self.message(number)
        view = await h._open_and_show(message, self.context, owner, login)
        return message, view

    async def test_new_lists_preserve_existing_text_keyboard_and_sessions(self):
        old,a=await self.open(20);text,keyboard=old.edit_text.await_args.args[0],old.edit_text.await_args.kwargs['reply_markup']
        await self.open(21,'second');await self.open(22,'third')
        old.edit_text.assert_awaited_once()
        self.assertEqual(text,old.edit_text.await_args.args[0]);self.assertIs(keyboard,old.edit_text.await_args.kwargs['reply_markup'])
        self.assertTrue(self.browser.is_current(a))
        current=await h.channel_vods_callback(self.update(callback(a,'pages'),old),self.context)
        self.assertTrue(self.browser.is_current(current))

    async def test_expired_and_restart_callbacks_answer_once_without_edit_even_if_busy(self):
        old,a=await self.open(20);new,b=await self.open(21)
        self.now+=601;self.context.user_data['vod_busy']=True
        before=old.edit_text.await_count
        q=self.update(callback(a,'pages'),old);await h.channel_vods_callback(q,self.context);assert_stale(self,q)
        self.assertEqual(before,old.edit_text.await_count)
        self.app.bot_data[h._STORE_KEY]=VodListBrowser()
        q=self.update(callback(b,'pages'),new);await h.channel_vods_callback(q,self.context);assert_stale(self,q)
        new.reply_text.assert_not_awaited()

    async def test_rejected_request_does_not_expire_valid_list(self):
        message, a = await self.open(20)
        await self.open(21, 'invalid login')
        self.assertTrue(self.browser.is_current(a))
        self.assertEqual(1, message.edit_text.await_count)
        result = await h.channel_vods_callback(self.update(callback(a, 'pages'), message), self.context)
        self.assertTrue(self.browser.is_current(result))

    async def test_foreign_owner_cannot_edit_or_invalidate_list(self):
        message, a = await self.open(20)
        q = self.update(callback(a, 'select', 0), message, owner=8)
        await h.channel_vods_callback(q, self.context)
        assert_stale(self, q)
        await self.open(21, owner=8)
        self.assertTrue(self.browser.is_current(a))
        self.assertEqual(1, message.edit_text.await_count)

    async def test_selected_html_message_is_never_replaced_by_old_list(self):
        message, a = await self.open(20)
        self.ready.return_value = self.ready_result()
        await h.channel_vods_callback(self.update(callback(a, 'select', 0), message), self.context)
        calls = message.edit_text.await_count
        await self.open(21)
        self.assertEqual(calls, message.edit_text.await_count)
        self.assertEqual(ready_offer_text(self.ready_result()), message.edit_text.await_args.kwargs['text'])

    async def test_started_processing_and_final_card_are_not_rewritten(self):
        message, a = await self.open(20)
        await h.channel_vods_callback(self.update(callback(a, 'select', 0), message), self.context)
        await asyncio.gather(*self.tasks)
        await asyncio.sleep(0)
        calls = message.edit_text.await_count
        await self.open(21)
        self.assertEqual(calls, message.edit_text.await_count)

    async def test_uneditable_old_message_does_not_break_new_session(self):
        message, a = await self.open(20)
        message.edit_text.side_effect = RuntimeError('unavailable')
        new, b = await self.open(21)
        self.assertTrue(self.browser.is_current(b))
        self.assertIsNotNone(new.edit_text.await_args.kwargs['reply_markup'])

    async def test_all_selected_card_states_are_outside_list_ownership(self):
        for number, text in enumerate(('VOD selected', 'HTML offer', 'Progress', 'HTML result', 'Processing error'), 30):
            with self.subTest(state=text):
                message, a = await self.open(number)
                async def selected(message, *args, **kwargs):
                    await message.edit_text(text)
                with patch.object(h, 'prepare_chat', side_effect=selected):
                    await h.channel_vods_callback(self.update(callback(a, 'select', 0), message), self.context)
                calls = message.edit_text.await_count
                await self.open(number+100)
                self.assertEqual(calls, message.edit_text.await_count)
                self.assertEqual(text, message.edit_text.await_args.args[0])

    async def test_back_restores_list_and_new_list_leaves_it_interactive(self):
        message,a=await self.open(20);self.ready.return_value=self.ready_result()
        selected=await h.channel_vods_callback(self.update(callback(a,'select',0),message),self.context)
        restored=await h.channel_vods_callback(self.update(callback(selected.view,'back'),message),self.context)
        before=message.edit_text.await_count
        await self.open(21);self.assertEqual(before,message.edit_text.await_count)
        self.assertTrue(self.browser.is_current(restored))

    async def test_opening_new_lists_never_touches_or_waits_for_old_ui(self):
        old,a=await self.open(20)
        async def forbidden(*a,**kw):raise AssertionError('New list must not edit old message')
        old.edit_text.side_effect=forbidden
        session=self.browser._sessions[a.session_id]
        async with h._ui_lock(session):
            new,b=await asyncio.wait_for(self.open(21),1)
            other,c=await asyncio.wait_for(self.open(22,owner=8),1)
        old.edit_text.assert_awaited_once()
        self.assertTrue(self.browser.is_current(a));self.assertTrue(self.browser.is_current(b));self.assertTrue(self.browser.is_current(c))
        self.assertIsNotNone(new.edit_text.await_args.kwargs['reply_markup'])




    async def test_navigation_edit_is_serialized_before_replacement(self):
        message, a = await self.open(20)
        entered, release = asyncio.Event(), asyncio.Event()
        async def slow(*args, **kwargs):
            if not entered.is_set():
                entered.set()
                await release.wait()
        message.edit_text.side_effect = slow
        nav = self.task(h.channel_vods_callback(self.update(callback(a, 'pages'), message), self.context))
        await asyncio.wait_for(entered.wait(), 1)
        newer = self.task(self.open(21))
        await asyncio.sleep(0)
        release.set()
        await asyncio.wait_for(asyncio.gather(nav, newer), 2)
        self.assertNotIn(h.STALE_LIST,str(message.edit_text.await_args))
        self.assertIsNotNone(message.edit_text.await_args.kwargs['reply_markup'])

    async def test_selection_during_new_list_loading_uses_its_own_message(self):
        message,a=await self.open(20);entered,release=asyncio.Event(),asyncio.Event()
        from src.twitch_api import videos
        original=videos.fetch_archive_vods.return_value
        async def slow(*args):entered.set();await release.wait();return original
        videos.fetch_archive_vods.side_effect=slow
        newer=self.task(self.open(21));await asyncio.wait_for(entered.wait(),1)
        q=self.update(callback(a,'select',0),message)
        await asyncio.wait_for(h.channel_vods_callback(q,self.context),1)
        q.callback_query.answer.assert_awaited_once_with()
        self.metadata.assert_awaited_once();release.set();await newer
        self.assertNotIn(h.STALE_LIST,str(message.edit_text.await_args))
