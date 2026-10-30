"""One active list or information card per bounded browser session."""
import asyncio
import html
import logging
import re
import secrets
from urllib.parse import urlsplit
from dataclasses import dataclass
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from src.vod_list import callback
from src.channel_browser import VodSelection
from src.ui.ui_channel_vods import _duration
from src.ui.ui_formatters import _fmt_dt_utc
from .handlers_state import is_busy, is_globally_busy
from src.ui.callbacks import CallbackReply
from src.ui.ui_labels import STALE_ACTION_NOTICE, PROCESSING_NOTICE
from src.ui.ui_labels import CREATE_CHAT, BACK_TO_LIST


TTL = 300
_ACTION = re.compile(r'ui:vi:([0-9a-f]{16}):([0-9a-f]{16}):(process|back)')


def _diagnostic(category):
    # Fixed internal categories only; never URLs, IDs or exception contents.
    try:
        logging.getLogger(__name__).info('vod_preview category=%s', category)
    except Exception:
        pass


def _placeholder(value):
    if not isinstance(value, str):
        return False
    try:
        url = urlsplit(value)
    except ValueError:
        return False
    return (url.scheme == 'https' and url.netloc == 'vod-secure.twitch.tv'
            and re.fullmatch(r'/_404/404_processing_(?:%\{width\}|\{width\}|[0-9]+)x(?:%\{height\}|\{height\}|[0-9]+)\.png', url.path) is not None
            and not url.query and not url.fragment)


@dataclass
class InfoCard:
    message: object
    photo: bool
    nonce: str
    revision: int
    index: int
    expires: float
    text: str
    keyboard: object
    preview: str | None


def preview_url(value):
    """No app-side fetch. Only the fixed Twitch CDN without redirect parameters."""
    if not isinstance(value, str) or len(value) > 1024:
        return None
    value = value.replace('%{width}', '480').replace('%{height}', '270')
    value = value.replace('{width}', '480').replace('{height}', '270')
    try:
        url = urlsplit(value)
    except ValueError:
        return None
    if (url.scheme != 'https' or url.netloc != 'static-cdn.jtvnw.net' or url.query or url.fragment
            or not url.path.startswith('/cf_vods/') or '..' in url.path.split('/')
            or not re.fullmatch(r'/[A-Za-z0-9_./-]+\.(?:jpe?g|png)', url.path)):
        return None
    return value


def take_info(session):
    """Consume actions synchronously; the selecting request owns cleanup now."""
    card = getattr(session, 'info_card', None)
    if card is not None:
        card.nonce = ''
        session.info_card = None
    return card


async def close_info(session, card=None):
    card = card if card is not None else getattr(session, 'info_card', None)
    # An inline card can already be the new list/progress at this exact ID.
    # Only the still-owned info surface may have its obsolete keyboard cleared.
    if card is None or getattr(session, 'info_card', None) is not card:
        return True
    take_info(session)
    try:
        async with asyncio.timeout(5):
            await card.message.edit_reply_markup(reply_markup=None)
    except Exception:
        return False
    return True


async def show_info(browser, session, source, request, *, bot):
    """Render text/actions under the caller's UI lock; preview is a second step."""
    from src.ui.result_preview import render_card
    previous = browser._view(session)
    if not browser.is_current(request.view):
        return None
    if session.revision == 999999999:
        browser.retire(request.view)
        return None
    session.revision += 1  # Dates/Back from the source screen are now obsolete.
    view = browser._view(session)
    vod = view.items[request.index]
    text = (f'🟣 {html.escape(view.channel.display_name[:80])}\n'
            f'🎬 {html.escape(vod.title or "Без названия")}\n'
            f'⏱ {_duration(vod.duration)}\n🗓 {_fmt_dt_utc(vod.created_at)}')
    nonce = secrets.token_hex(8)
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(CREATE_CHAT, callback_data=f'ui:vi:{session.token}:{nonce}:process')],
        [InlineKeyboardButton(BACK_TO_LIST, callback_data=f'ui:vi:{session.token}:{nonce}:back')]])
    try:
        shown = await render_card(bot, source.chat_id, text, keyboard,
                                  message_id=session.message_id, photo=getattr(session, 'photo', False),
                                  current=lambda: browser.is_current(view))
    except BaseException:
        if browser.is_current(view):
            session.revision = previous.revision
        raise
    if shown is None:
        if browser.is_current(view):
            session.revision = previous.revision
        return None
    take_info(session)
    session.message_id, session.photo = shown.message_id, shown.photo
    session.list_render = None
    session.info_card = InfoCard(shown.message or source, shown.photo, nonce, view.revision, request.index,
                                 min(session.expires, browser.clock()+TTL), text, keyboard, vod.thumbnail_url)
    return shown


async def preview_info(browser, session, card, bot):
    """Optional media never creates another card or borrows a newer binding."""
    from .handlers_channel_vods import _ui_lock
    from src.ui.result_preview import render_card, caption_fits
    def current():
        return (browser._live(session) and session.info_card is card and bool(card.nonce)
                and session.revision == card.revision and card.expires > browser.clock()
                and session.message_id == card.message.message_id)
    if not current() or not caption_fits(card.text):
        return
    url = preview_url(card.preview)
    if not url:
        _diagnostic('url_missing' if not card.preview else
                    'placeholder' if _placeholder(card.preview) else 'url_rejected')
        return
    async with _ui_lock(session):
        if not current():
            return
        # Cancellation/timeouts can follow a successful remote edit.
        session.photo = card.photo = True
        shown = await render_card(bot, session.chat_id, card.text, card.keyboard, url,
                                  message_id=session.message_id, current=current, keep_text=True)
        if shown is not None and current():
            session.photo = card.photo = shown.photo or shown.media_uncertain


async def info_callback(update, context):
    from .handlers_channel_vods import (get_channel_browser, _ui_lock,
                                        _consume_selection, _prepare_selection)
    q = update.callback_query
    reply = CallbackReply(q)
    match = _ACTION.fullmatch(q.data or '')
    browser = get_channel_browser(context.application)
    browser._prune()
    session = browser._sessions.get(match[1]) if match else None
    if session is None or session.owner_id != update.effective_user.id or q.message is None:
        await reply.send(STALE_ACTION_NOTICE)
        return
    def valid_card():
        card = getattr(session, 'info_card', None)
        return (browser._live(session) and card is not None and card.nonce == match[2]
                and card.revision == session.revision and card.expires > browser.clock()
                and session.status == 'ready'
                and (q.message.chat_id, q.message.message_id) == (session.chat_id, session.message_id)
                and session.message_id == card.message.message_id)
    if not valid_card():
        await reply.send(STALE_ACTION_NOTICE)
        return
    if match[3] == 'process' and (is_busy(context) or is_globally_busy(context)):
        await reply.send(PROCESSING_NOTICE)
        return
    if not await reply.send():
        return
    from .handlers_channel_vods import _render_list
    from src.ui.result_preview import render_card, cleanup_card
    replaced = None
    try:
        async with _ui_lock(session):
            if not valid_card():
                return
            card = session.info_card
            if match[3] == 'back':
                previous = browser._view(session)
                view = await browser.handle_callback(context.application.bot_data['aiohttp'], session.owner_id,
                    callback(previous, 'back'), session.chat_id, session.message_id)
                try:
                    replaced = await _render_list(card.message, view, session, bot=context.bot,
                                                  current=lambda: browser.is_current(view))
                except BaseException:
                    if browser.is_current(view):
                        session.revision = previous.revision
                    raise
                if getattr(session, 'photo', False):
                    session.revision = previous.revision
                    return  # Failed replacement: the old Back remains usable.
                take_info(session)
                return
            if is_busy(context) or is_globally_busy(context):
                return
            source = card.message
            if getattr(session, 'photo', False):
                # Send text first, transfer ownership, then retire the photo.
                # The normal starter/offer can now reuse this text message.
                replaced = await render_card(context.bot, session.chat_id, card.text, card.keyboard,
                    message_id=session.message_id, photo=True, current=valid_card)
                if replaced is None:
                    return
                session.message_id, session.photo = replaced.message_id, False
                source = card.message = replaced.message
                card.photo = False
            result = await browser.handle_callback(context.application.bot_data['aiohttp'], session.owner_id,
                callback(browser._view(session), 'select', card.index), session.chat_id, session.message_id)
            if not isinstance(result, VodSelection):
                return
            card = _consume_selection(session)
        await _prepare_selection(q, source, context, browser, session, result, card, reply=reply)
    finally:
        await cleanup_card(context.bot, session.chat_id, replaced)
