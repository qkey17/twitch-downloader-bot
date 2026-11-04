"""Telegram-only live observation. Historical capture and HTML stay unchanged."""
import asyncio
import time
from src.live_capture import captured_live
from src.twitch_api.live import stream_observation, match_stream, TTL
from src.twitch_api.videos import _get_json, _id

LIVE = '🔴 LIVE'
WARNING = '⚠️ Готовый чат может быть неполным, так как был обработан во время прямой трансляции'


def lines(item, *, ready=False):
    state = item.get('live_state') or {}
    stamp = state.get('checked_at') if isinstance(state, dict) else None
    age = time.monotonic() - stamp if type(stamp) in (int,float) else float('inf')
    live = isinstance(state, dict) and state.get('value') is True and 0 <= age < TTL
    return ('\n'+LIVE if live else '') + ('\n'+WARNING if ready and captured_live(item) else '')


def from_metadata(meta):
    # Runtime metadata from the existing fresh fetch, never a generation's JSON.
    return dict(value=getattr(meta,'is_live',None), checked_at=getattr(meta,'live_checked_at',None))


async def observe(context, vod_id, video=None):
    """At most one VOD lookup + cached stream lookup per explicit card opening.

    No polling, no retries and no persisted current status. Unknown stays None.
    Pure formatters never call this function.
    """
    session = getattr(getattr(context, 'application', None), 'bot_data', {}).get('aiohttp')
    value = None
    checked_at = time.monotonic()
    if session is not None and _id(vod_id) and len(vod_id)<=20:
        try:
            async with asyncio.timeout(3):
                if video is None:
                    rows = (await _get_json(session, '/videos', {'id':vod_id}))['data']
                    video = rows[0] if len(rows)==1 else None
                if (isinstance(video, dict) and video.get('id')==vod_id and video.get('type')=='archive'
                        and _id(video.get('user_id')) and _id(video.get('stream_id'))):
                    stream, checked_at = await stream_observation(session, video['user_id'])
                    value = match_stream(video['stream_id'], stream)
        except Exception:
            pass  # Optional status never breaks the card or implies known offline.
    return dict(value=value, checked_at=checked_at)
