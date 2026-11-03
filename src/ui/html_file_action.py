"""Durable generation actions authenticated for owner/chat and bot message markup.

The callback lives on the Telegram message, not in a last-card RAM capability.
The bot token is an existing signing key; rotating it invalidates old signatures.
"""
import asyncio
import base64
import hashlib
import hmac
import re
import secrets
import sqlite3
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import TelegramError
from src.html_sender import SENDER_KEY, SendStatus
from src.ready_html import HtmlUnavailable
from src.ui.callbacks import CallbackReply
from src.ui.ui_labels import CHAT_GENERIC, VOD_LINK, STALE_ACTION_NOTICE, DOWNLOAD_HTML
from src.ui.result_actions import stack_actions

PREFIX = 'ui:htmlfile:'
_CALLBACK = re.compile(re.escape(PREFIX) + r'2[A-Za-z0-9_-]{51}')
NOTICES = {
    SendStatus.UNAVAILABLE: 'Файл сейчас недоступен.',
    SendStatus.TOO_LARGE: 'Файл превышает предел отправки 40 МиБ.',
    SendStatus.USER_BUSY: 'Файл уже отправляется.',
    SendStatus.SENDER_BUSY: 'Отправка сейчас занята. Повторите позже.',
    SendStatus.ADMISSION_CLOSED: 'Отправка временно недоступна.',
    SendStatus.TELEGRAM_ERROR: 'Не удалось подтвердить отправку файла. Проверьте чат перед повторной попыткой.',
    SendStatus.TIMEOUT: 'Не удалось подтвердить отправку файла. Проверьте чат перед повторной попыткой.',
}


def lookup(vod_id):
    # UI discovery must not trigger get_ready_result's legacy migration writes.
    from src.ready_html import _row
    from src.database import db_results as results
    try:
        row = _row(results.normalize_vod_id(vod_id))
        return row if results.file_status(row) == 'available' and results._reference_matches(row) else None
    except (HtmlUnavailable, ValueError, OSError, sqlite3.Error):
        return None


def _signature(bot, owner, chat, payload):
    key = getattr(bot, 'token', None)
    if not isinstance(key, str) or not key:
        raise ValueError('Signing key unavailable')
    scope = f'tcd-file-v2:{owner}:{chat}:'.encode() + payload
    return hmac.new(key.encode(), scope, hashlib.sha256).digest()[:12]


def keyboard(context, owner, chat, ready, original, *, history_id=None):
    if owner is None or not ready or not re.fullmatch('[0-9a-f]{32}', str(ready.get('id', ''))):
        return stack_actions(original)
    try:
        payload = bytes.fromhex(ready['id']) + int(ready['vod_id']).to_bytes(9, 'big') + secrets.token_bytes(1)
        encoded = base64.urlsafe_b64encode(payload + _signature(context.bot, owner, chat, payload)).decode().rstrip('=')
    except (ValueError, OverflowError, TypeError):
        return stack_actions(original)
    action = PREFIX + '2' + encoded
    assert len(action.encode()) <= 64
    first, vod, extra = [], [], []
    for row in original.inline_keyboard if original else ():
        rest = []
        for button in row:
            if button.text == CHAT_GENERIC:
                first.append(button)
            elif button.text == VOD_LINK:
                vod.append(button)
            else:
                rest.append(button)
        if rest:
            extra.append(rest)
    first.append(InlineKeyboardButton(DOWNLOAD_HTML, callback_data=action))
    return stack_actions(InlineKeyboardMarkup([first] + ([vod] if vod else []) + extra))


def _selection(update, context):
    q = update.callback_query
    message, data = q.message, q.data or ''
    if message is None or not _CALLBACK.fullmatch(data):
        return None
    # Telegram supplies the bot-authored source message. Its own attached button
    # binds this action to that message ID, including after application restart.
    sender = getattr(message, 'from_user', None)
    if sender is None or sender.id != getattr(context.bot, 'id', None):
        return None
    markup = getattr(message, 'reply_markup', None)
    if not any(b.callback_data == data for row in (markup.inline_keyboard if markup else ()) for b in row):
        return None
    try:
        encoded = data[len(PREFIX)+1:]
        raw = base64.urlsafe_b64decode(encoded + '=')
        if len(raw) != 38 or base64.urlsafe_b64encode(raw).decode().rstrip('=') != encoded:
            return None
        payload, tag = raw[:26], raw[26:]
        if not hmac.compare_digest(tag, _signature(context.bot, update.effective_user.id, message.chat_id, payload)):
            return None
        vod = str(int.from_bytes(payload[16:25], 'big'))
        from src.database.db_results import normalize_vod_id
        return normalize_vod_id(vod), payload[:16].hex()
    except (ValueError, TypeError):
        return None


async def file_callback(update, context):
    q = update.callback_query
    reply = CallbackReply(q)
    selected = _selection(update, context)
    if selected is None:
        await reply.send(STALE_ACTION_NOTICE)
        return
    if not await reply.send():
        return
    service = context.application.bot_data.get(SENDER_KEY)
    if service is None:
        return
    vod, generation = selected
    outcome = await service.send(update.effective_user.id, q.message.chat_id, vod, generation_id=generation)
    notice = NOTICES.get(outcome.status)
    if notice and context.application.running and not service.closing:
        try:
            async with asyncio.timeout(5):
                await context.bot.send_message(chat_id=q.message.chat_id, text=notice)
        except (TelegramError, TimeoutError):
            pass
