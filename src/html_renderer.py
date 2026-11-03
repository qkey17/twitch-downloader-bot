import io
import json
from pathlib import Path
from src.viewer_data import CompactRows, FORMAT
from src.viewer_notices import prepare_notice


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
    captured_during_live: bool = False,
    duration_seconds: int | None = None,
) -> str:
    # The compatibility API shares the exact streaming encoder; it does not build
    # a second complete array of compact rows alongside chat_rows.
    output = io.StringIO()
    write_viewer_html(output, chat_rows, title, channel, vod_url, created_at,
                      thumbnail_preview_url, thumbnail_full_url, mode, channel_id,
                      local_emotes, cdn_emotes, badge_images,
                      captured_during_live=captured_during_live, duration_seconds=duration_seconds)
    return output.getvalue()


def write_viewer_html(output, chat_rows, title, channel, vod_url, created_at=None,
                      thumbnail_preview_url=None, thumbnail_full_url=None, mode="online",
                      channel_id=None, local_emotes=None, cdn_emotes=None, badge_images=None,
                      cancel_check=None, captured_during_live=False, duration_seconds=None):
    """Write the same template/payload incrementally, with one row in memory."""
    template = Path(__file__).with_name("viewer_template.html").read_text(encoding="utf-8")
    from src.ui.card_images import fallback_data
    template = template.replace("{{VOD_FALLBACK_PNG}}",fallback_data(False)).replace("{{LIVE_FALLBACK_PNG}}",fallback_data(True))
    prefix, marker, suffix = template.partition("{{CHAT_DATA_JSON}}")
    if not marker or marker in suffix:
        raise ValueError("Viewer template must contain exactly one data marker")
    meta = {
        "title": title, "channel": channel, "vod_url": vod_url, "created_at": created_at,
        "thumbnail_preview_url": thumbnail_preview_url, "thumbnail_full_url": thumbnail_full_url,
        "mode": mode, "channel_id": channel_id, "local_emotes": local_emotes or {},
        "cdn_emotes": cdn_emotes or {}, "badge_images": badge_images or {},
        **({'captured_during_live': True} if captured_during_live is True else {}),
    }
    if type(duration_seconds) is int and 0 < duration_seconds <= 2**53-1:
        meta['duration_seconds'] = duration_seconds
    encoder = json.JSONEncoder(ensure_ascii=False, separators=(',', ':'))
    compact = CompactRows()

    def write(text):
        if cancel_check is not None and cancel_check():
            raise RuntimeError("Загрузка была отменена.")
        written = output.write(text)
        if written != len(text):
            raise OSError("Incomplete HTML write")

    def write_json(value, *, streaming=False):
        # One message can be encoded at once. Metadata may contain a large
        # offline image catalog, so never materialize that whole JSON string.
        chunks = encoder.iterencode(value) if streaming else (encoder.encode(value),)
        for chunk in chunks:
            write(chunk.replace("<", "\\u003c"))

    iterator = iter(chat_rows)
    try:
        write(prefix)
        write('{"format":')
        write_json(FORMAT)
        write(',"meta":')
        write_json(meta, streaming=True)
        write(',"rows":[')
        first = True
        for row in iterator:
            if not first:
                write(',')
            write_json(compact.encode(prepare_notice(row)))
            first = False
        write('],"users":')
        write_json(compact.users.values, streaming=True)
        write(',"badges":')
        write_json(compact.badges.values, streaming=True)
        write('}')
        write(suffix)
    finally:
        close = getattr(iterator, "close", None)
        if close is not None:
            close()
