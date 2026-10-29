import time
import logging
import threading
import asyncio
from datetime import datetime
from pathlib import Path
from typing import Tuple, List, Dict, Optional

from src.config import OUT_DIR, SPOOL_DIR, SPOOL_BATCH_MESSAGES, SPOOL_BATCH_BYTES
from src.chat_spool import ChatSpool, encode_row
from src.resource_usage import log_resources, stage
from .worker_io import run_blocking
from src.twitch_api import fetch_vod_meta, gql_fetch_comments, get_client_id
from src.ui.ui import fmt_hhmmss

from .worker_progress import make_progress_updater
from .worker_html import build_html_result

from src.handlers.handlers_state import clear_cancel, is_cancelled, raise_if_cancelled


async def download_and_send(
    context,
    chat_id: int,
    progress_message_id: int,
    vod_url: str,
    vod_id: str,
    fmt: str,
    operation_id: str | None = None,
    publication=None,
) -> Tuple[Dict, Dict, List[Dict[str, str]], Optional[str]]:
    started_at = time.monotonic()
    spool = None
    comment_iter = None
    spool_bytes = 0
    stop = threading.Event()
    cancel_check = lambda: stop.is_set() or is_cancelled(context, operation_id)
    try:
        # Direct callers without an operation token retain the legacy behavior;
        # production runs clear stale cancellation state when they are reserved.
        if operation_id is None:
            clear_cancel(context)
        else:
            raise_if_cancelled(context, operation_id)

        session = context.application.bot_data["aiohttp"]
        client_id = get_client_id()

        raise_if_cancelled(context, operation_id)
        with stage('vod_metadata'):
            meta = await fetch_vod_meta(
                session,
                client_id,
                vod_id,
                cancel_check=lambda: is_cancelled(context, operation_id),
            )
            raise_if_cancelled(context, operation_id)

        meta_dict = {
            "title": meta.title,
            "channel": meta.channel,
            "length_seconds": meta.length_seconds,
            "vod_len": fmt_hhmmss(int(meta.length_seconds)) if isinstance(meta.length_seconds, int) else "—",
            "created_at": meta.created_at,
            "thumbnail_preview_url": meta.thumbnail_preview_url,
            "thumbnail_full_url": meta.thumbnail_full_url,
        }

        out_dir = Path(OUT_DIR)
        out_dir.mkdir(exist_ok=True)

        safe_ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        base_stem = f"vod_{vod_id}_{safe_ts}_{fmt}"

        progress = make_progress_updater(context, chat_id, progress_message_id, meta_dict, vod_url, fmt)

        last_progress_time = time.monotonic()
        progress_interval_s = 0.5
        progress_every_n = 200

        raise_if_cancelled(context, operation_id)
        await progress(0, 0, done=False)
        raise_if_cancelled(context, operation_id)

        with stage('messages_spool'):
            comment_iter = gql_fetch_comments(
                session,
                client_id,
                vod_id,
                cancel_check=lambda: is_cancelled(context, operation_id),
            )

            try:
                raise_if_cancelled(context, operation_id)
                first = await asyncio.wait_for(comment_iter.__anext__(), timeout=10)
                raise_if_cancelled(context, operation_id)
            except StopAsyncIteration:
                raise RuntimeError("CHAT_EMPTY")

            spool = ChatSpool(SPOOL_DIR)
            await run_blocking(spool.open, on_cancel=stop.set)
            batch = []
            batch_bytes = 0
            received = 0

            async def flush():
                nonlocal batch, batch_bytes, spool_bytes
                if not batch:
                    return
                pending_batch, batch = batch, []
                batch_bytes = 0
                await run_blocking(spool.append_batch, pending_batch, cancel_check, on_cancel=stop.set)
                spool_bytes = spool.size_bytes()
                if spool.messages % 5000 == 0:
                    log_resources("chat_spooled", messages=spool.messages, spool_bytes=spool_bytes)

            async def save(comment):
                nonlocal batch_bytes, received, last_progress_time
                raise_if_cancelled(context, operation_id)
                offset, created_at, user, text, fragments, color, badges = comment
                t = fmt_hhmmss(int(offset)) if isinstance(offset, (int, float)) else "00:00:00"
                row = {"t": t, "user": user, "text": text, "fragments": fragments,
                       "color": color, "badges": badges}
                encoded = encode_row(row)
                size = len(encoded[1].encode("utf-8"))
                if batch and batch_bytes + size > SPOOL_BATCH_BYTES:
                    await flush()
                batch.append(encoded)
                batch_bytes += size
                received += 1
                now = time.monotonic()
                if (len(batch) >= SPOOL_BATCH_MESSAGES or batch_bytes >= SPOOL_BATCH_BYTES
                        or received % progress_every_n == 0 or now - last_progress_time >= progress_interval_s):
                    await flush()
                if received % progress_every_n == 0 or now - last_progress_time >= progress_interval_s:
                    last_progress_time = now
                    await progress(spool.messages, spool.unique_users, done=False)
                    raise_if_cancelled(context, operation_id)

            await save(first)
            async for comment in comment_iter:
                await save(comment)
            await flush()
            raise_if_cancelled(context, operation_id)
            await progress(spool.messages, spool.unique_users, done=True)
            raise_if_cancelled(context, operation_id)

        sent_files: List[Dict[str, str]] = []
        html_result_ref: Optional[str] = None

        if publication is not None:
            publication.meta = meta_dict
            publication.stats = {"messages":spool.messages,"unique_users":spool.unique_users,"parts":0}
        sent_files, html_result_ref = await build_html_result(
            context=context,
            session=session,
            chat_id=chat_id,
            fmt=fmt,
            meta=meta,
            vod_url=vod_url,
            base_stem=base_stem,
            out_dir=out_dir,
            chat_rows=spool,
            token_counter=None,
            cancel_check=cancel_check,
            publication=publication,
        )

        meta_dict["html_url"] = html_result_ref

        stats = {
            "messages": spool.messages,
            "unique_users": spool.unique_users,
            "parts": len(sent_files),
        }

        return meta_dict, stats, sent_files, html_result_ref

    finally:
        try:
            if comment_iter is not None:
                close = getattr(comment_iter, "aclose", None)
                if close is not None:
                    await close()
        finally:
            try:
                if spool is not None:
                    try:
                        await run_blocking(spool.close)
                    except OSError as exc:
                        logging.getLogger(__name__).warning("Temporary chat cleanup failed: %s", type(exc).__name__)
            finally:
                clear_cancel(context, operation_id)
                log_resources("chat_operation_end", messages=spool.messages if spool else 0,
                              spool_bytes=spool_bytes, elapsed_seconds=round(time.monotonic() - started_at, 3))
