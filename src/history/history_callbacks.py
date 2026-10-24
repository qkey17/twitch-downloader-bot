from telegram import Update, InlineKeyboardMarkup
from telegram.ext import ContextTypes


def _history_id_from_callback(data: str, prefix: str) -> int | None:
    suffix = data[len(prefix):]
    # Numeric suffixes are legacy position-based callbacks and must never be
    # reinterpreted as a current row identifier.
    if not suffix.startswith("h") or not suffix[1:].isdigit():
        return None
    history_id = int(suffix[1:])
    return history_id if history_id > 0 else None


async def _reply_history_not_found(update, context):
    message = update.callback_query.message
    try:
        await message.reply_text("Запрос не найден.")
    except Exception:
        await context.bot.send_message(chat_id=message.chat_id, text="Запрос не найден.")


async def ui_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data or ""
    user_id = update.effective_user.id

    # локальные импорты чтобы избежать циклических зависимостей
    from src.database import database as db
    from src.ui.ui import CB_UI_HISTORY, CB_HIST_PAGE, CB_HIST_VOD_PREFIX, CB_HIST_FILES_PREFIX
    from src.history.history_view import _send_history_cards

    if data == CB_UI_HISTORY:
        total_items = db.get_history_count(user_id)
        items = db.get_history_for_user(user_id, limit=1, offset=0)
        await _send_history_cards(
            chat_id=q.message.chat_id,
            context=context,
            items=items,
            page=0,
            total_items=total_items,
        )
        return

    if data.startswith(CB_HIST_VOD_PREFIX):
        return await history_vod_callback(update, context)

    if data.startswith(CB_HIST_FILES_PREFIX):
        return  # обрабатывается в другом месте

    if data.startswith(CB_HIST_PAGE):
        return await history_page_callback(update, context)


async def history_vod_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data or ""

    from src.database import database as db
    from src.ui.ui import CB_HIST_VOD_PREFIX

    history_id = _history_id_from_callback(data, CB_HIST_VOD_PREFIX)
    if history_id is None:
        await _reply_history_not_found(update, context)
        return

    item = db.get_history_entry_for_user(update.effective_user.id, history_id)
    if not item:
        await _reply_history_not_found(update, context)
        return

    vod_url = item.get("vod_url")
    if not vod_url:
        try:
            await q.message.reply_text("Ссылка не найдена.")
        except Exception:
            await context.bot.send_message(chat_id=q.message.chat_id, text="Ссылка не найдена.")
        return

    try:
        await q.message.reply_text(f"<code>{vod_url}</code>", parse_mode="HTML")
    except Exception:
        await context.bot.send_message(chat_id=q.message.chat_id, text=vod_url)


async def history_page_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data or ""

    from src.ui.ui import CB_HIST_PAGE
    try:
        page = int(data[len(CB_HIST_PAGE):])
    except Exception:
        return

    from src.database import database as db
    # build_history_page может быть в ui — импорт локально
    from src.ui.ui import build_history_page

    total_items = db.get_history_count(update.effective_user.id)
    if total_items:
        last_page = total_items - 1
        page = max(0, min(page, last_page))
    items = db.get_history_for_user(update.effective_user.id, limit=1, offset=page)
    cards, nav_kb = build_history_page(
        items,
        page=page,
        per_page=1,
        total_items=total_items,
    )

    if not cards:
        try:
            await q.message.edit_text("История пуста.")
        except Exception:
            await context.bot.send_message(chat_id=q.message.chat_id, text="История пуста.")
        return

    text, card_kb = cards[0]

    # объединение клавиатур — аккуратно: обе могут быть InlineKeyboardMarkup или None
    if card_kb is not None and nav_kb is not None:
        combined_kb = InlineKeyboardMarkup(list(card_kb.inline_keyboard) + list(nav_kb.inline_keyboard))
    elif card_kb is not None:
        combined_kb = card_kb
    else:
        combined_kb = nav_kb

    try:
        await q.message.edit_text(text, parse_mode="HTML", reply_markup=combined_kb)
    except Exception:
        await context.bot.send_message(chat_id=q.message.chat_id, text=text, parse_mode="HTML", reply_markup=combined_kb)
