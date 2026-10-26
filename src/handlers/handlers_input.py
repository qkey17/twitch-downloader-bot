"""Automatic text routing and explicit commands, without input-wait modes."""
import time

from telegram import LinkPreviewOptions

from src.config import PENDING_TTL_SECONDS
from src.input_classifier import InputClassificationError, classify_input
from .handlers_channel_vods import show_channel_vods
from .handlers_vod import prepare_vod_choice


# Old messages can retain these buttons after an application restart.
INPUT_CALLBACK_PATTERN = r'^ui:input:'
_ROUTE = 'text_input_route'
_HINTS = {
    'vod': ('Нужна ссылка на Twitch VOD или числовой ID.\n'
            'Примеры: <code>https://www.twitch.tv/videos/&lt;VOD_ID&gt;</code> '
            'или <code>&lt;VOD_ID&gt;</code>.'),
    'channel': ('Нужно имя Twitch-канала или ссылка на канал.\n'
                'Примеры: <code>&lt;CHANNEL_NAME&gt;</code> '
                'или <code>https://www.twitch.tv/&lt;CHANNEL_NAME&gt;</code>.'),
}
_INVALID = 'Не удалось распознать ввод. Отправь VOD-ссылку, числовой ID или имя Twitch-канала. Примеры — /help.'
_BUSY = 'Предыдущий запрос ещё выполняется. Дождись завершения.'


def _alive(record):
    return isinstance(record, dict) and time.monotonic()-record.get('created_at', 0) < PENDING_TTL_SECONDS


def cancel_input_request(context, chat_id):
    """Invalidate a pending lookup, without touching a format choice/download."""
    record = context.user_data.get(_ROUTE)
    if record is not None and record.get('chat_id') == chat_id:
        context.user_data.pop(_ROUTE, None)
        return True
    return False


async def input_menu_callback(update, context):
    """Reject every legacy mode button; it must never modify current state."""
    query = update.callback_query
    if query is not None:
        await query.answer('Меню устарело. Отправь ссылку на VOD или имя канала.', show_alert=True)


def _active_route(context):
    record = context.user_data.get(_ROUTE)
    if record is not None and not _alive(record):
        context.user_data.pop(_ROUTE, None)
        return None
    return record


async def _dispatch(message, context, owner_id, value):
    if _active_route(context) is not None:
        await message.reply_text(_BUSY)
        return
    # Claim before awaiting: a second non-blocking PTB update cannot replace it.
    record = {'chat_id': message.chat_id, 'created_at': time.monotonic()}
    context.user_data[_ROUTE] = record

    def current():
        return context.user_data.get(_ROUTE) is record and _alive(record)

    try:
        if value.kind == 'vod':
            return await prepare_vod_choice(message, context, owner_id, value.value, is_current=current)
        task = await show_channel_vods(message, context, owner_id, value.value)
        return await task  # Already owned by Application.create_task.
    finally:
        if context.user_data.get(_ROUTE) is record:
            context.user_data.pop(_ROUTE, None)


async def text_input(update, context):
    message = update.message
    if message is None:
        return
    try:
        value = classify_input(message.text or '')
    except InputClassificationError:
        await message.reply_text(_INVALID)
        return
    return await _dispatch(message, context, update.effective_user.id, value)


async def _command(update, context, kind):
    message = update.effective_message
    argument = ' '.join(context.args or [])
    if not argument:
        await message.reply_text(_HINTS[kind], parse_mode='HTML',
                                 link_preview_options=LinkPreviewOptions(is_disabled=True))
        return
    try:
        value = classify_input(argument, expected_kind=kind)
    except InputClassificationError:
        await message.reply_text(f'Неверный формат для /{kind}. '+_HINTS[kind], parse_mode='HTML',
                                 link_preview_options=LinkPreviewOptions(is_disabled=True))
        return
    return await _dispatch(message, context, update.effective_user.id, value)


async def vod_command(update, context):
    return await _command(update, context, 'vod')


async def channel_command(update, context):
    return await _command(update, context, 'channel')
