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
from src.ui.ui_labels import GET_READY_CHAT, RECREATE_CHAT, BACK_TO_LIST, READY_UNAVAILABLE
from src.ui.ui_history import ready_offer_text
from src.ui.result_preview import render_card, resolve_preview, caption_fits, cleanup_card, CardMessage


async def prepare_chat(message, context, user_id, vod_id, *, is_current=None, edit=False, back=None, on_start=None, ui_lock=None, thumbnail_url=None, ready_only=False, on_bind=None, download_origin=None, on_download=None):
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
            kwargs['reply_markup'] = InlineKeyboardMarkup([[InlineKeyboardButton(BACK_TO_LIST, callback_data=back)]])
        return await message.edit_text(text, **kwargs)
    proxy = SimpleNamespace(chat_id=message.chat_id, message_id=message.message_id,
                            edit_text=message.edit_text, reply_text=respond, ui_lock=ui_lock, thumbnail_url=thumbnail_url,
                            on_bind=on_bind, download_origin=download_origin, on_download=on_download)
    try:
        outcome = await vod.prepare_vod_choice(proxy, context, user_id, vod_id,
                                              is_current=current, immediate=True, back=back, on_start=on_start,
                                              ready_only=ready_only)
        if outcome == 'started' and on_download is None and on_start:
            on_start()  # Compatibility for callers without cancellable list context.
        return outcome
    finally:
        preparing = False


async def offer_chat(message, context, owner, ready, current, back, on_start=None):
    token = secrets.token_hex(8)
    choice = dict(token=token, owner=owner, vod_id=ready['vod_id'], created_at=time.time(),
                  chat=message.chat_id, message=message.message_id, current=current, on_start=on_start,
                  ui_lock=getattr(message, 'ui_lock', None) or asyncio.Lock(),
                  thumbnail_url=getattr(message, 'thumbnail_url', None),
                  on_bind=getattr(message, 'on_bind', None), photo=False,
                  download_origin=getattr(message, 'download_origin', None),
                  on_download=getattr(message, 'on_download', None), meta=ready['meta'])
    rows = [[InlineKeyboardButton(GET_READY_CHAT, callback_data=f'ui:chat:open:{token}')],
            [InlineKeyboardButton(RECREATE_CHAT, callback_data=f'ui:chat:regen:{token}')]]
    if back:
        rows.append([InlineKeyboardButton(BACK_TO_LIST, callback_data=back)])
    text, keyboard = ready_offer_text(ready), InlineKeyboardMarkup(rows)
    choice.update(text=text, keyboard=keyboard)
    def offer_current():
        return (context.user_data.get('chat_choice') is choice and current()
                and not choice.get('action') and not choice.get('opened'))
    async with choice['ui_lock']:
        if not current():
            return
        context.user_data['chat_choice'] = choice
        async with asyncio.timeout(5):
            await message.edit_text(text, parse_mode='HTML', reply_markup=keyboard)
    if not offer_current() or not caption_fits(text):
        return
    item = vod._make_item(ready['meta'], ready['stats'], f'https://www.twitch.tv/videos/{ready["vod_id"]}', 'html_online')
    if choice['thumbnail_url'] is not None:
        item['thumbnail_preview_url'] = choice['thumbnail_url']
    else:
        # An Open while this optional lookup is pending must show its links
        # immediately, rather than start a second lookup for the same offer.
        choice['thumbnail_url'] = ''
    url = await resolve_preview(context, item)
    if not url:
        return
    async with choice['ui_lock']:
        if not offer_current():
            return
        choice['thumbnail_url'] = url
        # A cancelled/ambiguous media edit might have reached Telegram. Treat
        # it conservatively as photo until a definite text fallback is known.
        _bind_choice(choice, CardMessage(choice['message'], True))
        shown = await render_card(context.bot, choice['chat'], text, keyboard, url,
                                  message_id=choice['message'], current=offer_current, keep_text=True)
        if shown is not None and offer_current():
            _bind_choice(choice, shown)


def _bind_choice(choice, shown):
    choice.update(message=shown.message_id, photo=shown.photo or shown.media_uncertain)
    if choice.get('on_bind'):
        choice['on_bind'](shown)


async def chat_callback(update, context):
    q = update.callback_query
    await q.answer()
    choice = context.user_data.get('chat_choice')
    parts = (q.data or '').split(':')
    def valid():
        return (choice is not None and context.user_data.get('chat_choice') is choice
                and len(parts) == 4 and parts[2] in ('open', 'regen')
                and parts[3] == choice['token'] and not pending_expired(choice)
                and choice['owner'] == update.effective_user.id and q.message is not None
                and (choice['chat'], choice['message']) == (q.message.chat_id, q.message.message_id)
                and choice['current']() and not is_busy(context) and not context.user_data.get('vod_link_lookup'))
    if not valid():
        return
    async with choice.get('ui_lock') or nullcontext():
        if not valid() or choice.get('action') or choice.get('preview_pending') or choice.get('media_final'):
            return
        if parts[2] == 'open' and choice.get('opened'):
            return
        choice['action'] = parts[2]  # Fence the optional offer preview before any await.
    if parts[2] == 'open':
        choice['opened'] = True
        def terminal_photo():
            choice['media_final'] = True
            if choice.get('on_start'):
                choice['on_start']()
        choice['preview_pending'] = True
        try:
            shown = await show_ready(q.message, context, update.effective_user.id, choice['vod_id'],
                             current=lambda: context.user_data.get('chat_choice') is choice and choice['current'](),
                             ui_lock=choice.get('ui_lock'), on_photo=terminal_photo,
                             thumbnail_url=choice.get('thumbnail_url'), photo=choice.get('photo', False))
            if shown and context.user_data.get('chat_choice') is choice and not choice.get('media_final'):
                terminal_photo()  # A final text card also consumes old actions.
        finally:
            choice['preview_pending'] = False
            choice.pop('action', None)
        return
    replaced = None
    try:
        async with choice.get('ui_lock') or nullcontext():
            if not valid():
                return
            message = q.message
            async def edit_start(*args, **kwargs):
                nonlocal replaced, message
                if choice.get('photo'):
                    if kwargs.get('reply_markup') is None:
                        return  # Failed first delivery: keep the original photo/actions.
                    replaced = await render_card(context.bot, choice['chat'], kwargs['text'], kwargs['reply_markup'],
                                                 message_id=choice['message'], photo=True,
                                                 current=lambda: context.user_data.get('chat_choice') is choice and choice['current']())
                    if replaced is None:
                        raise RuntimeError('progress_delivery_failed')
                    _bind_choice(choice, replaced)
                    message = replaced.message
                    start_message.message_id = message.message_id
                    return message
                async with asyncio.timeout(5):
                    return await message.edit_text(*args, **kwargs)
            async def reply_start(*args, **kwargs):
                async with asyncio.timeout(5):
                    return await message.reply_text(*args, **kwargs)
            start_message = SimpleNamespace(chat_id=message.chat_id, message_id=message.message_id,
                                            edit_text=edit_start, reply_text=reply_start,
                                            download_origin=choice.get('download_origin'), on_download=choice.get('on_download'))
            request = SimpleNamespace(callback_query=SimpleNamespace(message=start_message), effective_user=update.effective_user)
            try:
                started = await vod._start_download(request, context, choice['vod_id'], regenerate=True, modern=True,
                                                    initial_meta=choice['meta'])
            except BaseException:
                # The existing starter owns slot/telemetry cleanup. Restore only
                # this still-owned action screen; never reserve/start a second time.
                if context.user_data.get('chat_choice') is choice and choice['current']():
                    try:
                        async with asyncio.timeout(5):
                            await message.edit_text(choice['text'], parse_mode='HTML', reply_markup=choice['keyboard'])
                    except Exception:
                        from .handlers_vod_info import _diagnostic
                        _diagnostic('offer_restore_failed')
                raise
            if started:
                if choice.get('on_download') is None and choice.get('on_start'):
                    choice['on_start']()
                if context.user_data.get('chat_choice') is choice:
                    context.user_data.pop('chat_choice', None)
    finally:
        choice.pop('action', None)
        await cleanup_card(context.bot, choice['chat'], replaced)


async def show_ready(message, context, user_id, vod_id, *, current=lambda: True, ui_lock=None, on_photo=None, thumbnail_url=None, photo=None):
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
                if (photo if photo is not None else getattr(message, 'photo', False)):
                    await message.edit_caption(caption=READY_UNAVAILABLE, reply_markup=None)
                else:
                    await message.edit_text(READY_UNAVAILABLE)
        return
    if vod.config.HISTORY_ENABLED and ready['cache_id'] is not None:
        vod.db.add_user_history(user_id, ready['cache_id'])
    item = vod._make_item(ready['meta'], ready['stats'], f'https://www.twitch.tv/videos/{vod_id}', 'html_online')
    if thumbnail_url is not None:
        item['thumbnail_preview_url'] = thumbnail_url
    return await vod._edit_progress_message_with_card(context, message.chat_id, message.message_id, item, ready['html_ref'],
        current=lambda: context.user_data.get('ready_preview_request') is request and current(),
        ui_lock=ui_lock, photo=bool(getattr(message, 'photo', False)) if photo is None else photo, on_photo=on_photo)


async def legacy_open_callback(update, context):
    """Compatibility only: never prepare a missing result or regenerate it."""
    q = update.callback_query
    await q.answer()
    match = re.fullmatch(r'ui:result:open:([1-9][0-9]{0,19})', q.data or '')
    if match and q.message:
        await show_ready(q.message, context, update.effective_user.id, match[1])
