import asyncio
from contextvars import ContextVar
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import json
import logging
import math
import os
import re
from typing import Any

import aiohttp
from src.pagination_diagnostics import CURRENT

from src.config import (
    GQL_ENDPOINT,
    GQL_TIMEOUT_S,
    get_twitch_gql_client_id,
    get_twitch_credentials,
)


_ERROR_BODY_READ_LIMIT = 2048
_ERROR_BODY_LOG_LIMIT = 512
_REQUEST_DIAGNOSTIC_LOG_LIMIT = 2048
_GRAPHQL_ERROR_LIMIT = 5
# Only the experimental comments consumer sets this; ordinary HTTP is unchanged.
COMMENTS_BODY_LIMIT = ContextVar('comments_body_limit', default=None)
_GRAPHQL_PATH_SEGMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_GRAPHQL_EXTENSION_FIELDS = frozenset({
    "code", "classification", "requestId", "request_id", "timestamp", "type",
})
_SENSITIVE_KEY_RE = re.compile(
    r"(?:authorization|access.?token|refresh.?token|client.?secret|client.?id|bot.?token|"
    r"token|secret|password|credential|cookie|api.?key)",
    re.IGNORECASE,
)
_SENSITIVE_ASSIGNMENT_RE = re.compile(
    r"(?i)([\"']?(?:authorization|access.?token|refresh.?token|client.?secret|client.?id|"
    r"bot.?token|token|secret|password|credential|cookie|api.?key)[\"']?\s*[:=]\s*)"
    r"(?:\"[^\"]*\"|'[^']*'|[^\s,;}]+)"
)
_BEARER_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")
_TELEGRAM_BOT_TOKEN_RE = re.compile(r"\b\d{5,}:[A-Za-z0-9_-]{20,}\b")


class TwitchAPIError(Exception):
    """A non-empty API failure that must not be confused with an empty result."""

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        retry_after: float | None = None,
        retryable: bool = False,
        response_body: str | None = None,
    ):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after
        self.retryable = retryable
        self.response_body = response_body


def _redact_json_secrets(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: "[REDACTED]" if _SENSITIVE_KEY_RE.search(str(key)) else _redact_json_secrets(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_json_secrets(item) for item in value]
    if isinstance(value, str):
        return _sanitize_error_text(value)
    return value


def _sanitize_error_text(value: str) -> str:
    text = _BEARER_RE.sub("Bearer [REDACTED]", value)
    text = _TELEGRAM_BOT_TOKEN_RE.sub("[REDACTED_BOT_TOKEN]", text)
    text = _SENSITIVE_ASSIGNMENT_RE.sub(r"\1[REDACTED]", text)

    # A service error may echo a configured value without labelling it as a secret.
    secret_name = re.compile(
        r"(?:token|secret|credential|authorization|password|api.?key|client.?id)",
        re.I,
    )
    for name, secret_value in os.environ.items():
        if secret_value and len(secret_value) >= 4 and secret_name.search(name):
            text = text.replace(secret_value, "[REDACTED]")
    return text


async def _safe_error_response_body(response) -> str:
    content = getattr(response, "content", None)
    if content is None:
        return ""
    try:
        raw = await content.read(_ERROR_BODY_READ_LIMIT + 1)
    except (aiohttp.ClientError, asyncio.TimeoutError, OSError):
        return ""
    if not isinstance(raw, (bytes, bytearray)):
        return ""

    text = bytes(raw[:_ERROR_BODY_READ_LIMIT]).decode("utf-8", errors="replace")
    try:
        decoded = json.loads(text)
    except (TypeError, ValueError):
        safe_text = _sanitize_error_text(text)
    else:
        safe_text = json.dumps(
            _redact_json_secrets(decoded),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        safe_text = _sanitize_error_text(safe_text)

    safe_text = " ".join(safe_text.split())
    if len(safe_text) > _ERROR_BODY_LOG_LIMIT:
        safe_text = safe_text[:_ERROR_BODY_LOG_LIMIT - 3] + "..."
    return safe_text


def _safe_graphql_error_details(errors: Any) -> str:
    """Return a bounded, sanitized summary without logging arbitrary error data."""
    if not isinstance(errors, list):
        errors = [errors]

    summaries = []
    for error in errors[:_GRAPHQL_ERROR_LIMIT]:
        if not isinstance(error, dict):
            continue

        summary = {}
        message = error.get("message")
        if isinstance(message, str):
            summary["message"] = _sanitize_error_text(message)

        path = error.get("path")
        if isinstance(path, list):
            safe_path = [
                part if isinstance(part, int) else part
                for part in path[:8]
                if isinstance(part, int)
                or (isinstance(part, str) and _GRAPHQL_PATH_SEGMENT_RE.fullmatch(part))
            ]
            if safe_path:
                summary["path"] = safe_path

        locations = error.get("locations")
        if isinstance(locations, list):
            safe_locations = [
                {key: location[key] for key in ("line", "column") if isinstance(location.get(key), int)}
                for location in locations[:3]
                if isinstance(location, dict)
                and any(isinstance(location.get(key), int) for key in ("line", "column"))
            ]
            if safe_locations:
                summary["locations"] = safe_locations

        extensions = error.get("extensions")
        if isinstance(extensions, dict):
            safe_extensions = {
                key: _sanitize_error_text(value) if isinstance(value, str) else value
                for key, value in extensions.items()
                if key in _GRAPHQL_EXTENSION_FIELDS
                and isinstance(value, (str, int, float, bool))
            }
            if safe_extensions:
                summary["extensions"] = safe_extensions

        if summary:
            summaries.append(summary)

    if len(errors) > _GRAPHQL_ERROR_LIMIT:
        summaries.append({"omitted_errors": len(errors) - _GRAPHQL_ERROR_LIMIT})

    safe_text = json.dumps({"errors": summaries}, ensure_ascii=False, separators=(",", ":"))
    safe_text = _sanitize_error_text(safe_text)
    if len(safe_text) > _ERROR_BODY_LOG_LIMIT:
        safe_text = safe_text[:_ERROR_BODY_LOG_LIMIT - 3] + "..."
    return safe_text


def _parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        seconds = float(value.strip())
        return seconds if math.isfinite(seconds) and seconds >= 0 else None
    except (TypeError, ValueError):
        pass

    try:
        retry_at = parsedate_to_datetime(value)
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=timezone.utc)
        return max(0.0, (retry_at - datetime.now(timezone.utc)).total_seconds())
    except (TypeError, ValueError, OverflowError):
        return None


async def gql_post_json(
    session: aiohttp.ClientSession,
    headers: dict,
    payload: Any,
    *,
    page_number: int | None = None,
) -> Any:
    timeout = aiohttp.ClientTimeout(total=GQL_TIMEOUT_S)
    requests = payload if isinstance(payload, list) else [payload]
    request_summaries = []
    for request in requests:
        if not isinstance(request, dict):
            continue
        extensions = request.get("extensions")
        persisted_query = extensions.get("persistedQuery") if isinstance(extensions, dict) else None
        variables = request.get("variables", {})
        cursor = variables.get("cursor") if isinstance(variables, dict) else None
        request_summaries.append({
            "operationName": request.get("operationName") or "<unnamed>",
            "variables": {
                key: value for key, value in variables.items() if key != "cursor"
            } if isinstance(variables, dict) else variables,
            "persistedQuery": persisted_query if isinstance(persisted_query, dict) else None,
            "cursor": {
                "present": isinstance(variables, dict) and "cursor" in variables,
                "length": len(cursor) if isinstance(cursor, str) else None,
            },
        })

    safe_headers = {}
    for key, value in headers.items():
        normalized_key = str(key).lower()
        if normalized_key in {"user-agent", "content-type", "accept"}:
            safe_headers[str(key)] = value
        elif normalized_key == "client-id":
            safe_headers[str(key)] = "[REDACTED]"

    request_diagnostic = json.dumps(_redact_json_secrets({
        "requests": request_summaries,
        "headers": safe_headers,
    }), ensure_ascii=False, separators=(",", ":"))
    request_diagnostic = _sanitize_error_text(request_diagnostic)
    if len(request_diagnostic) > _REQUEST_DIAGNOSTIC_LOG_LIMIT:
        request_diagnostic = request_diagnostic[:_REQUEST_DIAGNOSTIC_LOG_LIMIT - 3] + "..."

    logger = logging.getLogger(__name__)
    logger.info(
        "Twitch GraphQL request page=%s diagnostic=%s",
        page_number if page_number is not None else "n/a",
        request_diagnostic,
    )
    async with session.post(GQL_ENDPOINT, json=payload, headers=headers, timeout=timeout) as r:
        diagnostic = CURRENT.get()
        if diagnostic is not None and page_number is not None:
            diagnostic.event('http_response', page=page_number, attempt=diagnostic.attempt, status=r.status)
        logger.info(
            "Twitch GraphQL response page=%s status=%s",
            page_number if page_number is not None else "n/a",
            r.status,
        )
        if r.status >= 400:
            retryable = r.status == 429 or r.status >= 500
            response_body = await _safe_error_response_body(r)
            logging.getLogger(__name__).warning(
                "Twitch HTTP error status=%s response_body=%s",
                r.status,
                response_body or "<empty>",
            )
            raise TwitchAPIError(
                f"Twitch HTTP error ({r.status})",
                status=r.status,
                retry_after=_parse_retry_after(r.headers.get("Retry-After")) if r.status == 429 else None,
                retryable=retryable,
                response_body=response_body,
            )

        try:
            limit = COMMENTS_BODY_LIMIT.get()
            if limit is None:
                data = await r.json()
            else:
                if r.content_length is not None and r.content_length > limit:
                    raise TwitchAPIError("Twitch pagination repair pilot: response_limit")
                try:
                    raw = await r.content.readexactly(limit + 1)
                except asyncio.IncompleteReadError as exc:
                    raw = exc.partial
                if len(raw) > limit:
                    raise TwitchAPIError("Twitch pagination repair pilot: response_limit")
                data = json.loads(raw)
        except (aiohttp.ClientError, ValueError) as exc:
            raise TwitchAPIError("Twitch returned invalid JSON", status=r.status) from exc

        responses = data if isinstance(data, list) else [data]
        if not responses or any(not isinstance(item, dict) for item in responses):
            raise TwitchAPIError("Twitch returned an unexpected JSON structure", status=r.status)
        graphql_errors = [item["errors"] for item in responses if item.get("errors")]
        if graphql_errors:
            diagnostic = _safe_graphql_error_details(graphql_errors[0])
            logger.warning(
                "Twitch GraphQL errors page=%s status=%s details=%s",
                page_number if page_number is not None else "n/a",
                r.status,
                diagnostic,
            )
            raise TwitchAPIError(
                "Twitch GraphQL returned errors",
                status=r.status,
                response_body=diagnostic,
            )
        return data


def get_client_id() -> str:
    """Return the Client ID used for GraphQL calls (kept for API compatibility)."""
    return get_twitch_gql_client_id()


# --- Twitch Helix API (OAuth) ---
# Используется для:
# - глобальных эмоутов
# - эмоутов каналов
# Требует TWITCH_CLIENT_ID и TWITCH_ACCESS_TOKEN
def get_api_headers() -> dict:
    credentials = get_twitch_credentials()
    return {
        "Client-ID": credentials.client_id,
        "Authorization": f"Bearer {credentials.access_token}",
    }
