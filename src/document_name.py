"""Telegram presentation name only; never used as a filesystem path."""
from datetime import datetime,timezone
import json
import re
import unicodedata


def document_name(meta_json, fallback):
    try:
        if not isinstance(meta_json,str) or len(meta_json)>65536:return fallback
        meta=json.loads(meta_json)
        if not isinstance(meta,dict):return fallback
        channel,created=meta.get('channel'),meta.get('created_at')
        if not isinstance(channel,str) or not isinstance(created,str) or len(created)>128:return fallback
        date=datetime.fromisoformat(created.strip().replace('Z','+00:00'))
        if date.tzinfo is None or date.utcoffset() is None:return fallback
        date=date.astimezone(timezone.utc)
        channel=unicodedata.normalize('NFC',channel)
        channel=''.join(' ' if unicodedata.category(c).startswith('C') or c in '/\\:*?"<>|' else c for c in channel)
        channel=re.sub(r'\s+',' ',channel).strip(' .')
        # Keep the whole filename comfortably below common 255-byte limits.
        channel=channel.encode('utf-8')[:150].decode('utf-8','ignore').rstrip(' .')
        if not channel:return fallback
        return f'{channel} — {date.year:04d}-{date.month:02d}-{date.day:02d} {date.hour:02d}-{date.minute:02d} UTC.html'
    except (ValueError,TypeError,OverflowError,RecursionError):
        return fallback
