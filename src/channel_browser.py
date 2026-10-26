"""Bounded, temporary archive browsing. Independent of personal history/chat jobs."""
from dataclasses import dataclass, field
import math
import re
import secrets
import time

from src.config import ConfigurationError
from src.twitch_api import videos
from src.twitch_api.client import TwitchAPIError
from src.twitch_api.videos import ArchiveVod, TwitchUser


CALLBACK_PREFIX = 'ui:cv:'
_CALLBACK_RE = re.compile(r'ui:cv:([0-9a-f]{16}):([0-9]{1,9}):(prev|next|retry|select|noop)(?::([1-9][0-9]{0,19}))?')


@dataclass(frozen=True)
class BrowseView:
    status: str
    session_id: str | None = None
    revision: int = 0
    chat_id: int | None = None
    message_id: int | None = None
    channel: TwitchUser | None = None
    vod: ArchiveVod | None = None
    position: int = 0
    loaded: int = 0
    complete: bool = False
    has_previous: bool = False
    has_next: bool = False
    can_retry: bool = False
    retry_after: int | None = None
    reason: str | None = None

    @property
    def count_label(self):
        return str(self.loaded) + ('' if self.complete else '+')


@dataclass(frozen=True)
class VodSelection:
    owner_id: int
    vod_id: str
    vod_url: str
    channel_login: str
    view: BrowseView


@dataclass
class _Session:
    token: str
    owner_id: int
    chat_id: int
    message_id: int
    login: str
    expires: float
    revision: int = 1
    channel: TwitchUser | None = None
    vods: list[ArchiveVod] = field(default_factory=list)
    ids: set[str] = field(default_factory=set)
    cursors: set[str] = field(default_factory=set)
    cursor: str | None = None
    pages: int = 0
    position: int = 0
    complete: bool = False
    limited: bool = False
    blocked: bool = False
    busy: bool = False
    status: str = 'loading'
    reason: str | None = None


def callback_data(view: BrowseView, action: str) -> str:
    if view.session_id is None or action not in ('prev', 'next', 'retry', 'select', 'noop'):
        raise ValueError('Invalid channel browser callback')
    value = f'{CALLBACK_PREFIX}{view.session_id}:{view.revision}:{action}'
    if action == 'select':
        if view.vod is None:
            raise ValueError('No VOD selected')
        value += ':' + view.vod.id
    if not _CALLBACK_RE.fullmatch(value) or len(value.encode('utf-8')) > 64:
        raise ValueError('Invalid channel browser callback')
    return value


class ChannelBrowser:
    """One session per owner; absolute TTL, lazy expiry, and no background tasks.

    Each action makes at most one videos request (opening also resolves login).
    Limits bound retained sessions, VODs, metadata, and pagination cursors.
    Only the application's event loop should access this store.
    """
    def __init__(self, *, ttl=600, max_sessions=32, max_vods=500, max_pages=10, clock=time.monotonic):
        if not math.isfinite(ttl) or ttl <= 0 or any(type(n) is not int or n <= 0 for n in (max_sessions, max_vods, max_pages)):
            raise ValueError('Invalid channel browser limits')
        self.ttl, self.max_sessions, self.max_vods, self.max_pages = ttl, max_sessions, max_vods, max_pages
        self.clock = clock
        self._sessions = {}
        self._rate_until = 0.0

    def _prune(self):
        now = self.clock()
        for token in list(self._sessions):
            if self._sessions[token].expires <= now:
                del self._sessions[token]

    def __len__(self):
        self._prune()
        return len(self._sessions)

    def _live(self, session, revision=None):
        self._prune()
        return (self._sessions.get(session.token) is session
                and (revision is None or session.revision == revision))

    def is_current(self, view):
        self._prune()
        session = self._sessions.get(view.session_id)
        return bool(session and session.revision == view.revision
                    and (session.chat_id, session.message_id) == (view.chat_id, view.message_id))

    def _view(self, session):
        return BrowseView(
            status=session.status, session_id=session.token, revision=session.revision,
            chat_id=session.chat_id, message_id=session.message_id, channel=session.channel,
            vod=session.vods[session.position] if session.vods else None,
            position=session.position, loaded=len(session.vods), complete=session.complete,
            has_previous=session.position > 0,
            has_next=(session.position + 1 < len(session.vods)
                      or (session.cursor is not None and not session.limited and not session.blocked)),
            can_retry=session.status in ('api_error', 'rate_limited') and not session.blocked,
            retry_after=max(0, math.ceil(self._rate_until-self.clock())) if session.status == 'rate_limited' else None,
            reason=session.reason,
        )

    def _normal_status(self, session):
        session.reason = None if not session.blocked else 'pagination'
        session.status = ('limit' if session.limited else 'api_error' if session.blocked else
                          'ready' if session.vods else 'empty' if session.complete else 'empty_page')

    async def open(self, http_session, owner_id, login, chat_id, message_id) -> BrowseView:
        self._prune()
        if not isinstance(login, str) or not re.fullmatch(r'[a-z0-9_]{1,25}', login.strip().lower()):
            return BrowseView('invalid')
        previous = next((s for s in self._sessions.values() if s.owner_id == owner_id), None)
        if previous and previous.busy:
            return BrowseView('busy')
        if previous:
            del self._sessions[previous.token]
        if len(self._sessions) >= self.max_sessions:
            return BrowseView('capacity')
        session = _Session(secrets.token_hex(8), owner_id, chat_id, message_id,
                           login.strip().lower(), self.clock()+self.ttl)
        self._sessions[session.token] = session
        return await self._load(http_session, session, 0)

    async def handle_callback(self, http_session, owner_id, data, chat_id, message_id) -> BrowseView | VodSelection:
        match = _CALLBACK_RE.fullmatch(data) if isinstance(data, str) else None
        if not match:
            return BrowseView('invalid')
        token, revision, action, vod_id = match.groups()
        if (action == 'select') != (vod_id is not None):
            return BrowseView('invalid')
        self._prune()
        session = self._sessions.get(token)
        if session is None:
            return BrowseView('expired')
        if session.owner_id != owner_id or (session.chat_id, session.message_id) != (chat_id, message_id):
            return BrowseView('forbidden')
        if session.revision != int(revision):
            return BrowseView('stale')
        if session.busy:
            return BrowseView('busy')
        if action == 'noop':
            return self._view(session)
        if session.revision == 999999999:
            del self._sessions[token]
            return BrowseView('expired')
        if action == 'select' and (not session.vods or session.vods[session.position].id != vod_id):
            return BrowseView('stale')
        session.revision += 1  # Consume the old keyboard before the first await.
        if action == 'select':
            session.status = 'selected'
            return VodSelection(owner_id, vod_id, f'https://www.twitch.tv/videos/{vod_id}',
                                session.channel.login, self._view(session))
        if action == 'prev':
            session.position = max(0, session.position-1)
        elif action == 'next':
            wanted = session.position+1 if session.vods else 0
            if wanted < len(session.vods):
                session.position = wanted
            elif session.cursor is not None and not session.limited and not session.blocked:
                return await self._load(http_session, session, wanted)
        elif action == 'retry' and session.status in ('api_error', 'rate_limited') and not session.blocked:
            return await self._load(http_session, session, session.position+1 if session.vods else 0)
        self._normal_status(session)
        return self._view(session)

    def _append_page(self, session, page, cursor, wanted):
        if cursor is not None and (not isinstance(cursor, str) or not cursor or len(cursor) > 2048):
            raise TwitchAPIError('Invalid archive cursor')
        if cursor is not None and cursor in session.cursors:
            session.blocked = True
            raise TwitchAPIError('Archive pagination cycle')
        unique, ids = [], set(session.ids)
        if not isinstance(page, list) or len(page) > 100:
            raise TwitchAPIError('Invalid archive page')
        for vod in page:
            if (not isinstance(vod, ArchiveVod)
                    or any(not isinstance(value, str) or len(value) > 4096 for value in vars(vod).values())
                    or not re.fullmatch(r'[1-9][0-9]{0,19}', vod.id) or vod.user_id != session.channel.id
                    or sum(len(value.encode('utf-8')) for value in vars(vod).values()) > 4096):
                raise TwitchAPIError('Invalid or oversized archive metadata')
            if vod.id not in ids:
                unique.append(vod)
                ids.add(vod.id)
        room = self.max_vods-len(session.vods)
        truncated = len(unique) > room
        session.vods.extend(unique[:room])
        session.ids.update(vod.id for vod in unique[:room])
        session.pages += 1
        session.cursor = cursor
        if cursor is not None:
            session.cursors.add(cursor)
        session.complete = cursor is None and not truncated
        session.limited = truncated or (not session.complete and
                                       (len(session.vods) >= self.max_vods or session.pages >= self.max_pages))
        session.position = min(wanted, max(0, len(session.vods)-1))
        self._normal_status(session)

    async def _load(self, http_session, session, wanted):
        revision = session.revision
        session.busy = True
        session.status, session.reason = 'loading', None
        try:
            if self._rate_until > self.clock():
                session.status = 'rate_limited'
                return self._view(session)
            if session.channel is None:
                channel = await videos.fetch_user_by_login(http_session, session.login)
                if not self._live(session, revision):
                    return BrowseView('expired')
                if channel is None:
                    session.status = 'not_found'
                    return self._view(session)
                if (not isinstance(channel, TwitchUser)
                        or any(not isinstance(v, str) or len(v) > 1024 for v in vars(channel).values())
                        or sum(len(v.encode('utf-8')) for v in vars(channel).values()) > 1024):
                    raise TwitchAPIError('Invalid channel metadata')
                session.channel = channel
            page, cursor = await videos.fetch_archive_vods(http_session, session.channel.id, session.cursor)
            if not self._live(session, revision):
                return BrowseView('expired')
            self._append_page(session, page, cursor, wanted)
        except TwitchAPIError as exc:
            if exc.status == 429:
                delay = exc.retry_after
                delay = delay if isinstance(delay, (int, float)) and math.isfinite(delay) else 5
                self._rate_until = max(self._rate_until, self.clock()+max(1, delay))
            if self._live(session, revision):
                session.status = 'rate_limited' if exc.status == 429 else 'api_error'
                session.reason = ('pagination' if session.blocked else
                                  'authorization' if exc.status in (401, 403) else 'request')
        except (ConfigurationError, ValueError):
            if self._live(session, revision):
                session.status, session.reason = 'api_error', 'configuration'
        finally:
            # Also covers asyncio cancellation; a replacement session owns its
            # own state and cannot be unlocked by this older operation.
            if self._live(session, revision):
                session.busy = False
                if session.status == 'loading':
                    session.status, session.reason = 'api_error', 'interrupted'
        return self._view(session) if self._live(session, revision) else BrowseView('expired')
