# handlers_vod.py
import logging
import json
import re
from src.twitch_api import fetch_vod_meta, get_client_id

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes

from src.database import database as db
from src import config
from src.workers.download_worker import download_and_send
from src.config import HTML_PUBLISH_MODE
from src.html_publisher import (
    is_local_html_reference,
    local_published_html_status,
)
from src.ui.ui import CB_FMT_HTML_ONLINE, build_format_keyboard
from .handlers_state import (
    extract_vod_id_strict,
    is_busy,
    set_pending,
    begin_download,
    finish_download,
    get_pending,
    clear_pending,
    pending_expired,
    set_cancel,
)
from src.ui.ui_history import build_history_page
from src.ui.ui_labels import CHAT_GENERIC, VOD_LINK, CANCEL
from src.ui.ui_constants import CB_PENDING_CANCEL


_FORMAT_PROMPT_TEXT = (
    "В современной культуре термин «казуал» иногда используется в пренебрежительном контексте, "
    "однако чаще всего это просто описание стиля игры — легкого и непринужденного, без фанатизма."
)


def _make_item(meta, stats, vod_url, fmt):
    return {
        "channel": meta.get("channel"),
        "title": meta.get("title"),
        "created_at": meta.get("created_at"),
        "length_seconds": meta.get("length_seconds"),
        "messages": stats.get("messages"),
        "unique_users": stats.get("unique_users"),
        "vod_url": vod_url,
        "fmt": fmt,
        "html_url": meta.get("html_url"),
    }


def _build_card(item):
    cards, _ = build_history_page([item], page=0, per_page=1)
    if not cards:
        return None, None
    return cards[0]


async def _restore_format_prompt(message):
    try:
        await message.edit_text(
            text=_FORMAT_PROMPT_TEXT,
            reply_markup=build_format_keyboard(),
        )
    except Exception:
        logging.exception("Could not restore format selection after task startup failed")


def _cache_entry_has_usable_output(cached: dict | None, fmt: str) -> bool:
    if not cached:
        return False
    if fmt != "html_online":
        return True

    meta = cached.get("meta") or {}
    html_url = cached.get("html_url") or meta.get("html_url")
    if not html_url:
        return False

    if HTML_PUBLISH_MODE == "local":
        return is_local_html_reference(html_url) and local_published_html_status(html_url) is True

    # Unknown URLs may belong to a separate storage service; only invalidate
    # entries that this local publisher can verify as missing or unreadable.
    return local_published_html_status(html_url) is not False


def _html_button_url(html_ref: str | None) -> str | None:
    if HTML_PUBLISH_MODE != "public" or not html_ref or is_local_html_reference(html_ref):
        return None
    return html_ref


async def _edit_progress_message_with_card(context, chat_id: int, message_id: int, item: dict, html_url: str | None = None):
    text, kb = _build_card(item)
    if not text:
        return

    html_url = _html_button_url(html_url)
    html_btn = InlineKeyboardButton(CHAT_GENERIC, url=html_url) if html_url else None
    vod_btn = InlineKeyboardButton(VOD_LINK, url=item.get("vod_url"))

    row = []
    if html_btn:
        row.append(html_btn)
    row.append(vod_btn)

    final_kb = InlineKeyboardMarkup([row])

    try:
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            parse_mode="HTML",
            reply_markup=final_kb,
        )
    except Exception:
        await context.bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode="HTML",
            reply_markup=final_kb,
        )


async def _send_card_with_buttons(context, chat_id, item, html_url=None):
    text, kb = _build_card(item)
    if not text:
        return

    html_url = _html_button_url(html_url)
    html_btn = InlineKeyboardButton(CHAT_GENERIC, url=html_url) if html_url else None
    vod_btn = InlineKeyboardButton(VOD_LINK, url=item.get("vod_url"))

    row = []
    if html_btn:
        row.append(html_btn)
    row.append(vod_btn)

    final_kb = InlineKeyboardMarkup([row])

    await context.bot.send_message(
        chat_id=chat_id,
        text=text,
        parse_mode="HTML",
        reply_markup=final_kb,
    )


# ------ Entry handler (приём произвольного сообщения с VOD id) ------
async def vod_link_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    text = (update.message.text or "").strip()
    vod_id = extract_vod_id_strict(text)

    # fallback: ищем последнее длинное число в сообщении (7-12 цифр)
    if not vod_id:
        matches = re.findall(r"\d{7,12}", text)
        if matches:
            vod_id = matches[-1]

    if not vod_id:
        return

    if is_busy(context) or context.user_data.get("vod_link_lookup"):
        await update.message.reply_text("Я уже качаю чат. Подожди завершения.")
        return

    client_id = get_client_id()
    session = context.application.bot_data["aiohttp"]

    # Only link lookup is non-blocking in PTB. Keep the same user's dialog
    # serialized while metadata is awaited, without reserving the download slot.
    lookup = object()
    context.user_data["vod_link_lookup"] = lookup
    try:
        meta = await fetch_vod_meta(session, client_id, vod_id)
        if context.user_data.get("vod_link_lookup") is not lookup:
            return

        # если VOD не существует — просто игнорируем сообщение
        if not meta or not getattr(meta, "length_seconds", None):
            return

        vod_url = f"https://www.twitch.tv/videos/{vod_id}"
        set_pending(context, vod_url=vod_url, vod_id=vod_id)

        await update.message.reply_text(
            _FORMAT_PROMPT_TEXT,
            reply_markup=build_format_keyboard()
        )
    finally:
        if context.user_data.get("vod_link_lookup") is lookup:
            context.user_data.pop("vod_link_lookup", None)


# ------ Cancel pending download (кнопка отмены в прогрессе) ------
async def pending_cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    if is_busy(context):
        set_cancel(context, True)
    else:
        await q.message.reply_text("Нечего отменять.")


# ------ Cancel format selection (удалить pending) ------
async def format_cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    clear_pending(context)

    try:
        await q.message.delete()
    except Exception:
        pass


# ------ Format chosen handler (запуск воркера) ------
async def vod_format_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    if is_busy(context):
        await q.message.reply_text("Я уже качаю чат. Подожди завершения.")
        return

    if context.user_data.get("vod_link_lookup"):
        await q.message.reply_text("Дождись проверки ссылки.")
        return

    data = q.data or ""
    if data != CB_FMT_HTML_ONLINE:
        return

    pending = get_pending(context)
    if not pending:
        await q.message.reply_text("Ссылка не найдена.")
        return

    if pending_expired(pending):
        clear_pending(context)
        await q.message.reply_text("Ссылка устарела.")
        return

    fmt = "html_online"
    vod_url = pending["vod_url"]
    vod_id = pending["vod_id"]

    cached = db.get_cache(vod_id, fmt)
    if (
        cached
        and not db.cache_is_expired(cached)
        and _cache_entry_has_usable_output(cached, fmt)
    ):
        clear_pending(context)
        if config.HISTORY_ENABLED:
            db.add_user_history(update.effective_user.id, int(cached["id"]))

        meta_raw = cached.get("meta")
        stats_raw = cached.get("stats")
        html_url = cached.get("html_url")

        meta = json.loads(meta_raw) if isinstance(meta_raw, str) else (meta_raw or {})
        stats = json.loads(stats_raw) if isinstance(stats_raw, str) else (stats_raw or {})

        if not html_url:
            html_url = meta.get("html_url")

        safe_meta = {
            "channel": meta.get("channel") or "—",
            "title": meta.get("title") or "Без названия",
            "created_at": meta.get("created_at"),
            "length_seconds": meta.get("length_seconds") or 0,
            "html_url": html_url,
        }

        safe_stats = {
            "messages": stats.get("messages") or 0,
            "unique_users": stats.get("unique_users") or 0,
        }

        item = _make_item(safe_meta, safe_stats, vod_url, fmt)

        try:
            await _edit_progress_message_with_card(
                context=context,
                chat_id=q.message.chat_id,
                message_id=q.message.message_id,
                item=item,
                html_url=html_url,
            )
        except Exception:
            await _send_card_with_buttons(context, q.message.chat_id, item, html_url)
        return

    operation_id = begin_download(context)
    if operation_id is None:
        await q.message.reply_text("Бот занят обработкой VOD. Попробуй после её завершения.")
        return
    try:
        await q.message.edit_text(
            text="Загрузка...",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(CANCEL, callback_data=CB_PENDING_CANCEL)]
            ])
        )
    except BaseException:
        try:
            await _restore_format_prompt(q.message)
        finally:
            finish_download(context, operation_id)
        raise

    runner_coro = _runner(
        context=context,
        chat_id=q.message.chat_id,
        progress_message_id=q.message.message_id,
        vod_url=vod_url,
        vod_id=vod_id,
        fmt=fmt,
        user_id=update.effective_user.id,
        operation_id=operation_id,
    )
    try:
        task = context.application.create_task(runner_coro)
    except BaseException:
        runner_coro.close()
        try:
            await _restore_format_prompt(q.message)
        finally:
            finish_download(context, operation_id)
        raise

    def release_finished_task(_task):
        # Also cover cancellation before _runner gets its first turn.
        runner_coro.close()
        finish_download(context, operation_id)

    task.add_done_callback(release_finished_task)
    clear_pending(context)


async def _runner(
    context,
    chat_id: int,
    progress_message_id: int,
    vod_url: str,
    vod_id: str,
    fmt: str,
    user_id: int,
    operation_id: str | None = None,
):
    try:
        meta, stats, files, html_result_ref = await download_and_send(
            context=context,
            chat_id=chat_id,
            progress_message_id=progress_message_id,
            vod_url=vod_url,
            vod_id=vod_id,
            fmt=fmt,
            operation_id=operation_id,
        )

        if fmt == "html_online" and html_result_ref:
            meta["html_url"] = html_result_ref

        cache_id = db.upsert_cache(
            vod_id=vod_id,
            fmt=fmt,
            vod_url=vod_url,
            meta=meta,
            stats=stats,
            files=files,
        )
        if config.HISTORY_ENABLED:
            db.add_user_history(user_id, cache_id)

        item = _make_item(meta, stats, vod_url, fmt)
        html_url = html_result_ref

        await _edit_progress_message_with_card(
            context=context,
            chat_id=chat_id,
            message_id=progress_message_id,
            item=item,
            html_url=html_url,
        )

    except RuntimeError as e:
        if str(e) == "CHAT_EMPTY":
            try:
                await context.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=progress_message_id,
                    text="У этого VOD нет чата."
                )
            except Exception:
                pass
            return
        if "Загрузка была отменена." in str(e):
            try:
                await context.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=progress_message_id,
                    text="Загрузка была отменена.",
                )
            except Exception:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text="Загрузка была отменена.",
                )
            return
        raise

    except Exception as e:
        logging.exception("Download failed")
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=progress_message_id,
                text=f"Ошибка: {type(e).__name__}: {e}",
            )
        except Exception:
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"Ошибка: {type(e).__name__}: {e}",
            )
    finally:
        finish_download(context, operation_id)


__all__ = [
    "vod_link_entry",
    "vod_format_chosen",
    "pending_cancel_callback",
    "format_cancel_callback",
]
