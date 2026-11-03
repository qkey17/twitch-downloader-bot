"""One event-loop-owned FIFO; queued jobs own no task, spool or publication."""
import asyncio
import logging
from collections import deque
from dataclasses import dataclass
import uuid
from contextvars import ContextVar

KEY = 'tdb_vod_scheduler'
ACTIVE_LIMIT = 2
QUEUE_LIMIT = 20
INPUT_TICKET = ContextVar('vod_input_ticket', default=None)


@dataclass
class Job:
    id: str
    owner: int
    vod: str
    state: str = 'waiting'
    factory: object = None
    task: object = None
    context: object = None
    cancelled: bool = False
    committing: bool = False
    outcome: str = 'success'
    processing_done: bool = False


class Scheduler:
    def __init__(self, app, admission=None):
        self.app = app
        self.jobs = {}
        self.queue = deque()
        self.closing = False
        self.waker = None
        self.close_task = None
        self.preparing = 0
        self.notifications = set()
        self.admission = admission

    @property
    def active(self):
        return sum(j.state == 'running' for j in self.jobs.values())

    @property
    def full(self):
        return len(self.queue) + self.preparing >= QUEUE_LIMIT

    def prepare_input(self):
        from .maintenance import admission_closed
        if self.closing or self.full or admission_closed(self.app):
            return None
        ticket = [self, True]
        self.preparing += 1
        return ticket

    def release_input(self, ticket):
        if ticket[1]:
            ticket[1] = False
            self.preparing -= 1

    def reserve(self, owner, vod):
        ticket = INPUT_TICKET.get()
        if ticket is not None and ticket[0] is self:
            self.release_input(ticket)  # Transfer the bounded input place to this job.
        from .maintenance import admission_closed
        if self.closing or admission_closed(self.app):
            return None, 'Сейчас проводится обслуживание. Повторите запрос позже.'
        legacy = self.app.bot_data.get('vod_global_operation')
        if legacy is not None and legacy not in self.jobs:
            return None, 'Сейчас обрабатывается чат VOD. Повторите попытку позже.'
        if any(j.vod == vod and not j.processing_done for j in self.jobs.values()):
            return None, 'Этот VOD уже обрабатывается или ожидает в очереди.'
        if self.full:
            return None, 'Очередь заполнена. Повторите запрос позже.'
        job = Job(uuid.uuid4().hex, owner, vod)
        self.jobs[job.id] = job
        self.queue.append(job)
        return job, None

    def arm(self, job, factory):
        if job.id not in self.jobs:
            return
        job.factory = factory
        self.pump(admitting=job)

    def pump(self, *, admitting=None):
        from .maintenance import admission_closed
        from .vod_start_guard import start_guard
        while self.queue and not self.closing and self.active < ACTIVE_LIMIT:
            legacy = self.app.bot_data.get('vod_global_operation')
            if admission_closed(self.app) or (legacy is not None and legacy not in self.jobs):
                break
            job = self.queue[0]
            if job.factory is None:
                break  # Initial progress delivery is still owned by admission.
            with (self.admission() if self.admission else start_guard()) as allowed:
                if not allowed:
                    break
                self.queue.popleft()
                job.state = 'running'  # Same loop, no await through task registration.
                self.app.bot_data.setdefault('vod_global_operation', job.id)
                coro = self._run(job)
                try:
                    job.task = self.app.create_task(coro)
                except BaseException as exc:
                    coro.close()
                    self.drop(job, 'error')
                    if job is admitting or not isinstance(exc,Exception):
                        raise  # Only this admission may restore its own prompt.
                    logging.getLogger(__name__).error('Queued VOD task registration failed')
                    if job.context is not None:
                        task = asyncio.create_task(self._notify_start_failure(job))
                        self.notifications.add(task)
                        task.add_done_callback(self.notifications.discard)
                    continue
                job.task.add_done_callback(lambda task, j=job: self._done(j, task))
        if self.queue and not self.closing and (self.waker is None or self.waker.done()):
            self.waker = asyncio.create_task(self._wake())

    async def _wake(self):
        try:
            while self.queue and not self.closing:
                await asyncio.sleep(1)
                self.pump()
        finally:
            self.waker = None

    async def _notify_start_failure(self, job):
        ui = job.context.user_data.get('download_ui')
        if ui is None:
            return
        try:
            async with asyncio.timeout(5):
                await job.context.bot.edit_message_text(chat_id=ui['chat'],message_id=ui['message'],
                    text='Не удалось начать обработку. Выберите VOD заново.',reply_markup=None)
        except Exception:
            logging.getLogger(__name__).warning('Could not deliver queued VOD startup failure')

    async def _run(self, job):
        try:
            await job.factory()
        except asyncio.CancelledError:
            job.outcome = 'cancelled'
            raise
        except Exception:
            job.outcome = 'error'
            raise

    def _done(self, job, task):
        # Also owns the task cancelled before its coroutine ever entered.
        outcome = 'cancelled' if task.cancelled() or job.cancelled else job.outcome
        if not task.cancelled() and task.exception() is not None:
            outcome = 'error'
        self.drop(job, outcome)
        self.pump()

    def drop(self, job, outcome):
        if self.jobs.get(job.id) is not job:
            return
        job.state = outcome
        telemetry = getattr(job, 'telemetry', None)
        if telemetry is not None and not telemetry.finished:
            if outcome != 'success':
                telemetry.fail(asyncio.CancelledError() if outcome == 'cancelled' else RuntimeError('job_failed'))
            telemetry.finish()
        self.jobs.pop(job.id)
        if self.app.bot_data.get('vod_global_operation') == job.id:
            self.app.bot_data.pop('vod_global_operation', None)
            active = next((j for j in self.jobs.values() if j.state == 'running'), None)
            if active is not None:
                self.app.bot_data['vod_global_operation'] = active.id
        if job in self.queue:
            self.queue.remove(job)
        if job.context is not None:
            from .handlers.handlers_state import finish_download
            finish_download(job.context, job.id)
            root = job.context.shared_user_data
            root.get('vod_jobs', {}).pop(job.id, None)
            root['vod_busy'] = bool(root.get('vod_jobs'))
            ui = root.get('download_ui')
            if ui and ui.get('operation') == job.id:
                root.pop('download_ui', None)
                root.pop('vod_active_operation', None)

    def cancel(self, job):
        if self.jobs.get(job.id) is not job or job.committing or job.cancelled:
            return False
        job.cancelled = True
        if job.state == 'waiting':
            self.drop(job, 'cancelled')
            self.pump()
        return True

    async def close(self):
        self.closing = True
        if self.close_task is None:
            self.close_task = asyncio.create_task(self._close_owned())
        interrupted = False
        while not self.close_task.done():
            try:
                await asyncio.shield(self.close_task)
            except asyncio.CancelledError:
                interrupted = True
        self.close_task.result()
        if interrupted:
            raise asyncio.CancelledError()

    async def _close_owned(self):
        if self.waker:
            self.waker.cancel()
        tasks = []
        for job in list(self.jobs.values()):
            if job.state == 'waiting':
                job.cancelled = True
                self.drop(job, 'cancelled')
            elif job.task:
                if not job.committing:
                    job.cancelled = True
                    job.task.cancel()
                tasks.append(job.task)
        if self.waker:
            tasks.append(self.waker)
        tasks.extend(self.notifications)
        await asyncio.gather(*tasks, return_exceptions=True)


def scheduler(app):
    if KEY not in app.bot_data:
        app.bot_data[KEY] = Scheduler(app)
    return app.bot_data[KEY]


async def stop_scheduler(app):
    if KEY in app.bot_data:
        await app.bot_data[KEY].close()
