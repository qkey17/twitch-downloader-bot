"""One active history message per user; optional metadata outside UI lock."""
import asyncio
from telegram import InlineKeyboardMarkup
from src.ui.result_preview import render_card, resolve_preview, caption_fits, cleanup_card


def _current_message(context, message, owner=None, history_id=None):
    file_card = getattr(context, 'user_data', {}).get('history_file_card')
    if file_card and (file_card['chat'], file_card.get('message_id'), file_card['owner'], file_card['history_id']) == (message.chat_id, message.message_id, owner, history_id):
        return True
    state = getattr(context, 'user_data', {}).get('history_view')
    # Preserve legacy stable-row actions; DB ownership is still mandatory.
    if state is None:
        return True
    return ((state['chat'], state['message_id']) == (message.chat_id, message.message_id)
            and state['owner'] in (None, owner)
            and (history_id is None or state.get('history_id') == history_id))


async def _show_history(context, state, items, page, total_items, request):
    from src.ui.ui import build_history_page
    cards, nav = build_history_page(items, page=page, per_page=1, total_items=total_items)
    index = min(max(0, page), len(items)-1) if total_items is None else 0
    item = items[index] if cards and items else {}
    if cards:
        text, keyboard = cards[0]
        keyboard = InlineKeyboardMarkup(list(keyboard.inline_keyboard if keyboard else ()) +
                                         list(nav.inline_keyboard if nav else ()))
    else:
        text, keyboard = 'История пуста.', None
    text_shown = False

    def current():
        return context.user_data.get('history_view') is state and state['request'] is request

    async def apply(url=None):
        nonlocal text_shown
        async with state['lock']:
            if not current():
                return
            shown = await render_card(context.bot, state['chat'], text, keyboard, url,
                                      message_id=state['message_id'], photo=state['photo'], current=current,
                                      keep_text=text_shown and not state['photo'])
            if shown is not None and current():
                state.update(message_id=shown.message_id, photo=shown.photo,
                             history_id=item.get('history_id'))
                text_shown = not shown.photo
        await cleanup_card(context.bot, state['chat'], shown)

    if not state['photo']:
        await apply()  # HTML/navigation remain available while Twitch is slow.
    if not current():
        return
    url = await resolve_preview(context, item) if item and caption_fits(text) else None
    if url or state['photo']:
        await apply(url)


async def _send_history_cards(chat_id, context, items, page, total_items=None, *, owner_id=None):
    state = dict(chat=chat_id, owner=owner_id, message_id=None, photo=False,
                 request=object(), lock=asyncio.Lock())
    context.user_data['history_view'] = state
    context.user_data.pop('history_file_card', None)
    await _show_history(context, state, items, page, total_items, state['request'])


async def show_history_page(update, context, page):
    from src.database import database as db
    message = update.callback_query.message
    state = context.user_data.get('history_view')
    if state is None or not _current_message(context, message, update.effective_user.id):
        return
    async with state['lock']:
        if not _current_message(context, message, update.effective_user.id):
            return
        request = state['request'] = object()
    total = db.get_history_count(update.effective_user.id)
    page = max(0, min(page, max(0, total-1)))
    items = db.get_history_for_user(update.effective_user.id, limit=1, offset=page)
    await _show_history(context, state, items, page, total, request)
