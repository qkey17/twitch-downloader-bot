# handlers_vod.py
import logging
import asyncio
from src.resource_usage import OperationTelemetry, observe_operation, record_failure, stage
import json
import re
import secrets
import time
from contextlib import nullcontext
from src.twitch_api import fetch_vod_meta, get_client_id
from src.twitch_api.client import TwitchAPIError

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.error import BadRequest
from telegram.ext import ContextTypes

from src.database import database as db
from src.database import db_results as results
from src.workers.worker_io import run_blocking
from src import config
from src.workers.download_worker import download_and_send
from src.config import HTML_PUBLISH_MODE
from src.html_publisher import (
    is_local_html_reference,
    local_published_html_status,
)
from src.ui.ui import CB_FMT_HTML_ONLINE, build_format_keyboard
from .handlers_state import (
    extract_vod_id_strict,
    is_busy,
    input_busy,
    is_globally_busy,
    set_pending,
    begin_download,
    finish_download,
    get_pending,
    clear_pending,
    pending_expired,
    set_cancel,
    is_cancelled,
    CANCELLED_MESSAGE,
    bind_pending_message,
    pending_matches_message,
)
from src.ui.ui_history import build_history_page, ready_offer_text
from src.ui.callbacks import CallbackReply
from src.ui.ui_labels import STALE_ACTION_NOTICE
from src.ui.ui_labels import CHAT_GENERIC, VOD_LINK, CANCEL, ready_chat_label, RECREATE_CHAT, READY_UNAVAILABLE
from src.ui.ui_constants import CB_PENDING_CANCEL


_FORMAT_PROMPT_TEXT = (
    "В современной культуре термин «казуал» иногда используется в пренебрежительном контексте, "
    "однако чаще всего это просто описание стиля игры — легкого и непринужденного, без фанатизма."
)


def _make_item(meta, stats, vod_url, fmt):
    return {
        "channel": meta.get("channel"),
        "title": meta.get("title"),
        "created_at": meta.get("created_at"),
        "length_seconds": meta.get("length_seconds"),
        "messages": stats.get("messages"),
        "unique_users": stats.get("unique_users"),
        "vod_url": vod_url,
        "fmt": fmt,
        "html_url": meta.get("html_url"),
        "captured_during_live": meta.get('captured_during_live') is True,
        **({"thumbnail_preview_url": meta["thumbnail_preview_url"]} if "thumbnail_preview_url" in meta else
           {"thumbnail_url": meta["thumbnail_url"]} if "thumbnail_url" in meta else {}),
    }


def _build_card(item):
    cards, _ = build_history_page([item], page=0, per_page=1)
    if not cards:
        return None, None
    return cards[0]


async def _restore_format_prompt(message):
    try:
        await message.edit_text(
            text=_FORMAT_PROMPT_TEXT,
            reply_markup=build_format_keyboard(),
        )
    except Exception:
        logging.exception("Could not restore format selection after task startup failed")


def _cache_entry_has_usable_output(cached: dict | None, fmt: str) -> bool:
    if not cached:
        return False
    if fmt != "html_online":
        return True

    meta = cached.get("meta") or {}
    html_url = cached.get("html_url") or meta.get("html_url")
    if not html_url:
        return False

    if HTML_PUBLISH_MODE == "local":
        return is_local_html_reference(html_url) and local_published_html_status(html_url) is True

    # Unknown URLs may belong to a separate storage service; only invalidate
    # entries that this local publisher can verify as missing or unreadable.
    return local_published_html_status(html_url) is not False


def _html_button_url(html_ref: str | None) -> str | None:
    if HTML_PUBLISH_MODE != "public" or not html_ref or is_local_html_reference(html_ref):
        return None
    return html_ref


async def _edit_progress_message_with_card(context, chat_id: int, message_id: int, item: dict, html_url: str | None = None,
                                         *, current=lambda: True, ui_lock=None, photo=False, on_photo=None, ready=None, owner_id=None, fresh=False):
    from src.ui.result_preview import resolve_preview, render_card, caption_fits, cleanup_card
    text, kb = _build_card(item)
    if not text:
        return

    html_url = _html_button_url(html_url)
    html_btn = InlineKeyboardButton(CHAT_GENERIC, url=html_url) if html_url else None
    vod_btn = InlineKeyboardButton(VOD_LINK, url=item.get("vod_url"))

    row = []
    if html_btn:
        row.append(html_btn)
    row.append(vod_btn)

    final_kb = InlineKeyboardMarkup([row])
    from src.ui.html_file_action import keyboard
    final_kb = keyboard(context, owner_id, chat_id, ready, final_kb)

    from src.ui.card_images import item_image
    url = await resolve_preview(context,item) if current() and caption_fits(text) else None
    async with ui_lock or nullcontext():
        shown = await render_card(context.bot,chat_id,text,final_kb,url or item_image(item),
            message_id=message_id,photo=photo,current=current,file_context=context)
        if shown is not None and on_photo:on_photo()
    await cleanup_card(context.bot,chat_id,shown)
    return shown is not None


async def _send_card_with_buttons(context, chat_id, item, html_url=None, *, ready=None, owner_id=None):
    from src.ui.result_preview import send_card
    text, kb = _build_card(item)
    if not text:
        return

    html_url = _html_button_url(html_url)
    html_btn = InlineKeyboardButton(CHAT_GENERIC, url=html_url) if html_url else None
    vod_btn = InlineKeyboardButton(VOD_LINK, url=item.get("vod_url"))

    row = []
    if html_btn:
        row.append(html_btn)
    row.append(vod_btn)

    final_kb = InlineKeyboardMarkup([row])
    from src.ui.html_file_action import keyboard
    final_kb = keyboard(context, owner_id, chat_id, ready, final_kb)

    await send_card(context, chat_id, text, final_kb, item)


def ready_generation(vod_id, generation=None):
    current = results.get_ready_result(vod_id)
    if generation is None or current and current.get('id') == generation:
        return current
    from src.ready_html import _row, HtmlUnavailable
    try:
        if not isinstance(generation,str) or not re.fullmatch('[0-9a-f]{32}',generation):
            return None
        row = _row(vod_id,generation)
        if results.file_status(row) != 'available' or not results._reference_matches(row):
            return None
        row['meta'],row['stats'] = results._payload(row)
        return row
    except (HtmlUnavailable,ValueError,TypeError,OSError):
        return None


async def _offer_ready(message,context,result,edit=False, *, owner_id):
    vod_id = result["vod_id"]
    nonce = secrets.token_hex(8)
    context.user_data["vod_result_choice"] = {"vod_id":vod_id,"nonce":nonce,"created_at":time.time(),"generation":result.get("id")}
    from src.ui.ready_open_action import action
    open_action = action(context, owner_id, message.chat_id, result)
    text = ready_offer_text(result)
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(ready_chat_label(result),callback_data=open_action)],
        [InlineKeyboardButton(RECREATE_CHAT,callback_data=f"ui:result:regen:{vod_id}:{nonce}")],
    ])
    if edit:
        await context.bot.edit_message_text(chat_id=message.chat_id,message_id=message.message_id,
                                            text=text,parse_mode="HTML",reply_markup=keyboard)
    else:
        await message.reply_text(text,parse_mode="HTML",reply_markup=keyboard)


async def vod_result_callback(update,context):
    q = update.callback_query
    await q.answer()
    match = re.fullmatch(r"ui:result:(open|regen):([1-9][0-9]{0,19})(?::([0-9a-f]{16}))?",q.data or "")
    if not match:
        await q.message.reply_text("Действие недоступно. Выберите VOD заново.")
        return
    action,vod_id,nonce = match.groups()
    pending = get_pending(context)
    different_pending = pending and not pending_expired(pending) and pending['vod_id'] != vod_id
    if action == 'open':
        ready = results.get_ready_result(vod_id)
        if ready:
            if config.HISTORY_ENABLED and ready['cache_id'] is not None:
                db.add_user_history(update.effective_user.id,ready['cache_id'])
            item = _make_item(ready['meta'],ready['stats'],f"https://www.twitch.tv/videos/{vod_id}","html_online")
            await _send_card_with_buttons(context,q.message.chat_id,item,ready['html_ref'], ready=ready, owner_id=update.effective_user.id)
        else:
            if different_pending:
                await q.message.reply_text('Завершите или отмените выбор формата для другого VOD.')
                return
            if input_busy(context) or context.user_data.get('vod_link_lookup'):
                await q.message.reply_text('Дождитесь завершения предыдущего запроса.')
                return
            set_pending(context,f"https://www.twitch.tv/videos/{vod_id}",vod_id)
            pending = get_pending(context)
            bind_pending_message(context, q.message)
            prompt = await q.message.reply_text("Готовый чат недоступен. Можно создать его заново.",reply_markup=build_format_keyboard())
            bind_pending_message(context, prompt, expected=pending)
        return
    if different_pending:
        await q.message.reply_text('Завершите или отмените выбор формата для другого VOD.')
        return
    if input_busy(context):
        await q.message.reply_text("Чат уже обрабатывается. Дождитесь завершения.")
        return
    if context.user_data.get('vod_link_lookup'):
        await q.message.reply_text("Дождитесь проверки VOD.")
        return
    choice = context.user_data.get('vod_result_choice') or {}
    if (choice.get('vod_id') != vod_id or choice.get('nonce') != nonce or pending_expired(choice)):
        await q.message.reply_text("Действие устарело. Выберите VOD заново.")
        ready = results.get_ready_result(vod_id)
        if ready:
            await _offer_ready(q.message,context,ready,owner_id=update.effective_user.id)
        return
    await _start_download(update,context,vod_id,regenerate=True)


# ------ Entry handler (приём произвольного сообщения с VOD id) ------
async def vod_link_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    text = (update.message.text or "").strip()
    match = re.fullmatch(r"(?i:(?:https?://)?(?:www\.)?twitch\.tv)/(?:videos/|[^/?#\s]+/v/)([0-9]{1,20})/?(?:[?#][^\s]*)?", text)
    candidate = match.group(1) if match else (text if re.fullmatch(r"[0-9]{1,20}",text) else None)
    try:
        vod_id = results.normalize_vod_id(candidate)
    except ValueError:
        return
    await prepare_vod_choice(update.message, context, update.effective_user.id, vod_id)


async def prepare_vod_choice(message, context, user_id, vod_id, *, is_current=None, immediate=False, back=None, on_start=None, ready_only=False):
    """Shared validation; active HTML-only callers opt into immediate processing.

    A guarded entry supplies an owner/session or input validity guard. Such inputs
    preserve an existing format dialog and obey bounded scheduler admission.
    Direct links retain their existing metadata and ready-result behavior.
    """
    from_channel = is_current is not None
    current = is_current or (lambda: True)
    if not current():
        return 'stale'
    vod_id = results.normalize_vod_id(vod_id)
    pending = get_pending(context)
    pending_active = from_channel and pending and not pending_expired(pending)
    if pending_active and pending['vod_id'] != vod_id:
        await message.reply_text('Завершите или отмените выбор формата для другого VOD.')
        return 'pending'
    ready = results.get_ready_result(vod_id)
    if ready:
        clear_pending(context)
        if immediate:
            from .handlers_chat import offer_chat
            await offer_chat(message, context, user_id, ready, current, back, on_start)
            return 'ready'
        if config.HISTORY_ENABLED and ready['cache_id'] is not None:
            db.add_user_history(user_id,ready['cache_id'])
        await _offer_ready(message,context,ready,owner_id=user_id)
        return 'ready'
    if ready_only:
        await message.reply_text(READY_UNAVAILABLE)
        return 'missing'
    if pending_active:
        await message.reply_text('Выберите формат чата в предыдущем сообщении.')
        return 'pending'

    if input_busy(context) or context.user_data.get("vod_link_lookup"):
        await message.reply_text("Дождитесь завершения предыдущего запроса.")
        return 'busy'
    if from_channel and is_globally_busy(context, vod_id):
        await message.reply_text("Сейчас обрабатывается чат VOD. Повторите попытку позже.")
        return 'busy'

    client_id = get_client_id()
    session = context.application.bot_data["aiohttp"]

    # List/info preparation shares the same bounded places as direct input.
    # Direct input already owns a ticket; transfer it once in reserve().
    from src.vod_scheduler import scheduler, INPUT_TICKET
    manager = scheduler(context.application)
    existing = INPUT_TICKET.get()
    ticket = token = None
    if existing is None or existing[0] is not manager or not existing[1]:
        ticket = manager.prepare_input(vod_id)
        if ticket is None:
            await message.reply_text('Очередь недоступна. Повторите запрос позже.')
            return 'busy'
        token = INPUT_TICKET.set(ticket)

    # Both entry handlers are non-blocking in PTB. Serialize this user's
    # preparation without reserving the global download slot.
    lookup = object()
    context.user_data["vod_link_lookup"] = lookup
    try:
        meta = await fetch_vod_meta(session, client_id, vod_id)
        if (context.user_data.get("vod_link_lookup") is not lookup or not current()
                or get_pending(context) is not pending):
            return 'stale'
        if from_channel and is_globally_busy(context, vod_id):
            await message.reply_text("Сейчас обрабатывается чат VOD. Повторите попытку позже.")
            return 'busy'

        # Direct links keep the existing silent behavior for unavailable VODs.
        if not meta or not getattr(meta, "length_seconds", None):
            if from_channel:
                await message.reply_text('VOD не найден или недоступен.')
            return 'missing'

        ready = results.get_ready_result(vod_id)
        if ready:
            clear_pending(context)
            if immediate:
                from .handlers_chat import offer_chat
                await offer_chat(message,context,user_id,ready,current,back,on_start)
            else:
                if config.HISTORY_ENABLED and ready['cache_id'] is not None:
                    db.add_user_history(user_id,ready['cache_id'])
                await _offer_ready(message,context,ready,owner_id=user_id)
            return 'ready'
        if immediate:
            from types import SimpleNamespace
            started = await _start_download(SimpleNamespace(callback_query=SimpleNamespace(message=message),
                                                 effective_user=SimpleNamespace(id=user_id)), context, vod_id, modern=True,
                                            initial_meta=vars(meta))
            return 'started' if started else 'busy'
        # Legacy format chooser retained for old internal callers only.
        prompt = await message.reply_text(
            _FORMAT_PROMPT_TEXT,
            reply_markup=build_format_keyboard()
        )
        if (context.user_data.get('vod_link_lookup') is not lookup or not current()
                or get_pending(context) is not pending):
            return 'stale'
        set_pending(context, vod_url=f'https://www.twitch.tv/videos/{vod_id}', vod_id=vod_id)
        bind_pending_message(context, prompt)
        context.user_data.pop('vod_result_choice', None)
        return 'format'
    except (TwitchAPIError, config.ConfigurationError):
        if not from_channel:
            raise
        if context.user_data.get('vod_link_lookup') is lookup and current():
            await message.reply_text('Не удалось получить данные VOD. Повторите попытку позже.')
            return 'error'
        return 'stale'
    finally:
        if context.user_data.get("vod_link_lookup") is lookup:
            context.user_data.pop("vod_link_lookup", None)
        if ticket is not None:
            manager.release_input(ticket)
            INPUT_TICKET.reset(token)


# ------ Cancel pending download (кнопка отмены в прогрессе) ------
async def pending_cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    reply = CallbackReply(q)

    match = re.fullmatch(re.escape(CB_PENDING_CANCEL) + r':([0-9a-f]{32})', q.data or '')
    jobs = context.user_data.get('vod_jobs', {})
    job = jobs.get(match[1]) if match else None
    if job is None and match:
        job = next((j for j in jobs.values() if j.participants is not None
                    and (p:=j.participants.members.get(update.effective_user.id)) is not None and p.id==match[1]),None)
    if job is not None:
        group = job.participants
        part = group.members.get(update.effective_user.id) if group is not None else None
        target = part.context if part is not None else job.context
        record = target.user_data.get('download_ui') if target is not None else None
        if (not record or not q.message or record['owner'] != update.effective_user.id
                or group is not None and (part is None or not part.active or part.id!=match[1])
                or (record['chat'],record['message']) != (q.message.chat_id,q.message.message_id)):
            await reply.send(STALE_ACTION_NOTICE);return
        if not await reply.send():return
        from src.vod_scheduler import scheduler
        manager = scheduler(context.application)
        if group is not None:
            if job.committing:return
            try:cancel_ready=results.get_ready_result(job.vod)
            except Exception:
                cancel_ready=None
                logging.getLogger(__name__).warning('Cancel restore metadata unavailable')
            part = manager.cancel_participant(job,update.effective_user.id)
            if part is None:return
            record['cancel_ready']=cancel_ready
            await group.detach(part)
            if job.cancelled and job.state=='running':
                part.restore_after_close=True
                return  # Last active cancellation retains its card/slot until safe cleanup.
            await part.ready.wait()
            finish_download(target,job.id)
            try:await _show_cancelled(target,record['chat'],record['message'],record)
            finally:
                manager.release_context(job,target)
                if target.user_data.get('download_ui') is record:target.user_data.pop('download_ui',None)
                if group.members.get(part.owner) is part:group.members.pop(part.owner)
            return
        waiting = job.state == 'waiting'
        if not manager.cancel(job):return
        if waiting:
            delivery = getattr(job,'delivery',None)
            if delivery is not None:await delivery.wait()
            await _show_cancelled(target,record['chat'],record['message'],record)
        return
    if jobs:
        await reply.send(STALE_ACTION_NOTICE);return

    record = context.user_data.get('download_ui')
    operation = context.user_data.get('vod_active_operation')
    if record:
        if (q.data != CB_PENDING_CANCEL + ':' + record['operation'] or not q.message
                or record['owner'] != update.effective_user.id
                or (record['chat'], record['message']) != (q.message.chat_id, q.message.message_id)
                or context.user_data.get('vod_active_operation') != record['operation']):
            await reply.send(STALE_ACTION_NOTICE)
            return
    if is_busy(context):
        if await reply.send():
            if (context.user_data.get('vod_active_operation') == operation
                    and (record is None or context.user_data.get('download_ui') is record)
                    and is_busy(context)):
                set_cancel(context, True)
    else:
        await reply.send(STALE_ACTION_NOTICE)


# ------ Cancel format selection (удалить pending) ------
async def format_cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    reply = CallbackReply(q)

    pending = get_pending(context)
    if (q.message is None or (pending and (pending_expired(pending) or not pending_matches_message(pending, q.message)))
            or (pending is None and context.user_data.get('vod_link_lookup'))):
        await reply.send(STALE_ACTION_NOTICE)
        return
    if not pending:
        await reply.send(STALE_ACTION_NOTICE)
        return
    if not await reply.send():
        return
    if get_pending(context) is not pending or pending_expired(pending):
        return
    clear_pending(context)

    try:
        await q.message.delete()
    except Exception:
        pass


# ------ Format chosen handler (запуск воркера) ------
async def vod_format_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    if input_busy(context):
        await q.message.reply_text("Чат уже обрабатывается. Дождитесь завершения.")
        return

    if context.user_data.get("vod_link_lookup"):
        await q.message.reply_text("Дождитесь проверки VOD.")
        return

    data = q.data or ""
    if data != CB_FMT_HTML_ONLINE:
        return

    pending = get_pending(context)
    if not pending:
        await q.message.reply_text("Сначала выберите VOD.")
        return

    if pending_expired(pending):
        clear_pending(context)
        await q.message.reply_text("Действие устарело. Выберите VOD заново.")
        return
    if not pending_matches_message(pending, q.message):
        await q.message.reply_text('Действие устарело. Выберите VOD заново.')
        return

    fmt = "html_online"
    vod_url = pending["vod_url"]
    vod_id = pending["vod_id"]

    ready = results.get_ready_result(vod_id)
    if ready:
        clear_pending(context)
        if config.HISTORY_ENABLED and ready['cache_id'] is not None:
            db.add_user_history(update.effective_user.id,ready['cache_id'])
        await _offer_ready(q.message,context,ready,edit=True,owner_id=update.effective_user.id)
        return
    await _start_download(update,context,vod_id)


async def _start_download(update,context,vod_id,regenerate=False, *, modern=False, initial_meta=None):
    from src.vod_scheduler import scheduler
    from src.job_context import JobContext
    from src.workers.worker_progress import _build_progress_card, cancel_keyboard, safe_edit_html
    from src.ui.result_preview import render_card, cleanup_card, finish_error_card
    from src.ui.card_images import item_image
    from .handlers_vod_info import information_text
    from src.ui.ui_formatters import _fmt_len
    root_context = context
    expected_pending = get_pending(context)
    owner = update.effective_user.id
    manager = scheduler(context.application)
    key = None if regenerate else (vod_id,'html_online',HTML_PUBLISH_MODE)
    job, refusal = manager.reserve(owner,vod_id,compatibility=key)
    if job is None:
        await update.callback_query.message.reply_text(refusal)
        return False
    joined = job.context is not None
    group = job.participants
    part = group.members[owner] if group is not None else None
    context = JobContext(context,job)
    if not joined:job.context = context
    context.shared_user_data.setdefault('vod_jobs',{})[job.id] = job
    context.user_data['ready_preview_request'] = object()
    q = update.callback_query
    vod_url = f'https://www.twitch.tv/videos/{vod_id}'
    operation_id = begin_download(context)
    context.shared_user_data['vod_active_operation'] = operation_id
    context.shared_user_data['vod_busy'] = True
    if not joined:
        job.telemetry = OperationTelemetry(operation_id)
        job.telemetry.begin()
        job.delivery = asyncio.Event()
    telemetry = job.telemetry
    delivery = part.ready if part is not None else job.delivery
    ui = dict(operation=operation_id,chat=q.message.chat_id,message=q.message.message_id,
              owner=owner,vod_id=vod_id,regenerate=regenerate,
              origin=getattr(q.message,'download_origin',None),request=context.user_data.get('chat_request'),
              direct=bool(getattr(q.message,'direct_vod',False)),meta=dict(initial_meta or {}),
              ui_lock=getattr(q.message,'ui_lock',None) or asyncio.Lock(),render_lock=asyncio.Lock(),
              phase='waiting',photo=bool(getattr(q.message,'photo',False)))
    thumbnail = getattr(q.message,'thumbnail_url',None)
    if thumbnail is not None and not ui['meta'].get('thumbnail_preview_url'):
        ui['meta']['thumbnail_preview_url'] = thumbnail
    context.user_data['download_ui'] = ui
    context.shared_user_data['download_ui'] = ui
    if part is not None:
        ui['cancel_token']=part.id
        group.bind(part,context)
    meta = initial_meta or {}
    if regenerate:context.user_data.pop('vod_result_choice',None)

    async def runner_coro():
        ui['phase']='running'
        if not is_cancelled(context,operation_id):
            await safe_edit_html(context,ui['chat'],ui['message'],_build_progress_card(meta,vod_url,0,0,False))
        if is_cancelled(context,operation_id):
            finish_download(context,operation_id)
            if group is None:await _show_cancelled(context,ui['chat'],ui['message'],ui)
            return
        return await _runner(context=context,chat_id=ui['chat'],progress_message_id=ui['message'],
                             vod_url=vod_url,vod_id=vod_id,fmt='html_online',user_id=owner,
                             operation_id=operation_id,_telemetry=telemetry)

    # A successful initial card from ANY participant may arm the same worker.
    # pump() still refuses to run it until at least one active card is ready.
    if group is not None and not joined:job.factory = runner_coro
    startup_error = None
    try:
        queue_text = information_text((meta.get('channel') or '—')[:120],
                                      (meta.get('title') or 'Без названия')[:240],
                                      _fmt_len(meta.get('length_seconds')),meta.get('created_at'))
        queue_text += '\n🕒 В очереди — обработка начнётся автоматически'
        text = group.latest if group is not None and group.latest is not None else queue_text
        waiting = job.state == 'waiting'
        async with asyncio.timeout(15):
            if not modern:
                await q.message.edit_text(text=text,parse_mode='HTML',reply_markup=cancel_keyboard(context,waiting=waiting))
            ui['last_text']=text
            image=item_image(dict(meta,thumbnail_preview_url=thumbnail) if thumbnail is not None else meta)
            if not job.cancelled and (part is None or part.active):
                shown=await render_card(context.bot,ui['chat'],text,cancel_keyboard(context,waiting=waiting),image,
                                        message_id=ui['message'],photo=ui['photo'],file_context=context)
                if shown is None:raise RuntimeError('queue_media_delivery_unknown')
                ui.update(photo=shown.photo,message=shown.message_id)
                on_bind=getattr(q.message,'on_bind',None)
                if on_bind:on_bind(shown)
                await cleanup_card(context.bot,ui['chat'],shown)
    except BaseException as exc:
        startup_error=exc
        if part is not None:
            part.active=False
            await group.detach(part)
            # A failed UI must not cancel a worker owned by other participants.
            if not any(p.active for p in group.members.values()) and not job.committing:
                if job.state=='waiting':manager.drop(job,'error')
                else:manager.cancel(job)
        else:
            telemetry.fail(exc)
            manager.drop(job,'error')
        try:
            await _restore_start_prompt(q.message,context,vod_id,regenerate,modern=modern,owner_id=owner)
        finally:
            manager.release_context(job,context)
            if context.user_data.get('download_ui') is ui:context.user_data.pop('download_ui',None)
    finally:
        delivery.set()
        if not joined:job.delivery.set()
    if not job.cancelled and manager.jobs.get(job.id) is job and job.state=='waiting':
        try:
            manager.arm(job,job.factory if group is not None else runner_coro)
        except BaseException as exc:
            telemetry.fail(exc)
            try:
                if not modern and not ui.get('photo'):
                    await _restore_start_prompt(q.message,context,vod_id,regenerate,modern=False,owner_id=owner)
                    await manager._notify_start_failure(job,exclude=context)
                else:await manager._notify_start_failure(job)
            finally:
                manager.drop(job,'error')
            raise
    if startup_error is not None:
        if group is not None and group.members.get(owner) is part:
            group.members.pop(owner)
        raise startup_error
    if not job.cancelled and (part is None or part.active):
        on_download=getattr(q.message,'on_download',None)
        if on_download:
            try:on_download()
            except Exception:
                ui['origin']=None
                logging.getLogger(__name__).warning('Download navigation context unavailable')
    if get_pending(root_context) is expected_pending:clear_pending(root_context)
    return True


async def _restore_start_prompt(message,context,vod_id,regenerate, *, modern=False, owner_id):
    if modern:
        await message.edit_text('Не удалось начать обработку. Выберите VOD заново.')
        return
    if regenerate:
        try:
            ready = results.get_ready_result(vod_id)
            if ready:
                await _offer_ready(message,context,ready,edit=True,owner_id=owner_id)
                return
        except Exception:
            logging.getLogger(__name__).warning("Could not restore ready-result actions")
        set_pending(context,f"https://www.twitch.tv/videos/{vod_id}",vod_id)
        bind_pending_message(context, message)
    await _restore_format_prompt(message)


async def _shared_error(group, text):
    from src.ui.result_preview import finish_error_card
    async def deliver(part):
        ui=part.context.user_data.get('download_ui')
        if ui:await finish_error_card(part.context,ui['chat'],ui['message'],text,ui)
    await group.finish(deliver)


@observe_operation
async def _runner(
    context,
    chat_id: int,
    progress_message_id: int,
    vod_url: str,
    vod_id: str,
    fmt: str,
    user_id: int,
    operation_id: str | None = None,
):
    publication = None
    cancelled = False
    job = context.user_data.get('_vod_job')
    group = job.participants if job is not None else None
    ui = getattr(context, 'user_data', {}).get('download_ui')
    if ui and ui['operation'] != operation_id:
        ui = None
    try:
        publication = results.Publication(vod_id,fmt,mode=HTML_PUBLISH_MODE)
        with stage('result_reserve'):
            await run_blocking(publication.reserve)
        meta, stats, files, html_result_ref = await download_and_send(
            context=context,
            chat_id=chat_id,
            progress_message_id=progress_message_id,
            vod_url=vod_url,
            vod_id=vod_id,
            fmt=fmt,
            operation_id=operation_id,
            publication=publication,
        )

        if fmt == "html_online" and html_result_ref:
            meta["html_url"] = html_result_ref

        publication.meta,publication.stats = meta,stats
        if html_result_ref != publication.ref:
            raise ValueError("Unexpected HTML generation reference")
        with stage('result_commit'):
            from .handlers_state import raise_if_cancelled
            raise_if_cancelled(context, operation_id)
            job = context.user_data.get('_vod_job')
            if job is not None:
                # Cancellation linearizes before this fence or is declined.
                job.committing = True
            cache_id = await run_blocking(publication.complete)
            members = group.seal() if group is not None else None
            if config.HISTORY_ENABLED:
                if members is None:
                    db.add_user_history(user_id, cache_id)
                else:
                    for part in members:
                        try:await run_blocking(db.add_user_history,part.owner,cache_id)
                        except Exception:
                            part.history_failed=True
                            logging.getLogger(__name__).error('Participant history write failed')

        item = _make_item(meta, stats, vod_url, fmt)
        html_url = html_result_ref

        if group is not None:
            async def deliver(part):
                pctx=part.context
                record=pctx.user_data.get('download_ui')
                if record is None:return
                if part.history_failed:
                    from src.ui.result_preview import finish_error_card
                    await finish_error_card(pctx,record['chat'],record['message'],
                                            'Не удалось сохранить историю результата. Повторите запрос позже.',record)
                    return
                record['phase']='ready'
                await _edit_progress_message_with_card(pctx,record['chat'],record['message'],item,html_url,
                    ready=dict(id=getattr(publication,'id',None),vod_id=vod_id),owner_id=part.owner,
                    photo=record.get('photo',False))
            await group.finish(deliver)
        else:
            if ui is not None:ui['phase']='ready'
            await _edit_progress_message_with_card(
                context=context,
                chat_id=chat_id,
                message_id=progress_message_id,
                item=item,
                html_url=html_url,
                ready=dict(id=getattr(publication,'id',None), vod_id=vod_id), owner_id=user_id,
                photo=bool(ui and ui.get('photo')),
            )

    except RuntimeError as e:
        job = context.user_data.get('_vod_job')
        if job is not None:
            job.outcome = 'cancelled' if str(e) == CANCELLED_MESSAGE else 'error'
        record_failure(e)
        await _record_generation_failure(publication,e)
        if str(e) == "CHAT_EMPTY":
            from src.ui.result_preview import finish_error_card
            if group is not None:await _shared_error(group,'У этого VOD нет чата.')
            else:await finish_error_card(context,chat_id,progress_message_id,'У этого VOD нет чата.',ui)
            return
        if str(e) == CANCELLED_MESSAGE:
            cancelled = True
        else:
            from src.ui.result_preview import finish_error_card
            if group is not None:await _shared_error(group,'Не удалось обработать чат VOD. Повторите попытку.')
            else:await finish_error_card(context,chat_id,progress_message_id,'Не удалось обработать чат VOD. Повторите попытку.',ui)
            raise

    except Exception as e:
        job = context.user_data.get('_vod_job')
        if job is not None:
            job.outcome = 'error'
        record_failure(e)
        await _record_generation_failure(publication,e)
        logging.exception("Download failed")
        if isinstance(e, TwitchAPIError):
            reason = ("Сервис отклонил запрос." if e.status in (401, 403)
                      else "Не удалось загрузить чат VOD.")
            error_text = f"{reason} Повторите попытку позже."
        elif isinstance(e, TimeoutError):
            error_text = "Время ожидания истекло. Повторите попытку позже."
        else:
            error_text = "Не удалось обработать чат VOD. Повторите попытку."
        from src.ui.result_preview import finish_error_card
        if group is not None:await _shared_error(group,error_text)
        else:await finish_error_card(context,chat_id,progress_message_id,error_text,ui)
    except BaseException as exc:
        await _record_generation_failure(publication,exc)
        raise
    finally:
        completing = ()
        if group is not None:
            # Detached members own their cancellation/failed-admission UI. Do not
            # clear it underneath a concurrent restore when this worker finishes.
            completing = tuple(p for p in group.members.values() if p.active or p.restore_after_close)
            group.stop()
            await group.drain()
        close_ok = False
        try:
            try:
                if publication is not None:
                    await run_blocking(publication.close)
                close_ok = True
            finally:
                released = finish_download(context, operation_id)
                job = context.user_data.get('_vod_job')
                if job is not None:
                    job.processing_done = close_ok
                if group is not None:
                    from .handlers_channel_vods import retire_download_selection
                    for part in completing:
                        if part.context is None:continue
                        pctx=part.context
                        record=pctx.user_data.get('download_ui')
                        finish_download(pctx,operation_id)
                        if record is not None:
                            if close_ok and part.restore_after_close:
                                await part.ready.wait()
                                await _show_cancelled(pctx,record['chat'],record['message'],record)
                            else:retire_download_selection(pctx,record)
                            if pctx.user_data.get('download_ui') is record:pctx.user_data.pop('download_ui',None)
            if group is None and not cancelled and ui is not None:
                from .handlers_channel_vods import retire_download_selection
                retire_download_selection(context, ui)
            if group is None and cancelled and released:
                await _show_cancelled(context, chat_id, progress_message_id, ui)
        finally:
            if group is None and ui is not None and context.user_data.get('download_ui') is ui:
                context.user_data.pop('download_ui', None)


async def _show_cancelled(context, chat_id, message_id, ui):
    from .handlers_vod_info import _diagnostic
    from src.ui.result_preview import render_card, cleanup_card
    def current():
        return ui is None or (context.user_data.get('download_ui') is ui
                and not is_busy(context))
    if not current():
        return
    try:
        if ui:
            ui['phase']='cancelled'
            from .handlers_chat import restore_cancelled_card
            if await restore_cancelled_card(context,ui):return
        shown = await render_card(context.bot, chat_id, 'Обработка чата отменена пользователем', None,
                                  message_id=message_id, current=current, file_context=context)
        await cleanup_card(context.bot, chat_id, shown)
    except Exception:
        _diagnostic('cancel_restore_failed')


async def _record_generation_failure(publication,error):
    if publication is not None:
        try:
            await run_blocking(publication.fail,error)
        except Exception as exc:
            logging.getLogger(__name__).error("Could not record generation failure: %s",type(exc).__name__)


__all__ = [
    "vod_link_entry",
    "vod_format_chosen",
    "pending_cancel_callback",
    "format_cancel_callback",
]
