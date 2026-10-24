from telegram import InlineKeyboardMarkup
from telegram.ext import ContextTypes


async def _send_history_cards(
    chat_id: int,
    context: ContextTypes.DEFAULT_TYPE,
    items: list,
    page: int,
    total_items: int | None = None,
):
    """Send a single history card (one stream per page) with combined action + navigation buttons.

    Импорты делаем локально, чтобы избежать циклических зависимостей при старте.
    """

    # локальные импорты
    from src.ui.ui import build_history_page

    # build page with a single item per page
    cards, nav_kb = build_history_page(
        items,
        page=page,
        per_page=1,
        total_items=total_items,
    )

    if not cards:
        await context.bot.send_message(chat_id=chat_id, text="История пуста.")
        return

    # take the first (and only) card for this page
    text, card_kb = cards[0]

    # merge card keyboard and navigation keyboard into a single InlineKeyboardMarkup
    if card_kb is not None and nav_kb is not None:
        combined_kb = InlineKeyboardMarkup(card_kb.inline_keyboard + nav_kb.inline_keyboard)
    elif card_kb is not None:
        combined_kb = card_kb
    else:
        combined_kb = nav_kb

    await context.bot.send_message(
        chat_id=chat_id,
        text=text,
        parse_mode="HTML",
        reply_markup=combined_kb,
    )
