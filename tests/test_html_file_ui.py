import asyncio
import time
import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch
from telegram import InlineKeyboardButton as Button, InlineKeyboardMarkup as Markup
from src.ui import html_file_action as action, result_preview as preview
from src.handlers import handlers_chat as chat, handlers_vod as vod
from src.history import history_files, history_view
from src.html_sender import SendResult, SendStatus, SENDER_KEY

READY=dict(id='a'*32,vod_id='123',html_ref='https://example.invalid/chat.html',cache_id=None,meta={},stats={})
ITEM=dict(vod_id='123',vod_url='https://www.twitch.tv/videos/123',fmt='html_online',history_id=8)

class FileUiTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.bot=NS(send_message=AsyncMock(return_value=NS(message_id=20)),
                    edit_message_text=AsyncMock(return_value=NS(message_id=20)),
                    edit_message_media=AsyncMock(),delete_message=AsyncMock())
        self.sender=NS(send=AsyncMock(return_value=SendResult(SendStatus.SENT)),closing=False)
        self.app=NS(running=True,bot_data={SENDER_KEY:self.sender})
        self.context=NS(user_data={},bot=self.bot,application=self.app)
        self.kb=Markup([[Button('Открыть чат',url=READY['html_ref']),Button('🔗 Ссылка на VOD',url=ITEM['vod_url'])]])
        self.patch=patch.object(preview,'resolve_preview',AsyncMock(return_value=None));self.patch.start();self.addCleanup(self.patch.stop)

    def update(self,owner=7,message=20,chat_id=1,token=None):
        record=self.context.user_data.get(action.KEY,{})
        q=NS(data=action.PREFIX+(token or record.get('token','wrong')),message=NS(chat_id=chat_id,message_id=message),answer=AsyncMock())
        return NS(callback_query=q,effective_user=NS(id=owner))

    async def card(self,photo=False):
        kb=action.keyboard(self.context,7,1,READY,self.kb)
        await preview.render_card(self.bot,1,'card',kb,message_id=20,photo=photo,file_context=self.context)
        return kb

    async def test_ready_open_and_standalone_and_history_bind_same_generation(self):
        with patch.object(vod,'_build_card',return_value=('card',self.kb)),patch.object(vod,'_html_button_url',side_effect=lambda x:x):
            await vod._edit_progress_message_with_card(self.context,1,20,ITEM,READY['html_ref'],ready=READY,owner_id=7)
            self.assertEqual(READY['id'],self.context.user_data[action.KEY]['generation'])
            await vod._send_card_with_buttons(self.context,1,ITEM,READY['html_ref'],ready=READY,owner_id=7)
        await history_files._send_file_card(self.context,1,'card',self.kb,dict(ITEM,ready_result=READY),7)
        self.assertEqual(8,self.context.user_data[action.KEY]['history'])
        with patch.object(action.db,'get_history_entry_for_user',return_value=dict(vod_id='123',fmt='html_online')):
            await action.file_callback(self.update(),self.context)
        self.sender.send.assert_awaited_once_with(7,1,'123',generation_id='a'*32)

    async def test_button_layout_local_and_no_generation(self):
        kb=await self.card()
        self.assertEqual(['Открыть чат','📄 Получить файл'],[b.text for b in kb.inline_keyboard[0]])
        self.assertEqual('🔗 Ссылка на VOD',kb.inline_keyboard[1][0].text)
        self.assertLessEqual(len(kb.inline_keyboard[0][1].callback_data.encode()),64)
        local=action.keyboard(self.context,7,1,READY,Markup([[Button('🔗 Ссылка на VOD',url=ITEM['vod_url'])]]))
        self.assertEqual(['📄 Получить файл'],[b.text for b in local.inline_keyboard[0]])
        self.assertIs(self.kb,action.keyboard(self.context,7,1,None,self.kb))

    async def test_owner_message_chat_token_expiry_and_navigation_fences(self):
        await self.card();record=self.context.user_data[action.KEY]
        for kwargs in [dict(owner=9),dict(message=99),dict(chat_id=9),dict(token='f'*24)]:
            await action.file_callback(self.update(**kwargs),self.context)
        record['created']-=action.TTL+1
        await action.file_callback(self.update(),self.context)
        record['created']=time.monotonic();self.context.user_data['chat_request']=object()
        await action.file_callback(self.update(),self.context)
        self.sender.send.assert_not_awaited()

    async def test_old_generation_is_sent_and_old_token_revoked_on_new_card(self):
        await self.card();old=self.update()
        with patch.object(vod.results,'get_ready_result',return_value=dict(READY,id='b'*32)):
            await action.file_callback(old,self.context)
        self.sender.send.assert_awaited_once_with(7,1,'123',generation_id='a'*32)
        await self.card();await action.file_callback(old,self.context)
        self.assertEqual(1,self.sender.send.await_count)

    async def test_media_replacement_rebinds_and_intermediate_edit_revokes(self):
        self.bot.send_message.return_value=NS(message_id=21)
        await self.card(photo=True)
        self.assertEqual(21,self.context.user_data[action.KEY]['message'])
        await action.file_callback(self.update(message=20),self.context)
        self.sender.send.assert_not_awaited()
        await action.file_callback(self.update(message=21),self.context)
        await preview.render_card(self.bot,1,'intermediate',None,message_id=21,file_context=self.context)
        self.assertNotIn(action.KEY,self.context.user_data)

    async def test_ack_precedes_upload_no_success_notice_errors_once_shutdown_silent(self):
        await self.card();q=self.update();entered=asyncio.Event();release=asyncio.Event()
        async def send(*args,**kw):
            q.callback_query.answer.assert_awaited_once();entered.set();await release.wait();return SendResult(SendStatus.SENT)
        self.sender.send.side_effect=send
        task=asyncio.create_task(action.file_callback(q,self.context));await entered.wait()
        self.assertFalse(task.done());release.set();await task
        self.bot.send_message.assert_not_awaited()
        self.sender.send.side_effect=None
        for status in action.NOTICES:
            self.sender.send.return_value=SendResult(status,uncertain=True)
            await action.file_callback(self.update(),self.context)
        self.assertEqual(len(action.NOTICES),self.bot.send_message.await_count)
        before=self.bot.send_message.await_count
        for status in (SendStatus.CANCELLED,SendStatus.SHUTDOWN):
            self.sender.send.return_value=SendResult(status);await action.file_callback(self.update(),self.context)
        self.assertEqual(before,self.bot.send_message.await_count)

    async def test_history_ownership_rechecked(self):
        await history_files._send_file_card(self.context,1,'card',self.kb,dict(ITEM,ready_result=READY),7)
        with patch.object(action.db,'get_history_entry_for_user',return_value=None):
            await action.file_callback(self.update(),self.context)
        self.sender.send.assert_not_awaited()

    async def test_history_page_pins_current_at_render_and_page_change_revokes(self):
        with patch.object(action,'lookup',return_value=READY):
            await history_view._send_history_cards(1,self.context,[ITEM],0,1,owner_id=7)
        record=self.context.user_data[action.KEY]
        self.assertEqual(READY['id'],record['generation'])
        with patch.object(action.db,'get_history_entry_for_user',return_value=dict(vod_id='123',fmt='html_online')):
            await action.file_callback(self.update(),self.context)
            self.context.user_data['history_view']['request']=object()
            await action.file_callback(self.update(),self.context)
        self.assertEqual(1,self.sender.send.await_count)

    async def test_preview_pending_does_not_block_file_action_and_photo_keeps_binding(self):
        entered=asyncio.Event();release=asyncio.Event()
        async def resolve(*args):entered.set();await release.wait();return 'https://example.invalid/preview.jpg'
        with patch.object(vod,'_build_card',return_value=('card',self.kb)),patch.object(preview,'resolve_preview',side_effect=resolve):
            task=asyncio.create_task(vod._edit_progress_message_with_card(self.context,1,20,ITEM,READY['html_ref'],ready=READY,owner_id=7))
            await entered.wait()
            q=self.update();await action.file_callback(q,self.context)
            self.sender.send.assert_awaited_once()
            release.set();await task
        self.assertEqual(20,self.context.user_data[action.KEY]['message'])
        await action.file_callback(q,self.context)
        self.assertEqual(2,self.sender.send.await_count)

    async def test_ptb_shutdown_drains_callback_without_notice(self):
        from src.main import TcdApplication, Application
        await self.card()
        app=Application.builder().application_class(TcdApplication).token('123:synthetic').build()
        app._running=True;app.bot_data[SENDER_KEY]=self.sender;self.context.application=app
        entered=asyncio.Event();release=asyncio.Event()
        async def send(*args,**kwargs):
            entered.set();await release.wait();return SendResult(SendStatus.SHUTDOWN,uncertain=True)
        async def close():self.sender.closing=True;release.set()
        self.sender.send.side_effect=send;self.sender.close=close
        try:
            task=app.create_task(action.file_callback(self.update(),self.context))
            await entered.wait();await asyncio.wait_for(app.stop(),2)
            self.assertTrue(task.done());self.bot.send_message.assert_not_awaited()
        finally:
            release.set()
            for request in app.bot._request:await request.shutdown()
