import html
from src import config
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from .ui_constants import *
from .ui_formatters import _fmt_dt_utc, _fmt_len
from .ui_labels import CHAT_GENERIC, FILES, VOD_LINK, label_for_fmt
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
    if url and HTML_PUBLISH_MODE == "public":
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

        channel = html.escape(it.get("channel") or "—")
        vod_title = html.escape(it.get("title") or "Без названия")

        created_at = it.get("created_at")
        dt_raw = _fmt_dt_utc(created_at) if created_at else "—"
        dt = _fmt_date_ru(dt_raw) if dt_raw != "—" else "—"

        length_seconds = it.get("length_seconds")
        duration = _fmt_len(length_seconds) if length_seconds else "—"

        msgs = it.get("messages") or 0
        users = it.get("unique_users") or 0
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
            fmt_text = "📄 HTML-файл недоступен локально"
        else:
            fmt_text = label_for_fmt(fmt)

        text = (
            f"🟣 {channel}\n"
            f"🎬 {vod_title}\n"
            f"⏱ {duration} • 💬 {msgs} • 👥 {users}\n"
            f"🗓 {dt}\n"
            f"{fmt_text}"
        )
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
