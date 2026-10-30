import html
from src import config
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from .ui_constants import *
from .ui_formatters import _fmt_dt_utc, _fmt_len
from .ui_labels import CHAT_GENERIC, FILES, VOD_LINK, READY_CHAT, label_for_fmt
from src.config import HTML_PUBLISH_MODE
from src.html_publisher import (
    is_local_html_reference,
    local_html_relative_path,
    local_published_html_status,
)


def _format_button(it: dict, idx: int):
    url = it.get("html_url")

    if is_local_html_reference(url):
        return None
    if url and HTML_PUBLISH_MODE == "public" and local_published_html_status(url) is True:
        return InlineKeyboardButton(CHAT_GENERIC, url=url)

    if it.get("fmt") == "html_online":
        return None

    if not config.HISTORY_ENABLED:
        return None

    history_id = it.get("history_id")
    callback_id = f"h{history_id}" if history_id is not None else str(idx)
    return InlineKeyboardButton(FILES, callback_data=f"{CB_HIST_FILES_PREFIX}{callback_id}")


def _fmt_date_ru(dt: str) -> str:
    try:
        months = ["Янв", "Фев", "Мар", "Апр", "Май", "Июн", "Июл", "Авг", "Сен", "Окт", "Ноя", "Дек"]
        date_part, time_part, tz = dt.split(" ")
        y, m, d = date_part.split("-")
        m_txt = months[int(m) - 1]
        return f"{d} {m_txt} {y} {time_part} {tz}"
    except Exception:
        return dt


def format_vod_summary(it: dict, *, unknown_stats=False):
    channel = html.escape(it.get('channel') or '—')
    title = html.escape(it.get('title') or 'Без названия')
    date = _fmt_dt_utc(it.get('created_at')) if it.get('created_at') else '—'
    date = _fmt_date_ru(date) if date != '—' else date
    duration = _fmt_len(it.get('length_seconds')) if it.get('length_seconds') else '—'
    def count(key):
        value = it.get(key)
        if not unknown_stats:
            return value or 0  # Preserve existing history/result formatting.
        if type(value) is not int or value < 0:
            return '—'
        try:
            text = str(value)
        except ValueError:
            return '—'
        return text if len(text) <= 64 else '—'
    return (f'🟣 {channel}\n🎬 {title}\n'
            f'⏱ {duration} • 💬 {count("messages")} • 👥 {count("unique_users")}\n🗓 {date}')


def ready_offer_text(ready):
    meta, stats = ready.get('meta') or {}, ready.get('stats') or {}
    item = {key:meta.get(key) for key in ('created_at', 'length_seconds')}
    if type(item['length_seconds']) is not int or not 0 <= item['length_seconds'] < 10**18:
        item['length_seconds'] = None
    # Bound only this action screen, leaving other cards unchanged. Account for
    # astral characters conservatively; the parsed text stays below 4096 units.
    for key, limit in (('channel', 120), ('title', 1200)):
        value = meta.get(key)
        item[key] = value[:limit] if isinstance(value, str) else None
    item.update(messages=stats.get('messages'), unique_users=stats.get('unique_users'))
    return READY_CHAT + '\n\n' + format_vod_summary(item, unknown_stats=True)


def build_history_page(items: list[dict], page: int, per_page: int, total_items: int | None = None):
    total = len(items) if total_items is None else max(0, int(total_items))
    pages = max(1, (total + per_page - 1) // per_page)
    page = max(0, min(page, pages - 1))

    if total_items is None:
        start = page * per_page
        end = min(start + per_page, total)
        page_items = items[start:end]
    else:
        page_items = items[:per_page]

    cards = []

    for i, it in enumerate(page_items):
        idx = page * per_page + i

        fmt = it.get("fmt") or "—"

        html_ref = it.get("html_url")
        local_html_path = (
            local_html_relative_path(html_ref)
            if local_published_html_status(html_ref) is True
            else None
        )
        if local_html_path:
            fmt_text = "📄 Локальный HTML"
        elif fmt == "html_online" and HTML_PUBLISH_MODE == "local":
            fmt_text = "📄 Локальный HTML недоступен"
        elif fmt == 'html_online':
            fmt_text = ''  # The action belongs to the URL button, not the body.
        else:
            fmt_text = label_for_fmt(fmt)

        text = format_vod_summary(it) + ('\n' + fmt_text if fmt_text else '')
        if local_html_path:
            text += f"\nHTML сохранён локально: <code>{html.escape(local_html_path)}</code>"

        buttons = []
        main_btn = _format_button(it, idx)
        if main_btn:
            buttons.append(main_btn)

        vod_url = it.get("vod_url")
        if vod_url:
            buttons.append(
                InlineKeyboardButton(VOD_LINK, url=vod_url)
            )

        kb = InlineKeyboardMarkup([buttons])
        cards.append((text, kb))

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️", callback_data=f"{CB_HIST_PAGE}{page - 1}"))
    nav.append(InlineKeyboardButton(f"{page + 1}/{pages}", callback_data=CB_NOOP))
    if page < pages - 1:
        nav.append(InlineKeyboardButton("➡️", callback_data=f"{CB_HIST_PAGE}{page + 1}"))

    nav_kb = InlineKeyboardMarkup([nav]) if nav else None

    return cards, nav_kb
