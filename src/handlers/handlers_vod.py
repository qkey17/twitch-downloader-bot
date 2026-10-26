# handlers_vod.py
import logging
import json
import re
import secrets
import time
from src.twitch_api import fetch_vod_meta, get_client_id
from src.twitch_api.client import TwitchAPIError

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
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
    is_globally_busy,
    set_pending,
    begin_download,
    finish_download,
    get_pending,
    clear_pending,
    pending_expired,
    set_cancel,
    bind_pending_message,
    pending_matches_message,
)
from src.ui.ui_history import build_history_page
from src.ui.ui_labels import CHAT_GENERIC, VOD_LINK, CANCEL
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


async def _edit_progress_message_with_card(context, chat_id: int, message_id: int, item: dict, html_url: str | None = None):
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

    try:
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            parse_mode="HTML",
            reply_markup=final_kb,
        )
    except Exception:
        await context.bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode="HTML",
            reply_markup=final_kb,
        )


async def _send_card_with_buttons(context, chat_id, item, html_url=None):
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

    await context.bot.send_message(
        chat_id=chat_id,
        text=text,
        parse_mode="HTML",
        reply_markup=final_kb,
    )


async def _offer_ready(message,context,result,edit=False):
    vod_id = result["vod_id"]
    nonce = secrets.token_hex(8)
    context.user_data["vod_result_choice"] = {"vod_id":vod_id,"nonce":nonce,"created_at":time.time()}
    text,_ = _build_card(_make_item(result['meta'],result['stats'],f"https://www.twitch.tv/videos/{vod_id}","html_online"))
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("Показать результат",callback_data=f"ui:result:open:{vod_id}")],
        [InlineKeyboardButton("Создать заново",callback_data=f"ui:result:regen:{vod_id}:{nonce}")],
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
            await _send_card_with_buttons(context,q.message.chat_id,item,ready['html_ref'])
        else:
            if different_pending:
                await q.message.reply_text('Завершите или отмените выбор формата для другого VOD.')
                return
            if is_busy(context) or context.user_data.get('vod_link_lookup'):
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
    if is_busy(context):
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
            await _offer_ready(q.message,context,ready)
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


async def prepare_vod_choice(message, context, user_id, vod_id, *, is_current=None):
    """Shared preparation only; format/regeneration confirmation starts the job.

    A guarded entry supplies an owner/session or input validity guard. Such inputs
    preserve an existing format dialog and decline uncached work while the
    global slot is occupied. Direct links retain their existing entry behavior.
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
        if config.HISTORY_ENABLED and ready['cache_id'] is not None:
            db.add_user_history(user_id,ready['cache_id'])
        await _offer_ready(message,context,ready)
        return 'ready'
    if pending_active:
        await message.reply_text('Выберите формат чата в предыдущем сообщении.')
        return 'pending'

    if is_busy(context) or context.user_data.get("vod_link_lookup"):
        await message.reply_text("Дождитесь завершения предыдущего запроса.")
        return 'busy'
    if from_channel and is_globally_busy(context):
        await message.reply_text("Сейчас обрабатывается чат VOD. Повторите попытку позже.")
        return 'busy'

    client_id = get_client_id()
    session = context.application.bot_data["aiohttp"]

    # Both entry handlers are non-blocking in PTB. Serialize this user's
    # preparation without reserving the global download slot.
    lookup = object()
    context.user_data["vod_link_lookup"] = lookup
    try:
        meta = await fetch_vod_meta(session, client_id, vod_id)
        if (context.user_data.get("vod_link_lookup") is not lookup or not current()
                or get_pending(context) is not pending):
            return 'stale'
        if from_channel and is_globally_busy(context):
            await message.reply_text("Сейчас обрабатывается чат VOD. Повторите попытку позже.")
            return 'busy'

        # Direct links keep the existing silent behavior for unavailable VODs.
        if not meta or not getattr(meta, "length_seconds", None):
            if from_channel:
                await message.reply_text('VOD не найден или недоступен.')
            return 'missing'

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


# ------ Cancel pending download (кнопка отмены в прогрессе) ------
async def pending_cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    if is_busy(context):
        set_cancel(context, True)
    else:
        await q.message.reply_text("Нечего отменять.")


# ------ Cancel format selection (удалить pending) ------
async def format_cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    pending = get_pending(context)
    if ((pending and not pending_matches_message(pending, q.message))
            or (pending is None and context.user_data.get('vod_link_lookup'))):
        await q.message.reply_text('Действие устарело. Выберите VOD заново.')
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

    if is_busy(context):
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
        await _offer_ready(q.message,context,ready,edit=True)
        return
    await _start_download(update,context,vod_id)


async def _start_download(update,context,vod_id,regenerate=False):
    q = update.callback_query
    vod_url = f"https://www.twitch.tv/videos/{vod_id}"
    fmt = "html_online"
    operation_id = begin_download(context)
    if operation_id is None:
        await q.message.reply_text("Сейчас обрабатывается чат VOD. Повторите попытку позже.")
        return
    if regenerate:
        context.user_data.pop("vod_result_choice",None)
    try:
        await q.message.edit_text(
            text="Загрузка чата…",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(CANCEL, callback_data=CB_PENDING_CANCEL)]
            ])
        )
    except BaseException:
        try:
            await _restore_start_prompt(q.message,context,vod_id,regenerate)
        finally:
            finish_download(context, operation_id)
        raise

    runner_coro = _runner(
        context=context,
        chat_id=q.message.chat_id,
        progress_message_id=q.message.message_id,
        vod_url=vod_url,
        vod_id=vod_id,
        fmt=fmt,
        user_id=update.effective_user.id,
        operation_id=operation_id,
    )
    try:
        task = context.application.create_task(runner_coro)
    except BaseException:
        runner_coro.close()
        try:
            await _restore_start_prompt(q.message,context,vod_id,regenerate)
        finally:
            finish_download(context, operation_id)
        raise

    def release_finished_task(_task):
        # Also cover cancellation before _runner gets its first turn.
        runner_coro.close()
        finish_download(context, operation_id)

    task.add_done_callback(release_finished_task)
    clear_pending(context)


async def _restore_start_prompt(message,context,vod_id,regenerate):
    if regenerate:
        try:
            ready = results.get_ready_result(vod_id)
            if ready:
                await _offer_ready(message,context,ready,edit=True)
                return
        except Exception:
            logging.getLogger(__name__).warning("Could not restore ready-result actions")
        set_pending(context,f"https://www.twitch.tv/videos/{vod_id}",vod_id)
        bind_pending_message(context, message)
    await _restore_format_prompt(message)


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
    try:
        publication = results.Publication(vod_id,fmt,mode=HTML_PUBLISH_MODE)
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
        cache_id = await run_blocking(publication.complete)
        if config.HISTORY_ENABLED:
            db.add_user_history(user_id, cache_id)

        item = _make_item(meta, stats, vod_url, fmt)
        html_url = html_result_ref

        await _edit_progress_message_with_card(
            context=context,
            chat_id=chat_id,
            message_id=progress_message_id,
            item=item,
            html_url=html_url,
        )

    except RuntimeError as e:
        await _record_generation_failure(publication,e)
        if str(e) == "CHAT_EMPTY":
            try:
                await context.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=progress_message_id,
                    text="У этого VOD нет чата."
                )
            except Exception:
                pass
            return
        if "Загрузка была отменена." in str(e):
            try:
                await context.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=progress_message_id,
                    text="Обработка чата отменена.",
                )
            except Exception:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text="Обработка чата отменена.",
                )
            return
        raise

    except Exception as e:
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
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=progress_message_id,
                text=error_text,
            )
        except Exception:
            await context.bot.send_message(
                chat_id=chat_id,
                text=error_text,
            )
    except BaseException as exc:
        await _record_generation_failure(publication,exc)
        raise
    finally:
        try:
            if publication is not None:
                await run_blocking(publication.close)
        finally:
            finish_download(context, operation_id)


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
