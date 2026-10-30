"""Optional bounded preview delivery; no persistence or image downloads."""
import asyncio
import re
from dataclasses import dataclass
from telegram import InputMediaPhoto
from telegram.error import BadRequest
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


async def _delete(bot, chat_id, message_id):
    try:
        async with asyncio.timeout(5):
            await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception:
        _diagnostic('card_cleanup_failed')


async def cleanup_card(bot, chat_id, shown):
    if shown is not None and shown.retired_id is not None:
        await _delete(bot, chat_id, shown.retired_id)


async def send_card(context, chat_id, text, keyboard, item, *, current=lambda: True, on_sent=None):
    """Standalone card; notify its owner before waiting for optional metadata."""
    shown = await render_card(context.bot, chat_id, text, keyboard, current=current)
    if shown is None:
        return
    if on_sent:
        on_sent(shown.message_id)
    if not caption_fits(text):
        return
    url = await resolve_preview(context, item)
    if url:
        updated = await render_card(context.bot, chat_id, text, keyboard, url,
                                    message_id=shown.message_id, current=current, keep_text=True)
        if updated is not None and current():
            if updated.message_id != shown.message_id and on_sent:
                on_sent(updated.message_id)
            await cleanup_card(context.bot, chat_id, updated)


async def render_card(bot, chat_id, text, keyboard, url=None, *, message_id=None, photo=False,
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
            _diagnostic('photo_edit_failed' if message_id is not None else 'photo_send_failed')
        if not current():
            return None
    if keep_text and message_id is not None and not photo:
        _diagnostic('text_fallback')
        return CardMessage(message_id, False) if current() else None
    if message_id is not None and not photo:
        try:
            async with asyncio.timeout(5):
                await bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=text,
                                            parse_mode='HTML', reply_markup=keyboard)
            _diagnostic('text_fallback')
            return CardMessage(message_id, False) if current() else None
        except BadRequest as exc:
            if 'message is not modified' in str(exc).lower():
                return CardMessage(message_id, False) if current() else None
        except Exception:
            pass
    if not current():
        return None
    try:
        async with asyncio.timeout(5):
            sent = await bot.send_message(chat_id=chat_id, text=text, parse_mode='HTML', reply_markup=keyboard)
    except Exception:
        _diagnostic('card_delivery_failed')
        return None
    result = CardMessage(sent.message_id, False, message_id)
    if not current():
        await _delete(bot, chat_id, result.message_id)
        return None
    _diagnostic('text_fallback')
    return result
