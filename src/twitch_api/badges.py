import asyncio
import logging

import aiohttp
from .client import get_api_headers


async def _fetch_badge_map(session: aiohttp.ClientSession, url: str):
    headers = get_api_headers()
    try:
        async with session.get(url, headers=headers, timeout=10) as resp:
            if resp.status != 200:
                logging.warning("Twitch badges HTTP %s", resp.status)
                return {}
            data = await resp.json()
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, TypeError) as exc:
        logging.warning("Twitch badges request failed: %s", type(exc).__name__)
        return {}

    if not isinstance(data, dict) or not isinstance(data.get("data"), list):
        logging.warning("Twitch returned an unexpected badges response")
        return {}

    result = {}
    for badge in data["data"]:
        if not isinstance(badge, dict):
            continue
        set_id = str(badge.get("set_id") or "").strip()
        versions = badge.get("versions")
        if not set_id or not isinstance(versions, list):
            continue
        for version in versions:
            if not isinstance(version, dict):
                continue
            version_id = str(version.get("id") or "").strip()
            image = version.get("image_url_2x") or version.get("image_url_1x")
            if version_id and isinstance(image, str) and image:
                result[f"{set_id}/{version_id}"] = image

    return result


async def fetch_twitch_global_badge_map(session: aiohttp.ClientSession):
    return await _fetch_badge_map(session, "https://api.twitch.tv/helix/chat/badges/global")


async def fetch_twitch_channel_badge_map(session: aiohttp.ClientSession, channel_id: str):
    if not channel_id:
        return {}
    url = f"https://api.twitch.tv/helix/chat/badges?broadcaster_id={channel_id}"
    return await _fetch_badge_map(session, url)
