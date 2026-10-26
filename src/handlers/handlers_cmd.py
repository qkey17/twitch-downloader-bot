from telegram import Update, LinkPreviewOptions
from telegram.ext import ContextTypes
from src.ui.ui import about_text, build_about_keyboard
from .handlers_state import is_busy, get_pending, clear_pending
from .handlers_input import cancel_input_request


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    await msg.reply_text(
        "Отправь ссылку на Twitch VOD или числовой ID, чтобы получить чат.\n"
        "Отправь имя Twitch-канала или ссылку на канал, чтобы посмотреть записи.\n\n"
        "Подробности и примеры — /help.",
        link_preview_options=LinkPreviewOptions(is_disabled=True),
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "Просто отправь сообщение — тип ввода распознаётся автоматически.\n\n"
        "Чат VOD — ссылка или числовой ID.\n"
        "Список VODs канала — ссылка или имя канала.\n"
        "\n"
        "Ввод через команду задают принудительный тип запроса.",
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
        await msg.reply_text('Процесс отменен.')
        return
    if is_busy(context):
        await msg.reply_text("Сейчас идёт скачивание.")
        return

    if get_pending(context) or context.user_data.get("vod_link_lookup"):
        clear_pending(context)
        await msg.reply_text("Процесс отменен.")
    else:
        await msg.reply_text("Нечего отменять.")
