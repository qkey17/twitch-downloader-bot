"""Shared archive: synthetic Helix, isolated registry and stateful Telegram."""
import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, patch
from src import channel_archive as archive
from src.vod_list import VodListBrowser, callback
from src.twitch_api import videos
from src.twitch_api.client import TwitchAPIError
from src.handlers import handlers_vod_info as info
from src.ui.live_status import WARNING, LIVE
from src.ui.ui_labels import CREATE_CHAT, RECREATE_CHAT, DOWNLOAD_HTML, CHAT_GENERIC, READY_UNAVAILABLE
import test_channel_vods as cards
import test_result_registry as registry
import test_single_message_media as media


def record(n, day=None, **kw):
    date=(datetime(2026,1,1,tzinfo=timezone.utc)+timedelta(days=day if day is not None else 1000-n)).isoformat()
    return cards.vod(n,created_at=date,**kw)


class Registry(unittest.TestCase):
    setUp=registry.ResultRegistryTestCase.setUp
    plan=registry.ResultRegistryTestCase.plan
    publish=registry.ResultRegistryTestCase.publish

    def save(self,n,**meta):
        p=self.plan(str(n));p.meta.update(title='Title',created_at='2026-01-01T00:00:00Z',channel='Channel',channel_id='123');p.meta.update(meta)
        self.publish(p);p.complete();p.close();return p

    def test_current_available_identity_not_display_name_no_migration_or_write(self):
        from src.database import db_core,db_results
        a=self.save(1)
        self.save(2,channel_id='other')
        self.save(3,channel_id=None)
        self.save(4,created_at=None)
        self.save(5,length_seconds='invalid')
        b=self.save(1,channel_id='other')
        c=self.save(6);c.path().unlink()
        d=self.save(7,thumbnail_preview_url='https://evil.invalid/private.jpg')
        path=__import__('pathlib').Path(db_core.DB_PATH);before=path.read_bytes()
        with patch.object(db_results,'get_ready_result',side_effect=AssertionError('no migration')):
            rows=archive.candidates('123')
        self.assertEqual(['7'],[v.id for v in rows]);self.assertEqual('',rows[0].thumbnail_url)
        self.assertEqual(before,path.read_bytes());self.assertTrue(a.path().exists())


class Batch(unittest.IsolatedAsyncioTestCase):
    async def test_successful_ids_and_malformed_errors_are_not_archived(self):
        values=[record(1),record(2)]
        good={'data':[dict(id='2',user_id='123',type='archive')]}
        with patch.object(videos,'_get_json',AsyncMock(return_value=good)) as get:
            self.assertEqual(['1'],[v.id for v in await archive.confirmed(object(),'123',values)])
            self.assertEqual([('id','1'),('id','2')],get.await_args.args[2])
        for response in ({'data':[{}]},{'data':[dict(id='9',user_id='123',type='archive')]},
                         {'data':[dict(id='1',user_id='foreign',type='archive')]}, {'data':None}):
            with patch.object(videos,'_get_json',AsyncMock(return_value=response)):
                self.assertEqual([],await archive.confirmed(object(),'123',values))
        for error in (TimeoutError(),TwitchAPIError('synthetic',status=429),TwitchAPIError('synthetic',status=503)):
            with patch.object(videos,'_get_json',AsyncMock(side_effect=error)):
                self.assertEqual([],await archive.confirmed(object(),'123',values))

    async def test_requests_batched_and_bounded(self):
        with patch.object(videos,'_get_json',AsyncMock(return_value={'data':[]})) as get:
            result=await archive.confirmed(object(),'123',[record(n+1) for n in range(500)])
        self.assertEqual(500,len(result));self.assertEqual(5,get.await_count)
        self.assertTrue(all(len(c.args[2])==100 for c in get.await_args_list))


class Lists(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.browser=VodListBrowser();self.http=object()
        for target,value in (
            ('src.twitch_api.videos.fetch_user_by_login',AsyncMock(return_value=videos.TwitchUser('123','channel','Channel'))),
            ('src.vod_list.stream_observation',AsyncMock(return_value=(None,0))),
            ('src.channel_archive.candidates',lambda channel:[])):
            p=patch(target,value);p.start();self.addCleanup(p.stop)

    async def go(self,view,action,n=None):
        return await self.browser.handle_callback(self.http,7,callback(view,action,n),10,20)

    async def test_interleave_start_middle_end_pages_ties_dedup_and_no_recheck(self):
        saved=[record(1),record(2),record(4),record(5),record(11)]
        public=[record(n) for n in (2,3,6,7,8,9,10)]
        with patch.object(archive,'candidates',return_value=saved), \
             patch.object(archive,'confirmed',AsyncMock(return_value=saved)) as check, \
             patch.object(videos,'fetch_archive_vods',AsyncMock(side_effect=[(public[:3],'C'),(public[3:],None)])) as fetch:
            view=await self.browser.open(self.http,7,'channel',10,20)
            self.assertEqual(1,fetch.await_count) # first five are already settled
            self.assertEqual(['1','2','3','4','5'],[v.id for v in view.items[:5]])
            seen=[v.id for v in view.items[:5]]
            view=await self.go(view,'next')
            self.assertEqual(2,fetch.await_count)
            seen += [v.id for v in view.items[5:10]]
            view=await self.go(view,'next');seen += [v.id for v in view.items[10:15]]
            self.assertEqual([str(n) for n in range(1,12)],seen)
            selected=await self.go(view,'select',10)
            self.assertEqual('11',selected.vod_id)
            view=await self.go(selected.view,'back');self.assertEqual(2,view.page)
            check.assert_awaited_once();self.assertEqual(2,fetch.await_count)

    async def test_missing_on_page_is_not_archived_and_reappearing_is_ordinary(self):
        values=[record(5)]
        with patch.object(archive,'candidates',return_value=values), \
             patch.object(videos,'_get_json',AsyncMock(return_value={'data':[dict(id='5',type='archive',user_id='123')]})), \
             patch.object(videos,'fetch_archive_vods',AsyncMock(side_effect=[([record(n) for n in (1,2,3)],'C'),([record(n) for n in (4,5,6)],None)])):
            view=await self.browser.open(self.http,7,'channel',10,20)
            self.assertNotIn('5',view.archived_ids)
            self.assertEqual(1,len([v for v in view.items if v.id=='5']))

    async def test_unknown_keeps_normal_list_and_shared_for_all_owners(self):
        with patch.object(archive,'candidates',return_value=[record(1)]), \
             patch.object(videos,'_get_json',AsyncMock(side_effect=TimeoutError())), \
             patch.object(videos,'fetch_archive_vods',AsyncMock(return_value=([record(2)],None))):
            view=await self.browser.open(self.http,7,'channel',10,20)
            self.assertEqual(['2'],[v.id for v in view.items]);self.assertFalse(view.archived_ids)
        with patch.object(archive,'candidates',return_value=[record(1)]), \
             patch.object(archive,'confirmed',AsyncMock(return_value=[record(1)])), \
             patch.object(videos,'fetch_archive_vods',AsyncMock(return_value=([],None))):
            a=await self.browser.open(self.http,7,'channel',10,21)
            b=await self.browser.open(self.http,8,'channel',11,22)
            self.assertEqual(a.items,b.items);self.assertEqual(frozenset({'1'}),a.archived_ids)

    async def test_boundary_ties_load_next_before_exposing_page_and_sort_stably(self):
        same=[record(n,day=10) for n in range(1,8)]
        with patch.object(videos,'fetch_archive_vods',AsyncMock(side_effect=[(list(reversed(same[:4])),'C'),(same[4:]+[record(8,day=9)],None)])) as fetch:
            view=await self.browser.open(self.http,7,'channel',10,20)
            self.assertEqual(2,fetch.await_count)
            self.assertEqual([str(n) for n in range(1,9)],[v.id for v in view.items])

    async def test_page_picker_loads_only_requested_prefix_and_rejects_reordered_continuation(self):
        pages=[([record(n) for n in range(1,11)],'C'),([record(n) for n in range(11,21)],'D'),([record(21)],None)]
        with patch.object(videos,'fetch_archive_vods',AsyncMock(side_effect=pages)) as fetch:
            view=await self.browser.open(self.http,7,'channel',10,20)
            self.assertFalse(view.exhausted);self.assertEqual(1,fetch.await_count)
            view=await self.go(view,'pages');self.assertTrue(view.grid)
            view=await self.go(view,'page',1);self.assertEqual(1,view.page)
            self.assertEqual(2,fetch.await_count)
            self.assertEqual(['6','7','8','9','10'],[v.id for v in view.items[5:10]])
            view=await self.go(view,'prev');self.assertEqual(0,view.page)
            self.assertEqual(2,fetch.await_count)
        other=VodListBrowser()
        with patch.object(videos,'fetch_archive_vods',AsyncMock(side_effect=[([record(10)],'C'),([record(1)],None)])):
            bad=await other.open(self.http,7,'channel',10,21)
            self.assertEqual('api_error',bad.status)



class Card(unittest.IsolatedAsyncioTestCase):
    for name in ('_setupAsyncioRunner','setUp','asyncTearDown','task','api','message','update','open','select','publication','worker'):
        locals()[name]=getattr(media.MediaCards,name)

    async def test_saved_duration_in_rendered_information_card(self):
        for seconds, expected in ((0,'00:00:00'),(30,'00:00:30'),(90,'00:01:30'),(3600,'01:00:00'),(7200,'02:00:00'),(9999999999,'2777777:46:39')):
            with self.subTest(seconds=seconds):
                self.vods.clear()
                meta=dict(channel_id='1',title='Title',created_at='2026-01-01T00:00:00Z',length_seconds=seconds)
                ready=dict(id='a'*32,vod_id='1',meta_json=json.dumps(meta),html_ref='https://public.invalid/public_html/chat_'+'a'*32+'.html')
                saved=[archive.saved_vod(ready,'1')]
                with patch.object(archive,'candidates',return_value=saved),patch.object(archive,'confirmed',AsyncMock(return_value=saved)),patch.object(info,'lookup',return_value=ready):
                    message,view=await self.open();await self.select(message,view)
                    self.assertIn('⏱ '+expected,message.caption or message.text)

    async def test_archive_card_actions_capture_no_live_and_forged_process(self):
        self.vods.clear();saved=[record(1,thumbnail_url=media.PREVIEW)]
        ready=dict(id='a'*32,vod_id='1',meta_json=json.dumps({'captured_during_live':True,'channel_id':'1','title':'Title','created_at':'2026-01-01T00:00:00Z','length_seconds':30,'thumbnail_preview_url':media.PREVIEW}),html_ref='https://public.invalid/public_html/chat_'+'a'*32+'.html')
        with patch.object(archive,'candidates',return_value=saved),patch.object(archive,'confirmed',AsyncMock(return_value=saved)), \
             patch.object(info,'lookup',return_value=ready),patch.object(info,'observe',AsyncMock()) as live:
            message,view=await self.open();await self.select(message,view)
            text=message.caption
            self.assertTrue(text.endswith(WARNING+'\n'+archive.ARCHIVE));self.assertNotIn(LIVE,text)
            labels=[b.text for row in message.reply_markup.inline_keyboard for b in row]
            self.assertIn(DOWNLOAD_HTML,labels);self.assertIn(CHAT_GENERIC,labels)
            self.assertNotIn(RECREATE_CHAT,labels);self.assertNotIn(CREATE_CHAT,labels)
            self.assertEqual(media.PREVIEW,message.image);live.assert_not_awaited()
            session=media.lists.get_channel_browser(self.app)._sessions[view.session_id];card=session.info_card
            before=len(self.calls)
            with patch('src.handlers.handlers_chat.prepare_chat',AsyncMock()) as prepare:
                q=self.update(message,f'ui:vi:{session.token}:{card.nonce}:process')
                await info.info_callback(q,self.ctx);prepare.assert_not_awaited()
            self.assertEqual(before,len(self.calls));self.assertFalse(self.started)

    async def test_disappeared_generation_and_select_never_start_worker(self):
        self.vods.clear();saved=[record(1)]
        with patch.object(archive,'candidates',return_value=saved),patch.object(archive,'confirmed',AsyncMock(return_value=saved)),patch.object(info,'lookup',return_value=None):
            message,view=await self.open()
            await media.lists.channel_vods_callback(self.update(message,callback(view,'select',0)),self.ctx)
            self.assertIn(READY_UNAVAILABLE,message.caption or message.text)
            self.assertFalse(self.started)
            self.assertTrue(all(b.text not in (CREATE_CHAT,RECREATE_CHAT) for row in message.reply_markup.inline_keyboard for b in row))
