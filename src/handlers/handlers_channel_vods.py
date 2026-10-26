"""Channel browsing entry and callbacks, independent of chat downloads."""
from dataclasses import replace
from telegram.error import BadRequest
from telegram.ext import CallbackQueryHandler

from src.channel_browser import CALLBACK_PREFIX, ChannelBrowser, VodSelection
from src.ui.ui_channel_vods import build_channel_vod_card
from .handlers_vod import prepare_vod_choice


_STORE_KEY = 'channel_vod_browser'


def get_channel_browser(application):
    if _STORE_KEY not in application.bot_data:
        application.bot_data[_STORE_KEY] = ChannelBrowser()
    return application.bot_data[_STORE_KEY]


async def _edit(message, view):
    text, keyboard = build_channel_vod_card(view)
    try:
        await message.edit_text(text, parse_mode='HTML', reply_markup=keyboard)
    except BadRequest as exc:
        if 'message is not modified' not in str(exc).lower():
            raise


async def _open_and_show(message, context, owner_id, login):
    browser = get_channel_browser(context.application)
    view = await browser.open(context.application.bot_data['aiohttp'], owner_id, login,
                              message.chat_id, message.message_id)
    if view.session_id is None or browser.is_current(view):
        await _edit(message, view)
    return view


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
    await query.answer()
    if query.message is None:
        return None
    browser = get_channel_browser(context.application)
    result = await browser.handle_callback(
        context.application.bot_data['aiohttp'], update.effective_user.id, query.data,
        query.message.chat_id, query.message.message_id,
    )
    view = result.view if isinstance(result, VodSelection) else result
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
    return CallbackQueryHandler(channel_vods_callback, pattern='^'+CALLBACK_PREFIX, block=False)
