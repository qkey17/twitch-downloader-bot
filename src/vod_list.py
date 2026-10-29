"""Complete, bounded archive lists; legacy one-card browser remains available."""
import asyncio
from dataclasses import dataclass, replace
import html
import re

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from src.channel_browser import BrowseView, ChannelBrowser, VodSelection
from src.ui.ui_formatters import _fmt_dt_utc


PREFIX = 'ui:vl:'
_CALLBACK = re.compile(r'ui:vl:([0-9a-f]{16}):([0-9]{1,9}):(prev|next|pages|group|page|select|back)(?::([0-9]{1,6}))?')


@dataclass(frozen=True)
class ListView(BrowseView):
    items: tuple = ()
    page: int = 0
    page_count: int = 0
    grid: bool = False
    group: int = 0


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

    async def _load(self, http, session, wanted):
        try:
            async with asyncio.timeout(60):
                while True:
                    view = await super()._load(http, session, wanted)
                    if view.status != 'ready' or view.complete:
                        return view
        except TimeoutError:
            session.status, session.reason = 'api_error', 'timeout'
            return self._view(session)

    def _append_page(self, session, page, cursor, wanted):
        super()._append_page(session, page, cursor, wanted)
        # Selection needs only ID and creation date. Validate full API records
        # first, then discard titles/URLs/thumbnails from this temporary cache.
        session.vods[:] = [replace(v, title='', url='', thumbnail_url='', duration='',
                                  published_at='', user_login='', user_name='')
                           for v in session.vods if v.viewable == 'public']
        session.ids = {v.id for v in session.vods}

    def _view(self, session):
        base = super()._view(session)
        complete = base.complete and base.status in ('ready', 'empty', 'selected')
        return ListView(**vars(base), items=tuple(session.vods) if complete else (),
                        page=session.position // 5, page_count=(len(session.vods)+4)//5 if complete else 0,
                        grid=getattr(session, 'grid', False), group=getattr(session, 'group', 0))

    async def handle_callback(self, http, owner, data, chat, message):
        match = _CALLBACK.fullmatch(data or '')
        if not match:
            return BrowseView('invalid')
        token, revision, action, number = match.groups()
        if (action in ('group', 'page', 'select')) != (number is not None):
            return BrowseView('invalid')
        checked = await super().handle_callback(http, owner, f'ui:cv:{token}:{revision}:noop', chat, message)
        if not isinstance(checked, ListView) or not checked.complete:
            return checked
        session = self._sessions[token]
        if session.status == 'selected' and action != 'back':
            return BrowseView('stale')
        pages = checked.page_count
        value = int(number or 0)
        if ((action == 'select' and not (checked.page*5 <= value < min(checked.page*5+5, len(session.vods))))
                or (action == 'page' and value >= pages)
                or (action == 'group' and value >= (pages+11)//12)):
            return BrowseView('invalid')
        session.revision += 1
        session.status = 'ready'
        if action == 'select':
            session.position = value
            session.status = 'selected'
            vod = session.vods[value]
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
                 'empty': 'У канала нет опубликованных VODs.'}
        return texts.get(view.status, 'Не удалось загрузить полный список VODs. Повторите попытку позже.'), None
    text = f'🟣 {html.escape(view.channel.display_name)}\nVODs канала: {view.loaded}'
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
        rows.append([button('Вернуться к списку', 'back')])
    else:
        for n in range(view.page*5, min(len(view.items), (view.page+1)*5)):
            rows.append([button(_fmt_dt_utc(view.items[n].created_at), 'select', n)])
        nav = []
        if view.page:
            nav.append(button('⬅️', 'prev'))
        if view.page_count > 1:
            nav.append(button(f'📄 Стр. {view.page+1}/{view.page_count} ▾', 'pages'))
        if view.page+1 < view.page_count:
            nav.append(button('➡️', 'next'))
        if nav:
            rows.append(nav)
    return text, InlineKeyboardMarkup(rows)
