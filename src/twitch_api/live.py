"""Optional stream identity lookup; bounded channel cache, never thumbnail inference."""
from collections import OrderedDict
import time

from .videos import _get_json, _id

TTL = 30
_cache = OrderedDict()


def match_stream(vod_stream_id, current_stream_id):
    if not _id(vod_stream_id) or (current_stream_id != '' and not _id(current_stream_id)):
        return None
    return vod_stream_id == current_stream_id


async def current_stream(session, user_id, *, fresh=False):
    if not _id(user_id):
        return None
    key = (id(session), user_id)
    now = time.monotonic()
    cached = _cache.get(key)
    if not fresh and cached and cached[0] is session and now - cached[1] < TTL:
        _cache.move_to_end(key)
        return cached[2]
    value = None
    try:
        rows = (await _get_json(session, '/streams', {'user_id': user_id}))['data']
        if not rows:
            value = ''  # Known offline, distinct from an unknown response.
        elif (len(rows) == 1 and isinstance(rows[0], dict)
              and rows[0].get('user_id') == user_id and rows[0].get('type') == 'live'
              and _id(rows[0].get('id'))):
            value = rows[0]['id']
    except Exception:
        # Optional enrichment must not break chat downloads. Cancellation propagates.
        pass
    _cache[key] = (session, time.monotonic(), value)
    _cache.move_to_end(key)
    while len(_cache) > 64:
        _cache.popitem(last=False)
    return value


async def stream_observation(session, user_id):
    """Expose the existing observation time without extending cached LIVE TTL."""
    value = await current_stream(session, user_id)
    cached = _cache.get((id(session), user_id))
    return value, cached[1] if cached and cached[0] is session else time.monotonic()
