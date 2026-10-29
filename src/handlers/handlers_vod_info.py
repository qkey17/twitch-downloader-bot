"""One on-demand temporary info message per bounded browser session."""
import asyncio
import html
import re
import secrets
from urllib.parse import urlsplit
from dataclasses import dataclass
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto
from telegram.error import TelegramError, BadRequest
from src.vod_list import callback
from src.channel_browser import VodSelection
from src.ui.ui_channel_vods import _duration
from src.ui.ui_formatters import _fmt_dt_utc
from .handlers_state import is_busy


TTL = 300
_ACTION = re.compile(r'ui:vi:([0-9a-f]{16}):([0-9a-f]{16}):(process|back)')


@dataclass
class InfoCard:
    message: object
    photo: bool
    nonce: str
    revision: int
    index: int
    expires: float


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
            or not re.fullmatch(r'/[A-Za-z0-9_./-]+\.(?:jpg|png)', url.path)):
        return None
    return value


async def close_info(session):
    card = getattr(session, 'info_card', None)
    if card is None:
        return True
    try:
        async with asyncio.timeout(5):
            await card.message.delete()
    except Exception:
        return False
    session.info_card = None
    return True


async def show_info(browser, session, source, request):
    # The caller owns the session UI lock for the complete Telegram transaction.
    old = getattr(session, 'info_card', None)
    if old and old.revision == request.view.revision and old.index == request.index and old.expires > browser.clock():
        try:
            async with asyncio.timeout(5):
                await old.message.edit_reply_markup(reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton('Обработать чат', callback_data=f'ui:vi:{session.token}:{old.nonce}:process')],
                    [InlineKeyboardButton('Вернуться к списку', callback_data=f'ui:vi:{session.token}:{old.nonce}:back')]]))
        except BadRequest as exc:
            if 'message to edit not found' not in str(exc).lower():
                return
            session.info_card = old = None
        except (TelegramError, TimeoutError):
            return
        else:
            return
    vod = request.view.items[request.index]
    text = (f'🟣 {html.escape(request.view.channel.display_name[:80])}\n'
            f'🎬 {html.escape(vod.title or "Без названия")}\n'
            f'⏱ {_duration(vod.duration)}\n🗓 {_fmt_dt_utc(vod.created_at)}')
    nonce = secrets.token_hex(8)
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton('Обработать чат', callback_data=f'ui:vi:{session.token}:{nonce}:process')],
        [InlineKeyboardButton('Вернуться к списку', callback_data=f'ui:vi:{session.token}:{nonce}:back')]])
    url = preview_url(vod.thumbnail_url)
    message = None
    photo = bool(url)
    try:
        async with asyncio.timeout(10):
            if old and (url or not old.photo):
                try:
                    if url:
                        await old.message.edit_media(InputMediaPhoto(url, caption=text, parse_mode='HTML'), reply_markup=keyboard)
                    else:
                        await old.message.edit_text(text, parse_mode='HTML', reply_markup=keyboard)
                    message = old.message
                except TelegramError:
                    if not await close_info(session):
                        return
            elif old and not await close_info(session):
                return  # Cannot safely replace a photo with text in place.
            if message is None:
                if url:
                    try:
                        message = await source.reply_photo(url, caption=text, parse_mode='HTML', reply_markup=keyboard)
                    except TelegramError:
                        photo = False
                if message is None:
                    message = await source.reply_text(text, parse_mode='HTML', reply_markup=keyboard)
    except (TelegramError, TimeoutError):
        return
    session.info_card = InfoCard(message, photo, nonce, request.view.revision, request.index,
                                 min(session.expires, browser.clock()+TTL))
    if not browser.is_current(request.view):
        await close_info(session)


async def info_callback(update, context):
    from .handlers_channel_vods import get_channel_browser, _ui_lock, STALE_BUTTON
    from .handlers_chat import prepare_chat
    q = update.callback_query
    match = _ACTION.fullmatch(q.data or '')
    browser = get_channel_browser(context.application)
    browser._prune()
    session = browser._sessions.get(match[1]) if match else None
    if session is None or session.owner_id != update.effective_user.id or q.message is None:
        await q.answer(STALE_BUTTON)
        return
    async with _ui_lock(session):
        card = getattr(session, 'info_card', None)
        if (not browser._live(session) or card is None or card.nonce != match[2]
                or card.revision != session.revision
                or session.status != 'ready' or not getattr(session, 'list_render', None)
                or (q.message.chat_id, q.message.message_id) != (session.chat_id, card.message.message_id)):
            await q.answer(STALE_BUTTON)
            return
        if card.expires <= browser.clock():
            card.nonce = ''
            await q.answer(STALE_BUTTON)
            await close_info(session)
            return
        if match[3] == 'back':
            await q.answer()
            await close_info(session)
            return
        if is_busy(context):
            await q.answer()
            return
        source = session.list_render[0]
        result = await browser.handle_callback(context.application.bot_data['aiohttp'], session.owner_id,
                                               callback(browser._view(session), 'select', card.index),
                                               source.chat_id, source.message_id)
        if not isinstance(result, VodSelection):
            await q.answer(STALE_BUTTON)
            return
        await q.answer()
        session.list_render = None
        # Invalidate action before awaits; deletion failure cannot allow a rerun.
        card.nonce = ''
        await close_info(session)
        progress = await source.reply_text('Проверка VOD…')
    await prepare_chat(progress, context, update.effective_user.id, result.vod_id,
                       is_current=lambda: browser.is_current(result.view), edit=True,
                       on_start=lambda: browser.retire(result.view))
