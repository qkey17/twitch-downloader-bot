import base64
import logging
from typing import Callable, Dict, Tuple

import aiohttp

from .client import get_api_headers


# =========================
# 7TV
# =========================

async def fetch_7tv_emote_map(session: aiohttp.ClientSession, channel_id: str) -> Dict[str, str]:
    if not channel_id:
        return {}
    try:
        url = f"https://7tv.io/v3/users/twitch/{channel_id}"
        async with session.get(url, timeout=10) as r:
            if r.status != 200:
                return {}
            data = await r.json()

        if not isinstance(data, dict):
            return {}
        emote_set = data.get("emote_set") or {}
        if not isinstance(emote_set, dict):
            return {}
        emotes = emote_set.get("emotes") or []
        if not isinstance(emotes, list):
            return {}

        out: Dict[str, str] = {}
        for e in emotes:
            if not isinstance(e, dict):
                continue
            name = e.get("name")
            emote_data = e.get("data") or {}
            if not isinstance(emote_data, dict):
                continue
            host = emote_data.get("host") or {}
            if not isinstance(host, dict) or not isinstance(host.get("url"), str):
                continue
            if not isinstance(name, str) or not name:
                continue

            files = host.get("files") or []
            if not isinstance(files, list):
                continue
            file = None
            for f in files:
                if isinstance(f, dict) and f.get("name") == "1x.webp":
                    file = f
                    break
            if not file and files:
                file = next((f for f in files if isinstance(f, dict) and isinstance(f.get("name"), str)), None)
            if not file or not isinstance(file.get("name"), str):
                continue

            out[name] = f"https:{host['url']}/{file['name']}"
        return out
    except Exception:
        return {}


# =========================
# BTTV
# =========================

async def fetch_bttv_emote_map(
    session: aiohttp.ClientSession,
    channel_id: str,
    cancel_check: Callable[[], bool] | None = None,
) -> Dict[str, str]:
    out: Dict[str, str] = {}
    if cancel_check and cancel_check():
        return out
    try:
        async with session.get("https://api.betterttv.net/3/cached/emotes/global", timeout=10) as r:
            global_data = await r.json() if r.status == 200 else None
            if r.status != 200:
                logging.warning("BTTV global emotes HTTP %s", r.status)
    except Exception as exc:
        logging.warning("BTTV global emotes request failed: %s", type(exc).__name__)
        global_data = None

    if cancel_check and cancel_check():
        return out
    if global_data is not None:
        if isinstance(global_data, list):
            for e in global_data:
                if isinstance(e, dict) and isinstance(e.get("code"), str) and e.get("code") and e.get("id"):
                    out[e["code"]] = f"https://images.weserv.nl/?url=cdn.betterttv.net/emote/{e['id']}/1x"
        else:
            logging.warning("BTTV returned an unexpected global emotes response")

    if not channel_id:
        return out
    if cancel_check and cancel_check():
        return out
    try:
        async with session.get(
            f"https://api.betterttv.net/3/cached/users/twitch/{channel_id}",
            timeout=10,
        ) as r:
            if r.status != 200:
                logging.warning("BTTV channel emotes HTTP %s", r.status)
                return out
            data = await r.json()
    except Exception as exc:
        logging.warning("BTTV channel emotes request failed: %s", type(exc).__name__)
        return out

    if cancel_check and cancel_check():
        return out
    if not isinstance(data, dict):
        logging.warning("BTTV returned an unexpected channel emotes response")
        return out
    for collection in (data.get("channelEmotes", []), data.get("sharedEmotes", [])):
        if not isinstance(collection, list):
            continue
        for e in collection:
            if isinstance(e, dict) and isinstance(e.get("code"), str) and e.get("code") and e.get("id"):
                out[e["code"]] = f"https://images.weserv.nl/?url=cdn.betterttv.net/emote/{e['id']}/1x"
    return out


# =========================
# FFZ
# =========================

_FFZ_REQUEST_TIMEOUT_S = 3

async def fetch_ffz_emote_map(
    session: aiohttp.ClientSession,
    channel_name: str,
    cancel_check: Callable[[], bool] | None = None,
) -> Dict[str, str]:
    out: Dict[str, str] = {}
    if cancel_check and cancel_check():
        return out
    try:
        async with session.get(
            "https://api.frankerfacez.com/v1/set/global",
            timeout=_FFZ_REQUEST_TIMEOUT_S,
        ) as r:
            global_data = await r.json() if r.status == 200 else None
            if r.status != 200:
                logging.warning("FFZ global emotes HTTP %s", r.status)
    except Exception as exc:
        logging.warning("FFZ global emotes request failed: %s", type(exc).__name__)
        global_data = None

    if cancel_check and cancel_check():
        return out
    if global_data is not None:
        if isinstance(global_data, dict) and isinstance(global_data.get("sets", {}), dict):
            for s in global_data.get("sets", {}).values():
                if not isinstance(s, dict) or not isinstance(s.get("emoticons", []), list):
                    continue
                for e in s.get("emoticons", []):
                    if not isinstance(e, dict) or not isinstance(e.get("urls", {}), dict):
                        continue
                    urls = e.get("urls", {})
                    url = urls.get("1") or urls.get("2") or urls.get("4")
                    if isinstance(e.get("name"), str) and e.get("name") and isinstance(url, str) and url:
                        out[e["name"]] = "https:" + url
        else:
            logging.warning("FFZ returned an unexpected global emotes response")

    if not channel_name:
        return out
    if cancel_check and cancel_check():
        return out
    try:
        async with session.get(
            f"https://api.frankerfacez.com/v1/room/{channel_name}",
            timeout=_FFZ_REQUEST_TIMEOUT_S,
        ) as r:
            if r.status != 200:
                logging.warning("FFZ channel emotes HTTP %s", r.status)
                return out
            data = await r.json()
    except Exception as exc:
        logging.warning("FFZ channel emotes request failed: %s", type(exc).__name__)
        return out

    if cancel_check and cancel_check():
        return out
    if not isinstance(data, dict) or not isinstance(data.get("sets", {}), dict):
        logging.warning("FFZ returned an unexpected channel emotes response")
        return out
    for s in data.get("sets", {}).values():
        if not isinstance(s, dict) or not isinstance(s.get("emoticons", []), list):
            continue
        for e in s.get("emoticons", []):
            if not isinstance(e, dict) or not isinstance(e.get("urls", {}), dict):
                continue
            urls = e.get("urls", {})
            url = urls.get("1") or urls.get("2") or urls.get("4")
            if isinstance(e.get("name"), str) and e.get("name") and isinstance(url, str) and url:
                out[e["name"]] = "https:" + url
    return out


# =========================
# Twitch Global (HELIX)
# =========================

async def fetch_twitch_global_emote_map(session: aiohttp.ClientSession) -> Dict[str, str]:
    url = "https://api.twitch.tv/helix/chat/emotes/global"
    headers = get_api_headers()

    try:
        async with session.get(url, headers=headers, timeout=10) as resp:
            if resp.status != 200:
                logging.warning("Twitch global emotes HTTP %s", resp.status)
                return {}
            data = await resp.json()
    except Exception as e:
        logging.warning("Twitch global emotes error: %s", e)
        return {}

    if not isinstance(data, dict) or not isinstance(data.get("data", []), list):
        logging.warning("Twitch returned an unexpected global emotes response")
        return {}

    out: Dict[str, str] = {}
    for e in data.get("data", []) or []:
        if not isinstance(e, dict):
            continue
        name = e.get("name")
        emote_id = e.get("id")
        if not name or not emote_id:
            continue

        out[name] = f"https://static-cdn.jtvnw.net/emoticons/v2/{emote_id}/default/dark/1.0"

    return out


# =========================
# Twitch Badges
# =========================

async def _fetch_twitch_badge_map(session: aiohttp.ClientSession, url: str) -> Dict[str, str]:
    headers = get_api_headers()

    try:
        async with session.get(url, headers=headers, timeout=10) as resp:
            if resp.status != 200:
                logging.warning("Twitch badges HTTP %s for %s", resp.status, url)
                return {}
            data = await resp.json()
    except Exception as e:
        logging.warning("Twitch badges error for %s: %s", url, e)
        return {}

    out: Dict[str, str] = {}

    if not isinstance(data, dict) or not isinstance(data.get("data", []), list):
        logging.warning("Twitch returned an unexpected badges response")
        return {}
    for badge_set in data.get("data", []) or []:
        if not isinstance(badge_set, dict):
            continue
        set_id = str(badge_set.get("set_id") or "").strip().lower()
        if not set_id:
            continue

        versions = badge_set.get("versions", []) or []
        if not isinstance(versions, list):
            continue
        for version in versions:
            if not isinstance(version, dict):
                continue
            version_id = str(version.get("id") or "").strip()
            if not version_id:
                continue
            image = (
                version.get("image_url_1x")
                or version.get("image_url_2x")
                or version.get("image_url_4x")
            )
            if image:
                out[f"{set_id}:{version_id}"] = image

    return out


async def fetch_twitch_global_badge_map(session: aiohttp.ClientSession) -> Dict[str, str]:
    return await _fetch_twitch_badge_map(session, "https://api.twitch.tv/helix/chat/badges/global")


async def fetch_twitch_channel_badge_map(session: aiohttp.ClientSession, channel_id: str) -> Dict[str, str]:
    if not channel_id:
        return {}
    return await _fetch_twitch_badge_map(session, f"https://api.twitch.tv/helix/chat/badges?broadcaster_id={channel_id}")


# =========================
# Twitch Channel
# =========================

async def fetch_twitch_channel_emote_maps(
    session: aiohttp.ClientSession,
    channel_id: str,
) -> Tuple[Dict[str, str], Dict[str, str]]:
    if not channel_id:
        return {}, {}

    url = f"https://api.twitch.tv/helix/chat/emotes?broadcaster_id={channel_id}"
    headers = get_api_headers()

    try:
        async with session.get(url, headers=headers, timeout=10) as resp:
            if resp.status != 200:
                logging.warning("Twitch channel emotes HTTP %s", resp.status)
                return {}, {}
            data = await resp.json()
    except Exception as e:
        logging.warning("Twitch channel emotes error: %s", e)
        return {}, {}

    name_map: Dict[str, str] = {}
    id_map: Dict[str, str] = {}

    if not isinstance(data, dict) or not isinstance(data.get("data", []), list):
        logging.warning("Twitch returned an unexpected channel emotes response")
        return {}, {}

    for e in data.get("data", []) or []:
        if not isinstance(e, dict):
            continue
        name = e.get("name")
        emote_id = e.get("id")
        images = e.get("images") or {}
        src = images.get("url_1x")

        if not src and emote_id:
            src = f"https://static-cdn.jtvnw.net/emoticons/v2/{emote_id}/default/dark/1.0"

        if name and src:
            name_map[name] = src
        if emote_id and src:
            id_map[str(emote_id)] = src

    return name_map, id_map
