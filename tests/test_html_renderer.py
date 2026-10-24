import json
import unittest

from src.html_renderer import render_viewer_html


class HtmlRendererSerializationTests(unittest.TestCase):
    def _serialized_payload(self, **kwargs):
        html = render_viewer_html(
            chat_rows=kwargs.pop("chat_rows", []),
            title=kwargs.pop("title", "Title"),
            channel=kwargs.pop("channel", "channel"),
            vod_url=kwargs.pop("vod_url", "https://www.twitch.tv/videos/123"),
            **kwargs,
        )
        start_marker = '<script id="chat-data" type="application/json">'
        start = html.index(start_marker) + len(start_marker)
        end = html.index("</script>", start)
        return html[start:end]

    def test_script_closing_tag_variants_are_safe_and_round_trip(self):
        values = [
            "</script>",
            "</SCRIPT>",
            "</ScRiPt>",
            "<script>alert('x')</script><b>html</b>",
        ]

        serialized = self._serialized_payload(
            title=values[0],
            chat_rows=[{"user": values[1], "text": values[2], "fragments": [{"text": values[3]}]}],
        )

        self.assertNotIn("<", serialized)
        parsed = json.loads(serialized)
        self.assertEqual(parsed["meta"]["title"], values[0])
        self.assertEqual(parsed["rows"][0]["user"], values[1])
        self.assertEqual(parsed["rows"][0]["text"], values[2])
        self.assertEqual(parsed["rows"][0]["fragments"][0]["text"], values[3])

    def test_quotes_backslashes_ampersand_unicode_and_emoji_round_trip(self):
        value = 'quotes: " and \\; ampersand: &; Unicode: Привет; emoji: 🎮✨'
        serialized = self._serialized_payload(
            title=value,
            chat_rows=[{"text": value}],
            cdn_emotes={"🎮&": "https://cdn.example/emote?a=1&b=2"},
        )

        parsed = json.loads(serialized)
        self.assertEqual(parsed["meta"]["title"], value)
        self.assertEqual(parsed["rows"][0]["text"], value)
        self.assertEqual(parsed["meta"]["cdn_emotes"], {"🎮&": "https://cdn.example/emote?a=1&b=2"})

    def test_ordinary_payload_keeps_its_json_structure_and_semantics(self):
        rows = [{
            "t": "00:01:02",
            "user": "viewer",
            "text": "hello world",
            "fragments": [{"text": "hello world"}],
            "color": "#ffffff",
            "badges": [{"set_id": "subscriber", "version": "1"}],
        }]

        parsed = json.loads(self._serialized_payload(
            chat_rows=rows,
            title="Vod title",
            channel="streamer",
            vod_url="https://www.twitch.tv/videos/123",
            created_at="2026-01-01T00:00:00Z",
            thumbnail_preview_url="https://cdn.example/preview.jpg",
            thumbnail_full_url="https://cdn.example/full.jpg",
            mode="online",
            channel_id="456",
            local_emotes={"Kappa": "/kappa.png"},
            cdn_emotes={"Pog": "https://cdn.example/pog.png"},
            badge_images={"subscriber/1": "https://cdn.example/badge.png"},
        ))

        self.assertEqual(set(parsed), {"meta", "rows"})
        self.assertEqual(parsed["rows"], rows)
        self.assertEqual(parsed["meta"], {
            "title": "Vod title",
            "channel": "streamer",
            "vod_url": "https://www.twitch.tv/videos/123",
            "created_at": "2026-01-01T00:00:00Z",
            "thumbnail_preview_url": "https://cdn.example/preview.jpg",
            "thumbnail_full_url": "https://cdn.example/full.jpg",
            "mode": "online",
            "channel_id": "456",
            "local_emotes": {"Kappa": "/kappa.png"},
            "cdn_emotes": {"Pog": "https://cdn.example/pog.png"},
            "badge_images": {"subscriber/1": "https://cdn.example/badge.png"},
        })


if __name__ == "__main__":
    unittest.main()
