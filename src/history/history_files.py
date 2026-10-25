from telegram import InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes
from src.database import database as db
import json
from src.ui.ui_history import build_history_page
from src.ui.ui_labels import CHAT_GENERIC
from src.config import HTML_PUBLISH_MODE
from src.html_publisher import is_local_html_reference, local_published_html_status
from src.history.history_callbacks import (
    _answer_if_history_disabled, _history_id_from_callback, _reply_history_not_found,
)


async def send_cached_files(context, chat_id: int, files: list[dict]):
    for f in files:
        await context.bot.send_document(
            chat_id=chat_id,
            document=f["file_id"],
            filename=f.get("file_name"),
        )


async def history_files_callback(update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if await _answer_if_history_disabled(q):
        return
    await q.answer()
    data = q.data or ""
    if not data.startswith("ui:histfiles:"):
        return

    history_id = _history_id_from_callback(data, "ui:histfiles:")
    if history_id is None:
        await _reply_history_not_found(update, context)
        return

    hist = db.get_history_entry_for_user(update.effective_user.id, history_id)
    if not hist:
        await _reply_history_not_found(update, context)
        return

    vod_id = hist["vod_id"]
    fmt = hist["fmt"]
    cached = db.get_cache(vod_id, fmt)

    # Готовим данные карточки
    meta_raw = cached.get("meta") if cached else None
    stats_raw = cached.get("stats") if cached else None

    meta = json.loads(meta_raw) if isinstance(meta_raw, str) else (meta_raw or {})
    stats = json.loads(stats_raw) if isinstance(stats_raw, str) else (stats_raw or {})

    item = {
        "channel": meta.get("channel") or hist.get("channel"),
        "title": meta.get("title") or hist.get("title"),
        "created_at": meta.get("created_at") or hist.get("created_at"),
        "length_seconds": meta.get("length_seconds") or hist.get("length_seconds"),
        "messages": stats.get("messages") or hist.get("messages"),
        "unique_users": stats.get("unique_users") or hist.get("unique_users"),
        "vod_url": hist.get("vod_url"),
        "fmt": fmt,
        "html_url": (cached.get("html_url") if cached else None),
        "history_id": hist.get("history_id"),
    }

    # HTML online — вместо "Открыть HTML:" показываем карточку с кнопкой
    if fmt == "html_online":
        from src.database.db_results import get_ready_result
        try:
            ready = get_ready_result(vod_id)
        except ValueError:
            ready = None
        if not ready:
            await q.message.reply_text("Ссылка не найдена.")
            return
        item["html_url"] = ready["html_ref"]
        html_url = item.get("html_url")
        is_local = is_local_html_reference(html_url)
        if is_local and local_published_html_status(html_url) is not True:
            await q.message.reply_text("Локальный HTML-файл не найден.")
            return
        if not is_local and (not html_url or HTML_PUBLISH_MODE != "public"):
            await q.message.reply_text("Ссылка не найдена.")
            return

        cards, _ = build_history_page([item], page=0, per_page=1)
        text, kb = cards[0]

        if is_local:
            kb_final = kb
        else:
            extra_rows = [[InlineKeyboardButton(CHAT_GENERIC, url=html_url)]]
            merged = (kb.inline_keyboard if kb else []) + extra_rows
            kb_final = InlineKeyboardMarkup(merged)

        await context.bot.send_message(
            chat_id=q.message.chat_id,
            text=text,
            parse_mode="HTML",
            reply_markup=kb_final,
        )
        return

    # Остальные форматы — нужны файлы
    if not cached or not cached.get("files"):
        await q.message.reply_text("Файлы не найдены.")
        return

    # Сначала отправляем файлы
    await send_cached_files(context, q.message.chat_id, cached["files"])

    # Потом карточку
    cards, _ = build_history_page([item], page=0, per_page=1)
    text, kb = cards[0]

    html_url = item.get("html_url")
    extra_rows = []
    if html_url:
        extra_rows.append([InlineKeyboardButton(CHAT_GENERIC, url=html_url)])

    merged = (kb.inline_keyboard if kb else []) + extra_rows
    kb_final = InlineKeyboardMarkup(merged) if merged else None

    await context.bot.send_message(
        chat_id=q.message.chat_id,
        text=text,
        parse_mode="HTML",
        reply_markup=kb_final,
    )
