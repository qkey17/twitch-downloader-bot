"""Durable generation actions and independent bot messages, no live APIs."""
import asyncio
import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock,patch
from telegram import InlineKeyboardButton as Button,InlineKeyboardMarkup as Markup
from src.ui import html_file_action as action,result_preview as preview
from src.handlers import handlers_vod as vod
from src.history import history_files
from src.html_sender import SendResult,SendStatus,SENDER_KEY

READY=dict(id='a'*32,vod_id='123',html_ref='https://example.invalid/chat.html',cache_id=None,meta={},stats={})
ITEM=dict(vod_id='123',vod_url='https://www.twitch.tv/videos/123',fmt='html_online',history_id=8)

class FileUiTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.bot=NS(id=42,token='123:synthetic-signing-test-key',send_message=AsyncMock(return_value=NS(message_id=20)),
            edit_message_text=AsyncMock(return_value=NS(message_id=20)),edit_message_caption=AsyncMock(),
            edit_message_media=AsyncMock(),delete_message=AsyncMock())
        self.sender=NS(send=AsyncMock(return_value=SendResult(SendStatus.SENT)),closing=False)
        self.app=NS(running=True,bot_data={SENDER_KEY:self.sender})
        self.context=NS(user_data={},bot=self.bot,application=self.app)
        self.kb=Markup([[Button('🌐 Посмотреть чат онлайн',url=READY['html_ref']),Button('🔗 Ссылка на VOD',url=ITEM['vod_url'])]])
        p=patch.object(preview,'resolve_preview',AsyncMock(return_value=None));p.start();self.addCleanup(p.stop)
        self.markup=None

    def update(self,owner=7,message=20,chat_id=1,token=None,markup=None):
        markup=markup or self.markup
        data=next((b.callback_data for row in markup.inline_keyboard for b in row if b.callback_data), 'wrong') if markup else 'wrong'
        q=NS(data=token or data,message=NS(chat_id=chat_id,message_id=message,from_user=NS(id=42),reply_markup=markup),answer=AsyncMock())
        return NS(callback_query=q,effective_user=NS(id=owner))

    async def card(self,photo=False,ready=None):
        self.markup=action.keyboard(self.context,7,1,ready or READY,self.kb)
        await preview.render_card(self.bot,1,'card',self.markup,message_id=20,photo=photo,file_context=self.context)
        return self.markup

    async def test_three_same_vod_cards_and_fourth_other_survive_restart(self):
        updates=[]
        for i in range(3):
            kb=await self.card();updates.append(self.update(message=20+i,markup=kb))
        await self.card(ready=dict(READY,id='b'*32,vod_id='456'))
        fresh=NS(user_data={},application=self.app,bot=self.bot)
        for q in updates:await action.file_callback(q,fresh)
        self.assertEqual(3,self.sender.send.await_count)
        for call in self.sender.send.await_args_list:self.assertEqual((7,1,'123'),call.args);self.assertEqual('a'*32,call.kwargs['generation_id'])
        self.assertEqual({},fresh.user_data)

    async def test_layout_and_callback_within_64_bytes(self):
        kb=await self.card()
        self.assertEqual([['🌐 Посмотреть чат онлайн'],['📥 Скачать чат файлом'],['🔗 Ссылка на VOD']],[[b.text for b in row] for row in kb.inline_keyboard])
        self.assertLessEqual(len(self.update().callback_query.data.encode()),64)
        self.assertNotIn('/',self.update().callback_query.data[len(action.PREFIX):])

    async def test_owner_chat_signature_and_message_markup_cannot_be_substituted(self):
        await self.card();valid=self.update().callback_query.data
        for q in (self.update(owner=9),self.update(chat_id=9),self.update(token=valid[:-1]+('A' if valid[-1]!='A' else 'B'))):
            await action.file_callback(q,self.context)
        q=self.update();q.callback_query.message.reply_markup=None;await action.file_callback(q,self.context)
        q=self.update();q.callback_query.message.from_user.id=99;await action.file_callback(q,self.context)
        self.sender.send.assert_not_awaited()

    async def test_navigation_changes_do_not_revoke_generation(self):
        await self.card();q=self.update()
        self.context.user_data.update(chat_request=object(),history_view={'request':object()})
        with patch.object(vod.results,'get_ready_result',return_value=dict(READY,id='b'*32)):
            await action.file_callback(q,self.context)
        self.sender.send.assert_awaited_once_with(7,1,'123',generation_id='a'*32)

    async def test_missing_generation_never_follows_new_current(self):
        await self.card();self.sender.send.return_value=SendResult(SendStatus.UNAVAILABLE)
        await action.file_callback(self.update(),self.context)
        self.sender.send.assert_awaited_once_with(7,1,'123',generation_id='a'*32)
        self.bot.send_message.assert_awaited_once_with(chat_id=1,text=action.NOTICES[SendStatus.UNAVAILABLE])

    async def test_ready_history_and_worker_use_same_generation_action(self):
        with patch.object(vod,'_build_card',return_value=('card',self.kb)),patch.object(vod,'_html_button_url',side_effect=lambda x:x):
            await vod._edit_progress_message_with_card(self.context,1,20,ITEM,READY['html_ref'],ready=READY,owner_id=7)
            kb=self.bot.edit_message_text.await_args.kwargs['reply_markup']
            await action.file_callback(self.update(markup=kb),self.context)
        await history_files._send_file_card(self.context,1,'card',self.kb,dict(ITEM,ready_result=READY),7)
        kb=self.bot.send_message.await_args.kwargs['reply_markup']
        await action.file_callback(self.update(markup=kb),self.context)
        self.assertEqual(2,self.sender.send.await_count)

    async def test_ack_precedes_upload_and_shutdown_is_silent(self):
        await self.card();q=self.update()
        async def send(*args,**kw):
            q.callback_query.answer.assert_awaited_once();return SendResult(SendStatus.SHUTDOWN)
        self.sender.send.side_effect=send
        await action.file_callback(q,self.context);self.bot.send_message.assert_not_awaited()

    async def test_rotated_key_invalidates_old_callback_without_disclosing_it(self):
        await self.card();q=self.update();self.bot.token='456:other-synthetic-test-key'
        await action.file_callback(q,self.context);self.sender.send.assert_not_awaited()

    async def test_full_supported_vod_identifier_fits_the_durable_callback(self):
        kb=await self.card(ready=dict(READY,vod_id='9'*20))
        request=self.update(markup=kb)
        self.assertEqual(64,len(request.callback_query.data.encode()))
        await action.file_callback(request,self.context)
        self.sender.send.assert_awaited_once_with(7,1,'9'*20,generation_id='a'*32)
