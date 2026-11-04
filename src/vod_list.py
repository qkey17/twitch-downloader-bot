"""Complete, bounded archive lists; legacy one-card browser remains available."""
import asyncio
from dataclasses import dataclass, replace
import html
import re
import time
from src import channel_archive
from src.twitch_api.client import TwitchAPIError
from src.twitch_api.live import stream_observation, match_stream, TTL

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from src.channel_browser import BrowseView, ChannelBrowser, VodSelection
from src.ui.ui_formatters import _fmt_dt_utc
from src.ui.ui_labels import BACK_TO_LIST


PREFIX = 'ui:vl:'
_CALLBACK = re.compile(r'ui:vl:([0-9a-f]{16}):([0-9]{1,9}):(prev|next|pages|group|page|select|info|back)(?::([0-9]{1,6}))?')


@dataclass(frozen=True)
class ListView(BrowseView):
    items: tuple = ()
    page: int = 0
    page_count: int = 0
    grid: bool = False
    group: int = 0
    live_vod_ids: frozenset = frozenset()
    archived_ids: frozenset = frozenset()
    exhausted: bool = True
    archive_incomplete: bool = False
    archive_limited: bool = False


@dataclass(frozen=True)
class VodInformation:
    owner_id: int
    index: int
    view: ListView


def callback(view, action, number=None):
    data = f'{PREFIX}{view.session_id}:{view.revision}:{action}'
    if number is not None:
        data += f':{number}'
    if not _CALLBACK.fullmatch(data) or len(data.encode()) > 64:
        raise ValueError('Invalid list callback')
    return data


class VodListBrowser(ChannelBrowser):
    def retire(self, view):
        if self.is_current(view):
            self._sessions.pop(view.session_id, None)

    def _merged(self, session):
        twitch = {v.id:v for v in session.vods}
        merged = dict(getattr(session, 'archives', {})) | twitch
        try:
            ordered = sorted(merged.values(), key=channel_archive.order)
            return ordered
        except ValueError:
            return []

    async def _load(self, http, session, wanted):
        try:
            async with asyncio.timeout(60):
                while True:
                    view = await super()._load(http, session, wanted)
                    if view.status not in ('ready', 'empty', 'empty_page') or session.complete:
                        break
        except TimeoutError:
            session.status, session.reason = 'api_error', 'timeout'
            return self._view(session)
        if not self._live(session):
            return BrowseView('expired')
        # Optional archive confirmation retains its own bounded timeout and
        # cannot turn a successfully loaded Twitch list into a loading error.
        if session.complete and session.status in ('ready', 'empty'):
            session.busy = True
            try:
                records = channel_archive.candidates(session.channel.id)
                archives = await channel_archive.confirmed(http, session.channel.id, records)
                if not self._live(session):
                    return BrowseView('expired')
                session.archives = {v.id:v for v in archives or []}
                session.archive_incomplete = archives is None
                session.archive_limited = len(records) >= channel_archive.MAX_CANDIDATES
                session.position = min(wanted, max(0,len(self._merged(session))-1))
                await self._refresh_live(http, session)
            finally:
                session.busy = False
        return self._view(session) if self._live(session) else BrowseView('expired')

    def _append_page(self, session, page, cursor, wanted):
        # Reject a changing/out-of-order continuation rather than silently
        # moving records on pages the user has already seen.
        try:
            new_dates = [channel_archive.timestamp(v.created_at) for v in page if v.id not in session.ids]
            if session.vods and new_dates and max(new_dates)>min(channel_archive.timestamp(v.created_at) for v in session.vods):
                raise ValueError('Unordered continuation')
        except (ValueError,AttributeError,TypeError):
            raise TwitchAPIError('Invalid archive chronological boundary') from None
        super()._append_page(session, page, cursor, wanted)
        # Keep bounded Helix metadata for on-demand information, never images.
        session.vods[:] = [replace(v, title=v.title[:240], duration=v.duration[:32],
                                  thumbnail_url=v.thumbnail_url[:1024], created_at=v.created_at[:64],
                                  url='', published_at='', user_login='', user_name='')
                           for v in session.vods if v.viewable == 'public']
        session.ids = {v.id for v in session.vods}

    def _view(self, session):
        base = super()._view(replace(session, position=min(session.position, max(0,len(session.vods)-1))))
        items = self._merged(session)
        usable = session.complete and hasattr(session, 'archives') and base.status in ('ready', 'empty', 'selected')
        if session.complete and not items and base.status != 'selected':
            base = replace(base, status='empty')
        elif usable and base.status == 'empty':
            base = replace(base, status='ready')
        base = replace(base, vod=items[session.position] if session.position<len(items) else None,
                       loaded=len(items), complete=usable, has_next=not session.complete or session.position+5<len(items))
        return ListView(**vars(base), items=tuple(items) if usable else (),
                        page=session.position // 5, page_count=(len(items)+4)//5 if usable else 0,
                        grid=getattr(session, 'grid', False), group=getattr(session, 'group', 0),
                        archived_ids=frozenset(getattr(session,'archives',{}))-session.ids,
                        exhausted=session.complete,
                        archive_incomplete=getattr(session, 'archive_incomplete', False),
                        archive_limited=getattr(session, 'archive_limited', False),
                        live_vod_ids=frozenset(v.id for v in session.vods
                            if 0 <= time.monotonic()-getattr(session, 'live_checked_at', 0) < TTL
                            and match_stream(v.stream_id, getattr(session, 'live_stream_id', None)) is True))

    async def _refresh_live(self, http, session):
        stream_id, checked_at = await stream_observation(http, session.channel.id)
        if self._live(session):
            session.live_stream_id = stream_id
            session.live_checked_at = checked_at

    async def handle_callback(self, http, owner, data, chat, message):
        match = _CALLBACK.fullmatch(data or '')
        if not match:
            return BrowseView('invalid')
        token, revision, action, number = match.groups()
        if (action in ('group', 'page', 'select', 'info')) != (number is not None):
            return BrowseView('invalid')
        checked = await super().handle_callback(http, owner, f'ui:cv:{token}:{revision}:noop', chat, message)
        if not isinstance(checked, ListView) or not checked.complete:
            return checked
        session = self._sessions[token]
        await self._refresh_live(http, session)
        if not self.is_current(checked):
            return BrowseView('stale')
        if session.status == 'selected' and action != 'back':
            return BrowseView('stale')
        pages = checked.page_count
        value = int(number or 0)
        if ((action in ('select', 'info') and not (checked.page*5 <= value < min(checked.page*5+5, len(checked.items))))
                or (action == 'page' and value >= pages)
                or (action == 'group' and value >= (pages+11)//12)):
            return BrowseView('invalid')
        if action == 'info':
            return VodInformation(owner, value, self._view(session))
        session.revision += 1
        session.status = 'ready'
        if action == 'select':
            session.position = value
            session.status = 'selected'
            vod = checked.items[value]
            return VodSelection(owner, vod.id, f'https://www.twitch.tv/videos/{vod.id}', session.channel.login, self._view(session))
        if action in ('page', 'prev', 'next'):
            page = value if action == 'page' else checked.page + (-1 if action == 'prev' else 1)
            session.position = max(0, min(pages-1, page))*5
            session.grid = False
        elif action == 'pages':
            session.grid, session.group = pages > 1, checked.page//12
        elif action == 'group':
            session.grid, session.group = True, value
        elif action == 'back':
            session.grid = False
        return self._view(session)


def build_list(view):
    if not isinstance(view, ListView) or not view.complete or view.status not in ('ready', 'empty'):
        texts = {'not_found': 'Канал не найден. Проверьте имя или ссылку.',
                 'empty': 'У канала нет опубликованных VODs.',
                 'limit': 'Достигнут лимит списка VODs. Полный список не загружен.'}
        return texts.get(view.status, 'Не удалось загрузить полный список VODs. Повторите попытку позже.'), None
    text = f'🟣 {html.escape(view.channel.display_name)}'
    if view.archive_incomplete:
        text += '\nНе удалось проверить архив. Показаны только VODs Twitch.'
    elif view.archive_limited:
        text += '\nАрхив ограничен 500 кандидатами.'
    if not view.items:
        return text+'\nУ канала нет опубликованных VODs.', None
    button = lambda label, action, n=None: InlineKeyboardButton(label, callback_data=callback(view, action, n))
    rows = []
    if view.grid:
        text += '\nВыберите страницу'
        numbers = range(view.group*12, min(view.page_count, (view.group+1)*12))
        choices = [button(str(n+1), 'page', n) for n in numbers]
        rows.extend(choices[i:i+4] for i in range(0, len(choices), 4))
        arrows = []
        if view.group:
            arrows.append(button('⬅️', 'group', view.group-1))
        if (view.group+1)*12 < view.page_count:
            arrows.append(button('➡️', 'group', view.group+1))
        if arrows:
            rows.append(arrows)
        rows.append([button(BACK_TO_LIST, 'back')])
    else:
        for n in range(view.page*5, min(len(view.items), (view.page+1)*5)):
            label = _fmt_dt_utc(view.items[n].created_at)
            if view.items[n].id in view.live_vod_ids:
                label = '🔴LIVE'
            rows.append([button(label, 'info', n)])
        nav = []
        if view.page:
            nav.append(button('⬅️', 'prev'))
        if view.page_count > 1:
            nav.append(button(f'Стр. {view.page+1}/{view.page_count}', 'pages'))
        if view.page+1 < view.page_count:
            nav.append(button('➡️', 'next'))
        if nav:
            rows.append(nav)
    return text, InlineKeyboardMarkup(rows)
