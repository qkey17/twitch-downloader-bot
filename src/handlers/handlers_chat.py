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
from src.ui.callbacks import CallbackReply
from src.ui.ui_labels import STALE_ACTION_NOTICE, ACTION_DONE_NOTICE, PROCESSING_NOTICE, ACTION_PENDING_NOTICE
from src.ui.ui_labels import GET_READY_CHAT, RECREATE_CHAT, CREATE_CHAT, BACK_TO_LIST, READY_UNAVAILABLE
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
    ui_lock = ui_lock or asyncio.Lock()
    proxy = SimpleNamespace(chat_id=message.chat_id, message_id=message.message_id,
                            edit_text=message.edit_text, reply_text=respond, ui_lock=ui_lock, thumbnail_url=thumbnail_url,
                            on_bind=on_bind, download_origin=download_origin, on_download=on_download,
                            direct_vod=download_origin is None)
    try:
        outcome = await vod.prepare_vod_choice(proxy, context, user_id, vod_id,
                                              is_current=current, immediate=True, back=back, on_start=on_start,
                                              ready_only=ready_only)
        if outcome == 'started' and on_download is None and on_start:
            on_start()  # Compatibility for callers without cancellable list context.
        return outcome
    finally:
        preparing = False


def direct_information_text(meta, vod_id):
    from .handlers_vod_info import information_text
    from src.ui.ui_formatters import _fmt_len
    def known(key, limit):
        value = meta.get(key)
        return value[:limit] if isinstance(value, str) else None
    text = information_text(known('channel', 120), known('title', 1200),
                            _fmt_len(meta.get('length_seconds')), known('created_at', 64))
    if not known('title', 1200):
        text += f'\nVOD {vod_id}\nhttps://www.twitch.tv/videos/{vod_id}'
    return text


async def restore_direct_cancelled(context, record):
    """Called after worker/publication cleanup and operation-owned slot release."""
    request = record.get('request')
    # Admission is checked before installation. The request fence must survive
    # this card reserving its own next operation; choice/action fences own media.
    def current():
        return request is not None and context.user_data.get('chat_request') is request
    if (context.user_data.get('download_ui') is not record or not current()
            or is_busy(context) or context.user_data.get('vod_active_operation') is not None
            or record.get('origin') or not record.get('direct')):
        return False
    # Resolve availability at restoration time; never mutate or regenerate HTML.
    ready = vod.results.get_ready_result(record['vod_id'])
    meta = dict(record.get('meta') or {})
    if ready is not None:
        ready = dict(ready, meta=meta | {k:v for k,v in (ready.get('meta') or {}).items() if v is not None and v != ''})
    else:
        ready = dict(vod_id=record['vod_id'], meta=meta, stats={})
    message = SimpleNamespace(chat_id=record['chat'], message_id=record['message'],
                              ui_lock=record.get('ui_lock') or asyncio.Lock(), direct_vod=True)
    await offer_chat(message, context, record['owner'], ready, current, None,
                     create=not bool(ready.get('html_ref')), restore=True)
    return True


async def create_from_choice(query, context, choice):
    """Use the existing preparation/starter, including fresh metadata validation."""
    replaced = None
    def current():
        return context.user_data.get('chat_choice') is choice and choice['current']()
    try:
        async with choice['ui_lock']:
            if not current() or is_busy(context) or vod.is_globally_busy(context):
                return
            message = query.message
            if choice.get('photo'):
                replaced = await render_card(context.bot, choice['chat'], choice['text'], choice['keyboard'],
                    message_id=choice['message'], photo=True, current=current)
                if replaced is None:
                    return
                _bind_choice(choice, replaced)
                message = replaced.message
        # No await between the owned screen check and prepare_chat claiming a
        # fresh request. Its identity fences new input throughout preparation.
        outcome = await prepare_chat(message, context, choice['owner'], choice['vod_id'], edit=True,
            ui_lock=choice['ui_lock'], thumbnail_url=choice.get('thumbnail_url'))
        if outcome == 'started' and context.user_data.get('chat_choice') is choice:
            context.user_data.pop('chat_choice', None)
    finally:
        choice.pop('action', None)
        await cleanup_card(context.bot, choice['chat'], replaced)


async def offer_chat(message, context, owner, ready, current, back, on_start=None, *, create=False, restore=False):
    token = secrets.token_hex(8)
    choice = dict(token=token, owner=owner, vod_id=ready['vod_id'], created_at=time.time(),
                  chat=message.chat_id, message=message.message_id, current=current, on_start=on_start,
                  ui_lock=getattr(message, 'ui_lock', None) or asyncio.Lock(),
                  thumbnail_url=getattr(message, 'thumbnail_url', None),
                  on_bind=getattr(message, 'on_bind', None), photo=False,
                  download_origin=getattr(message, 'download_origin', None),
                  on_download=getattr(message, 'on_download', None), meta=ready['meta'],
                  direct=bool(getattr(message, 'direct_vod', False)), create=create)
    rows = [[InlineKeyboardButton(GET_READY_CHAT, callback_data=f'ui:chat:open:{token}')],
            [InlineKeyboardButton(RECREATE_CHAT, callback_data=f'ui:chat:regen:{token}')]]
    if create:
        rows = [[InlineKeyboardButton(CREATE_CHAT, callback_data=f'ui:chat:create:{token}')]]
    if back:
        rows.append([InlineKeyboardButton(BACK_TO_LIST, callback_data=back)])
    text = direct_information_text(ready['meta'], ready['vod_id']) if create else ready_offer_text(ready)
    keyboard = InlineKeyboardMarkup(rows)
    choice.update(text=text, keyboard=keyboard)
    def offer_current():
        return (context.user_data.get('chat_choice') is choice and current()
                and not choice.get('action') and not choice.get('opened'))
    async with choice['ui_lock']:
        if not current():
            return
        context.user_data['chat_choice'] = choice
        if restore:
            shown = await render_card(context.bot, choice['chat'], text, keyboard,
                                      message_id=choice['message'], current=offer_current)
            if shown is None:
                if context.user_data.get('chat_choice') is choice:
                    context.user_data.pop('chat_choice', None)
                return False
            _bind_choice(choice, shown)
        else:
            shown = None
            async with asyncio.timeout(5):
                await message.edit_text(text, parse_mode='HTML', reply_markup=keyboard)
    await cleanup_card(context.bot, choice['chat'], shown)
    if not offer_current() or not caption_fits(text):
        return
    item = vod._make_item(ready['meta'], ready['stats'], f'https://www.twitch.tv/videos/{ready["vod_id"]}', 'html_online')
    if choice['thumbnail_url'] is not None:
        item['thumbnail_preview_url'] = choice['thumbnail_url']
    elif 'thumbnail_preview_url' in choice['meta'] or 'thumbnail_url' in choice['meta']:
        choice['thumbnail_url'] = choice['meta'].get('thumbnail_preview_url', choice['meta'].get('thumbnail_url')) or ''
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
    reply = CallbackReply(q)
    choice = context.user_data.get('chat_choice')
    parts = (q.data or '').split(':')
    def valid():
        return (choice is not None and context.user_data.get('chat_choice') is choice
                and len(parts) == 4 and parts[2] in ('open', 'regen', 'create')
                and (parts[2] == 'create') == choice.get('create', False)
                and parts[3] == choice['token'] and not pending_expired(choice)
                and choice['owner'] == update.effective_user.id and q.message is not None
                and (choice['chat'], choice['message']) == (q.message.chat_id, q.message.message_id)
                and choice['current']())
    if not valid():
        await reply.send(STALE_ACTION_NOTICE)
        return
    if is_busy(context) or (parts[2] in ('regen', 'create') and vod.is_globally_busy(context)):
        await reply.send(PROCESSING_NOTICE)
        return
    if context.user_data.get('vod_link_lookup'):
        await reply.send(ACTION_PENDING_NOTICE)
        return
    if choice.get('media_final'):
        await reply.send(ACTION_DONE_NOTICE)
        return
    if choice.get('action') or choice.get('preview_pending'):
        await reply.send(ACTION_PENDING_NOTICE)
        return
    if parts[2] == 'open' and choice.get('opened'):
        await reply.send(STALE_ACTION_NOTICE)
        return
    if not await reply.send():
        return
    async with choice.get('ui_lock') or nullcontext():
        if not valid():
            await reply.send(STALE_ACTION_NOTICE)
            return
        if choice.get('media_final'):
            await reply.send(ACTION_DONE_NOTICE)
            return
        if choice.get('action') or choice.get('preview_pending'):
            await reply.send(ACTION_PENDING_NOTICE)
            return
        if parts[2] == 'open' and choice.get('opened'):
            await reply.send(STALE_ACTION_NOTICE)
            return
        if is_busy(context):
            await reply.send(PROCESSING_NOTICE)
            return
        if not await reply.send():
            return
        choice['action'] = parts[2]  # Fence the optional offer preview before any await.
    if parts[2] == 'create':
        return await create_from_choice(q, context, choice)
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
            if is_busy(context) or vod.is_globally_busy(context) or context.user_data.get('vod_link_lookup'):
                return  # Already acknowledged; never send a second answer on a race.
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
                                            download_origin=choice.get('download_origin'), on_download=choice.get('on_download'),
                                            direct_vod=choice.get('direct', False), ui_lock=choice.get('ui_lock'),
                                            thumbnail_url=choice.get('thumbnail_url'))
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
    reply = CallbackReply(q)
    match = re.fullmatch(r'ui:result:open:([1-9][0-9]{0,19})', q.data or '')
    if not match or q.message is None:
        await reply.send(STALE_ACTION_NOTICE)
        return
    key = (update.effective_user.id, q.message.chat_id, q.message.message_id, match[1])
    active = context.user_data.get('download_ui')
    if active and (active['chat'], active['message']) == key[1:3]:
        await reply.send(STALE_ACTION_NOTICE)
        return
    old = context.user_data.get('legacy_open')
    if old and old['key'] == key and not pending_expired(old):
        await reply.send(ACTION_DONE_NOTICE if old['done'] else ACTION_PENDING_NOTICE if old.get('pending') else STALE_ACTION_NOTICE)
        return
    if not vod.results.get_ready_result(match[1]):
        await reply.send(READY_UNAVAILABLE)
        return
    record = dict(key=key, created_at=time.time(), done=False, pending=True)
    context.user_data['legacy_open'] = record
    if not await reply.send():
        if context.user_data.get('legacy_open') is record:
            context.user_data.pop('legacy_open', None)
        return
    try:
        shown = await show_ready(q.message, context, update.effective_user.id, match[1],
                         current=lambda: context.user_data.get('legacy_open') is record)
        record['done'] = bool(shown)
    finally:
        record['pending'] = False
