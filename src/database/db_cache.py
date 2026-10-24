import time
from typing import Optional, List, Dict, Any

from src.config import CACHE_TTL_SECONDS
from .db_core import _connect


def upsert_cache(
    vod_id: str,
    fmt: str,
    vod_url: str,
    meta: Dict[str, Any],
    stats: Dict[str, Any],
    files: List[Dict[str, str]],
) -> int:
    con = _connect()
    try:
        cur = con.cursor()
        now = time.time()

        html_url = meta.get("html_url")

        cur.execute("""
        INSERT INTO vod_cache(
            vod_id, fmt, vod_url, title, created_at, channel,
            length_seconds, messages, unique_users, parts, html_url, cached_at
        )
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(vod_id, fmt) DO UPDATE SET
          vod_url=excluded.vod_url,
          title=excluded.title,
          created_at=excluded.created_at,
          channel=excluded.channel,
          length_seconds=excluded.length_seconds,
          messages=excluded.messages,
          unique_users=excluded.unique_users,
          parts=excluded.parts,
          html_url=excluded.html_url,
          cached_at=excluded.cached_at
        """, (
            vod_id, fmt, vod_url,
            meta.get("title"), meta.get("created_at"), meta.get("channel"), meta.get("length_seconds"),
            stats.get("messages"), stats.get("unique_users"), stats.get("parts"),
            html_url,
            now
        ))

        cur.execute("SELECT id FROM vod_cache WHERE vod_id=? AND fmt=?", (vod_id, fmt))
        row = cur.fetchone()
        cache_id = int(row["id"])

        cur.execute("DELETE FROM vod_files WHERE cache_id=?", (cache_id,))
        for i, f in enumerate(files, start=1):
            cur.execute("""
            INSERT INTO vod_files(cache_id, part_index, file_id, file_name)
            VALUES(?,?,?,?)
            """, (cache_id, i, f["file_id"], f.get("file_name")))

        con.commit()
        return cache_id
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def get_cache(vod_id: str, fmt: str) -> Optional[Dict[str, Any]]:
    con = _connect()
    try:
        cur = con.cursor()
        cur.execute("SELECT * FROM vod_cache WHERE vod_id=? AND fmt=?", (vod_id, fmt))
        row = cur.fetchone()
        if not row:
            return None

        cache_id = int(row["id"])

        cur.execute(
            "SELECT part_index, file_id, file_name FROM vod_files WHERE cache_id=? ORDER BY part_index ASC",
            (cache_id,),
        )
        files = [dict(r) for r in cur.fetchall()]

        # ВАЖНО: восстанавливаем meta и stats как раньше ожидал UI
        meta = {
            "title": row["title"],
            "created_at": row["created_at"],
            "channel": row["channel"],
            "length_seconds": row["length_seconds"],
            "html_url": row["html_url"],
        }

        stats = {
            "messages": row["messages"],
            "unique_users": row["unique_users"],
            "parts": row["parts"],
        }

        out = dict(row)
        out["files"] = files
        out["meta"] = meta
        out["stats"] = stats
        return out
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def cache_is_expired(cache_row: Dict[str, Any]) -> bool:
    cached_at = float(cache_row.get("cached_at") or 0)
    return (time.time() - cached_at) > CACHE_TTL_SECONDS


def get_cache_by_id(cache_id: int):
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM vod_cache WHERE id = ?", (cache_id,))
        row = cur.fetchone()
        return dict(row) if row else None
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
