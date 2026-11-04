"""Channel browsing entry and callbacks, independent of chat downloads."""
from src.ui.card_context import callback_context
import asyncio
from dataclasses import replace
from telegram.error import BadRequest
from telegram.ext import CallbackQueryHandler

from src.channel_browser import CALLBACK_PREFIX, ChannelBrowser, VodSelection
from src.vod_list import VodListBrowser, ListView, VodInformation, PREFIX, build_list, callback, _CALLBACK
from src.ui.callbacks import CallbackReply
from src.ui.ui_labels import STALE_LIST_NOTICE, STALE_ACTION_NOTICE, PROCESSING_NOTICE
from .handlers_chat import prepare_chat
from .handlers_state import input_busy as is_busy
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


async def _render_list(message, view, session, *, bot=None, current=lambda: True, context=None):
    from src.ui.result_preview import render_card
    from src.ui.card_images import DEFAULT
    from src.ui.channel_image import channel_image
    text,keyboard = build_list(view) if isinstance(view,ListView) else build_channel_vod_card(view)
    image = await channel_image(context,session,getattr(view.channel,'profile_image_url',None)) if context else DEFAULT
    shown = await render_card(bot,message.chat_id,text,keyboard,image,message_id=message.message_id,
                              photo=getattr(session,'photo',False),current=current,file_context=context)
    if shown is not None:
        session.message_id,session.photo=shown.message_id,shown.photo
        session.list_render=(shown.message or message,text)
    return shown


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
                           image_live=result.vod_id in getattr(view,'live_vod_ids',()),ready_only=ready_only, on_bind=bind, download_origin=origin, on_download=started,photo=getattr(session,'photo',False))
    finally:
        # The consumed inline card may already be progress/the ready screen.
        # close_info never edits a detached record or its reused message ID.
        if card is not None:
            from .handlers_vod_info import close_info
            await close_info(session, card)


def retire_download_selection(context, record):
    origin = record.get('origin') if record else None
    browser = context.application.bot_data.get(_STORE_KEY)
    if not origin or not isinstance(browser, VodListBrowser):
        return
    session = browser._sessions.get(origin['token'])
    if (session and browser._live(session, origin['revision']) and session.owner_id == record['owner']
            and (session.chat_id, session.message_id) == (record['chat'], record['message'])):
        browser.retire(browser._view(session))


async def _open_and_show(message, context, owner_id, login):
    from src.ui.card_context import bind,for_message
    context=for_message(context,owner_id,message.chat_id,message.message_id)
    # Reusing this exact message consumes its old screen, never another card.
    context.user_data.pop('chat_choice',None)
    context.user_data.pop('chat_request',None)
    bind(context,message.chat_id,message.message_id)
    browser = get_channel_browser(context.application)
    view = await browser.open(context.application.bot_data['aiohttp'],owner_id,login,
                              message.chat_id,message.message_id)
    session = browser._sessions.get(view.session_id)
    if session is not None:
        shown = None
        async with _ui_lock(session):
            if browser.is_current(view):
                shown = await _render_list(message,view,session,bot=getattr(context,'bot',None),context=context,current=lambda:browser.is_current(view))
        from src.ui.result_preview import cleanup_card
        await cleanup_card(context.bot,message.chat_id,shown)
    elif view.session_id is None:
        from src.ui.result_preview import render_card,cleanup_card
        from src.ui.card_images import DEFAULT
        text,keyboard=build_list(view) if isinstance(view,ListView) else build_channel_vod_card(view)
        shown=await render_card(context.bot,message.chat_id,text,keyboard,DEFAULT,
            message_id=message.message_id,photo=bool(getattr(message,'photo',False)),file_context=context)
        await cleanup_card(context.bot,message.chat_id,shown)
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
            or (action in ('select', 'info') and not view.page*5 <= value < min(view.page*5+5, len(view.items)))
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
                if view.items[result.index].id in view.archived_ids or results.get_ready_result(view.items[result.index].id) is None:
                    from .handlers_vod_info import show_info
                    if not await reply.send():
                        return
                    replaced = await show_info(browser, session, message, result, bot=context.bot, context=context)
                    if replaced is not None:
                        return replace(result, view=browser._view(session))
                    return None
                result = await browser.handle_callback(context.application.bot_data['aiohttp'], result.owner_id,
                    callback(view, 'select', result.index), message.chat_id, message.message_id)
                if not isinstance(result, VodSelection):
                    await reply.send(STALE_BUTTON)
                    return
                view, ready_only = result.view, True
            if isinstance(result, VodSelection) and result.vod_id in view.archived_ids:
                # Old/select callbacks from an archive surface never reach Create.
                from .handlers_vod_info import show_info
                session.status = 'ready'
                result = VodInformation(result.owner_id, view.position, browser._view(session))
                replaced = await show_info(browser, session, message, result, bot=context.bot, context=context)
                return result
            if isinstance(result, VodSelection):
                card = _consume_selection(session)
            else:
                from .handlers_vod_info import take_info
                if not await reply.send():
                    return
                if browser.is_current(view):
                    try:
                        replaced = await _render_list(message, view, session, bot=getattr(context, 'bot', None),context=context,
                                                      current=lambda: browser.is_current(view))
                    except BaseException:
                        if browser.is_current(view):
                            session.revision, session.status = previous.revision, previous.status
                        raise
                    if replaced is None:
                        if browser.is_current(view):
                            session.revision,session.status=previous.revision,previous.status
                        return
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

    if isinstance(result, VodSelection):
        await _prepare_selection(query, message, context, browser, session, result, card, ready_only=ready_only, reply=reply)
    return result


async def show_channel_vods(message, context, owner_id, login):
    """Create a placeholder and return a PTB-owned task, without awaiting Twitch.

    Text and command routing may await the returned task without blocking PTB's
    update processing. Browsing never reserves a download slot or reads chat.
    """
    from src.ui.result_preview import create_card
    shown=await create_card(message,context,'Загрузка VODs канала…')
    if shown is None:return
    progress=shown.message
    coroutine = _open_and_show(progress, context, owner_id, login)
    try:
        return context.application.create_task(coroutine)
    except BaseException:
        coroutine.close()
        from src.ui.result_preview import render_card
        await render_card(context.bot,progress.chat_id,'Не удалось открыть список VODs. Повторите попытку.',None,
                          message_id=progress.message_id,photo=shown.photo,file_context=context)
        raise


@callback_context
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
