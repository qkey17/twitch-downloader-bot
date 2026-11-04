"""Bounded read-only discovery of current channel results; never personal history."""
import asyncio
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from src.database import db_core, db_results as results
from src.twitch_api import videos

MAX_CANDIDATES = 500
ARCHIVE = '🗃️ Архив'


def timestamp(value):
    if not isinstance(value, str) or len(value) > 64:
        raise ValueError('Invalid archive date')
    date = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if date.tzinfo is None:
        raise ValueError('Archive date needs timezone')
    return date.astimezone(timezone.utc).timestamp()


def order(vod):
    return (-timestamp(vod.created_at), int(vod.id))


def candidates(channel_id):
    """No schema/legacy migration, writes, leases, or HTML content reads."""
    con = None
    found = []
    try:
        con = sqlite3.connect(Path(db_core.DB_PATH).resolve().as_uri()+'?mode=ro', uri=True)
        con.row_factory = sqlite3.Row
        rows = con.execute("""SELECT r.* FROM html_current h JOIN html_results r
            ON r.id=h.result_id AND r.vod_id=h.vod_id AND r.fmt=h.fmt
            WHERE h.fmt='html_online' AND r.state='ready' AND r.managed=1
            AND length(r.meta_json)<=16384 AND CASE WHEN json_valid(r.meta_json)
            THEN json_extract(r.meta_json,'$.channel_id')=? ELSE 0 END
            AND NOT EXISTS (SELECT 1 FROM html_deletions d
                WHERE d.result_id=r.id AND d.phase!='cancelled')
            ORDER BY json_extract(r.meta_json,'$.created_at') DESC,r.vod_id
            LIMIT ?""", (channel_id, MAX_CANDIDATES))
        for record in rows:
            row = dict(record)
            vod = saved_vod(row, channel_id)
            if vod and results.file_status(row) == 'available' and results._reference_matches(row):
                found.append(vod)
    except (sqlite3.Error,OSError):
        return []
    finally:
        if con is not None:
            con.close()
    return sorted(found,key=order)


def saved_vod(row, channel_id):
    try:
        meta = json.loads(row['meta_json'])
        if not isinstance(meta,dict) or meta.get('channel_id') != channel_id:
            return None
        title, date, seconds = meta.get('title'), meta.get('created_at'), meta.get('length_seconds')
        if not isinstance(title,str) or len(title)>4096 or type(seconds) is not int or not 0<=seconds<10**10:
            return None
        timestamp(date)
        results.normalize_vod_id(row['vod_id'])
        from src.handlers.handlers_vod_info import preview_url
        preview = preview_url(meta.get('thumbnail_preview_url') or meta.get('thumbnail_url')) or ''
        return videos.ArchiveVod(row['vod_id'],channel_id,'','',title[:240],date,'',str(seconds)+'s','',preview,'public')
    except (TypeError,ValueError,KeyError):
        return None


async def confirmed(http, channel_id, records):
    """At most five Helix requests, once per list session. Unknown omits archives."""
    records = records[:MAX_CANDIDATES]
    missing = []
    try:
        async with asyncio.timeout(5):
            for start in range(0,len(records),100):
                batch=records[start:start+100]
                ids={v.id for v in batch}
                response=await videos._get_json(http,'/videos',[('id',v.id) for v in batch])
                rows=response['data']
                if not isinstance(rows,list) or len(rows)>len(ids):
                    return []
                seen=set()
                for row in rows:
                    if (not isinstance(row,dict) or row.get('id') not in ids or row['id'] in seen
                            or row.get('user_id')!=channel_id or row.get('type')!='archive'):
                        return []
                    seen.add(row['id'])
                missing.extend(v for v in batch if v.id not in seen)
    except Exception:
        return []  # No inferred unavailability, retry, or background request.
    return missing
