from telegram import Update
from telegram.ext import ContextTypes
from src import config
from src.ui.callbacks import CallbackReply
from src.ui.ui_labels import STALE_ACTION_NOTICE


async def _answer_if_history_disabled(query) -> bool:
    if not config.HISTORY_ENABLED:
        await CallbackReply(query).send("История отключена.")
        return True
    return False


def _history_id_from_callback(data: str, prefix: str) -> int | None:
    suffix = data[len(prefix):]
    # Numeric suffixes are legacy position-based callbacks and must never be
    # reinterpreted as a current row identifier.
    if not suffix.startswith("h") or not 1 <= len(suffix[1:]) <= 19 or not suffix[1:].isascii() or not suffix[1:].isdigit():
        return None
    history_id = int(suffix[1:])
    return history_id if 0 < history_id < 2**63 else None


async def _reply_history_not_found(update, context):
    await CallbackReply(update.callback_query).send(STALE_ACTION_NOTICE)


async def ui_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if await _answer_if_history_disabled(q):
        return
    data = q.data or ""
    user_id = update.effective_user.id

    # локальные импорты чтобы избежать циклических зависимостей
    from src.database import database as db
    from src.ui.ui import CB_UI_HISTORY, CB_HIST_PAGE, CB_HIST_VOD_PREFIX, CB_HIST_FILES_PREFIX
    from src.history.history_view import _send_history_cards

    if data == CB_UI_HISTORY:
        if q.message is None:
            await CallbackReply(q).send(STALE_ACTION_NOTICE)
            return
        if not await CallbackReply(q).send():
            return
        total_items = db.get_history_count(user_id)
        items = db.get_history_for_user(user_id, limit=1, offset=0)
        await _send_history_cards(
            chat_id=q.message.chat_id,
            context=context,
            items=items,
            page=0,
            total_items=total_items,
            owner_id=user_id,
        )
        return

    if data.startswith(CB_HIST_VOD_PREFIX):
        return await history_vod_callback(update, context)

    if data.startswith(CB_HIST_FILES_PREFIX):
        from .history_files import history_files_callback
        return await history_files_callback(update, context)

    if data.startswith(CB_HIST_PAGE):
        return await history_page_callback(update, context)


async def history_vod_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if await _answer_if_history_disabled(q):
        return
    data = q.data or ""

    from src.database import database as db
    from src.ui.ui import CB_HIST_VOD_PREFIX

    history_id = _history_id_from_callback(data, CB_HIST_VOD_PREFIX)
    if history_id is None or q.message is None:
        await _reply_history_not_found(update, context)
        return

    item = db.get_history_entry_for_user(update.effective_user.id, history_id)
    if not item:
        await _reply_history_not_found(update, context)
        return

    vod_url = item.get("vod_url")
    from .history_view import _current_message
    if not _current_message(context, q.message, update.effective_user.id, history_id):
        await CallbackReply(q).send(STALE_ACTION_NOTICE)
        return
    if not vod_url:
        await CallbackReply(q).send('Ссылка на VOD недоступна.')
        return
    if not await CallbackReply(q).send():
        return
    if not _current_message(context, q.message, update.effective_user.id, history_id):
        return  # Binding changed during ACK; no second notification.

    try:
        await q.message.reply_text(f"<code>{vod_url}</code>", parse_mode="HTML")
    except Exception:
        await context.bot.send_message(chat_id=q.message.chat_id, text=vod_url)


async def history_page_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if await _answer_if_history_disabled(q):
        return
    data = q.data or ""

    from src.ui.ui import CB_HIST_PAGE
    try:
        page = int(data[len(CB_HIST_PAGE):])
    except (ValueError, TypeError):
        await CallbackReply(q).send(STALE_ACTION_NOTICE)
        return

    from .history_view import show_history_page
    await show_history_page(update, context, page)
