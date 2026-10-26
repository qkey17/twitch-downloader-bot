"""Text/command/menu routing; input modes are separate from vod_pending."""
import re
import secrets
import time

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from src.config import PENDING_TTL_SECONDS
from src.input_classifier import InputClassificationError, classify_input
from .handlers_channel_vods import show_channel_vods
from .handlers_vod import prepare_vod_choice


INPUT_CALLBACK_PATTERN = r'^ui:input:'
_CALLBACK_RE = re.compile(r'ui:input:([0-9a-f]{16}):(vod|channel|cancel)')
_WAIT = 'text_input_wait'
_MENU = 'text_input_menu'
_ROUTE = 'text_input_route'
_LABELS = {'vod': 'Получить чат VOD', 'channel': 'Найти VOD канала'}
_HINTS = {
    'vod': 'Отправь ссылку на Twitch VOD или числовой ID. Для выхода — /cancel.',
    'channel': 'Отправь имя Twitch-канала или URL вида https://www.twitch.tv/channel_name. Для выхода — /cancel.',
}
_INVALID = ('Не удалось распознать ввод. Отправь ссылку или ID Twitch VOD либо имя/URL Twitch-канала. '
            'Можно использовать /vod или /channel.')
_BUSY = 'Предыдущий запрос ещё выполняется. Дождись завершения или используй /cancel.'


def _alive(record):
    return isinstance(record, dict) and time.monotonic()-record.get('created_at', 0) < PENDING_TTL_SECONDS


def _new_menu(context, owner_id, chat_id, mode=None):
    # Explicit menu/mode changes invalidate only input work, never vod_pending.
    context.user_data.pop(_WAIT, None)
    context.user_data.pop(_ROUTE, None)
    record = {'token': secrets.token_hex(8), 'owner_id': owner_id, 'chat_id': chat_id,
              'message_id': None, 'mode': mode, 'created_at': time.monotonic()}
    context.user_data[_MENU] = record
    if mode is not None:
        context.user_data[_WAIT] = record
    return record


def _keyboard(record):
    choices = ('vod', 'channel') if record['mode'] is None else (
        'channel' if record['mode'] == 'vod' else 'vod', 'cancel')
    return InlineKeyboardMarkup([
        [InlineKeyboardButton('Отмена' if action == 'cancel' else _LABELS[action],
                              callback_data=f"ui:input:{record['token']}:{action}")]
        for action in choices
    ])


async def _send_menu(message, context, record, text, **options):
    try:
        sent = await message.reply_text(text, reply_markup=_keyboard(record), **options)
    except BaseException:
        if context.user_data.get(_MENU) is record:
            context.user_data.pop(_MENU, None)
        if context.user_data.get(_WAIT) is record:
            context.user_data.pop(_WAIT, None)
        raise
    if context.user_data.get(_MENU) is record:
        record['message_id'] = sent.message_id


async def show_start_menu(message, context, owner_id, text):
    record = _new_menu(context, owner_id, message.chat_id)
    await _send_menu(message, context, record, text, parse_mode='HTML')


async def _request_input(message, context, owner_id, kind):
    record = _new_menu(context, owner_id, message.chat_id, kind)
    await _send_menu(message, context, record, _HINTS[kind])


def cancel_input_wait(context, chat_id):
    """Cancel input only. A format dialog or active download is not modified."""
    cancelled = False
    for key in (_WAIT, _ROUTE):
        record = context.user_data.get(key)
        if record is not None and record.get('chat_id') == chat_id:
            context.user_data.pop(key, None)
            cancelled = True
    menu = context.user_data.get(_MENU)
    if menu is not None and menu.get('chat_id') == chat_id:
        context.user_data.pop(_MENU, None)
    return cancelled


async def input_menu_callback(update, context):
    query = update.callback_query
    if query is None:
        return
    match = _CALLBACK_RE.fullmatch(query.data or '')
    record = context.user_data.get(_MENU)
    message = query.message
    if (not match or not _alive(record) or message is None
            or record['token'] != match[1] or record['owner_id'] != update.effective_user.id
            or (record['chat_id'], record['message_id']) != (message.chat_id, message.message_id)):
        await query.answer('Меню устарело или принадлежит другому пользователю.', show_alert=True)
        return
    action = match[2]
    if action == 'cancel':
        if context.user_data.get(_WAIT) is not record:
            await query.answer('Режим ввода уже завершён.', show_alert=True)
            return
        cancel_input_wait(context, message.chat_id)
        await query.answer()
        await message.reply_text('Ввод отменён.')
        return
    # Replace the token before any Telegram await: repeated presses are stale.
    new_record = _new_menu(context, update.effective_user.id, message.chat_id, action)
    await query.answer()
    await _send_menu(message, context, new_record, _HINTS[action])


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
    # Claim without awaiting; a second non-blocking PTB update cannot replace it.
    record = {'chat_id': message.chat_id, 'created_at': time.monotonic()}
    context.user_data[_ROUTE] = record
    waiting = context.user_data.get(_WAIT)
    if waiting is not None and waiting['chat_id'] == message.chat_id:
        context.user_data.pop(_WAIT, None)
    menu = context.user_data.get(_MENU)
    if menu is not None and menu['chat_id'] == message.chat_id:
        context.user_data.pop(_MENU, None)

    def current():
        return context.user_data.get(_ROUTE) is record and _alive(record)

    try:
        if value.kind == 'vod':
            return await prepare_vod_choice(message, context, owner_id, value.value, is_current=current)
        task = await show_channel_vods(message, context, owner_id, value.value)
        return await task  # The task is already owned by Application.create_task.
    finally:
        if context.user_data.get(_ROUTE) is record:
            context.user_data.pop(_ROUTE, None)


async def text_input(update, context):
    message = update.message
    if message is None:
        return
    waiting = context.user_data.get(_WAIT)
    if waiting is not None and waiting['chat_id'] == message.chat_id:
        if not _alive(waiting):
            cancel_input_wait(context, message.chat_id)
            await message.reply_text('Время ожидания ввода истекло. Используй /vod, /channel или /start.')
            return
        expected = waiting['mode']
    else:
        expected = None
    try:
        value = classify_input(message.text or '')
    except InputClassificationError:
        await message.reply_text(_HINTS[expected] if expected else _INVALID)
        return
    if expected is not None and value.kind != expected:
        await message.reply_text(_HINTS[expected]+' Для смены режима используй /vod или /channel.')
        return
    return await _dispatch(message, context, update.effective_user.id, value)


async def _command(update, context, kind):
    message = update.effective_message
    argument = ' '.join(context.args or [])
    if not argument:
        await _request_input(message, context, update.effective_user.id, kind)
        return
    if _active_route(context) is not None:
        await message.reply_text(_BUSY)
        return
    try:
        value = classify_input(argument)
    except InputClassificationError:
        value = None
    if value is None or value.kind != kind:
        await _request_input(message, context, update.effective_user.id, kind)
        return
    # Explicit commands with arguments supersede a different waiting input type.
    cancel_input_wait(context, message.chat_id)
    return await _dispatch(message, context, update.effective_user.id, value)


async def vod_command(update, context):
    return await _command(update, context, 'vod')


async def channel_command(update, context):
    return await _command(update, context, 'channel')
