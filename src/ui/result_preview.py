"""Optional bounded preview delivery; no persistence or image downloads."""
import asyncio
import re
from contextlib import nullcontext
from dataclasses import dataclass
from telegram import InputMediaPhoto
from telegram.error import BadRequest, Forbidden
from src.handlers.handlers_vod_info import preview_url, _placeholder, _diagnostic
from src.twitch_api.videos import fetch_video_thumbnail


async def resolve_preview(context, item):
    value = item.get('thumbnail_preview_url', item.get('thumbnail_url'))
    if 'thumbnail_preview_url' not in item and 'thumbnail_url' not in item:
        vod_id = str(item.get('vod_id') or '')
        if not vod_id:
            match = re.fullmatch(r'https://www\.twitch\.tv/videos/([1-9][0-9]{0,19})', item.get('vod_url') or '')
            vod_id = match[1] if match else ''
        http = getattr(getattr(context, 'application', None), 'bot_data', {}).get('aiohttp')
        if not vod_id or http is None:
            return None
        try:
            async with asyncio.timeout(3):
                value = await fetch_video_thumbnail(http, vod_id)
        except Exception:
            _diagnostic('metadata_failed')
            return None
    url = preview_url(value)
    if not url:
        _diagnostic('url_missing' if not value else 'placeholder' if _placeholder(value) else 'url_rejected')
    return url


def caption_fits(text):
    # Conservative: count markup too, in UTF-16 units. Keep the full text when
    # it cannot safely fit; never truncate titles, statistics or local paths.
    return len(text.encode('utf-16-le')) // 2 <= 1024


@dataclass(frozen=True)
class CardMessage:
    message_id: int
    photo: bool
    retired_id: int | None = None
    message: object | None = None  # A replacement owner may need Message methods.
    media_uncertain: bool = False


async def _delete(bot, chat_id, message_id):
    try:
        async with asyncio.timeout(5):
            await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception:
        _diagnostic('card_cleanup_failed')
        try:
            async with asyncio.timeout(5):
                await bot.edit_message_reply_markup(chat_id=chat_id, message_id=message_id, reply_markup=None)
        except Exception:
            pass


async def cleanup_card(bot, chat_id, shown):
    if shown is not None and shown.retired_id is not None:
        await _delete(bot, chat_id, shown.retired_id)


async def edit_surface(bot, chat_id, message_id, text, keyboard, *, photo=False, current=lambda:True, parse_mode='HTML'):
    """Edit the owned surface in place; one definite type rejection may switch API."""
    if not current():return None
    async def edit(as_photo):
        async with asyncio.timeout(5):
            formatting = {'parse_mode':parse_mode} if parse_mode else {}
            if as_photo:
                await bot.edit_message_caption(chat_id=chat_id,message_id=message_id,
                                               caption=text,reply_markup=keyboard,**formatting)
            else:
                await bot.edit_message_text(chat_id=chat_id,message_id=message_id,
                                            text=text,reply_markup=keyboard,**formatting)
    try:
        try:await edit(photo)
        except BadRequest as exc:
            reason=str(exc).lower()
            if not ('no caption' in reason if photo else 'no text' in reason) or not current():raise
            photo=not photo
            await edit(photo)
    except BadRequest as exc:
        if 'message is not modified' not in str(exc).lower():raise
    return CardMessage(message_id,photo) if current() else None


async def finish_error_card(context, chat_id, message_id, text, record=None):
    """Bounded terminal edit; never duplicate an ambiguously delivered error."""
    def current():
        data = getattr(context, 'user_data', {})
        job = data.get('_vod_job')
        return (data.get('download_ui') is record
                and (record is None or ((record.get('chat'), record.get('message')) == (chat_id,message_id)
                     and (job is None or record.get('operation') == job.id))))
    if not current():
        return False
    if record is not None:record['phase']='error'
    photo = bool(record and record.get('photo'))
    definitive = False
    try:
        async with (record.get('render_lock') if record else None) or nullcontext():
            shown = await edit_surface(context.bot,chat_id,message_id,text,None,photo=photo,current=current,parse_mode=None)
            if record is not None and shown is not None:record['photo']=shown.photo
        return shown is not None
    except BadRequest as exc:
        if 'message is not modified' in str(exc).lower():
            return True
        definitive = True
    except Forbidden:
        definitive = True
    except Exception:
        pass  # Includes delivery timeouts: a new message could duplicate delivery.
    if not current():
        return False
    try:
        async with asyncio.timeout(5):
            await context.bot.edit_message_reply_markup(chat_id=chat_id,message_id=message_id,reply_markup=None)
    except Exception:
        _diagnostic('terminal_keyboard_cleanup_failed')
    if definitive and current():
        try:
            async with asyncio.timeout(5):
                await context.bot.send_message(chat_id=chat_id,text=text)
            return True
        except Exception:
            pass  # Never retry an uncertain fallback send.
    _diagnostic('terminal_card_delivery_failed')
    return False


async def send_card(context, chat_id, text, keyboard, item, *, current=lambda: True, on_sent=None):
    """Standalone card; notify its owner before waiting for optional metadata."""
    shown = await render_card(context.bot, chat_id, text, keyboard, current=current, file_context=context)
    if shown is None:
        return
    if on_sent:
        on_sent(shown.message_id)
    if not caption_fits(text):
        return
    url = await resolve_preview(context, item)
    if url:
        updated = await render_card(context.bot, chat_id, text, keyboard, url,
                                    message_id=shown.message_id, current=current, keep_text=True, file_context=context)
        if updated is not None and current():
            if updated.message_id != shown.message_id and on_sent:
                on_sent(updated.message_id)
            await cleanup_card(context.bot, chat_id, updated)


async def render_card(bot, chat_id, text, keyboard, url=None, *, file_context=None, **kwargs):
    current = kwargs.get('current',lambda:True)
    if not current():return None
    record = getattr(file_context,'user_data',{}).get('download_ui') if file_context else None
    owned = record and (record['chat'],record['message']) == (chat_id,kwargs.get('message_id'))
    async with (record.get('render_lock') if owned else None) or nullcontext():
        if not current():return None
        shown = await _render_card(bot,chat_id,text,keyboard,url,**kwargs)
        if owned and shown is not None:record['photo']=shown.photo or shown.media_uncertain
    if file_context and shown is not None and kwargs.get('message_id') is not None:
        from .card_context import bind
        bind(file_context,chat_id,shown.message_id)
    return shown


async def _render_card(bot, chat_id, text, keyboard, url=None, *, message_id=None, photo=False,
                      current=lambda: True, keep_text=False):
    """Caller serializes Telegram application; metadata must be fetched outside.

    Replacing photo with text sends first; caller binds the new ID, unlocks,
    then calls cleanup_card. Failed deletion leaves old callbacks inert.
    keep_text means this exact text/keyboard was already rendered by this
    request: optional media failure must not create another text card.
    """
    if not current():
        return None
    if not caption_fits(text):
        url = None
    media_uncertain = False
    if url:
        try:
            async with asyncio.timeout(5):
                if message_id is None:
                    sent = await bot.send_photo(chat_id=chat_id, photo=url, caption=text,
                                                parse_mode='HTML', reply_markup=keyboard)
                    result = CardMessage(sent.message_id, True)
                else:
                    await bot.edit_message_media(chat_id=chat_id, message_id=message_id,
                                                 media=InputMediaPhoto(url, caption=text, parse_mode='HTML'),
                                                 reply_markup=keyboard)
                    result = CardMessage(message_id, True)
            if not current():
                if message_id is None:
                    await _delete(bot, chat_id, result.message_id)
                return None
            return result
        except BadRequest as exc:
            if 'message is not modified' in str(exc).lower():
                return CardMessage(message_id, True) if current() else None
            _diagnostic('photo_edit_failed' if message_id is not None else 'photo_send_failed')
        except Exception:
            media_uncertain = True  # A network timeout may follow a remote edit.
            _diagnostic('photo_edit_failed' if message_id is not None else 'photo_send_failed')
            if message_id is None:
                return None  # An uncertain send must never be followed by a duplicate.
        if not current():
            return None
    if photo and message_id is not None and caption_fits(text):
        try:
            return await edit_surface(bot,chat_id,message_id,text,keyboard,photo=True,current=current)
        except (BadRequest,Forbidden):
            pass  # Definite refusal: retain the established bounded replacement.
        except Exception:
            _diagnostic('card_delivery_uncertain')
            return None  # Never send a duplicate after an ambiguous edit.
    if keep_text and message_id is not None and not photo:
        _diagnostic('text_fallback')
        return CardMessage(message_id, False, media_uncertain=media_uncertain) if current() else None
    if message_id is not None and not photo:
        try:
            async with asyncio.timeout(5):
                edited = await bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=text,
                                            parse_mode='HTML', reply_markup=keyboard)
            _diagnostic('text_fallback')
            message = edited if getattr(edited, 'message_id', None) == message_id else None
            return CardMessage(message_id, False, message=message) if current() else None
        except BadRequest as exc:
            if 'message is not modified' in str(exc).lower():
                return CardMessage(message_id, False) if current() else None
        except Exception:
            _diagnostic('card_delivery_uncertain')
            return None
    if not current():
        return None
    try:
        async with asyncio.timeout(5):
            sent = await bot.send_message(chat_id=chat_id, text=text, parse_mode='HTML', reply_markup=keyboard)
    except Exception:
        _diagnostic('card_delivery_failed')
        return None
    result = CardMessage(sent.message_id, False, message_id, sent)
    if not current():
        await _delete(bot, chat_id, result.message_id)
        return None
    _diagnostic('text_fallback')
    return result
