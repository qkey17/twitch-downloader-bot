"""Independent history messages; optional metadata outside the card's UI lock."""
import asyncio
import secrets
from src.live_capture import generation_capture
from telegram import InlineKeyboardMarkup
from src.ui.callbacks import CallbackReply
from src.ui.ui_labels import STALE_ACTION_NOTICE
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
    if item.get('fmt') == 'html_online' and state['owner'] is not None:
        from src.ui.html_file_action import keyboard as file_keyboard, lookup
        ready = lookup(item.get('vod_id'))
        if ready:
            selected = dict(item, html_url=ready['html_ref'], captured_during_live=generation_capture(ready))
            selected_cards, _ = build_history_page([selected], page=0, per_page=1)
            text, selected_keyboard = selected_cards[0]
            keyboard = InlineKeyboardMarkup(list(selected_keyboard.inline_keyboard) + list(nav.inline_keyboard if nav else ()))
        keyboard = file_keyboard(context, state['owner'], state['chat'], ready, keyboard, history_id=item.get('history_id'))
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
                                      keep_text=text_shown and not state['photo'], file_context=context)
            if shown is not None and current():
                from src.ui.card_context import bind
                bind(context,state['chat'],shown.message_id)
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
    from src.ui.card_context import for_message
    if owner_id is not None:
        context=for_message(context,owner_id,chat_id,-secrets.randbits(63),fresh=True)
    state = dict(chat=chat_id, owner=owner_id, message_id=None, photo=False,
                 request=object(), lock=asyncio.Lock())
    context.user_data['history_view'] = state
    context.user_data.pop('history_file_card', None)
    await _show_history(context, state, items, page, total_items, state['request'])


async def show_history_page(update, context, page):
    from src.database import database as db
    message = update.callback_query.message
    reply = CallbackReply(update.callback_query)
    state = context.user_data.get('history_view')
    if state is None or message is None or not _current_message(context, message, update.effective_user.id):
        await reply.send(STALE_ACTION_NOTICE)
        return
    if not await reply.send():
        return
    async with state['lock']:
        if not _current_message(context, message, update.effective_user.id):
            await reply.send(STALE_ACTION_NOTICE)
            return
        if not await reply.send():
            return
        request = state['request'] = object()
    total = db.get_history_count(update.effective_user.id)
    page = max(0, min(page, max(0, total-1)))
    items = db.get_history_for_user(update.effective_user.id, limit=1, offset=page)
    await _show_history(context, state, items, page, total, request)
