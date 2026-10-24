import asyncio
import logging
from dataclasses import dataclass
from typing import Callable, Optional

import aiohttp

from .client import TwitchAPIError, get_api_headers


def _check_cancel(cancel_check: Callable[[], bool] | None) -> None:
    if cancel_check is not None and cancel_check():
        raise RuntimeError("Загрузка была отменена.")


async def _sleep_with_cancel(delay: float, cancel_check: Callable[[], bool] | None) -> None:
    end_time = asyncio.get_running_loop().time() + delay
    while True:
        _check_cancel(cancel_check)
        remaining = end_time - asyncio.get_running_loop().time()
        if remaining <= 0:
            return
        await asyncio.sleep(min(0.1, remaining))


async def resolve_thumbnail(session, thumb_template, cancel_check: Callable[[], bool] | None = None):
    sizes = [
        (1920, 1080),
        (1280, 720),
        (640, 360),
        (480, 270),
    ]

    for w, h in sizes:
        _check_cancel(cancel_check)
        url = thumb_template.replace("%{width}", str(w)).replace("%{height}", str(h))
        try:
            async with session.get(url, timeout=5) as resp:
                status = resp.status
        except Exception:
            continue
        _check_cancel(cancel_check)
        if status == 200:
            return url

    return thumb_template.replace("%{width}", "480").replace("%{height}", "270")

@dataclass
class VodMeta:
    vod_id: str
    title: Optional[str] = None
    channel: Optional[str] = None
    channel_login: Optional[str] = None
    channel_id: Optional[str] = None
    length_seconds: Optional[int] = None
    created_at: Optional[str] = None

    thumbnail_preview_url: Optional[str] = None
    thumbnail_full_url: Optional[str] = None


def extract_user_color(node: dict) -> Optional[str]:
    msg = node.get("message") or {}
    color = (
        msg.get("userColor")
        or msg.get("userColorHex")
        or node.get("userColor")
        or node.get("userColorHex")
    )
    if not color:
        return None
    color = str(color).strip()
    return color or None


def extract_user_badges(node: dict) -> list[dict]:
    msg = node.get("message") or {}
    raw_badges = msg.get("userBadges") or node.get("userBadges") or []
    out: list[dict] = []

    for badge in raw_badges:
        if not isinstance(badge, dict):
            continue

        set_id = (
            badge.get("setID")
            or badge.get("setId")
            or badge.get("set_id")
            or badge.get("set")
        )
        version = (
            badge.get("version")
            or badge.get("id")
            or badge.get("badgeVersion")
            or badge.get("badge_version")
        )

        if set_id is None or version is None:
            continue

        out.append({
            "set_id": str(set_id),
            "version": str(version),
        })

    return out


def extract_message_fragments(node: dict) -> list[dict]:
    msg = node.get("message") or {}
    fragments = msg.get("fragments") or []

    out: list[dict] = []
    for fragment in fragments:
        if not isinstance(fragment, dict):
            continue

        item = {"text": fragment.get("text") or ""}
        emote = fragment.get("emote") or {}
        if isinstance(emote, dict):
            emote_id = emote.get("emoteID") or emote.get("id")
            if emote_id:
                item["emoteID"] = emote_id
            emote_name = emote.get("name")
            if emote_name:
                item["emoteName"] = emote_name

        out.append(item)

    return out


def render_message(node: dict) -> str:
    fragments = extract_message_fragments(node)
    return "".join((f.get("text") or "") for f in fragments).strip()


async def fetch_vod_meta(
    session: aiohttp.ClientSession,
    client_id: str,
    vod_id: str,
    cancel_check: Callable[[], bool] | None = None,
) -> VodMeta:
    from .client import gql_post_json
    headers = {
        "Client-Id": client_id,
        "User-Agent": "Mozilla/5.0",
        "Content-Type": "application/json",
    }
    query = """
    query($id: ID!) {
      video(id: $id) {
        id
        title
        lengthSeconds
        createdAt
        owner {
          id
          login
          displayName
        }
      }
    }
    """
    meta = VodMeta(vod_id=vod_id)
    data = None
    for attempt in range(3):
        _check_cancel(cancel_check)
        try:
            payload = {"query": query, "variables": {"id": vod_id}}
            data = await gql_post_json(session, headers, payload)
        except TwitchAPIError as exc:
            _check_cancel(cancel_check)
            if not exc.retryable or attempt == 2:
                raise
            await _sleep_with_cancel(
                exc.retry_after if exc.retry_after is not None else 0.2 * (attempt + 1),
                cancel_check,
            )
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            _check_cancel(cancel_check)
            if attempt == 2:
                raise TwitchAPIError("Twitch metadata request failed after retries") from exc
            await _sleep_with_cancel(0.2 * (attempt + 1), cancel_check)
        else:
            _check_cancel(cancel_check)
            break

    response_data = data.get("data") if isinstance(data, dict) else None
    if not isinstance(response_data, dict) or "video" not in response_data:
        raise TwitchAPIError("Twitch returned an unexpected VOD metadata response")
    video = response_data.get("video")
    if video is not None and not isinstance(video, dict):
        raise TwitchAPIError("Twitch returned invalid VOD metadata")

    # A valid null video is Twitch's response for a missing or unavailable VOD.
    if video is None:
        return meta
    if "lengthSeconds" not in video or not isinstance(video.get("lengthSeconds"), int):
        raise TwitchAPIError("Twitch returned incomplete VOD metadata")
    if "owner" not in video:
        raise TwitchAPIError("Twitch returned incomplete VOD owner metadata")

    meta.title = video.get("title")
    meta.length_seconds = video.get("lengthSeconds")
    meta.created_at = video.get("createdAt")
    owner = video.get("owner") or {}
    if not isinstance(owner, dict):
        raise TwitchAPIError("Twitch returned invalid VOD owner metadata")
    meta.channel = owner.get("displayName") or owner.get("login")
    meta.channel_login = owner.get("login")
    meta.channel_id = owner.get("id")

    # Thumbnail enrichment is optional and must not invalidate required metadata.
    _check_cancel(cancel_check)
    try:
        helix_url = f"https://api.twitch.tv/helix/videos?id={vod_id}"
        helix_headers = get_api_headers()
        async with session.get(helix_url, headers=helix_headers, timeout=10) as resp:
            if resp.status != 200:
                return meta
            data2 = await resp.json()
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, TypeError) as exc:
        logging.warning("Optional Twitch thumbnail lookup failed: %s", type(exc).__name__)
        return meta

    _check_cancel(cancel_check)
    if not isinstance(data2, dict) or not isinstance(data2.get("data", []), list):
        logging.warning("Twitch returned an unexpected thumbnail metadata response")
        return meta
    videos = data2.get("data") or []
    if videos and isinstance(videos[0], dict):
        thumb = videos[0].get("thumbnail_url")
        if thumb and isinstance(thumb, str):
            preview = thumb.replace("%{width}", "480").replace("%{height}", "270")
            full = await resolve_thumbnail(session, thumb, cancel_check=cancel_check)
            meta.thumbnail_preview_url = preview
            meta.thumbnail_full_url = full or preview

    return meta
