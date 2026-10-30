"""Active HTML-only entry; shares metadata checks and download lifecycle."""
import secrets
import re
import time
import asyncio
from contextlib import nullcontext
from types import SimpleNamespace
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from . import handlers_vod as vod
from .handlers_state import is_busy, pending_expired


async def prepare_chat(message, context, user_id, vod_id, *, is_current=None, edit=False, back=None, on_start=None, ui_lock=None, thumbnail_url=None):
    source_current = is_current or (lambda: True)
    if not source_current():
        return 'stale'
    request = object()
    context.user_data['chat_request'] = request
    preparing = True
    def current():
        return (context.user_data.get('chat_request') is request
                and (source_current() if preparing or edit else True))
    if not edit:
        message = await message.reply_text('Проверка VOD…')
    async def respond(text, **kwargs):
        if back:
            kwargs['reply_markup'] = InlineKeyboardMarkup([[InlineKeyboardButton('Вернуться к списку', callback_data=back)]])
        return await message.edit_text(text, **kwargs)
    proxy = SimpleNamespace(chat_id=message.chat_id, message_id=message.message_id,
                            edit_text=message.edit_text, reply_text=respond, ui_lock=ui_lock, thumbnail_url=thumbnail_url)
    try:
        outcome = await vod.prepare_vod_choice(proxy, context, user_id, vod_id,
                                              is_current=current, immediate=True, back=back, on_start=on_start)
        if outcome == 'started' and on_start:
            on_start()
        return outcome
    finally:
        preparing = False


async def offer_chat(message, context, owner, ready, current, back, on_start=None):
    token = secrets.token_hex(8)
    choice = dict(token=token, owner=owner, vod_id=ready['vod_id'], created_at=time.time(),
                  chat=message.chat_id, message=message.message_id, current=current, on_start=on_start,
                  ui_lock=getattr(message, 'ui_lock', None), thumbnail_url=getattr(message, 'thumbnail_url', None))
    context.user_data['chat_choice'] = choice
    rows = [[InlineKeyboardButton('Открыть HTML', callback_data=f'ui:chat:open:{token}')],
            [InlineKeyboardButton('Сгенерировать заново', callback_data=f'ui:chat:regen:{token}')]]
    if back:
        rows.append([InlineKeyboardButton('Вернуться к списку', callback_data=back)])
    await message.edit_text('Готовый HTML доступен.', reply_markup=InlineKeyboardMarkup(rows))


async def chat_callback(update, context):
    q = update.callback_query
    await q.answer()
    choice = context.user_data.get('chat_choice')
    parts = (q.data or '').split(':')
    if (not choice or len(parts) != 4 or parts[2] not in ('open', 'regen')
            or parts[3] != choice['token'] or pending_expired(choice)
            or choice['owner'] != update.effective_user.id or not q.message
            or (choice['chat'], choice['message']) != (q.message.chat_id, q.message.message_id)
            or not choice['current']() or is_busy(context) or context.user_data.get('vod_link_lookup')):
        return
    if choice.get('preview_pending') or choice.get('media_final'):
        return
    if parts[2] == 'open':
        if choice.get('opened'):
            return
        choice['opened'] = True  # Consume before edits, including fallback awaits.
        def terminal_photo():
            choice['media_final'] = True
            if choice.get('on_start'):
                choice['on_start']()
        choice['preview_pending'] = True
        try:
            await show_ready(q.message, context, update.effective_user.id, choice['vod_id'],
                             current=choice['current'], ui_lock=choice.get('ui_lock'), on_photo=terminal_photo,
                             thumbnail_url=choice.get('thumbnail_url'))
        finally:
            choice['preview_pending'] = False
        return
    context.user_data.pop('chat_choice', None)  # Consume before the first await.
    started = await vod._start_download(update, context, choice['vod_id'], regenerate=True, modern=True)
    if started and choice.get('on_start'):
        choice['on_start']()


async def show_ready(message, context, user_id, vod_id, *, current=lambda: True, ui_lock=None, on_photo=None, thumbnail_url=None):
    """Resolve the current usable generation using existing access/history rules."""
    if not current():
        return
    request = object()
    context.user_data['ready_preview_request'] = request
    ready = vod.results.get_ready_result(vod_id)
    if not ready:
        async with ui_lock or nullcontext():
            if not current() or context.user_data.get('ready_preview_request') is not request:
                return
            async with asyncio.timeout(5):
                if getattr(message, 'photo', False):
                    await message.edit_caption(caption='Готовый HTML недоступен. Выберите VOD заново.', reply_markup=None)
                else:
                    await message.edit_text('Готовый HTML недоступен. Выберите VOD заново.')
        return
    if vod.config.HISTORY_ENABLED and ready['cache_id'] is not None:
        vod.db.add_user_history(user_id, ready['cache_id'])
    item = vod._make_item(ready['meta'], ready['stats'], f'https://www.twitch.tv/videos/{vod_id}', 'html_online')
    if thumbnail_url is not None:
        item['thumbnail_preview_url'] = thumbnail_url
    await vod._edit_progress_message_with_card(context, message.chat_id, message.message_id, item, ready['html_ref'],
        current=lambda: context.user_data.get('ready_preview_request') is request and current(),
        ui_lock=ui_lock, photo=bool(getattr(message, 'photo', False)), on_photo=on_photo)


async def legacy_open_callback(update, context):
    """Compatibility only: never prepare a missing result or regenerate it."""
    q = update.callback_query
    await q.answer()
    match = re.fullmatch(r'ui:result:open:([1-9][0-9]{0,19})', q.data or '')
    if match and q.message:
        await show_ready(q.message, context, update.effective_user.id, match[1])
