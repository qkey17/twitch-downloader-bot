import json
from pathlib import Path


def render_viewer_html(
    chat_rows: list[dict],
    title: str,
    channel: str,
    vod_url: str,
    created_at: str | None = None,
    thumbnail_preview_url: str | None = None,
    thumbnail_full_url: str | None = None,
    mode: str = "online",
    channel_id: str | None = None,
    local_emotes: dict | None = None,
    cdn_emotes: dict | None = None,
    badge_images: dict | None = None,
) -> str:
    BASE_DIR = Path(__file__).resolve().parent
    template_path = BASE_DIR / "viewer_template.html"
    template = template_path.read_text(encoding="utf-8")

    payload = {
        "meta": {
            "title": title,
            "channel": channel,
            "vod_url": vod_url,
            "created_at": created_at,
            "thumbnail_preview_url": thumbnail_preview_url,
            "thumbnail_full_url": thumbnail_full_url,
            "mode": mode,
            "channel_id": channel_id,
            "local_emotes": local_emotes or {},
            "cdn_emotes": cdn_emotes or {},
            "badge_images": badge_images or {},
        },
        "rows": chat_rows,
    }

    data_json = json.dumps(payload, ensure_ascii=False).replace("<", "\\u003c")
    return template.replace("{{CHAT_DATA_JSON}}", data_json)
