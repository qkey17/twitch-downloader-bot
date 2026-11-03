"""Small repository assets and trusted Twitch image selectors; no image processing."""
from pathlib import Path
import re
from urllib.parse import urlsplit

ASSETS = Path(__file__).with_name('assets')
DEFAULT = ASSETS / 'vod.png'
LIVE = ASSETS / 'live-vod.png'


def telegram_image(source):
    """Explicit small multipart upload for repository covers, never a file:// URI."""
    if not isinstance(source, Path):
        return source
    if source not in (DEFAULT, LIVE):
        raise ValueError('Unknown card asset')
    from telegram import InputFile
    with source.open('rb') as file:
        data = file.read(32769)
    if len(data) > 32768:
        raise ValueError('Card asset too large')
    return InputFile(data, filename=source.name, attach=True)


def avatar_url(value):
    if not isinstance(value, str) or len(value) > 1024:
        return None
    try:
        url = urlsplit(value)
    except ValueError:
        return None
    if (url.scheme == 'https' and url.netloc == 'static-cdn.jtvnw.net'
            and not url.query and not url.fragment
            and re.fullmatch(r'/jtv_user_pictures/[A-Za-z0-9_./-]+\.(?:png|jpe?g|webp|gif)', url.path)
            and '..' not in url.path.split('/')):
        return value  # Original API URL, with no size/format/aspect-ratio substitution.
    return None


def vod_image(value=None, *, live=False):
    from src.handlers.handlers_vod_info import preview_url
    return preview_url(value) or (LIVE if live else DEFAULT)


def item_image(item):
    return vod_image(item.get('thumbnail_preview_url', item.get('thumbnail_url')),
                     live=item.get('is_live') is True or item.get('captured_during_live') is True)
