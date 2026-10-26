"""Single-page Helix lookups using the application's shared HTTP session."""
import asyncio
from dataclasses import dataclass
import math
import re
import time

import aiohttp

from .client import TwitchAPIError, get_api_headers, _parse_retry_after


_HELIX_URL = 'https://api.twitch.tv/helix'
_TIMEOUT_SECONDS = 10


@dataclass(frozen=True)
class TwitchUser:
    id: str
    login: str
    display_name: str


@dataclass(frozen=True)
class ArchiveVod:
    id: str
    user_id: str
    user_login: str
    user_name: str
    title: str
    created_at: str
    published_at: str
    duration: str
    url: str
    thumbnail_url: str
    # Raw API metadata, NOT evidence of unrestricted playback/chat access.
    viewable: str


def _rate_limit_delay(headers) -> float | None:
    delays = []
    reset = headers.get('Ratelimit-Reset')
    try:
        reset = float(reset)
        if math.isfinite(reset) and reset >= 0:
            delays.append(max(0.0, reset - time.time()))
    except (TypeError, ValueError, OverflowError):
        pass
    retry_after = _parse_retry_after(headers.get('Retry-After'))
    if retry_after is not None:
        delays.append(retry_after)
    return max(delays) if delays else None


async def _get_json(session, endpoint, params):
    headers = get_api_headers()
    try:
        async with session.get(
            _HELIX_URL + endpoint, params=params, headers=headers,
            timeout=aiohttp.ClientTimeout(total=_TIMEOUT_SECONDS),
            allow_redirects=False,
        ) as response:
            if response.status != 200:
                raise TwitchAPIError(
                    f'Twitch Helix HTTP error ({response.status})',
                    status=response.status,
                    retryable=response.status == 429 or 500 <= response.status < 600,
                    retry_after=_rate_limit_delay(response.headers) if response.status == 429 else None,
                )
            try:
                data = await response.json()
            except (ValueError, TypeError, aiohttp.ContentTypeError):
                raise TwitchAPIError('Twitch Helix returned invalid JSON', status=200) from None
    except (aiohttp.ClientError, asyncio.TimeoutError, OSError):
        # Do not retain exception text/causes that may contain request headers.
        raise TwitchAPIError('Twitch Helix network request failed', retryable=True) from None
    if (not isinstance(data, dict) or not isinstance(data.get('data'), list)
            or data.get('error') or data.get('errors')):
        raise TwitchAPIError('Twitch Helix returned an unexpected response', status=200)
    return data


def _string(item, field, *, allow_empty=False):
    value = item.get(field)
    if not isinstance(value, str) or (not allow_empty and not value):
        raise TwitchAPIError(f'Twitch Helix returned invalid {field}', status=200)
    return value


def _id(value):
    return isinstance(value, str) and re.fullmatch(r'[1-9][0-9]*', value) is not None


async def fetch_user_by_login(session: aiohttp.ClientSession, login: str) -> TwitchUser | None:
    """Return the matching user, or None ONLY for a valid empty data array.

    Performs one request. HTTP/network/schema failures raise TwitchAPIError;
    rate limits carry retry_after seconds without sleeping or retrying here.
    """
    if not isinstance(login, str) or not re.fullmatch(r'[a-z0-9_]{1,25}', login.strip().lower()):
        raise ValueError('Invalid Twitch login')
    login = login.strip().lower()
    data = (await _get_json(session, '/users', {'login': login}))['data']
    if not data:
        return None
    if len(data) != 1 or not isinstance(data[0], dict):
        raise TwitchAPIError('Twitch Helix returned an invalid user response', status=200)
    user = TwitchUser(*(_string(data[0], field) for field in ('id', 'login', 'display_name')))
    if not _id(user.id) or user.login.lower() != login:
        raise TwitchAPIError('Twitch Helix returned an unexpected user', status=200)
    return user


async def fetch_archive_vods(
    session: aiohttp.ClientSession, user_id: str, cursor: str | None = None,
) -> tuple[list[ArchiveVod], str | None]:
    """Return one page and its opaque continuation cursor (None at the end).

    No total or subscription-access status is inferred. Duration and thumbnail
    templates remain in Helix format; no per-VOD enrichment requests are made.
    Errors, including a rate limit, propagate as TwitchAPIError without retries.
    """
    if not _id(user_id):
        raise ValueError('Invalid Twitch user ID')
    if cursor is not None and (not isinstance(cursor, str) or not cursor):
        raise ValueError('Invalid Twitch videos cursor')
    params = {'user_id': user_id, 'type': 'archive', 'sort': 'time', 'first': 100}
    if cursor is not None:
        params['after'] = cursor
    response = await _get_json(session, '/videos', params)
    pagination = response.get('pagination')
    if not isinstance(pagination, dict):
        raise TwitchAPIError('Twitch Helix returned invalid videos pagination', status=200)
    next_cursor = pagination.get('cursor')
    if 'cursor' in pagination and (not isinstance(next_cursor, str) or not next_cursor):
        raise TwitchAPIError('Twitch Helix returned an invalid videos cursor', status=200)
    if len(response['data']) > 100:
        raise TwitchAPIError('Twitch Helix returned an oversized videos page', status=200)
    vods = []
    fields = ('id', 'user_id', 'user_login', 'user_name', 'title', 'created_at',
              'published_at', 'duration', 'url', 'thumbnail_url', 'viewable')
    for item in response['data']:
        if not isinstance(item, dict) or item.get('type') != 'archive':
            raise TwitchAPIError('Twitch Helix returned an invalid archive VOD', status=200)
        vod = ArchiveVod(*(_string(item, field, allow_empty=field in ('title', 'thumbnail_url'))
                           for field in fields))
        if not _id(vod.id) or vod.user_id != user_id:
            raise TwitchAPIError('Twitch Helix returned an unexpected VOD identity', status=200)
        vods.append(vod)
    return vods, next_cursor
