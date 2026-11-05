"""One ephemeral privileged batch on the existing VOD scheduler."""
import asyncio
import html
import logging
import time
import uuid
from dataclasses import dataclass, field
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest, RetryAfter, TelegramError
from src import config
from src.admin_access import role
from src.channel_browser import ChannelBrowser
from src.channel_archive import order
from src.live_capture import generation_incomplete
from src.ready_html import available_results
from src.twitch_api.live import current_stream, match_stream
from src.vod_scheduler import scheduler, INPUT_TICKET
from src.job_context import JobContext
from src.handlers.handlers_state import begin_download
from src.ui.callbacks import CallbackReply
from src.ui.card_images import DEFAULT, telegram_image
from src.ui.channel_image import channel_image
from src.ui.result_preview import caption_fits
from src.ui.ui_labels import CANCEL, STALE_ACTION_NOTICE

KEY = 'tdb_admin_batch'
BATCH_CANCEL = 'ui:batch_cancel:'
BATCH_LIST = 'ui:batch_list:'
_LIST_FIELD = 'batch_list'
_PROGRESS = '<b>⏳ Обработка…</b>'


async def _finish_delivery(coro):
    """Settle in-flight delivery before a cancellation can render the final card."""
    task = asyncio.create_task(coro)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if not task.cancelled():
            task.exception()
        raise


@dataclass
class Batch:
    owner: int
    context: object
    source: object = None
    message: object = None
    task: object = None
    cancelled: bool = False
    total: int = 0
    index: int = 0  # Settled positions in the selected queue.
    success: int = 0
    skipped: int = 0
    errors: int = 0
    cancelled_vods: int = 0
    channel: str = ''
    channel_login: str = ''
    current_job: object = None
    current_part: object = None
    last_summary: float = 0
    critical_refusal: bool = False
    halted: bool = False
    cleanup_failed: bool = False
    new_only: bool = False
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    photo: bool = False
    attempted: bool = False
    closed: bool = False
    render_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    retry_at: float = 0

    def stop(self):
        if not self.cancelled and not self.closed:
            self.cancelled = True  # No next item can be dispatched after this write.
            if self.task is not None:self.task.cancel()

    def keyboard(self):
        return InlineKeyboardMarkup([[InlineKeyboardButton(CANCEL, callback_data=BATCH_CANCEL + self.id)]])

    def final_keyboard(self):
        if self.channel_login:
            return InlineKeyboardMarkup([[InlineKeyboardButton('Открыть список', callback_data=BATCH_LIST + self.id)]])

    def remember_list(self):
        from src.ui.card_context import for_message
        if self.channel_login and self.message is not None:
            scoped = for_message(self.context, self.owner, self.message.chat_id, self.message.message_id, fresh=True)
            scoped.card.data[_LIST_FIELD] = (BATCH_LIST + self.id, self.channel_login, self.photo, asyncio.Lock())

    async def open_card(self, text, image=DEFAULT):
        if self.attempted:
            return
        self.attempted = True
        markup = self.final_keyboard() if self.closed else None if self.cancelled else self.keyboard()

        async def send():
            async with asyncio.timeout(10):
                if image != DEFAULT and caption_fits(text):
                    try:
                        self.message = await self.source.reply_photo(
                            photo=telegram_image(image), caption=text, parse_mode='HTML', reply_markup=markup)
                        self.photo = True
                        return
                    except BadRequest:
                        # A definite rejection permits a text fallback. Network
                        # uncertainty does not permit a second send.
                        pass
                self.message = await self.source.reply_text(text, parse_mode='HTML', reply_markup=markup)

        await _finish_delivery(send())

    async def summary(self, state, *, detail=None, terminal=False, job=None):
        if terminal:
            text = f'<b>{state}</b>\n🟣 {html.escape(self.channel[:100])}'
        else:
            prefix = _PROGRESS
            if self.total:
                prefix += f'\n🎬 {self.index} из {self.total}'
            text = f'{prefix}\n\n{detail or state}'
            if not caption_fits(text):
                # Worker cards may contain long escaped names. Keep their
                # other rows and shorten each visible line before re-escaping.
                plain = html.unescape((detail or state).replace('<b>', '').replace('</b>', ''))
                rows = plain.split('\n', 7)
                for limit in (100, 64, 36, 18):
                    text = prefix + '\n\n' + '\n'.join(html.escape(row[:limit]) for row in rows)
                    if caption_fits(text):
                        break
        async with self.render_lock:
            if self.closed:
                return False
            if not terminal and job is not None:
                if self.cancelled or self.closed or self.current_job is not job:
                    return
                if time.monotonic() - self.last_summary < max(1, config.PROGRESS_INTERVAL):
                    return
            if not terminal and not self.cancelled and time.monotonic() < self.retry_at:
                return
            if terminal:
                self.closed = True
            self.last_summary = time.monotonic()
            shown = await self._edit_card(text, terminal)
            if terminal and shown:
                self.remember_list()
            return shown

    async def _edit_card(self, text, terminal):
        try:
            if self.message is None:
                if terminal:
                    await self.open_card(text)
                return self.message is not None
            markup = self.final_keyboard() if terminal else None if self.cancelled else self.keyboard()
            async def edit():
                async with asyncio.timeout(10):
                    if self.photo:
                        await self.message.edit_caption(caption=text, parse_mode='HTML', reply_markup=markup)
                    else:
                        await self.message.edit_text(text, parse_mode='HTML', reply_markup=markup)
            await _finish_delivery(edit())
        except RetryAfter as exc:
            self.retry_at = time.monotonic() + float(exc.retry_after)
            logging.getLogger(__name__).warning('Batch card rate limited')
            return False
        except (TelegramError, OSError, TimeoutError):
            logging.getLogger(__name__).warning('Batch card update unavailable')
            return False
        return True


def stop_batch(app, owner=None):
    batch=app.bot_data.get(KEY)
    if batch is not None and (owner is None or batch.owner==owner):
        batch.stop()
        return batch.task
    return None


async def start_batch(message, context, owner, login, *, new_only):
    if role(owner) is None:
        await message.reply_text('Команда недоступна')
        return
    if KEY in context.application.bot_data:
        await message.reply_text('Массовая обработка уже выполняется')
        return
    batch=Batch(owner,context,source=message,channel=login,new_only=new_only)
    # No await between the access lookup and this claim. Revocation either
    # prevents the claim or finds the already registered batch.
    context.application.bot_data[KEY]=batch
    try:
        batch.task=asyncio.create_task(run_batch(batch))
        def done(task):
            if context.application.bot_data.get(KEY) is batch:
                context.application.bot_data.pop(KEY,None)
            if not task.cancelled():task.exception()
        batch.task.add_done_callback(done)
    except BaseException:
        context.application.bot_data.pop(KEY,None)
        raise


async def batch_cancel_callback(update, context):
    query = update.callback_query
    batch = context.application.bot_data.get(KEY)
    message = query.message
    user_id = getattr(update.effective_user, 'id', None)
    if (batch is not None and not batch.closed and not batch.cancelled and batch.message is not None
            and type(user_id) is int and user_id == batch.owner and query.data == BATCH_CANCEL + batch.id
            and (getattr(message, 'chat_id', None), getattr(message, 'message_id', None))
            == (batch.message.chat_id, batch.message.message_id)):
        batch.stop()
    await CallbackReply(query).send()


async def batch_list_callback(update, context):
    from src.ui.card_context import KEY as CARD_KEY, for_message
    from src.handlers.handlers_channel_vods import open_batch_list
    query = update.callback_query
    message = query.message
    owner = getattr(update.effective_user, 'id', None)
    reply = CallbackReply(query)
    if message is None or type(owner) is not int:
        return await reply.send(STALE_ACTION_NOTICE)
    key = (owner, message.chat_id, message.message_id)
    store = context.application.bot_data.get(CARD_KEY, {})
    card = store.get(key)
    record = card.data.get(_LIST_FIELD) if card and card.expires > time.monotonic() else None
    if record is None or query.data != record[0] or record[3].locked():
        return await reply.send(STALE_ACTION_NOTICE)
    scoped = for_message(context, *key, fresh=True)
    async with record[3]:
        if not await reply.send():
            return
        if await open_batch_list(message, scoped, owner, record[1], photo=record[2]):
            card.data.pop(_LIST_FIELD, None)


async def submit(batch, vod_id, meta, regenerate):
    """Adapter only: normal reserve/Participants/JobContext and production _runner."""
    from src.handlers.handlers_vod import compatibility_key, _runner
    manager=scheduler(batch.context.application)
    job,refusal=manager.reserve(batch.owner,vod_id,compatibility=compatibility_key(vod_id,regenerate))
    if job is None:
        batch.critical_refusal=refusal not in ('Этот VOD уже обрабатывается или ожидает в очереди.', 'Лимит участников обработки достигнут или вы уже участвуете.')
        return None
    part=next(p for p in reversed(list(job.participants.members.values())) if p.owner==batch.owner and p.context is None)
    ctx=JobContext(batch.context,job);ctx.participant_owner=batch.owner;ctx.batch_silent=True
    async def progress(text):
        await batch.summary('', detail=text, job=job)
    ctx.batch_progress = progress
    begin_download(ctx)
    ctx.shared_user_data.setdefault('vod_jobs',{})[job.id]=job
    ui=dict(operation=job.id,chat=batch.message.chat_id,message=batch.message.message_id,owner=batch.owner,
            vod_id=vod_id,regenerate=regenerate,meta=meta,phase='waiting',photo=False,origin=None,
            ui_lock=asyncio.Lock(),render_lock=asyncio.Lock())
    ctx.user_data['download_ui']=ui
    joined=job.context is not None
    if not joined:
        from src.resource_usage import OperationTelemetry
        job.context=ctx
        job.telemetry=OperationTelemetry(job.id);job.telemetry.begin()
        job.delivery=asyncio.Event();job.delivery.set()
    try:
        job.participants.bind(part,ctx);part.ready.set()
        batch.current_job,batch.current_part=job,part
        if not joined:
            # Same runner, commit fence, history, publication and cleanup as a card.
            async def run_one():
                if job.cancelled:return
                await _runner(context=ctx,chat_id=ui['chat'],progress_message_id=ui['message'],
                              vod_url=f'https://www.twitch.tv/videos/{vod_id}',vod_id=vod_id,fmt='html_online',
                              user_id=batch.owner,operation_id=job.id,_telemetry=job.telemetry)
            manager.arm(job,run_one)
    except BaseException:
        # Reservation/binding is transactional, including failed task creation.
        part.active=False
        if not any(p.active for p in job.participants.members.values()):
            manager.cancel(job)
        part.ready.set()
        await job.participants.detach(part)
        manager.release_context(job,ctx)
        job.participants.members.pop(part.id,None)
        batch.current_job=batch.current_part=None
        raise

    return job


async def run_batch(batch):
    from src.handlers import handlers_vod as vod
    from src.vod_cancellation import request_member, finish_member
    from src.workers.worker_progress import _build_progress_card
    app=batch.context.application
    manager=scheduler(app)
    excluded = 0
    try:
        browser=ChannelBrowser()  # Twitch only; never the merged Telegram list.
        http=app.bot_data['aiohttp']
        async with asyncio.timeout(60):
            try:
                view=await browser.open(http,batch.owner,batch.channel,
                                        getattr(batch.source,'chat_id',0),getattr(batch.source,'message_id',0))
            finally:
                # The private browser owns one session. Keep a verified login
                # even if cancellation interrupted its first videos request.
                session = next(iter(browser._sessions.values()), None)
                if session is not None and session.channel is not None:
                    batch.channel = session.channel.display_name[:100]
                    batch.channel_login = session.channel.login
            image = DEFAULT
            if session is not None and session.channel is not None:
                image = await channel_image(batch.context, session, session.channel.profile_image_url)
            await batch.open_card(_PROGRESS + '\n\nЗагрузка списка записей…', image)
            while session is not None and not session.complete and session.status in ('ready','empty_page'):
                view=await browser._load(http,session,len(session.vods))
        if session is None or not session.complete or session.status not in ('ready','empty'):
            batch.halted = True
            await batch.summary('⚠️ Не удалось загрузить полный список записей', terminal=True)
            return
        def newest_first(vod):
            date, vod_id = order(vod)
            return date, -vod_id  # Same-date VODs retain their previous ID tie-break.
        records=sorted((v for v in session.vods if v.viewable=='public'),key=newest_first)
        if batch.new_only and records:
            stream_id = await current_stream(http, session.channel.id)
            ready_results = available_results([item.id for item in records])
            queue = [item for item in records if item.id not in ready_results
                     or generation_incomplete(ready_results[item.id])
                     or match_stream(item.stream_id, stream_id) is True]
            excluded = batch.skipped = len(records) - len(queue)
            records = queue
        batch.channel=session.channel.display_name[:120];batch.total=len(records)
        if batch.total:
            await batch.summary('Подготовка обработки…')
        for item in records:
            if batch.cancelled or manager.closing:
                batch.halted=True;break
            from src.vod_start_guard import start_guard
            with start_guard() as allowed:
                if not allowed:
                    batch.errors+=1;batch.halted=True;break
            ticket=manager.prepare_input(item.id)
            if ticket is None:
                batch.errors+=1;batch.halted=True;break  # Admission/queue pressure is not bypassed or retried.
            token=INPUT_TICKET.set(ticket)
            job, skipped = None, False
            try:
                meta=await vod.fetch_vod_meta(http,vod.get_client_id(),item.id)
                if batch.cancelled or manager.closing:break
                if getattr(meta,'length_seconds',None) is not None:
                    ready=vod.results.get_ready_result(item.id)
                    skipped = (batch.new_only and ready and not generation_incomplete(ready)
                               and getattr(meta, 'is_live', None) is not True)
                    if not skipped:
                        job = await submit(batch, item.id, vars(meta), bool(ready))
            except Exception as exc:
                from src.twitch_api.client import TwitchAPIError
                if not isinstance(exc,TwitchAPIError) or exc.status in (401,403,429):
                    batch.errors+=1;batch.halted=True;break
            finally:
                manager.release_input(ticket);INPUT_TICKET.reset(token)
            if skipped:
                batch.skipped += 1
                batch.index += 1
                if batch.index < batch.total:
                    await batch.summary('Готовый чат уже доступен')
                continue
            if job is None:
                batch.errors+=1
                if batch.critical_refusal:
                    batch.halted=True;break
                batch.index += 1
                if batch.index < batch.total:
                    await batch.summary('Не удалось начать обработку записи')
                continue
            detail = _build_progress_card(vars(meta), None, None, None, False,
                                          status='Ожидание обработки…' if job.state=='waiting' else 'Подготовка чата…')
            await batch.summary('', detail=detail)
            await job.finished.wait()
            if job.generation_committed and job.outcome=='success' and not batch.current_part.history_failed:batch.success+=1
            elif job.outcome=='cancelled':batch.cancelled_vods+=1
            else:batch.errors+=1
            batch.current_job=batch.current_part=None
            if job.critical:
                batch.halted=True;break
            batch.index += 1
        if batch.halted or batch.index < batch.total:
            state = '⚠️ Обработка остановлена из-за ошибки'
        elif batch.errors or batch.cancelled_vods:
            state = '⚠️ Обработка завершена с ошибками'
        else:
            state = '✅ Массовая обработка завершена'
        await batch.summary(state, terminal=True)
    except asyncio.CancelledError:
        batch.cancelled=True
        job,part=batch.current_job,batch.current_part
        selected = None
        if job is not None:
            batch.cleanup_failed=True  # Remains set if cancellation cleanup raises.
            selected=request_member(manager,job,part)
        if batch.message is not None:
            await batch.summary('Остановка обработки…')
        if job is not None:
            try:
                if selected is not None:await finish_member(manager,job,selected,wait_last=True)
                elif job.committing or job.cancelled:
                    await job.finished.wait()
                else:
                    await job.participants.detach(part)
                    manager.release_context(job,part.context)
                batch.cleanup_failed=job.critical
            except Exception:
                logging.getLogger(__name__).warning('Batch cancellation cleanup failed')
            if job.generation_committed and job.outcome=='success' and not part.history_failed:
                batch.success += 1
                batch.index += 1
            elif job.generation_committed or job.outcome=='error':batch.errors+=1
            else:batch.cancelled_vods+=1
        finished = batch.success + (batch.skipped - excluded) + batch.errors + batch.cancelled_vods
        batch.cancelled_vods += max(0, batch.total - finished)
        state = ('⚠️ Не удалось подтвердить остановку' if batch.cleanup_failed
                 else '⏹️ Обработка остановлена по запросу')
        await batch.summary(state, terminal=True)
    except Exception:
        batch.halted = True
        await batch.summary('⚠️ Обработка остановлена из-за ошибки', terminal=True)
    finally:
        if app.bot_data.get(KEY) is batch:app.bot_data.pop(KEY,None)
