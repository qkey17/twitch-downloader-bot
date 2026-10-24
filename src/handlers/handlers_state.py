import time
import uuid
from typing import Optional
from telegram.ext import ContextTypes
from src.config import VOD_URL_RE, PENDING_TTL_SECONDS


_ACTIVE_OPERATION_KEY = "vod_active_operation"
_CANCEL_OPERATION_KEY = "vod_cancel"
CANCELLED_MESSAGE = "Загрузка была отменена."


def extract_vod_id_strict(url: str) -> Optional[str]:
    m = VOD_URL_RE.match(url.strip())
    return m.group(1) if m else None


def is_busy(context: ContextTypes.DEFAULT_TYPE) -> bool:
    return bool(context.user_data.get("vod_busy"))


def set_busy(context: ContextTypes.DEFAULT_TYPE, v: bool):
    context.user_data["vod_busy"] = v


def begin_download(context: ContextTypes.DEFAULT_TYPE) -> str:
    """Reserve this user's download state and discard any stale cancel request."""
    operation_id = uuid.uuid4().hex
    context.user_data[_ACTIVE_OPERATION_KEY] = operation_id
    context.user_data["vod_busy"] = True
    context.user_data.pop(_CANCEL_OPERATION_KEY, None)
    return operation_id


def finish_download(context: ContextTypes.DEFAULT_TYPE, operation_id: str | None) -> bool:
    """Release state only when it still belongs to this operation."""
    if operation_id is None:
        if context.user_data.get(_ACTIVE_OPERATION_KEY) is not None:
            return False
        context.user_data["vod_busy"] = False
        context.user_data.pop(_CANCEL_OPERATION_KEY, None)
        return True
    if context.user_data.get(_ACTIVE_OPERATION_KEY) != operation_id:
        return False
    context.user_data["vod_busy"] = False
    context.user_data.pop(_ACTIVE_OPERATION_KEY, None)
    context.user_data.pop(_CANCEL_OPERATION_KEY, None)
    return True


def set_pending(context: ContextTypes.DEFAULT_TYPE, vod_url: str, vod_id: str):
    context.user_data["vod_pending"] = {
        "vod_url": vod_url,
        "vod_id": vod_id,
        "created_at": time.time(),
    }


def get_pending(context: ContextTypes.DEFAULT_TYPE) -> Optional[dict]:
    return context.user_data.get("vod_pending")


def clear_pending(context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("vod_pending", None)


def pending_expired(p: dict) -> bool:
    return (time.time() - float(p.get("created_at") or 0)) > PENDING_TTL_SECONDS


def set_cancel(context: ContextTypes.DEFAULT_TYPE, v: bool):
    if v:
        operation_id = context.user_data.get(_ACTIVE_OPERATION_KEY)
        if operation_id is not None:
            context.user_data[_CANCEL_OPERATION_KEY] = operation_id
        else:
            context.user_data[_CANCEL_OPERATION_KEY] = True
    else:
        context.user_data.pop(_CANCEL_OPERATION_KEY, None)


def is_cancelled(context: ContextTypes.DEFAULT_TYPE, operation_id: str | None = None) -> bool:
    cancel_operation = context.user_data.get(_CANCEL_OPERATION_KEY)
    if operation_id is None:
        if context.user_data.get(_ACTIVE_OPERATION_KEY) is not None:
            return False
        return bool(cancel_operation)
    return (
        context.user_data.get(_ACTIVE_OPERATION_KEY) == operation_id
        and cancel_operation == operation_id
    )


def clear_cancel(context: ContextTypes.DEFAULT_TYPE, operation_id: str | None = None):
    if operation_id is None and context.user_data.get(_ACTIVE_OPERATION_KEY) is not None:
        return
    if operation_id is not None and context.user_data.get(_ACTIVE_OPERATION_KEY) != operation_id:
        return
    context.user_data.pop(_CANCEL_OPERATION_KEY, None)


def raise_if_cancelled(context: ContextTypes.DEFAULT_TYPE, operation_id: str | None = None):
    if is_cancelled(context, operation_id):
        raise RuntimeError(CANCELLED_MESSAGE)
