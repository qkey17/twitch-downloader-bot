from telegram import Update, LinkPreviewOptions
from telegram.ext import ContextTypes
from src.ui.ui import about_text, build_about_keyboard
from .handlers_state import is_busy, get_pending, clear_pending
from .handlers_input import cancel_input_request


_HELP_TEXT = (
    "Отправьте ссылку на VOD или его ID, чтобы получить чат.\n\n"
    "Отправьте имя канала или ссылку на него, чтобы посмотреть список VODs.\n\n"
    "Тип запроса определяется автоматически. При необходимости его можно указать командой:\n\n"
    "/vod — получить чат VOD.\n"
    "/channel — посмотреть VODs канала."
)


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await help_command(update, context)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        _HELP_TEXT,
        parse_mode='HTML',
        link_preview_options=LinkPreviewOptions(is_disabled=True),
    )


async def about_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    await msg.reply_text(
        about_text(),
        parse_mode="HTML",
        reply_markup=build_about_keyboard(),
    )


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message

    if cancel_input_request(context, msg.chat_id):
        await msg.reply_text('Действие отменено.')
        return
    if is_busy(context):
        await msg.reply_text("Чат обрабатывается. Для отмены используйте кнопку «Отмена».")
        return

    if get_pending(context) or context.user_data.get("vod_link_lookup"):
        clear_pending(context)
        await msg.reply_text("Действие отменено.")
    else:
        await msg.reply_text("Нечего отменять.")
