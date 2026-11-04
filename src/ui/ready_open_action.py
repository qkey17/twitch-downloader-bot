"""Generation-pinned offer buttons; owner/chat signature and source-message binding."""
import base64
import hashlib
import hmac
import re
import secrets

PREFIX = 'ui:ready:'
_PATTERN = re.compile(re.escape(PREFIX) + r'1[A-Za-z0-9_-]{51}')


def _tag(bot, owner, chat, payload):
    key = bot.token
    if not isinstance(key, str) or not key:
        raise ValueError('Signing key unavailable')
    return hmac.new(key.encode(), f'tcd-ready-v1:{owner}:{chat}:'.encode() + payload,
                    hashlib.sha256).digest()[:12]


def action(context, owner, chat, ready):
    from src.database.db_results import normalize_vod_id
    if owner is None:
        raise ValueError('Owner unavailable')
    generation = ready['id']
    if not isinstance(generation, str) or not re.fullmatch('[0-9a-f]{32}', generation):
        raise ValueError('Invalid generation identifier')
    vod = normalize_vod_id(ready['vod_id'])
    payload = bytes.fromhex(generation) + int(vod).to_bytes(9, 'big') + secrets.token_bytes(1)
    encoded = base64.urlsafe_b64encode(payload + _tag(context.bot, owner, chat, payload)).decode().rstrip('=')
    return PREFIX + '1' + encoded  # 61 ASCII bytes even for a 20-digit VOD ID.


def selection(update, context):
    q = update.callback_query
    message, data = q.message, q.data or ''
    if message is None or not _PATTERN.fullmatch(data):
        return None
    sender = getattr(message, 'from_user', None)
    if sender is None or sender.id != context.bot.id:
        return None
    markup = getattr(message, 'reply_markup', None)
    if not any(b.callback_data == data for row in (markup.inline_keyboard if markup else ()) for b in row):
        return None
    try:
        encoded = data[len(PREFIX)+1:]
        raw = base64.urlsafe_b64decode(encoded + '=')
        if len(raw) != 38 or base64.urlsafe_b64encode(raw).decode().rstrip('=') != encoded:
            return None
        payload, tag = raw[:26], raw[26:]
        if not hmac.compare_digest(tag, _tag(context.bot, update.effective_user.id, message.chat_id, payload)):
            return None
        from src.database.db_results import normalize_vod_id
        vod = normalize_vod_id(str(int.from_bytes(payload[16:25], 'big')))
        return vod, payload[:16].hex()
    except (ValueError, TypeError):
        return None
