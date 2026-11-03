"""Bounded image-card delivery; no persistence or external image downloads."""
import asyncio
import re
from contextlib import nullcontext
from dataclasses import dataclass
from telegram import InputMediaPhoto
from .card_images import DEFAULT, LIVE, item_image, telegram_image
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
    source: object | None = None


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


async def create_card(message, context, text):
    """The only new card for an input; never edit the user's message."""
    shown = await render_card(context.bot, message.chat_id, text, None, DEFAULT,
                              reply_to=message, file_context=context)
    return shown


async def send_card(context, chat_id, text, keyboard, item, *, current=lambda: True, on_sent=None,
                    message_id=None, photo=False, ui_lock=None):
    """Shared standalone or in-place result; metadata lookup stays outside UI locks."""
    url = await resolve_preview(context, item) if caption_fits(text) and current() else None
    async with ui_lock or nullcontext():
        shown = await render_card(context.bot, chat_id, text, keyboard, url or item_image(item),
                                  message_id=message_id, photo=photo, current=current, file_context=context)
        if shown is not None and on_sent:
            on_sent(shown.message_id)
    await cleanup_card(context.bot, chat_id, shown)
    return shown


async def render_card(bot, chat_id, text, keyboard, url=None, *, file_context=None, **kwargs):
    current = kwargs.get('current',lambda:True)
    if not current():return None
    record = getattr(file_context,'user_data',{}).get('download_ui') if file_context else None
    owned = record and (record['chat'],record['message']) == (chat_id,kwargs.get('message_id'))
    card = getattr(file_context,'card',None)
    data = card.data if card is not None else {}
    alias = data.get('rejected_image')
    if alias and alias[0] == url:url = alias[1]
    cached = data.get('image_surface')
    if cached and cached[0] == kwargs.get('message_id'):
        kwargs['photo'] = cached[1]
        if url == cached[2]:url = None  # Same image: edit the caption, not its media.
    async with (record.get('render_lock') if owned else None) or nullcontext():
        if not current():return None
        shown = await _render_card(bot,chat_id,text,keyboard,url,**kwargs)
        if owned and shown is not None:record['photo']=shown.photo or shown.media_uncertain
        if shown is not None:
            source = shown.source if shown.source is not None else (cached[2] if cached and shown.photo else None)
            data['image_surface'] = (shown.message_id,shown.photo,source)
            if url and shown.photo and source != url:data['rejected_image'] = (url,source)
        elif url:
            data.pop('image_surface',None)  # Unknown remote edit: never assume its image.
    if file_context and shown is not None and kwargs.get('message_id') is not None:
        from .card_context import bind
        bind(file_context,chat_id,shown.message_id)
    return shown


async def _render_card(bot, chat_id, text, keyboard, url=None, *, message_id=None, photo=False,
                      current=lambda: True, keep_text=False, reply_to=None):
    """One owned message; replacement only after definite refusal or caption overflow."""
    if not current():return None
    fits = caption_fits(text)
    desired = url if fits else None
    if fits and message_id is None and desired is None:desired = DEFAULT
    if desired:
        fallback = LIVE if desired == LIVE else DEFAULT
        candidates = [desired] + ([fallback] if desired != fallback else [])
        for source in candidates:
            if not current():return None
            try:
                upload=telegram_image(source)
            except (OSError,ValueError):
                _diagnostic('card_asset_unavailable')
                continue
            try:
                async with asyncio.timeout(5):
                    if message_id is None:
                        sent = (await reply_to.reply_photo(photo=upload,caption=text,parse_mode='HTML',reply_markup=keyboard)
                                if reply_to else await bot.send_photo(chat_id=chat_id,photo=upload,caption=text,parse_mode='HTML',reply_markup=keyboard))
                        result = CardMessage(sent.message_id,True,message=sent,source=source)
                    else:
                        edited = await bot.edit_message_media(chat_id=chat_id,message_id=message_id,
                            media=InputMediaPhoto(upload,caption=text,parse_mode='HTML'),reply_markup=keyboard)
                        result = CardMessage(message_id,True,message=edited if getattr(edited,'message_id',None)==message_id else None,source=source)
                if not current():
                    if message_id is None:await _delete(bot,chat_id,result.message_id)
                    return None
                return result
            except BadRequest as exc:
                if 'message is not modified' in str(exc).lower():
                    return CardMessage(message_id,True,source=source) if current() else None
                _diagnostic('photo_edit_failed' if message_id else 'photo_send_failed')
            except Forbidden:
                break
            except Exception:
                _diagnostic('card_delivery_uncertain')
                return None  # No duplicate send or competing edit after an ambiguous timeout.
        # A refused new image must not leave another VOD/avatar masquerading as it.
        # Replace with full text only after the bounded safe image fallback failed.
    elif message_id is not None and photo and fits:
        try:
            return await edit_surface(bot,chat_id,message_id,text,keyboard,photo=True,current=current)
        except (BadRequest,Forbidden):pass
        except Exception:
            _diagnostic('card_delivery_uncertain');return None
    if message_id is not None and not photo:
        try:
            result = await edit_surface(bot,chat_id,message_id,text,keyboard,current=current)
            return result
        except (BadRequest,Forbidden):pass
        except Exception:
            _diagnostic('card_delivery_uncertain');return None
    if not current():return None
    try:
        async with asyncio.timeout(5):
            sent = (await reply_to.reply_text(text,parse_mode='HTML',reply_markup=keyboard)
                    if reply_to else await bot.send_message(chat_id=chat_id,text=text,parse_mode='HTML',reply_markup=keyboard))
    except Exception:
        _diagnostic('card_delivery_failed');return None
    result = CardMessage(sent.message_id,False,message_id,sent)
    if not current():
        await _delete(bot,chat_id,result.message_id);return None
    _diagnostic('text_fallback')
    return result
