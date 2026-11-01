"""One bounded, message-bound capability per user for an existing ready HTML."""
import asyncio
import re
import secrets
import sqlite3
import time

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import TelegramError
from src.database import database as db
from src.html_sender import SENDER_KEY, SendStatus
from src.ready_html import HtmlUnavailable
from src.ui.callbacks import CallbackReply
from src.ui.ui_labels import CHAT_GENERIC, VOD_LINK, STALE_ACTION_NOTICE, DOWNLOAD_HTML
from src.ui.result_actions import stack_actions

KEY = 'html_file_action'
PREFIX = 'ui:htmlfile:'
TTL = 900
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


def keyboard(context, owner, chat, ready, original, *, history_id=None):
    """Caller has already selected/authorized this ready row, not a user path."""
    if (owner is None or not ready or not re.fullmatch('[0-9a-f]{32}', str(ready.get('id', '')))):
        return stack_actions(original)
    token = secrets.token_hex(12)
    record = dict(token=token, owner=owner, chat=chat, message=None, vod=ready['vod_id'],
                  generation=ready['id'], history=history_id, created=time.monotonic())
    context.user_data[KEY] = record  # Revoke previous card; never accumulate tokens.
    first, vod, extra = [], [], []
    for row in original.inline_keyboard if original else ():
        rest=[]
        for button in row:
            if button.text == CHAT_GENERIC: first.append(button)
            elif button.text == VOD_LINK: vod.append(button)
            else: rest.append(button)
        if rest: extra.append(rest)
    first.append(InlineKeyboardButton(DOWNLOAD_HTML, callback_data=PREFIX+token))
    return stack_actions(InlineKeyboardMarkup([first]+([vod] if vod else [])+extra))


def rendering(context, chat, message, markup):
    record = getattr(context,'user_data',{}).get(KEY)
    if not record:return None
    attached = any(b.callback_data == PREFIX+record['token']
                   for row in (markup.inline_keyboard if markup else ()) for b in row)
    if not attached:
        if (record['chat'],record['message']) == (chat,message):context.user_data.pop(KEY,None)
        return None
    return record


def bind(context, record, shown):
    if record is not None and shown is not None and context.user_data.get(KEY) is record:
        record['message']=shown.message_id
        # Identity/request fences survive media transitions but not navigation.
        record['fence']=tuple((k,context.user_data.get(k)) for k in
                              ('chat_request','ready_preview_request','history_view','history_file_card'))
        state=context.user_data.get('history_view')
        record['history_request']=state.get('request') if state else None


async def file_callback(update, context):
    q=update.callback_query;reply=CallbackReply(q);record=context.user_data.get(KEY)
    def valid():
        if (not record or context.user_data.get(KEY) is not record or q.message is None
                or q.data != PREFIX+record['token'] or time.monotonic()-record['created'] > TTL
                or update.effective_user.id != record['owner']
                or (q.message.chat_id,q.message.message_id)!=(record['chat'],record['message'])
                or 'fence' not in record):return False
        if any(context.user_data.get(k) is not v for k,v in record['fence']):return False
        state=context.user_data.get('history_view')
        if (state.get('request') if state else None) is not record['history_request']:return False
        if record['history'] is not None:
            hist=db.get_history_entry_for_user(record['owner'],record['history'])
            if not hist or hist['vod_id']!=record['vod'] or hist['fmt']!='html_online':return False
        return True
    if not valid():
        await reply.send(STALE_ACTION_NOTICE);return
    if not await reply.send() or not valid():return
    service=context.application.bot_data.get(SENDER_KEY)
    if service is None:return
    outcome=await service.send(record['owner'],record['chat'],record['vod'],generation_id=record['generation'])
    notice=NOTICES.get(outcome.status)
    if notice and context.application.running and not service.closing:
        try:
            async with asyncio.timeout(5):await context.bot.send_message(chat_id=record['chat'],text=notice)
        except TelegramError:pass
        except TimeoutError:pass
