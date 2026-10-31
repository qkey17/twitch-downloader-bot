import asyncio
import random
from src.config import FETCH_DELAY_BASE, FETCH_DELAY_MAX, GQL_MAX_RETRIES
from typing import Callable, Optional, List

import aiohttp

from .client import TwitchAPIError, COMMENTS_BODY_LIMIT
from .fetch_page import gql_fetch_page
from .meta import render_message, extract_message_fragments, extract_user_color, extract_user_badges
from src.pagination_diagnostics import CURRENT
from dataclasses import dataclass


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


@dataclass
class CommentPage:
    edges: list
    next_cursor: str | None
    has_next: bool
    number: int


def message_row(node):
    """The existing public seven-field row; identity stays internal."""
    commenter = node.get("commenter") or {}
    fragments = extract_message_fragments(node)
    text = render_message(node)
    return (node.get("contentOffsetSeconds"), node.get("createdAt") or "",
            commenter.get("displayName") or commenter.get("login") or "unknown",
            text, fragments,
            extract_user_color(node), extract_user_badges(node))


async def validated_pages(
    session: aiohttp.ClientSession,
    client_id: str,
    vod_id: str,
    start_offset: int = 0,
    cancel_check: Callable[[], bool] | None = None,
    *,
    before_attempt=None,
    pilot_branch=None,
    body_limit=None,
):
    """Shared validated-page/retry/cursor path; the caller owns row emission."""
    cursor = None
    delay_s = FETCH_DELAY_BASE
    page_number = 1
    cycle_anchor = None
    cycle_span = 1
    cycle_steps = 0
    diagnostic = CURRENT.get()

    while True:
        edges: List[dict] = []
        next_cursor: Optional[str] = None
        has_next = False

        for attempt in range(GQL_MAX_RETRIES):
            _check_cancel(cancel_check)
            if before_attempt is not None:
                before_attempt()
            if diagnostic is not None and pilot_branch is None:
                diagnostic.request(page_number, attempt + 1, cursor, start_offset)
            elif diagnostic is not None:
                diagnostic.event('repair_request', branch=pilot_branch, page=page_number,
                                 attempt=attempt + 1, request_type='cursor' if cursor else 'offset')
            try:
                # Buffered branches cannot mutate the ordinary journal's active
                # page/position. They have separate receive/emission events.
                token = CURRENT.set(None) if pilot_branch is not None else None
                body_token = COMMENTS_BODY_LIMIT.set(body_limit) if body_limit is not None else None
                try:
                    page = await gql_fetch_page(
                        session=session, client_id=client_id, vod_id=vod_id,
                        cursor=cursor, start_offset=start_offset, page_number=page_number,
                    )
                finally:
                    if body_token is not None:
                        COMMENTS_BODY_LIMIT.reset(body_token)
                    if token is not None:
                        CURRENT.reset(token)
            except TwitchAPIError as e:
                if diagnostic is not None:
                    diagnostic.error('request', e)
                _check_cancel(cancel_check)
                if not e.retryable or attempt == GQL_MAX_RETRIES - 1:
                    raise
                retry_delay = e.retry_after
                if retry_delay is None:
                    delay_s = min(FETCH_DELAY_MAX, max(delay_s * 1.6, 0.15))
                    retry_delay = delay_s + random.uniform(0, 0.2)
                await _sleep_with_cancel(retry_delay, cancel_check)
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                if diagnostic is not None:
                    diagnostic.error('request', e)
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

        if diagnostic is not None and pilot_branch is not None:
            diagnostic.repair_page(pilot_branch, page_number, attempt + 1,
                                   edges, cursor, next_cursor, has_next)

        if not edges:
            if diagnostic is not None:
                diagnostic.event('pagination_stop', reason='empty_page', page=page_number)
            return

        # Do not emit a repeated page or fill the disk indefinitely on no progress.
        if has_next:
            if next_cursor == cursor or next_cursor == cycle_anchor:
                if diagnostic is not None:
                    diagnostic.event('pagination_stop', reason='cursor_cycle', page=page_number)
                raise TwitchAPIError("Twitch comments pagination made no progress")
            # Brent's cycle check retains one anchor instead of an unbounded set
            # of cursors. Also stop multi-page cycles before accepting a result.
            if cycle_steps == cycle_span:
                cycle_anchor = next_cursor
                cycle_span *= 2
                cycle_steps = 0
            cycle_steps += 1

        yield CommentPage(edges, next_cursor, has_next, page_number)

        if not has_next or not next_cursor:
            if diagnostic is not None:
                diagnostic.event('pagination_stop', reason='has_next_false', page=page_number)
            return
        cursor = next_cursor
        page_number += 1

        if delay_s > FETCH_DELAY_BASE * 0.4:
            await _sleep_with_cancel(delay_s, cancel_check)


async def gql_fetch_comments(
    session: aiohttp.ClientSession,
    client_id: str,
    vod_id: str,
    start_offset: int = 0,
    cancel_check: Callable[[], bool] | None = None,
):
    """yields the unchanged public row; repair is an explicit one-window opt-in."""
    from .pagination_repair import settings, repaired_comments
    selected = settings(vod_id)
    if selected is not None:
        iterator = repaired_comments(session, client_id, vod_id, start_offset, cancel_check, selected)
    else:
        iterator = _ordinary_comments(session, client_id, vod_id, start_offset, cancel_check)
    try:
        async for row in iterator:
            yield row
    finally:
        await iterator.aclose()


async def _ordinary_comments(session, client_id, vod_id, start_offset, cancel_check):
    diagnostic = CURRENT.get()
    pages = validated_pages(session, client_id, vod_id, start_offset, cancel_check)
    try:
        async for page in pages:
            for position, edge in enumerate(page.edges):
                _check_cancel(cancel_check)
                row = message_row(edge.get("node") or {})
                if diagnostic is not None:
                    diagnostic.emitted_row(position, bool(row[3]))
                if row[3]:
                    yield row
            if diagnostic is not None:
                diagnostic.page_end()
    finally:
        await pages.aclose()
