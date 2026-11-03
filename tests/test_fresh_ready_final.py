import asyncio
import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock,patch
from telegram.error import BadRequest
import test_html_file_ui as fixture
from src.handlers import handlers_chat as chat,handlers_vod as vod
from src.ui import html_file_action as action,result_preview as preview


class FreshFinal(unittest.IsolatedAsyncioTestCase):
    setUp=fixture.FileUiTests.setUp
    update=fixture.FileUiTests.update
    def _setupAsyncioRunner(self):
        self._asyncioRunner=asyncio.Runner(loop_factory=asyncio.SelectorEventLoop)

    async def test_fresh_photo_and_text_bind_generation_and_retire_offer(self):
        for photo in (False,True):
            self.bot.send_photo=AsyncMock(return_value=NS(message_id=21))
            self.bot.send_message.reset_mock();self.bot.delete_message.reset_mock()
            preview.resolve_preview.return_value='https://static-cdn.jtvnw.net/cf_vods/x/test.jpg' if photo else None
            message=NS(chat_id=1,message_id=10,photo=[],edit_text=AsyncMock())
            with patch.object(vod.results,'get_ready_result',return_value=fixture.READY):
                self.assertTrue(await chat.show_ready(message,self.context,7,'123'))
            self.bot.edit_message_text.assert_not_awaited();self.bot.edit_message_media.assert_not_awaited()
            markup=(self.bot.send_photo if photo else self.bot.send_message).await_args.kwargs['reply_markup']
            self.bot.delete_message.assert_awaited_once_with(chat_id=1,message_id=10)
            self.sender.send.reset_mock()
            await action.file_callback(self.update(message=10,markup=fixture.Markup([])),self.context)
            self.sender.send.assert_not_awaited()
            await action.file_callback(self.update(message=21 if photo else 20,markup=markup),self.context)
            self.sender.send.assert_awaited_once_with(7,1,'123',generation_id='a'*32)

    async def test_photo_rejection_falls_back_once_without_edit(self):
        self.bot.send_photo=AsyncMock(side_effect=BadRequest('synthetic'))
        preview.resolve_preview.return_value='https://static-cdn.jtvnw.net/cf_vods/x/test.jpg'
        with patch.object(vod.results,'get_ready_result',return_value=fixture.READY):
            self.assertTrue(await chat.show_ready(NS(chat_id=1,message_id=10),self.context,7,'123'))
        self.bot.send_photo.assert_awaited_once();self.bot.send_message.assert_awaited_once()
        self.bot.edit_message_media.assert_not_awaited();self.bot.edit_message_text.assert_not_awaited()

    async def test_failed_delivery_preserves_offer_and_is_bounded(self):
        self.bot.send_photo=AsyncMock(side_effect=TimeoutError())
        self.bot.send_message.side_effect=TimeoutError()
        preview.resolve_preview.return_value='https://static-cdn.jtvnw.net/cf_vods/x/test.jpg'
        with patch.object(vod.results,'get_ready_result',return_value=fixture.READY):
            self.assertFalse(await chat.show_ready(NS(chat_id=1,message_id=10),self.context,7,'123'))
        self.bot.send_photo.assert_awaited_once();self.bot.send_message.assert_not_awaited()
        self.bot.delete_message.assert_not_awaited()

    async def test_first_processing_route_still_edits_its_progress_message(self):
        with patch.object(vod,'_build_card',return_value=('card',self.kb)),patch.object(vod,'_html_button_url',side_effect=lambda x:x):
            await vod._edit_progress_message_with_card(self.context,1,10,fixture.ITEM,fixture.READY['html_ref'],ready=fixture.READY,owner_id=7)
        self.bot.edit_message_text.assert_awaited_once()
        self.bot.send_message.assert_not_awaited()

    async def test_stale_lookup_does_not_send_or_retire_source(self):
        owned=True
        async def resolve(*_):
            nonlocal owned
            owned=False
            return 'https://static-cdn.jtvnw.net/cf_vods/x/test.jpg'
        self.bot.send_photo=AsyncMock()
        preview.resolve_preview.side_effect=resolve
        with patch.object(vod.results,'get_ready_result',return_value=fixture.READY):
            self.assertFalse(await chat.show_ready(NS(chat_id=1,message_id=10),self.context,7,'123',current=lambda:owned))
        self.bot.send_photo.assert_not_awaited();self.bot.send_message.assert_not_awaited()
        self.bot.delete_message.assert_not_awaited()

    async def test_failed_source_delete_clears_keyboard_without_resending(self):
        self.bot.delete_message.side_effect=TimeoutError()
        self.bot.edit_message_reply_markup=AsyncMock()
        with patch.object(vod.results,'get_ready_result',return_value=fixture.READY):
            self.assertTrue(await chat.show_ready(NS(chat_id=1,message_id=10),self.context,7,'123'))
        self.bot.send_message.assert_awaited_once()
        self.bot.edit_message_reply_markup.assert_awaited_once_with(chat_id=1,message_id=10,reply_markup=None)
