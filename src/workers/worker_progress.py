import time
import asyncio
import html

from telegram.error import BadRequest, RetryAfter, TimedOut, NetworkError
from src.config import PROGRESS_INTERVAL
from src.ui.ui_formatters import _fmt_len, _fmt_dt_utc

from telegram import InlineKeyboardMarkup, InlineKeyboardButton
from src.ui.ui_constants import CB_PENDING_CANCEL
from src.ui.ui_labels import CANCEL


def cancel_keyboard(context):
    record = context.user_data.get('download_ui')
    data = CB_PENDING_CANCEL + ':' + record['operation'] if record else CB_PENDING_CANCEL
    return InlineKeyboardMarkup([[InlineKeyboardButton(CANCEL, callback_data=data)]])


async def safe_edit_html(context, chat_id: int, message_id: int, text: str):
    record = context.user_data.get('download_ui')
    owned = record and (record['chat'], record['message']) == (chat_id, message_id)
    if owned and record.get('last_text') == text:
        return
    keyboard = cancel_keyboard(context)

    try:
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            parse_mode="HTML",
            reply_markup=keyboard,
        )
        if owned and context.user_data.get('download_ui') is record:
            record['last_text'] = text
    except RetryAfter as e:
        await asyncio.sleep(float(e.retry_after))
    except BadRequest as e:
        if "message is not modified" in str(e).lower():
            return
    except (TimedOut, NetworkError):
        return


def _fmt_date_ru(dt: str) -> str:
    try:
        months = ["Янв", "Фев", "Мар", "Апр", "Май", "Июн", "Июл", "Авг", "Сен", "Окт", "Ноя", "Дек"]
        date_part, time_part, tz = dt.split(" ")
        y, m, d = date_part.split("-")
        m_txt = months[int(m) - 1]
        return f"{d} {m_txt} {y} {time_part} {tz}"
    except Exception:
        return dt


def _build_progress_card(meta, vod_url, messages, users_len, done):
    channel = html.escape((meta.get("channel") or "—")[:120])
    title = html.escape((meta.get("title") or "Без названия")[:240])
    duration = _fmt_len(meta.get("length_seconds"))
    dt = _fmt_date_ru(_fmt_dt_utc(meta.get("created_at")))

    status = "Чат загружен. Подготовка HTML…" if done else "Загрузка чата…"

    return (
        f"⏳ <b>{status}</b>\n"
        f"🟣 {channel}\n"
        f"🎬 {title}\n"
        f"⏱ {duration} • 💬 {messages} • 👥 {users_len}\n"
        f"🗓 {dt}"
    )


def make_progress_updater(context, chat_id, progress_message_id, meta_dict, vod_url, fmt):
    start_t = time.monotonic()
    last_progress_t = 0.0

    async def maybe_progress(messages, users_len, done: bool = False):
        nonlocal last_progress_t
        now = time.monotonic()
        if (not done) and (now - last_progress_t < PROGRESS_INTERVAL):
            return
        last_progress_t = now

        text = _build_progress_card(
            meta=meta_dict,
            vod_url=vod_url,
            messages=messages,
            users_len=users_len,
            done=done,
        )

        await safe_edit_html(context, chat_id, progress_message_id, text)

    return maybe_progress
