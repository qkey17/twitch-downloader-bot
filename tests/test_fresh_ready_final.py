import asyncio
import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock,patch
from telegram.error import BadRequest
import test_html_file_ui as fixture
from src.handlers import handlers_chat as chat,handlers_vod as vod
from src.ui import html_file_action as action,result_preview as preview


class InPlaceFinal(unittest.IsolatedAsyncioTestCase):
    setUp=fixture.FileUiTests.setUp
    update=fixture.FileUiTests.update
    async def test_final_keeps_exact_message_and_generation_button(self):
        with patch.object(vod.results,'get_ready_result',return_value=fixture.READY):
            self.assertTrue(await chat.show_ready(NS(chat_id=1,message_id=10,photo=[]),self.context,7,'123'))
        args=self.bot.edit_message_media.await_args.kwargs
        self.assertEqual(10,args['message_id'])
        await action.file_callback(self.update(message=10,markup=args['reply_markup']),self.context)
        self.sender.send.assert_awaited_once_with(7,1,'123',generation_id='a'*32)
        self.bot.send_message.assert_not_awaited();self.bot.delete_message.assert_not_awaited()
    async def test_ambiguous_final_delivery_never_sends_duplicate(self):
        self.bot.edit_message_media.side_effect=TimeoutError()
        with patch.object(vod.results,'get_ready_result',return_value=fixture.READY):
            self.assertFalse(await chat.show_ready(NS(chat_id=1,message_id=10),self.context,7,'123'))
        self.bot.edit_message_media.assert_awaited_once();self.bot.send_message.assert_not_awaited();self.bot.delete_message.assert_not_awaited()
    async def test_stale_lookup_cannot_edit_newer_screen(self):
        owned=True
        async def resolve(*_):
            nonlocal owned
            owned=False;return 'https://static-cdn.jtvnw.net/cf_vods/x/a.jpg'
        preview.resolve_preview.side_effect=resolve
        with patch.object(vod.results,'get_ready_result',return_value=fixture.READY):
            self.assertFalse(await chat.show_ready(NS(chat_id=1,message_id=10),self.context,7,'123',current=lambda:owned))
        self.bot.edit_message_media.assert_not_awaited();self.bot.send_message.assert_not_awaited()
