"""Run one owned blocking operation; never detach its writes on cancellation."""
import asyncio


async def run_blocking(function, *args, on_cancel=None, **kwargs):
    def invoke():
        # Return failures as values while draining: Python 3.14 shield otherwise
        # logs them as unhandled after cancellation, even when we retrieve them.
        try:
            return True, function(*args, **kwargs)
        except BaseException as exc:
            return False, exc

    work = asyncio.create_task(asyncio.to_thread(invoke))
    try:
        succeeded, result = await asyncio.shield(work)
    except asyncio.CancelledError:
        if on_cancel is not None:
            on_cancel()
        while not work.done():
            try:
                await asyncio.shield(work)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if not work.cancelled():
            work.exception()
        raise
    if not succeeded:
        raise result
    return result
