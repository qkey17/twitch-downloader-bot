"""Automatic routing for VODs, channels and privileged batch requests."""
import time
import asyncio
from src.ui.callbacks import CallbackReply
from src.ui.ui_labels import STALE_ACTION_NOTICE

from src.config import PENDING_TTL_SECONDS
from src.input_classifier import ClassifiedInput, InputClassificationError, classify_chat_input
from src.channel_browser import latest_channel_vod
from .handlers_channel_vods import show_channel_vods
from .handlers_chat import prepare_chat as prepare_vod_choice


# Old messages can retain these buttons after an application restart.
INPUT_CALLBACK_PATTERN = r'^ui:input:'
_ROUTE = 'text_input_route'
_INVALID = 'Не удалось распознать запрос. Подробнее — /help.'
_BUSY = 'Дождитесь завершения предыдущего запроса.'


def _alive(record):
    return isinstance(record, dict) and time.monotonic()-record.get('created_at', 0) < PENDING_TTL_SECONDS


def cancel_input_request(context, chat_id):
    """Invalidate a pending lookup, without touching a format choice/download."""
    record = context.user_data.get(_ROUTE)
    if record is not None and record.get('chat_id') == chat_id:
        record['cancelled']=True
        context.user_data.pop(_ROUTE, None)
        return True
    return False


async def input_menu_callback(update, context):
    """Reject every legacy mode button; it must never modify current state."""
    query = update.callback_query
    if query is not None:
        await CallbackReply(query).send(STALE_ACTION_NOTICE)


def _active_route(context):
    record = context.user_data.get(_ROUTE)
    if record is not None and not _alive(record):
        context.user_data.pop(_ROUTE, None)
        return None
    return record


async def _dispatch(message, context, owner_id, value):
    from src.ui.card_context import for_message
    context = for_message(context,owner_id,message.chat_id,message.message_id)
    if value.kind != 'vod':
        return await _dispatch_one(message,context,owner_id,value)
    from src.vod_scheduler import scheduler, INPUT_TICKET
    manager = scheduler(context.application)
    from src.database import db_results
    if db_results.get_ready_result(value.value):
        return await _dispatch_one(message,context,owner_id,value)
    ticket = manager.prepare_input(value.value)
    if ticket is None:
        await message.reply_text('Очередь недоступна. Повторите запрос позже.')
        return
    lock = context.user_data.setdefault('vod_prepare_lock', asyncio.Lock())
    token = INPUT_TICKET.set(ticket)
    try:
        # Serialize only this user's brief metadata/UI preparation, never the VOD.
        # Rapid consecutive links wait here instead of replacing/dropping one another.
        async with lock:
            return await _dispatch_one(message,context,owner_id,value)
    finally:
        manager.release_input(ticket)
        INPUT_TICKET.reset(token)


async def _dispatch_one(message, context, owner_id, value):
    if _active_route(context) is not None:
        await message.reply_text(_BUSY)
        return
    # Claim before awaiting: a second non-blocking PTB update cannot replace it.
    record = {'chat_id': message.chat_id, 'created_at': time.monotonic()}
    context.user_data[_ROUTE] = record
    context.user_data.pop('chat_choice', None)
    context.user_data.pop('chat_request', None)

    def current():
        return context.user_data.get(_ROUTE) is record and not record.get('cancelled') and _alive(record)

    try:
        if value.kind == 'vod':
            return await prepare_vod_choice(message, context, owner_id, value.value, is_current=current)
        if value.batch == 'last':
            try:
                notice, vod_id = await latest_channel_vod(context.application.bot_data['aiohttp'],
                    owner_id, value.value, message.chat_id, message.message_id)
            except (TimeoutError, ValueError):
                notice, vod_id = 'Не удалось определить последний VOD. Повторите попытку позже.', None
            if not current():
                return
            if vod_id is None:
                return await message.reply_text(notice)
            context.user_data.pop(_ROUTE, None)
            return await _dispatch(message, context, owner_id, ClassifiedInput('vod', vod_id))
        task = await show_channel_vods(message, context, owner_id, value.value)
        return await task if task is not None else None  # Already PTB-owned; uncertain delivery stops here.
    finally:
        if context.user_data.get(_ROUTE) is record:
            context.user_data.pop(_ROUTE, None)


async def text_input(update, context):
    message = update.message
    if message is None:
        return
    try:
        value = classify_chat_input(message.text or '')
    except InputClassificationError:
        await message.reply_text(_INVALID)
        return
    owner = getattr(update.effective_user, 'id', None)
    if value.batch in ('all', 'new'):
        from src.admin_batch import start_batch
        return await start_batch(message, context, owner, value.value, new_only=value.batch == 'new')
    if type(owner) is not int:
        return
    return await _dispatch(message, context, owner, value)
