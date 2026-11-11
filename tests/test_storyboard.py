import io
import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from PIL import Image

from src.html_renderer import render_viewer_html
from src.twitch_api import storyboard as sb
from test_viewer_data import payload


def spec(count, interval):
    return dict(quality='low', width=16, height=9, cols=5, rows=4,
                count=count, interval=interval,
                images=[f'123-low-{i}.jpg' for i in range((count + 19) // 20)])


class Response:
    def __init__(self, data, kind='application/json', status=200):
        self.data = data
        self.content = self
        self.content_length = len(data)
        self.content_type = kind
        self.status = status
        self.headers = {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        pass

    async def iter_chunked(self, size):
        for offset in range(0, len(self.data), size):
            yield self.data[offset:offset + size]


def encoded(value):
    return json.dumps(value).encode()


class StoryboardTests(unittest.IsolatedAsyncioTestCase):
    def test_sampling_covers_100_hours_and_bounds_requests(self):
        layout, atlases = sb.plan([spec(3600, 100)], '123', 360000)
        times = [t for _, cells in atlases for _, t in cells]
        self.assertEqual((16, 9, 5, 4), layout)
        self.assertEqual(sb.MAX_ATLASES, len(atlases))
        self.assertEqual(sb.MAX_ATLASES, len(times))
        self.assertEqual((0, 359900), (times[0], times[-1]))
        self.assertEqual(times, sorted(set(times)))
        self.assertLessEqual(max(b-a for a, b in zip(times, times[1:])), 32800)

    def test_rejects_bad_paths_incomplete_specs_and_untrusted_hosts(self):
        asset = spec(21, 100)
        for change in ({'images': asset['images'][:1]}, {'interval': None}, {'width': 10000},
                       {'images': ['https://example.test/a.jpg', asset['images'][1]]}):
            self.assertIsNone(sb.plan([{**asset, **change}], '123', 2100))
        self.assertIsNone(sb.manifest_url(
            'https://d1111111111.cloudfront.net/key/storyboards/123-info.json', '123', None))
        self.assertIsNone(sb.manifest_url(
            f'https://{sb.OBSERVED_CDN}/key/storyboards/124-info.json', '123', None))

    async def test_acquires_cover_and_atlas_and_falls_back_on_bad_twitch_response(self):
        with io.BytesIO() as out:
            Image.new('RGB', (80, 36), 'blue').save(out, 'JPEG')
            atlas = out.getvalue()
        manifest = f'https://{sb.OBSERVED_CDN}/vod-key/storyboards/123-info.json'
        cover = 'https://static-cdn.jtvnw.net/cf_vods/test/thumb.jpg'
        meta = SimpleNamespace(vod_id='123', length_seconds=2000, is_live=False, thumbnail_preview_url=cover)
        gql = Response(encoded({'data': {'video': {'id': '123', 'lengthSeconds': 2000,
                                                   'seekPreviewsURL': manifest}}}))
        session = SimpleNamespace(post=Mock(return_value=gql), get=Mock(side_effect=[
            Response(atlas, 'image/jpeg'), Response(encoded([spec(20, 100)])),
            Response(atlas, 'binary/octet-stream'),
        ]))
        result = await sb.fetch_preview(session, meta, lambda: None)
        self.assertEqual(list(range(0, 2000, 100)), [time for time, _ in result['frames']])
        self.assertTrue(result['cover'].startswith(sb.JPEG_PREFIX))
        for call in [*session.get.call_args_list, *session.post.call_args_list]:
            self.assertFalse(call.kwargs['allow_redirects'])
        session.get = Mock(side_effect=[Response(atlas, 'image/jpeg'), Response(b'', status=302)])
        self.assertEqual({'cover': result['cover']}, await sb.fetch_preview(session, meta, lambda: None))
        session.get = Mock(side_effect=[
            Response(atlas, 'image/jpeg'), Response(encoded([spec(20, 100)])),
            Response(b'not an image', 'image/jpeg'),
        ])
        self.assertEqual({'cover': result['cover']}, await sb.fetch_preview(session, meta, lambda: None))
        meta.is_live = True
        self.assertEqual({}, await sb.fetch_preview(session, meta, lambda: None))

    async def test_streamed_body_is_bounded_without_content_length(self):
        response = Response(b'x' * 32)
        response.content_length = None
        with self.assertRaises(ValueError):
            await sb.read_asset(response, 16, {'application/json'})

    def test_html_reduces_frames_to_budget_without_losing_endpoints(self):
        args = ([], 't', 'c', 'v')
        base = render_viewer_html(*args)
        frames = [[i, sb.JPEG_PREFIX + 'A' * 50_000] for i in range(96)]
        cover = sb.JPEG_PREFIX + 'AAAA'
        assets = {'frames': frames, 'cover': cover}
        page = render_viewer_html(*args, preview_assets=assets)
        self.assertLessEqual(len(page) - len(base), sb.EMBED_LIMIT)
        actual = payload(page)['vod_preview']
        self.assertEqual((0, 95), (actual['frames'][0][0], actual['frames'][-1][0]))
        self.assertEqual(cover, actual['cover'])
        base_size = len(base.replace('\n', os.linesep).encode('utf-8'))
        with patch('src.html_renderer.MAX_CLOUD_HTML_BYTES', base_size + 500):
            self.assertEqual({'cover': cover}, payload(render_viewer_html(*args, preview_assets=assets))['vod_preview'])
