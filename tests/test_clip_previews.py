import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from src.html_renderer import render_viewer_html
from src.twitch_api.clips import MAX_CLIPS, clip_metadata, collect_clip_ids, fetch_clip_metadata
from test_storyboard import Response
from test_viewer_data import payload


class ClipPreviewTests(unittest.IsolatedAsyncioTestCase):
    def test_clip_links_are_deduplicated_and_bounded(self):
        ids = {}
        collect_clip_ids("(https://clips.twitch.tv/Slug?t=1) twitch.tv/user/clip/Slug "
                         "www.twitch.tv/user/clip/Other/?tracking=x "
                         "https://evil.twitch.tv/user/clip/Fake "
                         "https://clips.twitch.tv/Not/Clip javascript:clips.twitch.tv/Bad", ids)
        self.assertEqual(["Slug", "Other"], list(ids))
        collect_clip_ids(" ".join(f"clips.twitch.tv/Clip{i}" for i in range(200)), ids)
        self.assertEqual(MAX_CLIPS, len(ids))

    def test_thumbnail_url_path_escapes_and_authority(self):
        base = "https://clips-media-assets2.twitch.tv"
        for url, accepted in (
            (base + "/example%7Cpreview.jpg", True),
            (base + "/example.jpg", True),
            (base + "/example%7Gpreview.jpg", False),
            (base + "/example%.jpg", False),
            (base + ".evil.test/example%7Cpreview.jpg", False),
            (base + "@evil.test/example%7Cpreview.jpg", False),
            (base + "/example.jpg\n", False),
        ):
            with self.subTest(url=url):
                result = clip_metadata(dict(title="Clip", creator_name="Author", thumbnail_url=url))
                self.assertEqual(url if accepted else None, result and result["thumbnail"])

    @patch("src.twitch_api.clips.get_api_headers", return_value={"Authorization": "Bearer fixture-secret"})
    async def test_batched_metadata_and_safe_optional_payload(self, headers):
        clip = dict(id="Slug", title="</script><img onerror=bad()>", creator_name="Author",
                    thumbnail_url="https://clips-media-assets2.twitch.tv/clip-preview-480x272.jpg")
        bad = dict(clip, id="Bad", thumbnail_url="https://clips-media-assets2.twitch.tv.evil.test/image.jpg")
        session = SimpleNamespace(get=Mock(return_value=Response(json.dumps({"data": [clip,bad]}).encode())))
        result = await fetch_clip_metadata(session, ["Slug","Slug","Bad","Deleted"])
        session.get.assert_called_once()
        self.assertEqual([("id","Slug"),("id","Bad"),("id","Deleted")], session.get.call_args.kwargs["params"])
        self.assertFalse(session.get.call_args.kwargs["allow_redirects"])
        self.assertEqual(["Slug"], list(result))
        page = render_viewer_html([], "title", "channel", "vod", clips=result)
        self.assertEqual(result, payload(page)["clips"])
        self.assertNotIn(clip["title"], page)
        self.assertNotIn("fixture-secret", page)
        session.get.return_value = Response(b"", status=503)
        self.assertEqual({}, await fetch_clip_metadata(session, ["Slug"]))
        session.get.side_effect = TimeoutError
        self.assertEqual({}, await fetch_clip_metadata(session, ["Slug"]))
