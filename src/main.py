import aiohttp
import logging
from .log_setup import setup_logging
from telegram import BotCommand
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

from src.database import database as db
from src import config
from .config import ConfigurationError, get_telegram_bot_token, validate_credentials
from .html_publisher import cleanup_stale_html_temp_files
from .chat_spool import cleanup_orphaned_chat_spools
from .database.db_results import recover_results, protected_temp_files
from .handlers.handlers_vod import vod_result_callback
from .handlers.handlers_chat import chat_callback, legacy_open_callback
from .handlers.handlers_channel_vods import build_channel_vods_handler
from .handlers.handlers_vod_info import info_callback
from .handlers.handlers_input import (
    text_input, vod_command, channel_command, input_menu_callback, INPUT_CALLBACK_PATTERN,
)
from .html_cleanup import start_cleanup, stop_cleanup
from .maintenance import start_maintenance, stop_maintenance
from src.handlers.handlers import (
    start_command,
    help_command,
    about_command,
    cancel_command,
    vod_link_entry,
    vod_format_chosen,
    pending_cancel_callback,
    ui_buttons,
    history_page_callback,
    history_files_callback,
    history_vod_callback,
    noop_callback,
    format_cancel_callback,
)


async def history_command(update, context: ContextTypes.DEFAULT_TYPE):
    if not config.HISTORY_ENABLED:
        await update.effective_message.reply_text("История отключена.")
        return
    user_id = update.effective_user.id
    total_items = db.get_history_count(user_id)
    if not total_items:
        await update.effective_message.reply_text("История пуста.")
        return
    items = db.get_history_for_user(user_id, limit=1, offset=0)

    from src.history.history_view import _send_history_cards

    await _send_history_cards(
        chat_id=update.effective_chat.id,
        context=context,
        items=items,
        page=0,
        total_items=total_items,
        owner_id=update.effective_user.id,
    )


async def post_init(app: Application):
    db.init_db()
    recover_results()
    removed_spools = cleanup_orphaned_chat_spools(config.SPOOL_DIR)
    if removed_spools:
        logging.getLogger("tcd").info("Removed %s orphaned chat spool(s)", removed_spools)
    removed_temp_files = cleanup_stale_html_temp_files(protected=protected_temp_files())
    if removed_temp_files:
        logging.getLogger("tcd").info(
            "Removed %s stale HTML publication temp file(s)", removed_temp_files
        )
    # Reclaim already eligible orphan temps before considering finished HTML.
    await start_cleanup(app)
    app.bot_data["aiohttp"] = aiohttp.ClientSession()

    commands = [
        BotCommand("help", "Справка"),
    ]
    if config.HISTORY_ENABLED:
        commands.append(BotCommand("history", "История"))
    await app.bot.set_my_commands(commands)
    await start_maintenance(app)


async def post_stop(app: Application):
    try:
        await stop_maintenance(app)
    finally:
        await stop_cleanup(app)


async def post_shutdown(app: Application):
    # Also covers a failed startup, when PTB does not invoke post_stop.
    try:
        try:
            await stop_maintenance(app)
        finally:
            await stop_cleanup(app)
    finally:
        session: aiohttp.ClientSession | None = app.bot_data.get("aiohttp")
        if session:
            await session.close()


def main():
    setup_logging()
    log = logging.getLogger("tcd")
    log.info("Bot starting…")

    try:
        token = get_telegram_bot_token()
        validate_credentials()
    except ConfigurationError as exc:
        log.error("Configuration error: %s", exc)
        raise

    app = (
        Application.builder()
        .token(token)
        .post_init(post_init)
        .post_stop(post_stop)
        .post_shutdown(post_shutdown)
        .build()
    )

    async def on_error(update, context: ContextTypes.DEFAULT_TYPE):
        logging.getLogger("tcd").error("Unhandled error", exc_info=context.error)

    app.add_error_handler(on_error)

    # commands
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("about", about_command))
    app.add_handler(CommandHandler("history", history_command, block=False))
    app.add_handler(CommandHandler("cancel", cancel_command))
    app.add_handler(CommandHandler("vod", vod_command, block=False))
    app.add_handler(CommandHandler("channel", channel_command, block=False))

    # Text/command routing guards per-user input; Twitch waits remain non-blocking.
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_input, block=False), group=0)

    # callbacks
    # Legacy format/result functions are kept, but old keyboards cannot revive
    # the retired confirmation flow after an upgrade.
    app.add_handler(CallbackQueryHandler(legacy_open_callback, pattern=r"^ui:result:open:[1-9][0-9]{0,19}$"))
    app.add_handler(CallbackQueryHandler(input_menu_callback, pattern=r"^(?:vodfmt:|ui:result:)"))
    app.add_handler(CallbackQueryHandler(chat_callback, pattern=r"^ui:chat:", block=False))
    app.add_handler(CallbackQueryHandler(info_callback, pattern=r"^ui:vi:", block=False))
    app.add_handler(build_channel_vods_handler())
    app.add_handler(CallbackQueryHandler(input_menu_callback, pattern=INPUT_CALLBACK_PATTERN))

    app.add_handler(CallbackQueryHandler(format_cancel_callback, pattern=r"^ui:format_cancel$"))
    app.add_handler(CallbackQueryHandler(pending_cancel_callback, pattern=r"^ui:pending_cancel$"))

    app.add_handler(CallbackQueryHandler(ui_buttons, pattern=r"^ui:(history)$", block=False))
    app.add_handler(CallbackQueryHandler(history_page_callback, pattern=r"^ui:histpage:\d+$", block=False))
    app.add_handler(CallbackQueryHandler(history_files_callback, pattern=r"^ui:histfiles:(?:h\d+|\d+)$"))
    app.add_handler(CallbackQueryHandler(history_vod_callback, pattern=r"^ui:histvod:(?:h\d+|\d+)$"))
    app.add_handler(CallbackQueryHandler(noop_callback, pattern=r"^noop$"))

    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
