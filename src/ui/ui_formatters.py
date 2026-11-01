from datetime import datetime, timezone
import re

MONTHS_RU = ('янв','фев','мар','апр','май','июн','июл','авг','сен','окт','ноя','дек')


def fmt_hhmmss(total_seconds: int) -> str:
    h = total_seconds // 3600
    m = (total_seconds % 3600) // 60
    s = total_seconds % 60
    return f"{h:02d}:{m:02d}:{s:02d}"


def _fmt_dt_utc(iso: str | None) -> str:
    if not iso:
        return "—"
    try:
        if re.search(r'[Tt ]24:', iso):
            return '—'
        dt = datetime.fromisoformat(iso.strip().replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        dt = dt.astimezone(timezone.utc)
        return f'{dt.day} {MONTHS_RU[dt.month-1]} {dt.year:04d} {dt.hour:02d}:{dt.minute:02d} UTC'
    except Exception:
        return "—"


def _fmt_len(seconds: int | None) -> str:
    if type(seconds) is not int or seconds <= 0:
        return "—"
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    return f"{h:02d}:{m:02d}:{s:02d}"
