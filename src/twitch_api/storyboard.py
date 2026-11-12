"""Optional, bounded assets for self-contained VOD chat HTML."""
import asyncio
import base64
import io
import json
import logging
import re
from urllib.parse import urlsplit

import aiohttp
from PIL import Image

from src.config import ConfigurationError
from .client import get_client_id

EMBED_LIMIT = 2 * 1024 * 1024
MAX_ATLAS_BYTES = 768 * 1024
MAX_ATLASES = 12
MAX_FRAMES = 96
JPEG_PREFIX = 'data:image/jpeg;base64,'
OBSERVED_CDN = 'd1m7jfoe9zdc1j.cloudfront.net'


def spread(count, limit):
    """Choose evenly spaced indexes, including the first and last."""
    size = min(count, limit)
    if size <= 1:
        return range(size)
    return [i * (count - 1) // (size - 1) for i in range(size)]


def manifest_url(value, vod_id, cover):
    if (not isinstance(value, str) or len(value) > 1024
            or not re.fullmatch(r'https://[a-z0-9.-]+/[A-Za-z0-9_/-]+\.json', value)):
        return None
    match = re.match(r'https://static-cdn\.jtvnw\.net/cf_vods/(d[a-z0-9]{10,18})/', cover or '')
    hosts = {OBSERVED_CDN}
    if match:
        hosts.add(match[1] + '.cloudfront.net')
    url = urlsplit(value)
    if (url.scheme == 'https' and url.netloc in hosts and not url.query and not url.fragment
            and re.fullmatch(r'/[A-Za-z0-9_-]+/storyboards/' + re.escape(vod_id) + r'-info\.json', url.path)):
        return value
    return None


def plan(spec, vod_id, duration, qualities=('high', 'low')):
    if not isinstance(spec, list):
        return None
    for quality in qualities:
        for item in spec:
            if not isinstance(item, dict) or item.get('quality') != quality:
                continue
            keys = ('count', 'interval', 'width', 'height', 'cols', 'rows')
            values = [item.get(k) for k in keys]
            if any(type(v) is not int or v <= 0 for v in values):
                continue
            count, interval, width, height, cols, rows = values
            capacity = cols * rows
            if (width * height * capacity > 4_000_000 or not 1 <= width <= 480 or not 1 <= height <= 270
                    or duration > (count + 1) * interval):
                continue
            # Twitch can round duration/count up to an integer interval. Keep
            # the manifest's grid and sample only timestamps inside the VOD.
            usable_count = count
            if (count - 1) * interval > duration:
                if interval != (duration + count - 1) // count:
                    continue
                usable_count = duration // interval + 1
            images = item.get('images')
            if not isinstance(images, list) or len(images) != (count + capacity - 1) // capacity:
                continue
            if not all(name == f'{vod_id}-{quality}-{i}.jpg' for i, name in enumerate(images)):
                continue
            # If all atlases cannot be read, sample fewer times across the full
            # VOD instead of clustering multiple frames in each selected atlas.
            limit = MAX_FRAMES if len(images) <= MAX_ATLASES else MAX_ATLASES
            selected = {}
            for index in spread(usable_count, limit):
                atlas, cell = divmod(index, capacity)
                selected.setdefault(images[atlas], []).append((cell, index * interval))
            return (width, height, cols, rows), list(selected.items())
    return None


async def read_asset(response, limit, types):
    if (response.status != 200 or response.content_type not in types
            or response.headers.get('Content-Encoding', 'identity').lower() != 'identity'):
        raise ValueError('Unexpected preview response')
    if response.content_length is not None and response.content_length > limit:
        raise ValueError('Preview response exceeds limit')
    data = bytearray()
    async for chunk in response.content.iter_chunked(16384):
        if len(data) + len(chunk) > limit:
            raise ValueError('Preview response exceeds limit')
        data.extend(chunk)
    return bytes(data)


async def get_asset(session, url, limit, types):
    async with session.get(url, timeout=3, allow_redirects=False, auto_decompress=False,
                           headers={'Accept-Encoding': 'identity'}) as response:
        return await read_asset(response, limit, types)


def jpeg(image, size, limit, quality=65):
    if size is not None:
        image.thumbnail(size)
    with io.BytesIO() as output:
        image.convert('RGB').save(output, 'JPEG', quality=quality, subsampling=0 if quality >= 90 else -1)
        data = output.getvalue()
    if len(data) > limit:
        raise ValueError('Encoded preview exceeds limit')
    return JPEG_PREFIX + base64.b64encode(data).decode('ascii')


def cover_data(data, *, large=False):
    with Image.open(io.BytesIO(data), formats=['JPEG', 'PNG']) as image:
        if image.width * image.height > 2_000_000:
            raise ValueError('Cover exceeds pixel limit')
        if large:
            for size, quality in (((1280, 720), 84), ((1280, 720), 76), ((960, 540), 80)):
                with image.copy() as candidate:
                    try:
                        return jpeg(candidate, size, 128 * 1024, quality=quality)
                    except ValueError:
                        continue
        return jpeg(image, (720, 405), 128 * 1024)


async def _upgrade_cover(session, result, cover, full, cancel_check):
    # Twitch serves size variants for this specific filename. Both URLs have
    # already passed the same Twitch CDN validator.
    candidate = re.sub(r'(/thumb[0-9]+-)[0-9]+x[0-9]+(\.jpe?g)$', r'\g<1>1280x720\2', cover or '')
    for url in dict.fromkeys((candidate, full)):
        if not url or url == cover:
            continue
        cancel_check()
        try:
            data = await get_asset(session, url, 512 * 1024, IMAGE_TYPES)
            encoded = await asyncio.to_thread(cover_data, data, large=True)
            if result.get('cover') and len(result['cover']) < len(encoded):
                result['cover_fallback'] = result['cover']
            result['cover'] = encoded
            return
        except ASSET_ERRORS:
            continue


def atlas_frames(data, layout, cells):
    width, height, cols, rows = layout
    with Image.open(io.BytesIO(data), formats=['JPEG']) as image:
        needed_rows = cells[-1][0] // cols + 1
        if image.width != width * cols or not height * needed_rows <= image.height <= height * rows:
            raise ValueError('Storyboard grid does not match the image')
        image.load()
        frames = []
        for cell, timestamp in cells:
            x, y = (cell % cols) * width, (cell // cols) * height
            with image.crop((x, y, x + width, y + height)) as tile:
                # Extracting a cell from a JPEG grid requires a new encoding.
                frames.append([timestamp, jpeg(tile, None, 128 * 1024, quality=92)])
        return frames


ASSET_ERRORS = (aiohttp.ClientError, TimeoutError, ValueError, OSError, Image.DecompressionBombError)
IMAGE_TYPES = {'image/jpeg', 'image/png', 'binary/octet-stream', 'application/octet-stream'}


def _preview_result(result, status):
    logging.getLogger(__name__).info('HTML storyboard: %s', status)
    return result


async def fetch_preview(session, meta, cancel_check):
    """One optional query for a confirmed archived VOD. No media playlists."""
    from src.handlers.handlers_vod_info import preview_url

    result = {}
    if getattr(meta, 'is_live', None) is True:
        return result
    full = preview_url(getattr(meta, 'thumbnail_full_url', None))
    cover = preview_url(getattr(meta, 'thumbnail_preview_url', None)) or full
    if cover:
        try:
            data = await get_asset(session, cover, 512 * 1024, IMAGE_TYPES)
            result['cover'] = await asyncio.to_thread(cover_data, data)
        except ASSET_ERRORS:
            pass
    async def cover_only(status):
        await _upgrade_cover(session, result, cover, full, cancel_check)
        return _preview_result(result, status)

    cancel_check()
    vod_id, duration = getattr(meta, 'vod_id', None), getattr(meta, 'length_seconds', None)
    if (getattr(meta, 'is_live', None) is not False or not isinstance(vod_id, str)
            or not re.fullmatch(r'[0-9]{1,20}', vod_id) or type(duration) is not int or duration <= 0):
        return await cover_only('metadata_unconfirmed')
    status = await _fetch_frames(session, result, cover, vod_id, duration, cancel_check)
    return _preview_result(result, status) if 'frames' in result else await cover_only(status)


async def _fetch_frames(session, result, cover, vod_id, duration, cancel_check):
    status = 'metadata_unavailable'
    try:
        async with asyncio.timeout(12):
            query = 'query($id: ID!) { video(id: $id) { id lengthSeconds seekPreviewsURL } }'
            payload = {'query': query, 'variables': {'id': vod_id}}
            async with session.post('https://gql.twitch.tv/gql', json=payload, timeout=3,
                                    headers={'Client-Id': get_client_id(), 'Accept-Encoding': 'identity'},
                                    allow_redirects=False, auto_decompress=False) as response:
                body = json.loads(await read_asset(response, 32 * 1024, {'application/json'}))
            data = body.get('data') if isinstance(body, dict) and not body.get('errors') else None
            video = data.get('video') if isinstance(data, dict) else None
            if not isinstance(video, dict) or video.get('id') != vod_id or video.get('lengthSeconds') != duration:
                return 'metadata_mismatch'
            seek_url = video.get('seekPreviewsURL')
            if not seek_url:
                return 'seek_url_missing'
            url = manifest_url(seek_url, vod_id, cover)
            if not url:
                return 'manifest_url_rejected'
            status = 'manifest_unavailable'
            manifest = await get_asset(session, url, 128 * 1024, {
                'application/json', 'binary/octet-stream', 'application/octet-stream',
            })
            status = 'manifest_unsupported'
            spec = json.loads(manifest)
            remaining = MAX_ATLASES
            for quality in ('high', 'low'):
                parsed = plan(spec, vod_id, duration, (quality,))
                if not parsed:
                    continue
                layout, atlases = parsed
                if len(atlases) > remaining:
                    status = 'atlas_request_budget'
                    continue
                status = 'atlas_failed'
                frames = []
                try:
                    for name, cells in atlases:
                        cancel_check()
                        remaining -= 1
                        image = await get_asset(session, url.rsplit('/', 1)[0] + '/' + name, MAX_ATLAS_BYTES, IMAGE_TYPES)
                        frames.extend(await asyncio.to_thread(atlas_frames, image, layout, cells))
                except ASSET_ERRORS:
                    continue
                result.update(frames=frames, aspect=list(layout[:2]))
                return 'frames_acquired'
    except (*ASSET_ERRORS, ConfigurationError, RecursionError):
        pass
    return status
