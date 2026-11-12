import asyncio
import base64
import io
import json
import unittest
from types import SimpleNamespace as NS
from unittest.mock import Mock
from PIL import Image, ImageDraw
from src.ui.channel_image import AvatarCache, ChannelImage, compose, BACKGROUND, MAX_INPUT
from src.ui.card_images import DEFAULT, LIVE, telegram_image
from src.html_renderer import render_viewer_html

URL='https://static-cdn.jtvnw.net/jtv_user_pictures/test-profile_image-300x300.png'
def picture(size=(300,300)):
    out=io.BytesIO();Image.new('RGB',size,(30,180,90)).save(out,format='PNG');return out.getvalue()
def avatar_http(data=None,status=200):
    data=picture() if data is None else data
    class Response:
        content_length=len(data);headers={}
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
        async def iter_chunked(self,n):
            for p in range(0,len(data),n):yield data[p:p+n]
    r=Response();r.status=status;r.content=r
    return NS(get=Mock(return_value=r))

class Compositions(unittest.TestCase):
    def test_geometry_mask_background_and_aspect(self):
        for size in ((300,300),(600,300),(300,600)):
            result=compose(picture(size));im=Image.open(io.BytesIO(result.data))
            self.assertEqual((720,405),im.size)
            self.assertEqual(BACKGROUND,im.getpixel((0,0)))
            self.assertEqual((30,180,90),im.getpixel((360,202)))
            self.assertLess(im.getpixel((240,82))[1],40)
            self.assertGreater(im.getpixel((360,83))[1],100)
            # A centered circular source marker retains equal radii after central crop.
            self.assertGreater(im.getpixel((242,202))[1],100)
            self.assertLess(im.getpixel((238,202))[1],40)
    def test_non_square_source_marker_is_not_stretched(self):
        for w,h in ((600,300),(300,600)):
            source=Image.new('RGB',(w,h),'#305060')
            ImageDraw.Draw(source).ellipse((w/2-60,h/2-60,w/2+60,h/2+60),fill='white')
            data=io.BytesIO();source.save(data,format='PNG')
            image=Image.open(io.BytesIO(compose(data.getvalue()).data))
            mask=image.point(lambda v:255 if v>230 else 0).convert('L').point(lambda v:255 if v==255 else 0)
            box=mask.getbbox();self.assertIsNotNone(box)
            self.assertLessEqual(abs((box[2]-box[0])-(box[3]-box[1])),1)
            self.assertAlmostEqual(360,(box[0]+box[2])/2,delta=1)
            self.assertAlmostEqual(202.5,(box[1]+box[3])/2,delta=1)

    def test_bad_and_oversized_sources_are_rejected(self):
        for data in (b'broken',b'x'*(MAX_INPUT+1),picture((2049,1))):
            with self.assertRaises(Exception):compose(data)
        frames=[Image.new('RGB',(20,20),c) for c in ('red','blue')];out=io.BytesIO()
        frames[0].save(out,format='GIF',save_all=True,append_images=frames[1:])
        with self.assertRaises(ValueError):compose(out.getvalue())
    def test_assets_match_original_background_size_and_live_accent(self):
        for p in (DEFAULT,LIVE):
            im=Image.open(p).convert('RGB');self.assertEqual((720,405),im.size)
            self.assertEqual(BACKGROUND,im.getpixel((0,0)))
            red=sum(r>120 and r>g*2 and r>b*2 for r,g,b in im.getdata())
            self.assertEqual(p==LIVE,red>100)
    def test_only_composed_bytes_are_uploaded(self):
        result=compose(picture());upload=telegram_image(result)
        self.assertEqual(result.data,upload.input_file_content);self.assertEqual('channel.png',upload.filename)
    def test_html_resources_are_internal_and_self_contained(self):
        title = '</script><img onerror=x>'
        for live in (False, True):
            with self.subTest(live=live):
                html = render_viewer_html([], title, 'channel', 'https://www.twitch.tv/videos/1',
                                          captured_during_live=live)
                _, marker, rest = html.partition('<script id="chat-data" type="application/json">')
                self.assertTrue(marker)
                data = json.loads(rest.split('</script>', 1)[0])
                self.assertEqual(title, data['meta']['title'])
                self.assertNotIn('vod_preview', data)
                self.assertRegex(html, r'<button\b[^>]*\bid="vodCard"[^>]*\shidden\s*>')
                self.assertNotIn('data:image/png;base64,', html)
                for resource in (DEFAULT, LIVE):
                    self.assertNotIn(base64.b64encode(resource.read_bytes()).decode(), html)
                    self.assertNotIn(str(resource), html)
                self.assertNotIn('{{VOD_FALLBACK', html)
                self.assertNotIn(title, html)


class AvatarRequests(unittest.IsolatedAsyncioTestCase):
    def _setupAsyncioRunner(self):self._asyncioRunner=asyncio.Runner(loop_factory=asyncio.SelectorEventLoop)
    async def test_cache_limits_and_no_redirect(self):
        cache=AvatarCache();http=avatar_http()
        a=await cache.get(http,URL);self.assertIsInstance(a,ChannelImage)
        self.assertIs(a,await cache.get(http,URL));http.get.assert_called_once()
        self.assertIs(http.get.call_args.kwargs['allow_redirects'],False)
        self.assertIs(http.get.call_args.kwargs['auto_decompress'],False)
        for n in range(18):await cache.get(http,URL.replace('test-',str(n)+'-'))
        self.assertEqual(16,len(cache.entries))
    async def test_untrusted_redirect_bad_data_and_size_fall_back(self):
        for url,data,status in [('http://127.0.0.1/x',b'',200),(URL,b'x',302),(URL,b'bad',200),(URL,b'x'*(MAX_INPUT+1),200)]:
            cache=AvatarCache();http=avatar_http(data,status)
            self.assertEqual(DEFAULT,await cache.get(http,url))
            if url!=URL:http.get.assert_not_called()
            else:
                await cache.get(http,url);http.get.assert_called_once()
    async def test_stream_without_length_is_capped_and_failure_expires(self):
        from unittest.mock import patch
        cache=AvatarCache();http=avatar_http(b'x'*(MAX_INPUT+1));http.get.return_value.content_length=None
        self.assertEqual(DEFAULT,await cache.get(http,URL));http.get.assert_called_once()
        with patch('src.ui.channel_image.time.monotonic',return_value=10**20):
            self.assertEqual(DEFAULT,await cache.get(http,URL))
        self.assertEqual(2,http.get.call_count)

    async def test_busy_decoder_never_builds_queue(self):
        cache=AvatarCache();http=avatar_http()
        async with cache.lock:self.assertEqual(DEFAULT,await cache.get(http,URL))
        http.get.assert_not_called()

    async def test_repeated_cancel_keeps_decoder_slot_until_thread_finishes(self):
        import threading
        from unittest.mock import patch
        entered=threading.Event();release=threading.Event();original=compose
        def slow(data):
            entered.set();release.wait(2);return original(data)
        cache=AvatarCache();http=avatar_http()
        with patch('src.ui.channel_image.compose',side_effect=slow):
            task=asyncio.create_task(cache.get(http,URL))
            try:
                for _ in range(100):
                    if entered.is_set():break
                    await asyncio.sleep(.01)
                self.assertTrue(entered.is_set());task.cancel();await asyncio.sleep(0)
                task.cancel();await asyncio.sleep(0)
                self.assertTrue(cache.lock.locked())
                self.assertEqual(DEFAULT,await cache.get(http,URL.replace('test-','other-')))
                http.get.assert_called_once()
            finally:release.set()
            with self.assertRaises(asyncio.CancelledError):await task
            self.assertFalse(cache.lock.locked())
