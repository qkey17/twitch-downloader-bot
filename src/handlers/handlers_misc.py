from telegram import Update
from src.ui.callbacks import CallbackReply
from telegram.ext import ContextTypes


async def noop_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q:
        await CallbackReply(q).send()