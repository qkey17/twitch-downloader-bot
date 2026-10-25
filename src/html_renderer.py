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


def write_viewer_html(output, chat_rows, title, channel, vod_url, created_at=None,
                      thumbnail_preview_url=None, thumbnail_full_url=None, mode="online",
                      channel_id=None, local_emotes=None, cdn_emotes=None, badge_images=None,
                      cancel_check=None):
    """Write the same template/payload incrementally, with one row in memory."""
    template = Path(__file__).with_name("viewer_template.html").read_text(encoding="utf-8")
    prefix, marker, suffix = template.partition("{{CHAT_DATA_JSON}}")
    if not marker or marker in suffix:
        raise ValueError("Viewer template must contain exactly one data marker")
    meta = {
        "title": title, "channel": channel, "vod_url": vod_url, "created_at": created_at,
        "thumbnail_preview_url": thumbnail_preview_url, "thumbnail_full_url": thumbnail_full_url,
        "mode": mode, "channel_id": channel_id, "local_emotes": local_emotes or {},
        "cdn_emotes": cdn_emotes or {}, "badge_images": badge_images or {},
    }
    encoder = json.JSONEncoder(ensure_ascii=False)

    def write(text):
        if cancel_check is not None and cancel_check():
            raise RuntimeError("Загрузка была отменена.")
        written = output.write(text)
        if written != len(text):
            raise OSError("Incomplete HTML write")

    def write_json(value):
        for chunk in encoder.iterencode(value):
            write(chunk.replace("<", "\\u003c"))

    iterator = iter(chat_rows)
    try:
        write(prefix)
        write('{"meta": ')
        write_json(meta)
        write(', "rows": [')
        first = True
        for row in iterator:
            if not first:
                write(', ')
            write_json(row)
            first = False
        write(']}')
        write(suffix)
    finally:
        close = getattr(iterator, "close", None)
        if close is not None:
            close()
