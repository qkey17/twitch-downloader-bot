"""LIVE identity, optional lookups and durable capture metadata; synthetic services."""
import asyncio
import io
import json
import re
import unittest
import tempfile
from pathlib import Path
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from src.twitch_api import live, meta, videos
from src.live_capture import WARNING, captured_live, generation_capture
from src.html_renderer import render_viewer_html, write_viewer_html
from src.ui.ui_history import ready_offer_text, format_vod_summary
from src.workers.worker_progress import _build_progress_card
from src.handlers.handlers_vod_info import information_text
from src.vod_list import VodListBrowser, build_list, callback
import test_channel_vods as fixture
import test_result_registry as registry
import test_result_previews as previews


class LiveAPITests(unittest.IsolatedAsyncioTestCase):
    def _setupAsyncioRunner(self):
        self._asyncioRunner=asyncio.Runner(loop_factory=asyncio.SelectorEventLoop)
    def setUp(self):
        live._cache.clear()

    def test_stream_identity_requires_a_matching_archive(self):
        self.assertTrue(live.match_stream('55','55'))
        self.assertFalse(live.match_stream('54','55'))
        self.assertFalse(live.match_stream('55',''))
        for source,current in [('', '55'),(None,'55'),('55',None),('bad','55'),('55','bad')]:
            self.assertIsNone(live.match_stream(source,current))

    async def test_cache_bounds_and_expiry_including_unknown(self):
        session=object();request=AsyncMock(return_value={'data':[{'id':'55','user_id':'123','type':'live'}]})
        with patch.object(live,'_get_json',request),patch.object(live,'time',SimpleNamespace(monotonic=lambda:100)):
            for _ in range(200):self.assertEqual('55',await live.current_stream(session,'123'))
        self.assertEqual(1,request.await_count)
        with patch.object(live,'_get_json',AsyncMock(return_value={'data':[]})),patch.object(live,'time',SimpleNamespace(monotonic=lambda:131)):
            self.assertEqual('',await live.current_stream(session,'123'))
        with patch.object(live,'_get_json',AsyncMock(side_effect=ValueError('private detail'))):
            self.assertIsNone(await live.current_stream(session,'123',fresh=True))
            for n in range(1,80):await live.current_stream(session,str(n),fresh=True)
        self.assertLessEqual(len(live._cache),64)

    async def test_malformed_foreign_and_offline_responses(self):
        for rows in ([{'id':'55','user_id':'other','type':'live'}],
                     [{'id':'55','user_id':'123','type':'archive'}], [{'id':None}], [{},{}]):
            with self.subTest(rows=rows),patch.object(live,'_get_json',AsyncMock(return_value={'data':rows})):
                self.assertIsNone(await live.current_stream(object(),'123'))
        with patch.object(live,'_get_json',AsyncMock(side_effect=asyncio.CancelledError)):
            with self.assertRaises(asyncio.CancelledError):await live.current_stream(object(),'123')

    async def test_meta_matches_helix_video_and_stream_not_thumbnail(self):
        gql={'data':{'video':{'id':'11','title':'T','lengthSeconds':30,'createdAt':None,
                            'owner':{'id':'123','login':'channel','displayName':'C'}}}}
        class Response:
            status=200
            async def __aenter__(self):return self
            async def __aexit__(self,*_):pass
            async def json(self):return {'data':[dict(video)]}
        session=SimpleNamespace(get=lambda *a,**kw:Response())
        video={'id':'11','user_id':'123','stream_id':'55','type':'archive','thumbnail_url':''}
        with patch('src.twitch_api.client.gql_post_json',AsyncMock(return_value=gql)), \
             patch.object(meta,'get_api_headers',return_value={}), \
             patch.object(live,'current_stream',AsyncMock(return_value='55')) as stream:
            self.assertIs((await meta.fetch_vod_meta(session,'client','11')).is_live,True)
            stream.assert_awaited_with(session,'123',fresh=True)
            video['stream_id']='54'
            self.assertIs((await meta.fetch_vod_meta(session,'client','11')).is_live,False)
            video['user_id']='999'
            self.assertIsNone((await meta.fetch_vod_meta(session,'client','11')).is_live)
            video.update(user_id='123',stream_id=None)
            self.assertIsNone((await meta.fetch_vod_meta(session,'client','11')).is_live)
            video['stream_id']='55';stream.return_value=None
            unknown=await meta.fetch_vod_meta(session,'client','11')
            self.assertIsNone(unknown.is_live)
            self.assertEqual(30,unknown.length_seconds)
            stream.return_value='55'
            video['thumbnail_url']='https://vod-secure.twitch.tv/_404/404_processing_%{width}x%{height}.png'
            confirmed=await meta.fetch_vod_meta(session,'client','11')
            self.assertTrue(confirmed.is_live)
            self.assertIsNone(confirmed.thumbnail_preview_url)
            video['stream_id']='54'
            for template in ('%{width}x%{height}','{width}x{height}'):
                video['thumbnail_url']='https://static-cdn.jtvnw.net/cf_vods/x/custom-'+template+'.jpg'
                with patch.object(meta,'resolve_thumbnail',AsyncMock(return_value='https://synthetic.invalid/full')) as full:
                    enriched=await meta.fetch_vod_meta(session,'client','11')
                    self.assertEqual('https://static-cdn.jtvnw.net/cf_vods/x/custom-720x405.jpg',enriched.thumbnail_preview_url)
                    full.assert_awaited_once()

    async def test_download_freezes_fact_before_html_publication(self):
        from src.workers import download_worker as worker
        context=SimpleNamespace(application=SimpleNamespace(bot_data={'aiohttp':object()}),user_data={})
        publication=SimpleNamespace(meta={},stats={})
        async def comments(*a,**k):
            yield (1,None,'fixture','message',[],None,[])
        async def build(**kw):
            self.assertTrue(kw['publication'].meta['captured_during_live'])
            self.assertTrue(kw['meta'].is_live)
            return [],'https://example.invalid/chat.html'
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder, \
             patch.object(worker,'OUT_DIR',folder),patch.object(worker,'SPOOL_DIR',str(Path(folder)/'spool')), \
             patch.object(worker,'fetch_vod_meta',AsyncMock(return_value=meta.VodMeta('11',length_seconds=30,is_live=True))), \
             patch.object(worker,'get_client_id',return_value='client'), \
             patch.object(worker,'gql_fetch_comments',comments), \
             patch.object(worker,'make_progress_updater',return_value=AsyncMock()), \
             patch.object(worker,'build_html_result',side_effect=build):
            saved,_,_,_=await worker.download_and_send(context,1,2,'vod','11','html_online',publication=publication)
        self.assertTrue(saved['captured_during_live'])

    async def test_list_refreshes_channel_once_and_preserves_navigation(self):
        browser=VodListBrowser();session=object()
        records=[replace(fixture.vod(n),stream_id='55' if n==1 else '54') for n in range(1,7)]
        with patch.object(videos,'fetch_user_by_login',AsyncMock(return_value=videos.TwitchUser('123','channel','C'))), \
             patch.object(videos,'fetch_archive_vods',AsyncMock(return_value=(records,None))), \
             patch.object(live,'_get_json',AsyncMock(return_value={'data':[{'id':'55','user_id':'123','type':'live'}]})) as request:
            view=await browser.open(session,7,'channel',10,20)
            self.assertEqual(frozenset({'1'}),view.live_vod_ids)
            labels=[b.text for row in build_list(view)[1].inline_keyboard for b in row]
            self.assertEqual(1,sum('(LIVE)' in s for s in labels))
            moved=await browser.handle_callback(session,7,callback(view,'next'),10,20)
            self.assertEqual(1,moved.page)
            self.assertEqual(1,request.await_count)
            expired=live._cache[(id(session),'123')][1]+31
            with patch.object(live,'time',SimpleNamespace(monotonic=lambda:expired)), \
                 patch.object(live,'_get_json',AsyncMock(return_value={'data':[]})):
                moved=await browser.handle_callback(session,7,callback(moved,'prev'),10,20)
                self.assertFalse(moved.live_vod_ids)


class CaptureTests(unittest.TestCase):
    setUp=registry.ResultRegistryTestCase.setUp
    plan=registry.ResultRegistryTestCase.plan
    publish=registry.ResultRegistryTestCase.publish
    rows=registry.ResultRegistryTestCase.rows

    def test_exact_shared_warning_and_heading_free_offer(self):
        expected='🔴 Чат получен во время LIVE-трансляции и может быть неполным'
        self.assertEqual(expected,WARNING)
        ready={'meta':{'captured_during_live':True,'channel':'C','title':'T'},'stats':{'messages':42,'unique_users':7}}
        for text in [ready_offer_text(ready),format_vod_summary(ready['meta']|ready['stats']),
                     _build_progress_card(ready['meta'],'vod',1,1,False)]:
            self.assertEqual(1,text.count(expected))
            self.assertNotIn('Чат VOD уже готов',text)
            self.assertFalse(text.startswith('\n'))
        self.assertIn('💬 42',ready_offer_text(ready))
        rendered=render_viewer_html([],'title','channel','https://www.twitch.tv/videos/123',captured_during_live=True)
        self.assertEqual(1,rendered.count(expected))

    def test_generation_retains_live_fact_after_stream_ends_and_history_reopens(self):
        from src.database import database as db, db_results
        plan=self.plan();plan.meta['captured_during_live']=True
        self.publish(plan);cache=plan.complete();plan.close();db.add_user_history(7,cache)
        # A current offline status must never replace the saved generation fact.
        with patch.object(live,'current_stream',AsyncMock(return_value='')) as request:
            ready=db_results.get_ready_result('123456789')
            self.assertTrue(generation_capture(ready))
            self.assertIn(WARNING,ready_offer_text(ready))
            self.assertEqual(1,db.get_history_count(7))
            row=self.rows()[0]
            self.assertTrue(generation_capture(row))
            request.assert_not_called()
        old_html=plan.path().read_bytes()
        replacement=self.plan();self.publish(replacement);replacement.complete();replacement.close()
        self.assertEqual(old_html,plan.path().read_bytes())
        self.assertFalse(generation_capture(db_results.get_ready_result('123456789')))
        self.assertTrue(generation_capture(self.rows()[0]))

    def test_old_and_malformed_metadata_do_not_gain_live_warning(self):
        for value in ({}, {'captured_during_live':False},{'captured_during_live':'true'},None):
            self.assertFalse(captured_live(value))
            self.assertNotIn(WARNING,format_vod_summary(value or {}))
        for value in ('invalid','[]','null'):
            self.assertFalse(generation_capture({'meta_json':value}))
        self.assertIn(WARNING,_build_progress_card({'captured_during_live':True},'vod',1,1,False))
        self.assertIn(WARNING,information_text('<x>','<script>','00:01',None,is_live=True))
        self.assertNotIn('<script>',information_text('<x>','<script>','00:01',None,is_live=True))

    def test_streaming_html_parity_persistent_flag_and_xss_escaping(self):
        args=dict(title='</script><script>alert(1)</script>',channel='<img onerror=x>',vod_url='vod',captured_during_live=True)
        rendered=render_viewer_html([],**args);output=io.StringIO()
        write_viewer_html(output,iter([]),**args)
        self.assertEqual(rendered,output.getvalue())
        payload=json.loads(re.search(r'<script[^>]*id="chat-data"[^>]*>(.*?)</script>',rendered,re.S).group(1))
        self.assertIs(payload['meta']['captured_during_live'],True)
        self.assertNotIn(args['title'],rendered)
        self.assertIn('warning.textContent =',rendered)
        self.assertIn('overflow-wrap:anywhere',rendered)
        self.assertIn('captured_during_live === true',rendered)
        self.assertGreater(rendered.index('id="liveCaptureWarning"'),rendered.index('class="panel-actions"'))
        legacy=render_viewer_html([],'old','channel','vod')
        old_payload=json.loads(re.search(r'<script[^>]*id="chat-data"[^>]*>(.*?)</script>',legacy,re.S).group(1))
        self.assertNotIn('captured_during_live',old_payload['meta'])


class HistoryLiveTests(unittest.IsolatedAsyncioTestCase):
    _setupAsyncioRunner=previews.PreviewTests._setupAsyncioRunner
    setUp=previews.PreviewTests.setUp
    open_history=previews.PreviewTests.open_history
    query=previews.PreviewTests.query

    async def test_history_uses_saved_generation_after_stream_ends_and_on_navigation(self):
        from src.ui import html_file_action
        from src.history.history_callbacks import history_page_callback
        saved=dict(self.ready,id='a'*32,meta_json=json.dumps({'captured_during_live':True}))
        saved.pop('meta')
        self.lookup.return_value=''
        with patch.object(html_file_action,'lookup',side_effect=lambda vod_id:dict(saved,vod_id=vod_id)), \
             patch.object(live,'current_stream',AsyncMock(return_value='')) as status:
            await self.open_history()
            self.assertIn(WARNING,self.context.bot.send_message.await_args.kwargs['text'])
            await history_page_callback(self.query(1),self.context)
            self.assertIn(WARNING,self.context.bot.edit_message_text.await_args.kwargs['text'])
            self.assertEqual(2,self.context.user_data['history_view']['history_id'])
            status.assert_not_awaited()
