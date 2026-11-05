"""Owner-scoped cancellation using the same participation/commit fences as buttons."""
import asyncio
from src.handlers.handlers_state import finish_download


def request_member(manager, job, part):
    if job.committing:
        return None
    from src.handlers import handlers_vod as vod
    record = part.context.user_data.get('download_ui') if part.context is not None else None
    try:
        ready = vod.results.get_ready_result(job.vod)
    except Exception:
        ready = None
    selected = manager.cancel_participant(job, part.id)
    if selected is not None and record is not None:
        record['cancel_ready'] = ready
    if selected is not None and job.cancelled and job.state == 'running':
        part.restore_after_close = True
    return selected


async def finish_member(manager, job, part, *, wait_last):
    group = job.participants
    await group.detach(part)
    if part.restore_after_close:
        if wait_last:
            await job.finished.wait()
        return
    await part.ready.wait()
    target = part.context
    if target is None:
        return
    record = target.user_data.get('download_ui')
    finish_download(target, job.id)
    try:
        if record is not None:
            from src.handlers.handlers_vod import _show_cancelled
            await _show_cancelled(target,record['chat'],record['message'],record)
    finally:
        manager.release_context(job,target)
        if target.user_data.get('download_ui') is record:
            target.user_data.pop('download_ui',None)
        if group.members.get(part.id) is part:
            group.members.pop(part.id)


def request_owner(app, owner):
    """Mark every own participation before yielding; never select by last card."""
    manager = app.bot_data.get('tdb_vod_scheduler')
    work, commits = [], []
    if manager is not None:
        for job in list(manager.jobs.values()):
            if job.participants is not None:
                for part in list(job.participants.members.values()):
                    if part.owner != owner or not part.active:
                        continue
                    if job.committing:
                        if job not in commits:commits.append(job)
                    elif request_member(manager, job, part) is not None:
                        work.append((job,part))
            elif job.owner == owner:
                if job.committing:commits.append(job)
                elif manager.cancel(job):work.append((job,None))
    return manager, work, commits


async def finish_owner(plan):
    manager, work, commits = plan
    async def finish(job, part):
        if part is not None:
            await finish_member(manager,job,part,wait_last=True)
        else:
            await job.finished.wait()
    results = await asyncio.gather(*(finish(job,part) for job,part in work),return_exceptions=True)
    for result in results:
        if isinstance(result,BaseException):
            import logging
            logging.getLogger(__name__).warning('Cancelled card restoration failed')
    for job in commits:
        await job.finished.wait()  # Already publishing: never cross the commit fence.
    return (not any(isinstance(result,BaseException) for result in results)
            and not any(job.critical for job,_ in work) and not any(job.critical for job in commits))


def invalidate_inputs(context, owner):
    from src.ui.card_context import KEY
    mappings = [context.user_data]
    mappings += [card.data for card in context.application.bot_data.get(KEY,{}).values() if card.owner==owner]
    changed = False
    browser=context.application.bot_data.get('channel_vod_browser')
    if browser is not None:
        for key,session in list(browser._sessions.items()):
            if session.owner_id==owner and (session.busy or session.status=='loading'):
                browser._sessions.pop(key,None);changed=True
    for data in mappings:
        for name in ('text_input_route','vod_pending','vod_link_lookup','chat_choice','chat_request',
                     'vod_result_choice','ready_preview_request'):
            value = data.pop(name,None)
            if value is not None:
                changed = True
                if isinstance(value,dict):value['cancelled']=True
    return changed
