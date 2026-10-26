"""One archive card and its session-bound keyboard; no personal history lookup."""
import html
import re

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from src.channel_browser import BrowseView, callback_data
from .ui_formatters import _fmt_dt_utc, fmt_hhmmss
from .ui_labels import VOD_LINK


_MESSAGES = {
    'loading': 'Загружаю архивы канала…',
    'empty': 'У канала нет опубликованных архивов.',
    'empty_page': 'В этой части списка архивы не найдены. Можно загрузить продолжение.',
    'not_found': 'Twitch-канал не найден.',
    'api_error': 'Не удалось получить архивы Twitch. Попробуй позже.',
    'rate_limited': 'Достигнут лимит запросов Twitch. Попробуй позже.',
    'limit': 'Достигнут лимит просмотра. Доступна только загруженная часть списка.',
    'capacity': 'Сейчас слишком много просмотров каналов. Попробуй позже.',
    'busy': 'Список архивов уже загружается. Подожди завершения.',
    'expired': 'Просмотр устарел. Открой канал заново.',
    'stale': 'Кнопка устарела. Используй текущую карточку.',
    'forbidden': 'Эта кнопка относится к другому пользователю или сообщению.',
    'invalid': 'Запрос просмотра не найден.',
    'selected': 'VOD выбран.',
}


def _short(text, limit):
    return html.escape(text if len(text) <= limit else text[:limit-1]+'…')


def _duration(value):
    match = re.fullmatch(r'(?:(\d{1,6})h)?(?:(\d{1,2})m)?(?:(\d{1,2})s)?', value)
    if not match or not any(match.groups()):
        return '—'
    hours, minutes, seconds = (int(part or 0) for part in match.groups())
    if minutes >= 60 or seconds >= 60:
        return '—'
    return fmt_hhmmss(hours*3600+minutes*60+seconds)


def build_channel_vod_card(view: BrowseView) -> tuple[str, InlineKeyboardMarkup | None]:
    lines = []
    if view.channel is not None:
        lines.append(f'🟣 {_short(view.channel.display_name, 80)} (<code>{_short(view.channel.login, 25)}</code>)')
    if view.vod is not None:
        vod = view.vod
        lines.extend((
            f'🎬 {_short(vod.title or "Без названия", 240)}',
            f'⏱ {_duration(vod.duration)}',
            f'🗓 {_fmt_dt_utc(vod.created_at)}',
            f'🆔 <code>{vod.id}</code>',
        ))
    if view.channel is not None:
        lines.append(f'📚 Опубликованных архивов: {view.count_label}')
    if view.status != 'ready':
        lines.append(_MESSAGES.get(view.status, _MESSAGES['api_error']))
        if view.status == 'api_error' and view.reason == 'authorization':
            lines.append('Twitch отклонил авторизацию приложения.')
        if view.status == 'api_error' and view.reason == 'pagination':
            lines.append('Продолжение списка некорректно. Открой канал заново.')
        if view.status == 'rate_limited' and view.retry_after:
            lines.append(f'Повторить можно через {view.retry_after} сек.')
    buttons = []
    if view.vod is not None:
        buttons.append([
            InlineKeyboardButton('Выбрать VOD', callback_data=callback_data(view, 'select')),
            InlineKeyboardButton(VOD_LINK, url=f'https://www.twitch.tv/videos/{view.vod.id}'),
        ])
    if view.session_id is not None and (view.vod is not None or view.has_next):
        navigation = []
        if view.has_previous:
            navigation.append(InlineKeyboardButton('⬅️', callback_data=callback_data(view, 'prev')))
        position = str(view.position+1) if view.vod is not None else '—'
        navigation.append(InlineKeyboardButton(f'{position}/{view.count_label}', callback_data=callback_data(view, 'noop')))
        if view.has_next:
            navigation.append(InlineKeyboardButton('➡️', callback_data=callback_data(view, 'next')))
        buttons.append(navigation)
    if view.session_id is not None and view.can_retry:
        buttons.append([InlineKeyboardButton('Повторить', callback_data=callback_data(view, 'retry'))])
    return '\n'.join(lines), InlineKeyboardMarkup(buttons) if buttons else None
