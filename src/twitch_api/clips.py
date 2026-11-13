"""Optional, bounded Twitch clip metadata for the HTML viewer."""
import asyncio
import json
import re
from urllib.parse import urlsplit

import aiohttp

from .client import get_api_headers


MAX_CLIPS = 100
CLIP_LINK = re.compile(
    r"(?<![\w@./:\\%+\-])(?:https?://)?(?:clips\.twitch\.tv/|"
    r"(?:www\.)?twitch\.tv/[a-z0-9_]{1,25}/clip/)"
    r"([a-z0-9_-]{1,128})/?(?=$|[\s?#)\]}>,.!;:'\"”’»、。，！？])",
    re.IGNORECASE | re.ASCII,
)
THUMBNAIL_HOSTS = {"clips-media-assets.twitch.tv", "clips-media-assets2.twitch.tv"}


def collect_clip_ids(text, ids):
    if len(ids) >= MAX_CLIPS or not isinstance(text, str) or "twitch.tv/" not in text.lower():
        return
    for match in CLIP_LINK.finditer(text):
        ids.setdefault(match[1], None)
        if len(ids) >= MAX_CLIPS:
            return


def clip_metadata(item):
    title, creator, thumbnail = (item.get(key) for key in ("title", "creator_name", "thumbnail_url"))
    if not all(isinstance(value, str) and value.strip() for value in (title, creator, thumbnail)):
        return None
    if len(thumbnail) > 512 or not re.fullmatch(r"https://[a-z0-9.-]+/[a-zA-Z0-9_./-]+\.jpe?g", thumbnail):
        return None
    if urlsplit(thumbnail).netloc not in THUMBNAIL_HOSTS:
        return None
    title = title.strip()[:256].encode("utf-8", errors="replace").decode()
    creator = creator.strip()[:80].encode("utf-8", errors="replace").decode()
    return {"title": title, "creator": creator, "thumbnail": thumbnail}


async def fetch_clip_metadata(session, ids):
    if not ids:
        return {}
    requested = dict.fromkeys(list(ids)[:MAX_CLIPS])
    try:
        headers = get_api_headers()
        async with asyncio.timeout(3):
            async with session.get(
                "https://api.twitch.tv/helix/clips", headers=headers,
                params=[("id", slug) for slug in requested], allow_redirects=False,
            ) as response:
                if response.status != 200:
                    return {}
                chunks = bytearray()
                async for chunk in response.content.iter_chunked(8192):
                    chunks.extend(chunk)
                    if len(chunks) > 256 * 1024:
                        return {}
                payload = json.loads(chunks)
    except (aiohttp.ClientError, TimeoutError, ValueError, RuntimeError):
        return {}
    items = payload.get("data") if isinstance(payload, dict) else None
    result = {}
    for item in items if isinstance(items, list) else ():
        slug = item.get("id") if isinstance(item, dict) else None
        if isinstance(slug, str) and slug in requested and (metadata := clip_metadata(item)):
            result[slug] = metadata
    return result
