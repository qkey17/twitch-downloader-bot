"""Local text classification; no requests, database queries or Telegram state."""
from dataclasses import dataclass
import re
from typing import Literal
from urllib.parse import parse_qs, urlsplit

from .config import VOD_URL_RE
from .database.db_results import normalize_vod_id


_LOGIN_RE = re.compile(r'[a-z0-9_]{1,25}', re.IGNORECASE | re.ASCII)
# These one-segment Twitch URLs are site routes, not channel links. The same
# words without a URL remain syntactically valid login input.
_SITE_ROUTES = frozenset({
    'videos', 'clip', 'clips', 'directory', 'search', 'downloads', 'settings',
    'subscriptions', 'inventory', 'login', 'signup', 'turbo', 'drops', 'wallet',
})


class InputClassificationError(ValueError):
    """The text is neither a supported VOD input nor a channel login/URL."""


@dataclass(frozen=True)
class ClassifiedInput:
    kind: Literal['vod', 'channel']
    value: str


def _vod(value):
    try:
        return ClassifiedInput('vod', normalize_vod_id(value))
    except ValueError:
        raise InputClassificationError('Invalid Twitch VOD ID') from None


def classify_input(text: str, *, expected_kind: Literal['vod', 'channel'] | None = None) -> ClassifiedInput:
    """Classify automatically, or enforce a command's type without rerouting.

    A numeric login is a channel only when explicitly requested by /channel.
    The default classification retains VOD ID precedence.
    """
    if expected_kind not in (None, 'vod', 'channel'):
        raise InputClassificationError('Unsupported input type')
    if expected_kind == 'channel' and isinstance(text, str) and _LOGIN_RE.fullmatch(text.strip()):
        return ClassifiedInput('channel', text.strip().lower())
    result = _classify_input(text)
    if expected_kind is not None and result.kind != expected_kind:
        raise InputClassificationError('Input does not match command type')
    return result


def _classify_input(text: str) -> ClassifiedInput:
    """Classify a VOD URL/ID or channel login/URL, trimming outer whitespace.

    VOD IDs have precedence over numeric logins. Existing HTTP(S), bare-host,
    /videos/ID and /channel/v/ID forms retain their query/fragment support.
    Channel URLs require a single login segment, with an optional trailing
    slash and optional sharing query parameters. Fragments are not channel input.
    Documented Twitch app channel deep links are also accepted.
    Syntax does not establish that a channel or VOD exists.
    """
    if not isinstance(text, str):
        raise InputClassificationError('Expected text input')
    text = text.strip()
    if not text or text.startswith('/') or re.search(r'[\x00-\x20\x7f]', text):
        raise InputClassificationError('Unsupported Twitch input')
    if re.fullmatch(r'[0-9]+', text):
        return _vod(text)
    if _LOGIN_RE.fullmatch(text):
        return ClassifiedInput('channel', text.lower())

    try:
        target = urlsplit(text if '://' in text else 'https://' + text)
    except ValueError:
        raise InputClassificationError('Invalid Twitch URL') from None
    if target.scheme == 'twitch':
        login = ''
        if target.netloc.lower() in ('stream', 'channel'):
            login = target.path.removeprefix('/').removesuffix('/')
        elif target.netloc.lower() == 'open' and target.path in ('', '/'):
            query = parse_qs(target.query, keep_blank_values=True)
            destinations = set(query) & {'stream', 'channel', 'video', 'game'}
            if len(destinations) == 1 and destinations <= {'stream', 'channel'}:
                values = query[next(iter(destinations))]
                if len(values) == 1:
                    login = values[0]
        if '#' not in text and _LOGIN_RE.fullmatch(login) and login.lower() not in _SITE_ROUTES:
            return ClassifiedInput('channel', login.lower())
        raise InputClassificationError('Unsupported Twitch app URL')
    if (target.scheme not in ('http', 'https')
            or target.netloc.lower() not in ('twitch.tv', 'www.twitch.tv', 'm.twitch.tv')):
        raise InputClassificationError('Unsupported Twitch URL')

    path = target.path.removesuffix('/')
    # Reuse the existing VOD parser's grammar. Canonicalizing only the origin
    # supports the entry handler's HTTP/non-www forms; fullmatch rejects the
    # legacy helper's otherwise accepted suffixes such as /videos/123/extra.
    match = VOD_URL_RE.fullmatch('https://www.twitch.tv' + path)
    if match:
        return _vod(match.group(1))

    login = path[1:] if path.startswith('/') else ''
    if ('#' not in text and _LOGIN_RE.fullmatch(login)
            and login.lower() not in _SITE_ROUTES):
        return ClassifiedInput('channel', login.lower())
    raise InputClassificationError('Unsupported Twitch URL')
