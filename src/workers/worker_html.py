import threading
from pathlib import Path
from typing import Callable, Dict, List, Optional
from collections import Counter

from src.config import HTML_PUBLISH_MODE
from src.html_renderer import render_viewer_html, write_viewer_html
from src.html_publisher import publish_html, publish_html_local, publish_html_stream
from src.chat_spool import ChatSpool
from .worker_io import run_blocking
from src.twitch_api import (
    fetch_7tv_emote_map,
    fetch_bttv_emote_map,
    fetch_ffz_emote_map,
    fetch_twitch_global_emote_map,
    fetch_twitch_channel_emote_maps,
    fetch_twitch_global_badge_map,
    fetch_twitch_channel_badge_map,
)


async def build_html_result(
    context,
    session,
    chat_id: int,
    fmt: str,
    meta,
    vod_url: str,
    base_stem: str,
    out_dir: Path,
    chat_rows: List[Dict] | ChatSpool,
    token_counter: Counter,
    cancel_check: Callable[[], bool] | None = None,
    publication=None,
):
    stop = threading.Event()

    def check_cancelled():
        if stop.is_set() or (cancel_check is not None and cancel_check()):
            raise RuntimeError("Загрузка была отменена.")

    sent_files: List[Dict[str, str]] = []
    html_result_ref: Optional[str] = None

    cdn_emotes: Dict[str, str] = {}
    combined_map: Dict[str, str] = {}
    twitch_emote_id_map: Dict[str, str] = {}
    badge_images: Dict[str, str] = {}

    # 7TV
    if meta.channel_id:
        check_cancelled()
        m = await fetch_7tv_emote_map(session, meta.channel_id)
        check_cancelled()
        combined_map.update(m)

    # BTTV
    if meta.channel_id:
        check_cancelled()
        m = await fetch_bttv_emote_map(session, meta.channel_id, cancel_check=cancel_check)
        check_cancelled()
        combined_map.update(m)

    # FFZ
    if meta.channel:
        check_cancelled()
        m = await fetch_ffz_emote_map(session, meta.channel, cancel_check=cancel_check)
        check_cancelled()
        combined_map.update(m)

    # Twitch Global
    check_cancelled()
    m = await fetch_twitch_global_emote_map(session)
    check_cancelled()
    combined_map.update(m)

    # Twitch Channel
    if meta.channel_id:
        check_cancelled()
        name_map, id_map = await fetch_twitch_channel_emote_maps(session, meta.channel_id)
        check_cancelled()
        combined_map.update(name_map)
        twitch_emote_id_map.update(id_map)
        # Channel/subscriber emotes should be available even if the text scan misses
        # a token boundary or the row has no fragment metadata.
        cdn_emotes.update(name_map)
        cdn_emotes.update(id_map)

    # Twitch badges
    check_cancelled()
    badge_images.update(await fetch_twitch_global_badge_map(session))
    check_cancelled()
    if meta.channel_id:
        check_cancelled()
        badge_images.update(await fetch_twitch_channel_badge_map(session, str(meta.channel_id)))
        check_cancelled()

    def render_and_publish():
        targets = set()
        targets_by_id = set()

        rows = chat_rows.iter_rows() if isinstance(chat_rows, ChatSpool) else iter(chat_rows)
        try:
            for row in rows:
                check_cancelled()
                text = row.get("text") or ""
                for word in text.split():
                    if word in combined_map:
                        targets.add(word)

                for fragment in row.get("fragments") or []:
                    if not isinstance(fragment, dict):
                        continue
                    frag_text = (fragment.get("text") or "").strip()
                    if frag_text and frag_text in combined_map:
                        targets.add(frag_text)

                    emote = fragment.get("emote") or {}
                    if isinstance(emote, dict):
                        emote_id = emote.get("emoteID") or emote.get("id")
                        if emote_id and str(emote_id) in twitch_emote_id_map:
                            targets_by_id.add(str(emote_id))
        finally:
            close = getattr(rows, "close", None)
            if close is not None:
                close()

        # HTML ONLINE — используем CDN ссылки эмоутов
        for name in targets:
            cdn_emotes[name] = combined_map[name]
        for emote_id in targets_by_id:
            cdn_emotes[emote_id] = twitch_emote_id_map[emote_id]

        check_cancelled()
        options = dict(
            title=(meta.title or "—"),
            channel=(meta.channel or "—"),
            vod_url=vod_url,
            created_at=meta.created_at,
            thumbnail_preview_url=meta.thumbnail_preview_url,
            thumbnail_full_url = meta.thumbnail_full_url,
            mode="online",
            channel_id=meta.channel_id,
            local_emotes={},
            cdn_emotes=cdn_emotes,
            badge_images=badge_images,
        )

        if isinstance(chat_rows, ChatSpool):
            if HTML_PUBLISH_MODE not in {"local", "public"}:
                raise ValueError(f"Unsupported HTML publication mode: {HTML_PUBLISH_MODE}")

            def write_content(output):
                write_viewer_html(output, chat_rows.iter_rows(), **options,
                                  cancel_check=lambda: stop.is_set() or (cancel_check and cancel_check()))

            return publish_html_stream(
                write_content, local=HTML_PUBLISH_MODE == "local",
                cancel_check=lambda: stop.is_set() or (cancel_check and cancel_check()),
                publication=publication,
            )

        # Retain the small, in-memory compatibility interface for direct callers.
        html_text = render_viewer_html(chat_rows=chat_rows, **options)

        check_cancelled()
        if HTML_PUBLISH_MODE == "local":
            html_result_ref = publish_html_local(html_text)
        elif HTML_PUBLISH_MODE == "public":
            html_result_ref = publish_html(html_text)
        else:
            raise ValueError(f"Unsupported HTML publication mode: {HTML_PUBLISH_MODE}")

        return html_result_ref

    html_result_ref = await run_blocking(render_and_publish, on_cancel=stop.set)

    return sent_files, html_result_ref
