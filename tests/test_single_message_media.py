"""One-message media lifecycle with a stateful synthetic Telegram transport."""
import asyncio
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock,Mock,patch
import unittest
from telegram.error import BadRequest
from src.handlers import handlers_input as inputs,handlers_channel_vods as lists,handlers_vod_info as info,handlers_chat as chat,handlers_vod as vod
from src.twitch_api import videos
from src.vod_list import callback
from src.ui import result_preview as renderer,html_file_action as files
from src.ui.card_context import for_message
from src.ui.card_images import DEFAULT,LIVE
from test_card_images import avatar_http
from src.html_sender import SENDER_KEY,SendStatus,SendResult
from src.vod_scheduler import scheduler

REAL_USER_FETCH=videos.fetch_user_by_login

AVATAR='https://static-cdn.jtvnw.net/jtv_user_pictures/original-profile_image-300x300.png'
PREVIEW='https://static-cdn.jtvnw.net/cf_vods/x/720x405.jpg'

def image_name(value):
    return value.name if isinstance(value,Path) else value.filename if hasattr(value,'filename') else value

class MediaCards(unittest.IsolatedAsyncioTestCase):
    def _setupAsyncioRunner(self):self._asyncioRunner=asyncio.Runner(loop_factory=asyncio.SelectorEventLoop)
    def setUp(self):
        self.messages={};self.calls=[];self.number=100;self.tasks=[]
        self.bot=NS(id=42,token='123:synthetic-media-key')
        for name in ('send_photo','send_message','edit_message_media','edit_message_caption','edit_message_text','delete_message','edit_message_reply_markup'):
            setattr(self.bot,name,AsyncMock(side_effect=lambda _name=name,**kw:self.api(_name,**kw)))
        self.app=NS(bot_data={'aiohttp':avatar_http()},running=True,create_task=self.task)
        self.ctx=NS(bot=self.bot,user_data={},application=self.app,args=[])
        self.ready={};self.gates={};self.started={}
        self.meta=NS(title='Title',channel='Channel',length_seconds=3723,created_at='2026-01-02T03:04:00Z',thumbnail_preview_url=PREVIEW,is_live=False)
        self.user=videos.TwitchUser('1','channel','Channel',AVATAR)
        self.vods=[videos.ArchiveVod(str(i),'1','channel','Channel','Title '+str(i),'2026-01-02T03:04:00Z','','1h','','%s'%PREVIEW,'public') for i in range(1,9)]
        self.fetch=AsyncMock(return_value=self.user)
        for target,value in [('src.twitch_api.videos.fetch_user_by_login',self.fetch),('src.twitch_api.videos.fetch_archive_vods',AsyncMock(return_value=(self.vods,None))),('src.handlers.handlers_vod.fetch_vod_meta',AsyncMock(return_value=self.meta)),('src.handlers.handlers_vod.get_client_id',lambda:'mock'),('src.handlers.handlers_vod.results.get_ready_result',lambda v:self.ready.get(v)),('src.handlers.handlers_vod.results.Publication',self.publication),('src.handlers.handlers_vod.download_and_send',self.worker),('src.ui.result_preview.fetch_video_thumbnail',AsyncMock(return_value='')),('src.vod_list.current_stream',AsyncMock(return_value=None)),('src.config.HISTORY_ENABLED',False),('src.handlers.handlers_vod.HTML_PUBLISH_MODE','public')]:
            p=patch(target,value);p.start();self.addCleanup(p.stop)
    def task(self,coro):
        t=asyncio.create_task(coro);self.tasks.append(t);return t
    async def asyncTearDown(self):
        for e in self.gates.values():e.set()
        await scheduler(self.app).close()
        await asyncio.gather(*self.tasks,return_exceptions=True)
    def api(self,name,**kw):
        self.calls.append((name,kw))
        if name.startswith('send_'):
            self.number+=1;m=self.message(self.number)
        else:m=self.messages[kw['message_id']]
        if name in ('send_photo','edit_message_media'):
            m.photo=[NS(file_id='synthetic')];m.image=image_name(kw['photo'] if name=='send_photo' else kw['media'].media)
            m.caption=kw['caption'] if name=='send_photo' else kw['media'].caption;m.text=None
        elif name=='edit_message_caption':
            if not m.photo:raise BadRequest('There is no caption in the message to edit')
            m.caption=kw['caption']
        elif name in ('edit_message_text','send_message'):
            if m.photo:raise BadRequest('There is no text in the message to edit')
            m.text=kw['text']
        if 'reply_markup' in kw:m.reply_markup=kw['reply_markup']
        return m
    def message(self,number,text=''):
        m=NS(chat_id=10,message_id=number,text=text,photo=[],reply_markup=None,from_user=NS(id=42))
        m.reply_photo=AsyncMock(side_effect=lambda **kw:self.api('send_photo',chat_id=10,**kw))
        m.reply_text=AsyncMock(side_effect=lambda text,**kw:self.api('send_message',chat_id=10,text=text,**kw))
        m.edit_text=AsyncMock(side_effect=lambda text=None,**kw:self.api('edit_message_text',message_id=number,text=text,**kw))
        m.edit_caption=AsyncMock(side_effect=lambda **kw:self.api('edit_message_caption',message_id=number,**kw))
        self.messages[number]=m;return m
    def update(self,m,data=None,owner=7):
        return NS(message=m,effective_user=NS(id=owner),callback_query=NS(message=m,data=data,answer=AsyncMock()))
    async def open(self,number=20):
        result=await inputs.text_input(self.update(self.message(number,'channel')),self.ctx)
        return self.messages[result.message_id],result
    async def select(self,m,view,index=0):return await lists.channel_vods_callback(self.update(m,callback(view,'info',index)),self.ctx)
    async def act(self,m,action):
        c=for_message(self.ctx,7,10,m.message_id).user_data['chat_choice']
        await chat.chat_callback(self.update(m,f'ui:chat:{action}:{c["token"]}'),self.ctx)
    def publication(self,v,fmt,**kw):return NS(id='a'*32,ref='https://public.invalid/public_html/chat_'+'a'*32+'.html',reserve=Mock(),complete=Mock(return_value=1),close=Mock(),fail=Mock())
    async def worker(self,**kw):
        v=kw['vod_id'];self.started.setdefault(v,asyncio.Event()).set();await self.gates.setdefault(v,asyncio.Event()).wait()
        if kw['context'].user_data['_vod_job'].cancelled:raise RuntimeError('Загрузка была отменена.')
        return vars(self.meta),{},[],kw['publication'].ref
    async def settle(self,v):
        # Share the worker's event instead of racing a fixed number of loop turns.
        started=self.started.setdefault(v,asyncio.Event())
        await asyncio.wait_for(started.wait(),1)
    def saved(self,v='1'):return dict(id='a'*32,vod_id=v,cache_id=None,meta=dict(vars(self.meta)),stats={},html_ref='https://public.invalid/public_html/chat_'+'a'*32+'.html')

    async def test_channel_avatar_vod_back_and_pages_keep_one_original_message(self):
        m,v=await self.open();self.assertEqual(101,m.message_id);self.assertEqual('channel.png',m.image)
        self.assertEqual(1,sum(n.startswith('send_') for n,k in self.calls))
        await self.select(m,v);session=lists.get_channel_browser(self.app)._sessions[v.session_id]
        self.assertEqual(PREVIEW,m.image)
        c=session.info_card;await info.info_callback(self.update(m,f'ui:vi:{session.token}:{c.nonce}:back'),self.ctx)
        self.assertEqual('channel.png',m.image)
        from dataclasses import replace
        other=PREVIEW.replace('/x/','/another/')
        session.vods[1]=replace(session.vods[1],thumbnail_url=other)
        await self.select(m,lists.get_channel_browser(self.app)._view(session),1)
        self.assertEqual(other,m.image)
        c=session.info_card;await info.info_callback(self.update(m,f'ui:vi:{session.token}:{c.nonce}:back'),self.ctx)
        self.assertEqual('channel.png',m.image)
        before=self.bot.edit_message_media.await_count
        await lists.channel_vods_callback(self.update(m,callback(lists.get_channel_browser(self.app)._view(session),'next')),self.ctx)
        self.assertEqual(before,self.bot.edit_message_media.await_count);self.fetch.assert_awaited_once()
        self.assertEqual(101,m.message_id);self.bot.delete_message.assert_not_awaited()

    async def test_three_ready_cards_update_in_place_and_download_exact_generation(self):
        self.ready['1']=self.saved();sender=NS(send=AsyncMock(return_value=SendResult(SendStatus.SENT)),closing=False);self.app.bot_data[SENDER_KEY]=sender
        cards=[]
        for i in range(3):
            m,v=await self.open(20+i);await self.select(m,v);before=len(self.calls);await self.act(m,'open')
            self.assertFalse(any(n.startswith('send_') or n=='delete_message' for n,k in self.calls[before:]));cards.append(m)
        for m in cards:
            data=next(b.callback_data for row in m.reply_markup.inline_keyboard for b in row if b.callback_data)
            await files.file_callback(self.update(m,data),NS(bot=self.bot,user_data={},application=self.app))
        self.assertEqual(3,sender.send.await_count)
        for call in sender.send.await_args_list:self.assertEqual('a'*32,call.kwargs['generation_id'])

    async def test_live_and_missing_preview_select_repository_cover(self):
        from dataclasses import replace
        from src.ui.card_images import vod_image,avatar_url
        self.assertEqual(LIVE,vod_image('',live=True));self.assertEqual(DEFAULT,vod_image('https://evil.invalid/x.jpg'))
        self.assertEqual(AVATAR,avatar_url(AVATAR));self.assertIsNone(avatar_url(AVATAR+'?secret=x'))
        m,v=await self.open();browser=lists.get_channel_browser(self.app);session=browser._sessions[v.session_id]
        session.vods[0]=replace(session.vods[0],thumbnail_url='',stream_id='55')
        import src.vod_list
        src.vod_list.current_stream.return_value='55'
        await self.select(m,browser._view(session));self.assertEqual('live-vod.png',m.image)

    async def test_first_queued_regeneration_cancel_and_result_keep_message_and_image(self):
        for regenerate,queued in ((False,False),(False,True),(True,False),(True,True)):
            with self.subTest(regenerate=regenerate,queued=queued):
                self.gates.clear();self.started.clear();self.ready.clear()
                if regenerate:self.ready['1']=self.saved()
                blockers=asyncio.Event();manager=scheduler(self.app)
                if queued:
                    for v in ('10','11'):
                        j,_=manager.reserve(9,v);manager.arm(j,blockers.wait)
                before=len(self.calls)
                await inputs.text_input(self.update(self.message(50,'1')),self.ctx)
                m=self.messages[self.number]
                if regenerate:await self.act(m,'regen')
                job=next(j for j in manager.jobs.values() if j.vod=='1');self.assertEqual(PREVIEW,m.image)
                if queued:self.assertIn('В очереди',m.caption)
                blockers.set();await self.settle('1')
                await vod.pending_cancel_callback(self.update(m,'ui:pending_cancel:'+job.context.user_data['download_ui'].get('cancel_token',job.id)),self.ctx)
                self.gates['1'].set();await job.task;await asyncio.sleep(0)
                self.assertIn('Обработать чат',str(m.reply_markup));mid=m.message_id
                self.gates.clear();self.started.clear()
                await self.act(m,'regen' if regenerate else 'create');await self.settle('1')
                job=next(j for j in manager.jobs.values() if j.vod=='1');self.gates['1'].set();await job.task;await asyncio.sleep(0)
                self.assertEqual(mid,m.message_id);self.assertEqual(PREVIEW,m.image)
                self.assertEqual(1,sum(n.startswith('send_') for n,k in self.calls[before:]));self.bot.delete_message.assert_not_awaited()

    async def test_long_caption_definite_refusal_and_ambiguous_timeout_are_bounded(self):
        m,v=await self.open();ctx=for_message(self.ctx,7,10,m.message_id)
        before=self.number
        shown=await renderer.render_card(self.bot,10,'x'*1100,None,PREVIEW,message_id=m.message_id,photo=True,file_context=ctx)
        self.assertEqual(before+1,shown.message_id);self.assertFalse(shown.photo)
        self.bot.edit_message_media.side_effect=TimeoutError()
        before=self.number
        self.assertIsNone(await renderer.render_card(self.bot,10,'next',None,PREVIEW,message_id=m.message_id,photo=True,file_context=ctx))
        self.assertEqual(before,self.number)

    async def test_avatar_rejection_uses_cover_without_processing_or_repeated_fetch(self):
        original=self.bot.edit_message_media.side_effect
        async def media(**kw):
            if image_name(kw['media'].media)=='channel.png':raise BadRequest('Unsupported original format')
            return original(**kw)
        self.bot.edit_message_media.side_effect=media
        m,v=await self.open();self.assertEqual('vod.png',m.image)
        attempted=sum(image_name(c.kwargs['media'].media)=='channel.png' for c in self.bot.edit_message_media.await_args_list)
        self.assertEqual(1,attempted)
        await lists.channel_vods_callback(self.update(m,callback(v,'next')),self.ctx)
        self.assertEqual(attempted,sum(image_name(c.kwargs['media'].media)=='channel.png' for c in self.bot.edit_message_media.await_args_list))
        self.fetch.assert_awaited_once();self.assertEqual(101,m.message_id)

    async def test_profile_image_metadata_is_optional_and_original_url_is_unchanged(self):
        for value,expected in ((AVATAR,AVATAR),(AVATAR.replace('.png','.gif'),AVATAR.replace('.png','.gif')),(None,''),('https://evil.invalid/x.png','')):
            with self.subTest(value=value),patch.object(videos,'_get_json',AsyncMock(return_value={'data':[dict(id='1',login='channel',display_name='Channel',profile_image_url=value)]})):
                user=await REAL_USER_FETCH(object(),'channel')
                self.assertEqual(expected,user.profile_image_url)

    async def test_inflight_media_is_serialized_before_ready_and_late_actions_are_inert(self):
        self.ready['1']=self.saved();m,v=await self.open()
        entered,release=asyncio.Event(),asyncio.Event();original=self.bot.edit_message_media.side_effect
        async def slow(**kw):
            if kw['media'].media==PREVIEW:entered.set();await release.wait()
            return original(**kw)
        self.bot.edit_message_media.side_effect=slow
        selecting=self.task(self.select(m,v));await asyncio.wait_for(entered.wait(),1)
        opening=self.task(self.act(m,'open'));await asyncio.sleep(0);self.assertFalse(opening.done())
        release.set();await asyncio.gather(selecting,opening)
        self.assertIn('📥 Скачать чат файлом',str(m.reply_markup))
        before=len(self.calls)
        await lists.channel_vods_callback(self.update(m,callback(v,'info',1)),self.ctx)
        await lists.channel_vods_callback(self.update(m,callback(v,'info',0),owner=8),self.ctx)
        self.assertEqual(before,len(self.calls));self.assertEqual(101,m.message_id)

    async def test_cancel_after_another_list_restores_only_original_card_and_media(self):
        await inputs.text_input(self.update(self.message(30,'1')),self.ctx)
        job=next(iter(scheduler(self.app).jobs.values()));m=self.messages[self.number];await self.settle('1')
        other,view=await self.open(31);body=other.caption;image=other.image
        lists.get_channel_browser(self.app)._sessions.clear()
        await vod.pending_cancel_callback(self.update(m,'ui:pending_cancel:'+job.context.user_data['download_ui'].get('cancel_token',job.id)),self.ctx)
        self.gates['1'].set();await job.task;await asyncio.sleep(0)
        self.assertEqual(body,other.caption);self.assertEqual(image,other.image)
        self.assertEqual(PREVIEW,m.image);self.assertIn('Обработать чат',str(m.reply_markup))
        self.assertEqual(2,sum(n.startswith('send_') for n,k in self.calls));self.bot.delete_message.assert_not_awaited()

    async def test_capacity_failure_and_mapping_iteration_keep_initial_card_safe(self):
        browser=lists.get_channel_browser(self.app);browser.max_sessions=1
        await self.open();v=await inputs.text_input(self.update(self.message(22,'channel')),self.ctx);m=self.messages[self.number]
        self.assertEqual(102,m.message_id);self.assertTrue(m.photo)
        self.assertEqual('vod.png',m.image)
        self.assertIsNone(v.session_id)
        state=for_message(self.ctx,7,10,m.message_id)
        self.assertIn('image_surface',dict(state.user_data))
        self.assertEqual(2,sum(n.startswith('send_') for n,k in self.calls))

    async def test_exceptional_replacement_rebinds_before_bounded_cleanup(self):
        m,v=await self.open();ctx=for_message(self.ctx,7,10,m.message_id)
        self.bot.edit_message_media.side_effect=BadRequest('Editing forbidden')
        shown=await renderer.render_card(self.bot,10,'next',None,PREVIEW,message_id=m.message_id,photo=True,file_context=ctx)
        self.assertNotEqual(m.message_id,shown.message_id)
        self.assertEqual(shown.message_id,ctx.card.message)
        self.bot.delete_message.assert_not_awaited()
        entered=asyncio.Event()
        async def stuck(**kw):entered.set();await asyncio.Event().wait()
        self.bot.delete_message.side_effect=stuck
        task=self.task(renderer.cleanup_card(self.bot,10,shown));await asyncio.wait_for(entered.wait(),1)
        task.cancel();await asyncio.gather(task,return_exceptions=True)
        self.assertEqual(shown.message_id,ctx.card.message)
        self.assertIn(shown.message_id,self.messages)

    async def test_static_assets_are_small_png_720x405(self):
        import struct
        from src.ui.card_images import telegram_image
        for path in (DEFAULT,LIVE):
            data=path.read_bytes()
            self.assertLess(len(data),10000);self.assertEqual(b'\x89PNG\r\n\x1a\n',data[:8])
            self.assertEqual((720,405),struct.unpack('>II',data[16:24]))
            upload=telegram_image(path);self.assertEqual(data,upload.input_file_content)
        with self.assertRaises(ValueError):telegram_image(Path('untrusted.png'))
