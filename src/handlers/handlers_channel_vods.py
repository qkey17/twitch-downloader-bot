"""Channel browsing entry and callbacks, independent of chat downloads."""
import asyncio
from dataclasses import replace
from telegram.error import BadRequest
from telegram.ext import CallbackQueryHandler

from src.channel_browser import CALLBACK_PREFIX, ChannelBrowser, VodSelection
from src.vod_list import VodListBrowser, ListView, VodInformation, PREFIX, build_list, callback, _CALLBACK
from src.ui.callbacks import CallbackReply
from src.ui.ui_labels import STALE_LIST_NOTICE, STALE_ACTION_NOTICE, PROCESSING_NOTICE
from .handlers_chat import prepare_chat
from .handlers_state import is_busy
from src.ui.ui_channel_vods import build_channel_vod_card
from .handlers_vod import prepare_vod_choice, results


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
STALE_BUTTON = STALE_LIST_NOTICE


def _ui_lock(session):
    if not hasattr(session, 'ui_lock'):
        session.ui_lock = asyncio.Lock()
    return session.ui_lock


async def _render_list(message, view, session, *, bot=None, current=lambda: True):
    # Called under this session's UI lock, never use Message.text as a cache.
    if getattr(session, 'photo', False):
        from src.ui.result_preview import render_card
        text, keyboard = build_list(view)
        shown = await render_card(bot, message.chat_id, text, keyboard,
                                  message_id=message.message_id, photo=True, current=current)
        if shown is None:
            return None
        # Transfer ownership before cleanup/unlocking. Old callbacks fail both
        # their revision and message binding; never edit a photo as text.
        session.message_id, session.photo = shown.message_id, False
        session.list_render = (shown.message, text)
        return shown
    await _edit(message, view)
    session.list_render = (message, build_list(view)[0]) if view.status in ('ready', 'empty') and view.complete else None


def _consume_selection(session):
    # Fence the source AND info actions before callback answers/Telegram awaits.
    from .handlers_vod_info import take_info
    session.list_render = None
    return take_info(session)


async def _prepare_selection(query, message, context, browser, session, result, card, *, ready_only=False, reply=None):
    reply = reply or CallbackReply(query)
    view = result.view
    origin = dict(token=session.token, revision=view.revision, index=view.position)
    def started():
        nonlocal view
        if browser.is_current(view):
            if session.revision == 999999999:
                browser.retire(view)
                return
            session.revision += 1  # Old Back/selection actions remain invalid after slot release.
            view = replace(view, revision=session.revision)
            origin['revision'] = session.revision
    def bind(shown):
        nonlocal view
        if browser.is_current(view):
            session.message_id, session.photo = shown.message_id, shown.photo or shown.media_uncertain
            view = replace(view, message_id=shown.message_id)
    try:
        if not await reply.send():
            return
        await prepare_chat(message, context, result.owner_id, result.vod_id,
                           is_current=lambda: browser.is_current(view), edit=True,
                           back=callback(view, 'back'), on_start=lambda: browser.retire(view),
                           ui_lock=_ui_lock(session), thumbnail_url=result.view.vod.thumbnail_url,
                           ready_only=ready_only, on_bind=bind, download_origin=origin, on_download=started)
    finally:
        # The consumed inline card may already be progress/the ready screen.
        # close_info never edits a detached record or its reused message ID.
        if card is not None:
            from .handlers_vod_info import close_info
            await close_info(session, card)


async def restore_cancelled_selection(context, record):
    """Use only a still-live bounded session; never recreate an expired token."""
    from types import SimpleNamespace
    from .handlers_state import is_busy
    from .handlers_vod_info import show_info, preview_info
    origin = record.get('origin')
    browser = context.application.bot_data.get(_STORE_KEY)
    session = browser._sessions.get(origin['token']) if isinstance(browser, VodListBrowser) and origin else None
    def current():
        return (context.user_data.get('download_ui') is record and not is_busy(context)
                and context.user_data.get('chat_request') is record['request']
                and session is not None and browser._live(session, origin['revision'])
                and session.owner_id == record['owner'] and session.status == 'selected'
                and (session.chat_id, session.message_id) == (record['chat'], record['message']))
    if not current():
        return False
    bot, chat, mid = context.bot, record['chat'], record['message']
    async def edit_text(text, **kwargs):
        return await bot.edit_message_text(chat_id=chat, message_id=mid, text=text, **kwargs)
    async def reply_text(text, **kwargs):
        return await bot.send_message(chat_id=chat, text=text, **kwargs)
    async def reply_photo(photo, **kwargs):
        return await bot.send_photo(chat_id=chat, photo=photo, **kwargs)
    message = SimpleNamespace(chat_id=chat, message_id=mid,
                              edit_text=edit_text, reply_text=reply_text, reply_photo=reply_photo)
    async with _ui_lock(session):
        if not current():
            return False
        session.revision += 1
        view = browser._view(session)
        ready = results.get_ready_result(record['vod_id']) if record['regenerate'] else None
        if ready is None:
            session.status = 'ready'
            view = browser._view(session)
            shown = await show_info(browser, session, message, VodInformation(record['owner'], origin['index'], view), bot=bot)
            card = getattr(session, 'info_card', None)
        else:
            shown = card = None
        result = VodSelection(record['owner'], record['vod_id'], f'https://www.twitch.tv/videos/{record["vod_id"]}',
                              session.channel.login, view)
    if ready is None:
        from src.ui.result_preview import cleanup_card
        await cleanup_card(bot, chat, shown)
        if shown is not None and card is not None:
            await preview_info(browser, session, card, bot)
        return shown is not None
    async def answered():
        pass
    # Reuse all normal ready-only, binding, nonce and preview fencing.
    await _prepare_selection(SimpleNamespace(answer=answered), message, context, browser, session, result, None, ready_only=True)
    return True


def retire_download_selection(context, record):
    origin = record.get('origin') if record else None
    browser = context.application.bot_data.get(_STORE_KEY)
    if not origin or not isinstance(browser, VodListBrowser):
        return
    session = browser._sessions.get(origin['token'])
    if (session and browser._live(session, origin['revision']) and session.owner_id == record['owner']
            and (session.chat_id, session.message_id) == (record['chat'], record['message'])):
        browser.retire(browser._view(session))


async def _expire_message(browser, old):
    record = getattr(old, 'list_render', None)
    from .handlers_vod_info import close_info
    if not browser._live(old):
        await close_info(old)
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
                    await _render_list(message, view, session, bot=getattr(context, 'bot', None),
                                       current=lambda: browser.is_current(view))
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
    reply = CallbackReply(query)
    message = query.message
    # Resolve only the token here. Full grammar/revision/action validation is
    # still performed by the browser, under the same lock as list rendering.
    parts = (query.data or '').split(':')
    browser._prune()
    session = browser._sessions.get(parts[2]) if len(parts) >= 4 else None
    if (session is None or session.owner_id != update.effective_user.id or message is None
            or (session.chat_id, session.message_id) != (message.chat_id, message.message_id)):
        await reply.send(STALE_ACTION_NOTICE if session is not None and session.owner_id != update.effective_user.id else STALE_BUTTON)
        return
    if str(session.revision) != parts[3]:
        await reply.send(STALE_BUTTON)
        return
    match = _CALLBACK.fullmatch(query.data or '')
    if match is None:
        await reply.send(STALE_BUTTON)
        return
    action, number = match[3], match[4]
    view = browser._view(session)
    value = int(number or 0)
    if ((action in ('group', 'page', 'select', 'info')) != (number is not None)
            or (session.status == 'selected' and action != 'back')
            or (action in ('select', 'info') and not view.page*5 <= value < min(view.page*5+5, len(session.vods)))
            or (action == 'page' and value >= view.page_count)
            or (action == 'group' and value >= (view.page_count+11)//12)):
        await reply.send(STALE_BUTTON)
        return
    if is_busy(context):
        await reply.send(PROCESSING_NOTICE)
        return
    if not await reply.send():
        return
    navigation_card = None
    replaced = None
    ready_only = False
    info_preview = None
    try:
        async with _ui_lock(session):
            if not browser._live(session) or str(session.revision) != parts[3]:
                await reply.send(STALE_BUTTON)
                return
            if is_busy(context):
                await reply.send(PROCESSING_NOTICE)
                return
            previous = browser._view(session)
            result = await browser.handle_callback(context.application.bot_data['aiohttp'], update.effective_user.id,
                                                   query.data, message.chat_id, message.message_id)
            if not isinstance(result, (ListView, VodSelection, VodInformation)):
                await reply.send(STALE_BUTTON)
                return
            view = result.view if isinstance(result, (VodSelection, VodInformation)) else result
            if isinstance(result, VodInformation):
                if results.get_ready_result(view.items[result.index].id) is None:
                    from .handlers_vod_info import show_info
                    if not await reply.send():
                        return
                    replaced = await show_info(browser, session, message, result, bot=context.bot)
                    if replaced is not None:
                        info_preview = session.info_card
                        return replace(result, view=browser._view(session))
                    return None
                result = await browser.handle_callback(context.application.bot_data['aiohttp'], result.owner_id,
                    callback(view, 'select', result.index), message.chat_id, message.message_id)
                if not isinstance(result, VodSelection):
                    await reply.send(STALE_BUTTON)
                    return
                view, ready_only = result.view, True
            if isinstance(result, VodSelection):
                card = _consume_selection(session)
            else:
                from .handlers_vod_info import take_info
                if not await reply.send():
                    return
                if browser.is_current(view):
                    try:
                        replaced = await _render_list(message, view, session, bot=getattr(context, 'bot', None),
                                                      current=lambda: browser.is_current(view))
                    except BaseException:
                        if browser.is_current(view):
                            session.revision, session.status = previous.revision, previous.status
                        raise
                    if getattr(session, 'photo', False):
                        # A failed photo->text delivery leaves the old Back usable.
                        session.revision, session.status = previous.revision, previous.status
                        return None
                    navigation_card = take_info(session)
                    context.user_data.pop('chat_choice', None)
                    if replaced is not None:
                        result = browser._view(session)
    finally:
        if replaced is not None:
            from src.ui.result_preview import cleanup_card
            await cleanup_card(context.bot, message.chat_id, replaced)
        # A detached inline info record must not clear the newly rendered list.
        # Physical replacements are cleaned up only by their captured retired ID.
        if navigation_card is not None:
            from .handlers_vod_info import close_info
            await close_info(session, navigation_card)
        if info_preview is not None:
            from .handlers_vod_info import preview_info
            await preview_info(browser, session, info_preview, context.bot)
    if isinstance(result, VodSelection):
        await _prepare_selection(query, message, context, browser, session, result, card, ready_only=ready_only, reply=reply)
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
    reply = CallbackReply(query)
    if query.message is None:
        await reply.send(STALE_ACTION_NOTICE)
        return None
    browser = get_channel_browser(context.application)
    result = await browser.handle_callback(
        context.application.bot_data['aiohttp'], update.effective_user.id, query.data,
        query.message.chat_id, query.message.message_id,
    )
    view = result.view if isinstance(result, (VodSelection, VodInformation)) else result
    if view.status in ('invalid', 'expired', 'stale', 'forbidden'):
        await reply.send(STALE_ACTION_NOTICE)
        return None
    # Legacy one-card adapter, retained for old internal callers.
    prepared = False
    if isinstance(result, VodSelection):
        def selection_is_current():
            return (result.owner_id == update.effective_user.id and browser.is_current(view)
                    and view.vod is not None and result.vod_id == view.vod.id
                    and result.vod_url == f'https://www.twitch.tv/videos/{result.vod_id}')

        if not selection_is_current():
            await reply.send(STALE_ACTION_NOTICE)
            return None
        if not await reply.send():
            return None
        outcome = await prepare_vod_choice(query.message, context, update.effective_user.id,
                                           result.vod_id, is_current=selection_is_current)
        prepared = outcome in ('ready', 'format')
        if not prepared:
            view = replace(view, status='ready')
    if not await reply.send():
        return None
    if view.session_id is None:
        text, _ = build_channel_vod_card(view)
        await query.message.reply_text(text)
    elif browser.is_current(view):
        await _edit(query.message, view)
    return result if prepared else None


def build_channel_vods_handler():
    """Keep metadata/Helix waits outside the sequential PTB update path."""
    return CallbackQueryHandler(channel_vods_callback, pattern='^(?:'+CALLBACK_PREFIX+'|'+PREFIX+')', block=False)
