"""Bounded per-job UI mailboxes; registration belongs to the scheduler's loop.

No chat rows, publications or additional workers live here. One consumer per
participant coalesces progress and drains a single terminal action before exit.
"""
import asyncio
import logging
import uuid
from dataclasses import dataclass, field

PARTICIPANT_LIMIT = 20
DELIVERY_TIMEOUT = 20
log = logging.getLogger(__name__)


@dataclass
class Participant:
    owner: int
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    context: object = None
    active: bool = True
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    changed: asyncio.Event = field(default_factory=asyncio.Event)
    task: object = None
    pending: object = None
    terminal: bool = False
    history_failed: bool = False
    restore_after_close: bool = False


class Participants:
    def __init__(self):
        self.members = {}
        self.latest = None
        self.accepting = True

    def add(self, owner, participation_id=None):
        if not self.accepting or len(self.members) >= PARTICIPANT_LIMIT:
            return None
        part = Participant(owner)
        if participation_id is not None:
            part.id = participation_id
        if part.id in self.members:
            return None
        self.members[part.id] = part
        return part

    def bind(self, part, context):
        part.context = context
        part.task = asyncio.create_task(self._deliver(part))
        if self.latest is not None:
            part.pending = self.latest
            part.changed.set()

    def progress(self, text):
        if not self.accepting:
            return
        self.latest = text
        for part in self.members.values():
            if part.active:
                part.pending = text
                part.changed.set()

    def seal(self):
        self.accepting = False
        return [p for p in self.members.values() if p.active]

    async def finish(self, action):
        self.seal()
        for part in self.members.values():
            if part.active:
                part.pending = action
                part.terminal = True
                part.changed.set()
        await self.drain()

    async def drain(self):
        tasks = [p.task for p in self.members.values() if p.task is not None]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def stop(self):
        self.accepting = False
        for part in self.members.values():
            part.active = False
            if part.task is not None:
                part.task.cancel()

    async def detach(self, part):
        # Marking inactive is done synchronously by Scheduler.cancel_participant.
        if part.task is not None:
            part.task.cancel()
            await asyncio.gather(part.task, return_exceptions=True)

    async def _deliver(self, part):
        try:
            # Initial media/caption delivery is owned by that input's handler.
            async with asyncio.timeout(DELIVERY_TIMEOUT):
                await part.ready.wait()
            while part.active:
                await part.changed.wait()
                part.changed.clear()
                value, part.pending = part.pending, None
                terminal = part.terminal
                try:
                    async with asyncio.timeout(DELIVERY_TIMEOUT):
                        if getattr(part.context, 'batch_silent', False):
                            pass  # One batch summary owns UI; job/history/publication are unchanged.
                        elif callable(value):
                            await value(part)
                        elif value is not None:
                            from .workers.worker_progress import safe_edit_html
                            ui = part.context.user_data.get('download_ui')
                            if ui:
                                ui['phase'] = 'running'
                                await safe_edit_html(part.context, ui['chat'], ui['message'], value, _local=True)
                except Exception:
                    log.warning('Participant card delivery failed')
                if terminal:
                    return
        except TimeoutError:
            log.warning('Participant initial card delivery timed out')
        except asyncio.CancelledError:
            return  # Owner detachment/shutdown drains this consumer before UI cleanup.
