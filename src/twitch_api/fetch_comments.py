import asyncio
import random
from src.config import FETCH_DELAY_BASE, FETCH_DELAY_MAX, GQL_MAX_RETRIES
from typing import Callable, Optional, List

import aiohttp

from .client import TwitchAPIError
from .fetch_page import gql_fetch_page
from .meta import render_message, extract_message_fragments, extract_user_color, extract_user_badges


def _check_cancel(cancel_check: Callable[[], bool] | None) -> None:
    if cancel_check is not None and cancel_check():
        raise RuntimeError("Загрузка была отменена.")


async def _sleep_with_cancel(delay: float, cancel_check: Callable[[], bool] | None) -> None:
    end_time = asyncio.get_running_loop().time() + delay
    while True:
        _check_cancel(cancel_check)
        remaining = end_time - asyncio.get_running_loop().time()
        if remaining <= 0:
            return
        await asyncio.sleep(min(0.1, remaining))


async def gql_fetch_comments(
    session: aiohttp.ClientSession,
    client_id: str,
    vod_id: str,
    start_offset: int = 0,
    cancel_check: Callable[[], bool] | None = None,
):
    """yields (offset_seconds, created_at_iso, display_name, message_text, fragments, color, badges)"""
    cursor = None
    delay_s = FETCH_DELAY_BASE
    page_number = 1

    while True:
        edges: List[dict] = []
        next_cursor: Optional[str] = None
        has_next = False

        for attempt in range(GQL_MAX_RETRIES):
            _check_cancel(cancel_check)
            try:
                page = await gql_fetch_page(
                    session=session,
                    client_id=client_id,
                    vod_id=vod_id,
                    cursor=cursor,
                    start_offset=start_offset,
                    page_number=page_number,
                )
            except TwitchAPIError as e:
                _check_cancel(cancel_check)
                if not e.retryable or attempt == GQL_MAX_RETRIES - 1:
                    raise
                retry_delay = e.retry_after
                if retry_delay is None:
                    delay_s = min(FETCH_DELAY_MAX, max(delay_s * 1.6, 0.15))
                    retry_delay = delay_s + random.uniform(0, 0.2)
                await _sleep_with_cancel(retry_delay, cancel_check)
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                _check_cancel(cancel_check)
                if attempt == GQL_MAX_RETRIES - 1:
                    raise TwitchAPIError("Twitch network request failed after retries") from e
                delay_s = min(FETCH_DELAY_MAX, max(delay_s * 1.6, 0.15))
                await _sleep_with_cancel(delay_s + random.uniform(0, 0.2), cancel_check)
            else:
                _check_cancel(cancel_check)
                edges, next_cursor, has_next = page
                delay_s = max(FETCH_DELAY_BASE * 0.35, delay_s * 0.75)
                break

        if not edges:
            return

        for edge in edges:
            _check_cancel(cancel_check)
            node = edge.get("node") or {}
            commenter = node.get("commenter") or {}
            display = commenter.get("displayName") or commenter.get("login") or "unknown"
            offset = node.get("contentOffsetSeconds")
            created_at = node.get("createdAt") or ""
            fragments = extract_message_fragments(node)
            text = render_message(node)
            color = extract_user_color(node)
            badges = extract_user_badges(node)
            if text:
                yield (offset, created_at, display, text, fragments, color, badges)

        if not has_next or not next_cursor:
            return
        cursor = next_cursor
        page_number += 1

        if delay_s > FETCH_DELAY_BASE * 0.4:
            await _sleep_with_cancel(delay_s, cancel_check)
