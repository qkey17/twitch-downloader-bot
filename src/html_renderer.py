import io
import json
import logging
import os
from pathlib import Path
from src.viewer_data import CompactRows, FORMAT
from src.viewer_notices import prepare_notice
from src.ready_html import MAX_CLOUD_HTML_BYTES
from src.twitch_api.storyboard import EMBED_LIMIT, spread


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
    *,
    channel_login: str | None = None,
    badge_titles: dict | None = None,
    cdn_emote_sources: dict | None = None,
    global_twitch_emote_ids: list[str] | None = None,
    subscriber_twitch_emote_ids: list[str] | None = None,
    preview_assets: dict | None = None,
) -> str:
    # The compatibility API shares the exact streaming encoder; it does not build
    # a second complete array of compact rows alongside chat_rows.
    output = io.StringIO()
    write_viewer_html(output, chat_rows, title, channel, vod_url, created_at,
                      thumbnail_preview_url, thumbnail_full_url, mode, channel_id,
                      local_emotes, cdn_emotes, badge_images,
                      captured_during_live=captured_during_live, duration_seconds=duration_seconds,
                      channel_login=channel_login, badge_titles=badge_titles, cdn_emote_sources=cdn_emote_sources,
                      global_twitch_emote_ids=global_twitch_emote_ids,
                      subscriber_twitch_emote_ids=subscriber_twitch_emote_ids, preview_assets=preview_assets)
    return output.getvalue()


def write_viewer_html(output, chat_rows, title, channel, vod_url, created_at=None,
                      thumbnail_preview_url=None, thumbnail_full_url=None, mode="online",
                      channel_id=None, local_emotes=None, cdn_emotes=None, badge_images=None,
                      cancel_check=None, captured_during_live=False, duration_seconds=None, *,
                      channel_login=None, badge_titles=None, cdn_emote_sources=None, global_twitch_emote_ids=None,
                      subscriber_twitch_emote_ids=None, preview_assets=None):
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
        **({'captured_during_live': True} if captured_during_live is True else {}),
    }
    if type(duration_seconds) is int and 0 < duration_seconds <= 2**53-1:
        meta['duration_seconds'] = duration_seconds
    for key, value in (
        ('channel_login', channel_login),
        ('badge_titles', badge_titles),
        ('cdn_emote_sources', cdn_emote_sources),
        ('global_twitch_emote_ids', global_twitch_emote_ids),
        ('subscriber_twitch_emote_ids', subscriber_twitch_emote_ids),
    ):
        if value:
            meta[key] = value
    encoder = json.JSONEncoder(ensure_ascii=False, separators=(',', ':'))
    compact = CompactRows()
    output_bytes = 0

    def byte_count(text):
        return len(text.replace('\n', os.linesep).encode('utf-8'))

    def write(text):
        nonlocal output_bytes
        if cancel_check is not None and cancel_check():
            raise RuntimeError("Загрузка была отменена.")
        written = output.write(text)
        if written != len(text):
            raise OSError("Incomplete HTML write")
        if preview_assets:
            output_bytes += byte_count(text)

    def write_json(value, *, streaming=False):
        # One message can be encoded at once. Metadata may contain a large
        # offline image catalog, so never materialize that whole JSON string.
        chunks = encoder.iterencode(value) if streaming else (encoder.encode(value),)
        for chunk in chunks:
            write(chunk.replace("<", "\\u003c"))

    def write_template(part):
        chunk_size = 64 * 1024
        for start in range(0, len(part), chunk_size):
            write(part[start:start + chunk_size])

    iterator = iter(chat_rows)
    try:
        write_template(prefix)
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
        if preview_assets:
            budget = min(EMBED_LIMIT, MAX_CLOUD_HTML_BYTES - output_bytes - byte_count(suffix) - 1)
            assets = preview_assets.copy()
            cover_fallback = assets.pop('cover_fallback', None)
            frames = assets.pop('frames', [])
            aspect = assets.pop('aspect', None)
            storyboard = {**assets, 'aspect': aspect} if aspect else assets
            frame_sizes = [len(encoder.encode(frame)) for frame in frames]
            prefix_size = len(',"vod_preview":')
            frame_overhead = prefix_size + len(encoder.encode({**storyboard, 'frames': []}))
            for count in range(len(frames), -1, -1):
                if count == 1 and len(frames) > 1:
                    continue
                indexes = spread(len(frames), count)
                size = (frame_overhead + sum(frame_sizes[i] for i in indexes) + count - 1
                        if count else prefix_size + len(encoder.encode(assets)))
                if not count and frames:
                    logging.getLogger(__name__).info('HTML storyboard: embed_budget_exceeded')
                if not count and size > budget and cover_fallback:
                    assets['cover'] = cover_fallback
                    size = prefix_size + len(encoder.encode(assets))
                if not count and not assets:
                    break
                if size <= budget:
                    selected = {**storyboard, 'frames': [frames[i] for i in indexes]} if count else assets
                    encoded = ',"vod_preview":' + encoder.encode(selected).replace("<", "\\u003c")
                    write_template(encoded)
                    break
        write('}')
        write_template(suffix)
    finally:
        close = getattr(iterator, "close", None)
        if close is not None:
            close()
