"""Channel browsing entry and callbacks, independent of chat downloads."""
import asyncio
from dataclasses import replace
from telegram.error import BadRequest
from telegram.ext import CallbackQueryHandler

from src.channel_browser import CALLBACK_PREFIX, ChannelBrowser, VodSelection
from src.vod_list import VodListBrowser, ListView, PREFIX, build_list, callback
from .handlers_chat import prepare_chat
from .handlers_state import is_busy
from src.ui.ui_channel_vods import build_channel_vod_card
from .handlers_vod import prepare_vod_choice


_STORE_KEY = 'channel_vod_browser'


def get_channel_browser(application):
    if _STORE_KEY not in application.bot_data:
        application.bot_data[_STORE_KEY] = VodListBrowser()
    return application.bot_data[_STORE_KEY]


async def _edit(message, view):
    text, keyboard = build_list(view) if isinstance(view, ListView) else build_channel_vod_card(view)
    try:
        await message.edit_text(text, parse_mode='HTML', reply_markup=keyboard)
    except BadRequest as exc:
        if 'message is not modified' not in str(exc).lower():
            raise


STALE_LIST = '⚠️ Список устарел. Отправьте имя канала повторно.'
STALE_BUTTON = '⚠️ Этот список устарел. Запросите канал повторно.'


def _ui_lock(session):
    if not hasattr(session, 'ui_lock'):
        session.ui_lock = asyncio.Lock()
    return session.ui_lock


async def _render_list(message, view, session):
    # Called under this session's UI lock, never use Message.text as a cache.
    await _edit(message, view)
    session.list_render = (message, build_list(view)[0]) if view.status in ('ready', 'empty') and view.complete else None


async def _expire_message(browser, old):
    record = getattr(old, 'list_render', None)
    if browser._live(old) or old.status not in ('ready', 'empty') or record is None:
        return
    old.list_render = None  # At most one attempt, including failure/cancellation.
    message, text = record
    try:
        async with asyncio.timeout(10):
            await message.edit_text(text+'\n'+STALE_LIST, parse_mode='HTML', reply_markup=None)
    except Exception:
        # Deleted/inaccessible messages must never prevent the new request.
        pass


async def _open_and_show(message, context, owner_id, login):
    browser = get_channel_browser(context.application)
    if not isinstance(browser, VodListBrowser):
        view = await browser.open(context.application.bot_data['aiohttp'], owner_id, login,
                                  message.chat_id, message.message_id)
        if view.session_id is None or browser.is_current(view):
            await _edit(message, view)
        return view
    browser._prune()
    old = next((s for s in browser._sessions.values() if s.owner_id == owner_id), None)
    async def open_new():
        return await browser.open(context.application.bot_data['aiohttp'], owner_id, login,
                                  message.chat_id, message.message_id)
    if old is not None:
        async with _ui_lock(old):
            view = await open_new()
    else:
        view = await open_new()
    try:
        session = browser._sessions.get(view.session_id)
        if session is not None:
            async with _ui_lock(session):
                if browser.is_current(view):
                    await _render_list(message, view, session)
        elif view.session_id is None:
            await _edit(message, view)
    finally:
        # Render/unlock the new list first. Old-message cleanup stays bounded
        # and owned by this request, without blocking the new session's UI.
        if old is not None:
            async with _ui_lock(old):
                await _expire_message(browser, old)
    return view


async def _list_callback(update, context, browser):
    query = update.callback_query
    message = query.message
    # Resolve only the token here. Full grammar/revision/action validation is
    # still performed by the browser, under the same lock as list rendering.
    parts = (query.data or '').split(':')
    browser._prune()
    session = browser._sessions.get(parts[2]) if len(parts) >= 4 else None
    if (session is None or session.owner_id != update.effective_user.id or message is None
            or (session.chat_id, session.message_id) != (message.chat_id, message.message_id)):
        await query.answer(STALE_BUTTON)
        return
    async with _ui_lock(session):
        if not browser._live(session) or str(session.revision) != parts[3]:
            await query.answer(STALE_BUTTON)
            return
        if is_busy(context):
            await query.answer()
            return
        result = await browser.handle_callback(context.application.bot_data['aiohttp'], update.effective_user.id,
                                               query.data, message.chat_id, message.message_id)
        if not isinstance(result, (ListView, VodSelection)):
            await query.answer(STALE_BUTTON)
            return
        await query.answer()
        view = result.view if isinstance(result, VodSelection) else result
        if isinstance(result, VodSelection):
            # Fence the message BEFORE metadata or any HTML/progress edit.
            session.list_render = None
        elif browser.is_current(view):
            context.user_data.pop('chat_choice', None)
            await _render_list(message, view, session)
    if isinstance(result, VodSelection):
        await prepare_chat(message, context, update.effective_user.id, result.vod_id,
                           is_current=lambda: browser.is_current(view), edit=True,
                           back=callback(view, 'back'), on_start=lambda: browser.retire(view))
    return result


async def show_channel_vods(message, context, owner_id, login):
    """Create a placeholder and return a PTB-owned task, without awaiting Twitch.

    Text and command routing may await the returned task without blocking PTB's
    update processing. Browsing never reserves a download slot or reads chat.
    """
    progress = await message.reply_text('Загрузка VODs канала…')
    coroutine = _open_and_show(progress, context, owner_id, login)
    try:
        return context.application.create_task(coroutine)
    except BaseException:
        coroutine.close()
        await progress.edit_text('Не удалось открыть список VODs. Повторите попытку.')
        raise


async def channel_vods_callback(update, context):
    """Prepare a selected VOD using the existing confirmation flow, never download.

    Register via build_channel_vods_handler() so network waits do not serialize
    other PTB updates. Session ownership/revisions are checked by the store.
    """
    query = update.callback_query
    if query is None:
        return None
    browser = get_channel_browser(context.application)
    if isinstance(browser, VodListBrowser):
        return await _list_callback(update, context, browser)
    await query.answer()
    if query.message is None:
        return None
    browser = get_channel_browser(context.application)
    result = await browser.handle_callback(
        context.application.bot_data['aiohttp'], update.effective_user.id, query.data,
        query.message.chat_id, query.message.message_id,
    )
    view = result.view if isinstance(result, VodSelection) else result
    # Legacy one-card adapter, retained for old internal callers.
    prepared = False
    if isinstance(result, VodSelection):
        def selection_is_current():
            return (result.owner_id == update.effective_user.id and browser.is_current(view)
                    and view.vod is not None and result.vod_id == view.vod.id
                    and result.vod_url == f'https://www.twitch.tv/videos/{result.vod_id}')

        if not selection_is_current():
            await query.message.reply_text('Кнопка устарела. Используйте текущую карточку VOD.')
            return None
        outcome = await prepare_vod_choice(query.message, context, update.effective_user.id,
                                           result.vod_id, is_current=selection_is_current)
        prepared = outcome in ('ready', 'format')
        if not prepared:
            view = replace(view, status='ready')
    if view.session_id is None:
        text, _ = build_channel_vod_card(view)
        await query.message.reply_text(text)
    elif browser.is_current(view):
        await _edit(query.message, view)
    return result if prepared else None


def build_channel_vods_handler():
    """Keep metadata/Helix waits outside the sequential PTB update path."""
    return CallbackQueryHandler(channel_vods_callback, pattern='^(?:'+CALLBACK_PREFIX+'|'+PREFIX+')', block=False)
