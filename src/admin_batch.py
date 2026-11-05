"""One ephemeral admin batch; all work belongs to the existing VOD scheduler."""
import asyncio
import html
import logging
import sqlite3
import time
from telegram.error import TelegramError
from dataclasses import dataclass
from src import config
from src.database import db_privileges
from src.channel_browser import ChannelBrowser
from src.input_classifier import classify_input, InputClassificationError
from src.channel_archive import order
from src.vod_scheduler import scheduler, INPUT_TICKET
from src.job_context import JobContext
from src.handlers.handlers_state import begin_download

KEY = 'tdb_admin_batch'


@dataclass
class Batch:
    owner: int
    context: object
    source: object = None
    message: object = None
    task: object = None
    cancelled: bool = False
    total: int = 0
    index: int = 0
    success: int = 0
    skipped: int = 0
    errors: int = 0
    cancelled_vods: int = 0
    channel: str = ''
    current_job: object = None
    current_part: object = None
    last_summary: float = 0
    critical_refusal: bool = False
    halted: bool = False
    cleanup_failed: bool = False

    def stop(self):
        if not self.cancelled:
            self.cancelled = True  # No next item can be dispatched after this write.
            if self.task is not None:self.task.cancel()

    async def summary(self, state):
        if self.message is None:return
        stamp=time.monotonic()
        if state=='Массовая обработка' and stamp-self.last_summary<max(1,config.PROGRESS_INTERVAL):return
        self.last_summary=stamp
        text=(f'{state}\n🟣 {html.escape(self.channel)}\n'
              f'Всего: {self.total}\nТекущий: {self.index}\nУспешно: {self.success}\n'
              f'Пропущено: {self.skipped}\nОшибок: {self.errors}\nОтменено: {self.cancelled_vods}\n'
              f'Осталось: {max(0,self.total-self.success-self.skipped-self.errors-self.cancelled_vods)}')
        try:
            async with asyncio.timeout(10):
                await self.message.edit_text(text,parse_mode='HTML')
        except (TelegramError, OSError, TimeoutError):
            logging.getLogger(__name__).warning('Batch summary update unavailable')
            return False
        return True


def stop_batch(app, owner=None):
    batch=app.bot_data.get(KEY)
    if batch is not None and (owner is None or batch.owner==owner):
        batch.stop()
        return batch.task
    return None


async def allvods_command(update, context):
    owner=getattr(update.effective_user,'id',None)
    configured_owner=config.get_admin_user_id()
    allowed=False
    if configured_owner is not None and type(owner) is int and 0 < owner <= 2**63-1:
        try:
            allowed=owner==configured_owner or db_privileges.has_access(owner)
        except (sqlite3.Error, OSError):
            logging.getLogger(__name__).warning('Delegated access check unavailable')
    if not allowed:
        await update.effective_message.reply_text('Команда недоступна')
        return
    try:
        if len(context.args)!=1:raise InputClassificationError()
        login=classify_input(context.args[0],expected_kind='channel').value
    except (InputClassificationError,TypeError):
        await update.effective_message.reply_text('Используйте /allvods название_канала')
        return
    if KEY in context.application.bot_data:
        await update.effective_message.reply_text('Массовая обработка уже выполняется')
        return
    batch=Batch(owner,context,source=update.effective_message,channel=login)
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
    app=batch.context.application
    manager=scheduler(app)
    try:
        batch.message=await batch.source.reply_text('Загрузка VODs канала…')
        if batch.cancelled:return
        browser=ChannelBrowser()  # Twitch only; never the merged Telegram list.
        http=app.bot_data['aiohttp']
        async with asyncio.timeout(60):
            view=await browser.open(http,batch.owner,batch.channel,batch.message.chat_id,batch.message.message_id)
            session=browser._sessions.get(view.session_id)
            while session is not None and not session.complete and session.status in ('ready','empty_page'):
                view=await browser._load(http,session,len(session.vods))
        if session is None or not session.complete or session.status not in ('ready','empty'):
            await batch.summary('Полный список VODs получить не удалось (ошибка или ограничение)')
            return
        records=sorted((v for v in session.vods if v.viewable=='public'),key=order,reverse=True)
        batch.channel=session.channel.display_name[:120];batch.total=len(records)
        await batch.summary('Очередь сформирована')
        for item in records:
            if batch.cancelled or manager.closing:
                batch.halted=True;break
            batch.index+=1
            await batch.summary('Массовая обработка')  # Once per VOD, never chat-message progress.
            from src.vod_start_guard import start_guard
            with start_guard() as allowed:
                if not allowed:
                    batch.errors+=1;batch.halted=True;break
            ticket=manager.prepare_input(item.id)
            if ticket is None:
                batch.errors+=1;batch.halted=True;break  # Admission/queue pressure is not bypassed or retried.
            token=INPUT_TICKET.set(ticket)
            try:
                meta=await vod.fetch_vod_meta(http,vod.get_client_id(),item.id)
                if batch.cancelled or manager.closing:break
                if getattr(meta,'length_seconds',None) is None:
                    batch.skipped+=1;continue
                ready=vod.results.get_ready_result(item.id)
                job=await submit(batch,item.id,vars(meta),bool(ready))
            except Exception as exc:
                batch.errors+=1
                from src.twitch_api.client import TwitchAPIError
                if not isinstance(exc,TwitchAPIError) or exc.status in (401,403,429):
                    batch.halted=True;break
                continue
            finally:
                manager.release_input(ticket);INPUT_TICKET.reset(token)
            if job is None:
                batch.errors+=1
                if batch.critical_refusal:
                    batch.halted=True;break
                continue
            await job.finished.wait()
            if job.generation_committed and job.outcome=='success' and not batch.current_part.history_failed:batch.success+=1
            elif job.outcome=='cancelled':batch.cancelled_vods+=1
            else:batch.errors+=1
            batch.current_job=batch.current_part=None
            if job.critical:
                batch.halted=True;break
        await batch.summary('Массовая обработка остановлена' if batch.halted or batch.index<batch.total else 'Массовая обработка завершена')
    except asyncio.CancelledError:
        batch.cancelled=True
        job,part=batch.current_job,batch.current_part
        if job is not None:
            batch.cleanup_failed=True  # Remains set if cancellation cleanup raises.
            selected=request_member(manager,job,part)
            if selected is not None:await finish_member(manager,job,selected,wait_last=True)
            elif job.committing or job.cancelled:
                await job.finished.wait()
            else:
                await job.participants.detach(part)
                manager.release_context(job,part.context)
            batch.cleanup_failed=job.critical
            if job.generation_committed:batch.success+=1
            elif job.outcome=='error':batch.errors+=1
            else:batch.cancelled_vods+=1
        batch.cancelled_vods += max(0,batch.total-batch.success-batch.skipped-batch.errors-batch.cancelled_vods)
        try:await batch.summary('Массовая обработка отменена')
        except Exception:pass
    except Exception:
        batch.errors+=1
        try:await batch.summary('Массовая обработка остановлена из-за ошибки')
        except Exception:pass
    finally:
        if app.bot_data.get(KEY) is batch:app.bot_data.pop(KEY,None)
