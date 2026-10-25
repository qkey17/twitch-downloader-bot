from telegram import Update
from telegram.ext import ContextTypes
from src.ui.ui import about_text, build_about_keyboard
from .handlers_state import is_busy, get_pending, clear_pending


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    await msg.reply_text(
        "Просто отправь ссылку на Twitch VOD:\n"
        "<code>https://www.twitch.tv/videos/0123456789</code>",
        parse_mode="HTML",
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

    if is_busy(context):
        await msg.reply_text("Сейчас идёт скачивание.")
        return

    if get_pending(context) or context.user_data.get("vod_link_lookup"):
        clear_pending(context)
        await msg.reply_text("Процесс отменен.")
    else:
        await msg.reply_text("Нечего отменять.")
