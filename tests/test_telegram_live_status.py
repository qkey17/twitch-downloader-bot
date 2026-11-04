import time
import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch
from src.ui import live_status as status
from src.ui.ui_history import ready_offer_text, format_vod_summary
from src.handlers.handlers_vod_info import information_text
from src.workers.worker_progress import _build_progress_card
from src.twitch_api import live
from src.live_capture import generation_capture
import test_result_registry as registry
import test_ready_chat_preview as cards
from src.handlers import handlers_vod as vod
REAL_BUILD_CARD=vod._build_card


class Layout(unittest.TestCase):
    def test_five_scenarios_in_exact_order_after_date(self):
        self.assertEqual('🔴 LIVE',status.LIVE)
        self.assertEqual('⚠️ Готовый чат может быть неполным, так как был обработан во время прямой трансляции',status.WARNING)
        for current,captured,ready,expected in (
            (True,False,False,[status.LIVE]),
            (True,True,True,[status.LIVE,status.WARNING]),
            (False,True,True,[status.WARNING]),
            (False,False,True,[]),
            (True,False,True,[status.LIVE]),
        ):
            with self.subTest(current=current,captured=captured,ready=ready):
                value=dict(meta=dict(channel='<x>',title='<script>',created_at='2026-10-29T14:28:00Z',captured_during_live=captured),stats={},
                           html_ref='https://example.invalid/a.html' if ready else None,
                           live_state=dict(value=current,checked_at=time.monotonic()))
                text=ready_offer_text(value)
                self.assertEqual(expected,text.split('🗓 29 окт 2026 14:28 UTC',1)[1].strip().splitlines())
                for line in expected:self.assertEqual(1,text.count(line));self.assertFalse(line.endswith('.'))
                self.assertNotIn('<script>',text);self.assertIn('&lt;script&gt;',text)

    def test_no_ready_no_warning_and_no_raw_cached_live(self):
        for value in (None,True,'true',False):
            meta=dict(is_live=True,captured_during_live=value)
            self.assertNotIn(status.LIVE,format_vod_summary(meta))
            self.assertNotIn(status.WARNING,ready_offer_text(dict(meta=meta,stats={})))
            self.assertNotIn(status.WARNING,_build_progress_card(meta,'vod',1,1,False))
        self.assertNotIn(status.WARNING,information_text('C','T','1:00',None,is_live=True))
        self.assertIn(status.LIVE,information_text('C','T','1:00',None,is_live=True))

    def test_expired_unknown_future_and_malformed_observations(self):
        for stamp,value in ((69,True),(101,True),(100,None),(100,False),('bad',True)):
            with patch.object(status,'time',NS(monotonic=lambda:100)):
                text=status.lines(dict(live_state=dict(value=value,checked_at=stamp),captured_during_live=True),ready=True)
            self.assertNotIn(status.LIVE,text);self.assertIn(status.WARNING,text)
        with patch.object(status,'time',NS(monotonic=lambda:100)):
            self.assertIn(status.LIVE,_build_progress_card(dict(live_state=dict(value=True,checked_at=99)),'vod',1,1,False))


class Observation(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        live._cache.clear()
        self.session=object();self.context=NS(application=NS(bot_data={'aiohttp':self.session}))
        self.video=dict(id='1',user_id='2',stream_id='3',type='archive')

    async def test_matching_other_broadcast_offline_and_unknown(self):
        for rows,expected in (([dict(id='3',user_id='2',type='live')],True),
                              ([dict(id='4',user_id='2',type='live')],False),([],False),([{}],None)):
            live._cache.clear()
            with patch.object(status,'_get_json',AsyncMock(return_value={'data':[self.video]})) as vod, \
                 patch.object(live,'_get_json',AsyncMock(return_value={'data':rows})) as stream:
                observed=await status.observe(self.context,'1')
                self.assertIs(expected,observed['value'])
                vod.assert_awaited_once();stream.assert_awaited_once()
                for _ in range(10):ready_offer_text(dict(meta={},stats={},live_state=observed))
                self.assertEqual(1,vod.await_count);self.assertEqual(1,stream.await_count)

    async def test_cached_observation_time_not_renewed_and_refreshes_after_expiry(self):
        clock=[100]
        request=AsyncMock(return_value={'data':[dict(id='3',user_id='2',type='live')]})
        with patch.object(live,'time',NS(monotonic=lambda:clock[0])),patch.object(status,'time',NS(monotonic=lambda:clock[0])), \
             patch.object(live,'_get_json',request):
            self.assertIs(True,(await status.observe(self.context,'1',self.video))['value'])
            clock[0]=129
            old=await status.observe(self.context,'1',self.video)
            self.assertEqual(100,old['checked_at']);self.assertEqual(1,request.await_count)
            clock[0]=131;request.return_value={'data':[]}
            self.assertNotIn(status.LIVE,status.lines(dict(live_state=old)))
            self.assertIs(False,(await status.observe(self.context,'1',self.video))['value'])
            self.assertEqual(2,request.await_count)

    async def test_error_malformed_foreign_and_missing_identity_stay_unknown(self):
        for video in ({},dict(self.video,id='9'),dict(self.video,type='highlight'),dict(self.video,stream_id=None)):
            with patch.object(status,'_get_json',AsyncMock(return_value={'data':[video]})),patch.object(live,'_get_json',AsyncMock()) as stream:
                self.assertIsNone((await status.observe(self.context,'1'))['value']);stream.assert_not_awaited()
        with patch.object(status,'_get_json',AsyncMock(side_effect=ValueError('synthetic'))):
            self.assertIsNone((await status.observe(self.context,'1'))['value'])


class Generation(unittest.TestCase):
    setUp=registry.ResultRegistryTestCase.setUp
    plan=registry.ResultRegistryTestCase.plan
    publish=registry.ResultRegistryTestCase.publish

    def test_a_keeps_warning_b_does_not_inherit_it_and_no_sqlite_migration(self):
        from src.handlers.handlers_vod import ready_generation
        a=self.plan();a.meta['captured_during_live']=True
        self.publish(a);a.complete();a.close()
        b=self.plan();b.meta['captured_during_live']=False
        self.publish(b);b.complete();b.close()
        selected=ready_generation(a.vod_id,a.id);current=ready_generation(b.vod_id)
        self.assertIn(status.WARNING,ready_offer_text(selected))
        self.assertNotIn(status.WARNING,ready_offer_text(current))
        self.assertTrue(generation_capture(selected));self.assertFalse(generation_capture(current))


class CardRoutes(unittest.IsolatedAsyncioTestCase):
    for name in ('_setupAsyncioRunner','setUp','asyncTearDown','context_for_user','task','update','ready_result','open','message','offer','act','pick','back'):
        locals()[name]=getattr(cards.ReadyPreviewTests,name)

    async def test_photo_and_text_offer_and_reopen_keep_buttons_and_capture(self):
        for preview in (cards.URL,''):
            with self.subTest(preview=preview):
                self.available['123456789']['meta']['captured_during_live']=True
                with patch.object(status,'observe',AsyncMock(return_value=dict(value=True,checked_at=time.monotonic()))), patch.object(vod,'_build_card',REAL_BUILD_CARD):
                    source,view,choice=await self.offer(url=preview)
                    text=source.caption if source.photo else source.text
                    self.assertIn(status.LIVE+'\n'+status.WARNING,text)
                    payload=source.keyboard.inline_keyboard[0][0].callback_data
                    self.assertTrue(payload.startswith('ui:chat:open:'))
                    await self.act(choice,'open',source)
                    final=source.caption if source.photo else source.text
                    self.assertIn(status.LIVE+'\n'+status.WARNING,final)
                    self.assertIn(self.available['123456789']['html_ref'],str(source.keyboard))


import test_live_vods as live_fixture
from src.history.history_callbacks import history_page_callback


class HistoryRows(unittest.IsolatedAsyncioTestCase):
    for name in ('_setupAsyncioRunner','setUp','open_history','query'):
        locals()[name]=getattr(live_fixture.HistoryLiveTests,name)

    async def test_current_live_expires_but_selected_generation_warning_survives_history_navigation(self):
        from src.ui import html_file_action
        saved=dict(self.ready, meta={'captured_during_live':True})
        lookup=patch.object(html_file_action,'lookup',side_effect=lambda vod_id:dict(saved,vod_id=vod_id))
        lookup.start();self.addCleanup(lookup.stop)
        with patch.object(status,'observe',AsyncMock(return_value=dict(value=True,checked_at=time.monotonic()))):
            await self.open_history()
        caption=self.context.bot.send_photo.await_args.kwargs['caption']
        self.assertIn(status.LIVE+'\n'+status.WARNING,caption)
        with patch.object(status,'observe',AsyncMock(return_value=dict(value=False,checked_at=time.monotonic()))):
            await history_page_callback(self.query(1),self.context)
        caption=self.context.bot.edit_message_caption.await_args.kwargs['caption']
        self.assertNotIn(status.LIVE,caption)
        self.assertIn(status.WARNING,caption)
