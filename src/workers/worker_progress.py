import time
import asyncio
import html

from telegram.error import BadRequest, RetryAfter, TimedOut, NetworkError
from src.config import PROGRESS_INTERVAL
from src.ui.ui_formatters import _fmt_len, _fmt_dt_utc

from telegram import InlineKeyboardMarkup, InlineKeyboardButton
from src.ui.ui_constants import CB_PENDING_CANCEL
from src.ui.ui_labels import CANCEL
from src.ui.live_status import lines


def cancel_keyboard(context, *, waiting=False):
    record = context.user_data.get('download_ui')
    data = CB_PENDING_CANCEL + ':' + record.get('cancel_token',record['operation']) if record else CB_PENDING_CANCEL
    return InlineKeyboardMarkup([[InlineKeyboardButton('Отмена' if waiting else CANCEL, callback_data=data)]])


async def safe_edit_html(context, chat_id: int, message_id: int, text: str, *, _local=False):
    from contextlib import nullcontext
    from src.ui.result_preview import edit_surface
    record = context.user_data.get('download_ui')
    job = context.user_data.get('_vod_job')
    if job is not None and job.participants is not None and not _local:
        job.participants.progress(text)
        return
    owned = record and (record['chat'], record['message']) == (chat_id, message_id)
    def current():
        if record is None:
            return job is None  # A finished job must not recreate its old progress.
        return (owned and context.user_data.get('download_ui') is record
                and (job is None or record.get('operation') == job.id)
                and record.get('phase', 'running') in ('waiting', 'running'))
    retry_delay = None
    async with (record.get('render_lock') if owned else None) or nullcontext():
        if not current() or owned and record.get('last_text') == text:
            return
        try:
            shown = await edit_surface(context.bot, chat_id, message_id, text, cancel_keyboard(context),
                                       photo=bool(owned and record.get('photo')), current=current)
            if owned and shown is not None:
                record['last_text'] = text
                record['photo'] = shown.photo
        except RetryAfter as e:
            retry_delay = float(e.retry_after)
        except (BadRequest, TimedOut, NetworkError, TimeoutError):
            return
    if retry_delay is not None:
        await asyncio.sleep(retry_delay)  # Honor Telegram without holding the render lock.


def _fmt_date_ru(dt: str) -> str:
    try:
        months = ["Янв", "Фев", "Мар", "Апр", "Май", "Июн", "Июл", "Авг", "Сен", "Окт", "Ноя", "Дек"]
        date_part, time_part, tz = dt.split(" ")
        y, m, d = date_part.split("-")
        m_txt = months[int(m) - 1]
        return f"{d} {m_txt} {y} {time_part} {tz}"
    except Exception:
        return dt


def _build_progress_card(meta, vod_url, messages, users_len, done, *, status=None):
    channel = html.escape((meta.get("channel") or "—")[:120])
    title = html.escape((meta.get("title") or "Без названия")[:240])
    duration = _fmt_len(meta.get("length_seconds"))
    dt = _fmt_date_ru(_fmt_dt_utc(meta.get("created_at")))

    if status is None:
        status = "Чат загружен. Подготовка HTML…" if done else "Загрузка чата…"
    chat_stats = f" • 💬 {messages} • 👥 {users_len}" if messages is not None else ""

    return (
        f"⏳ <b>{status}</b>\n"
        f"🟣 {channel}\n"
        f"🎬 {title}\n"
        f"⏱ {duration}{chat_stats}\n"
        f"🗓 {dt}"
        + lines(meta)
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
