import time
from typing import List, Dict, Any

from .db_core import _connect


def add_user_history(user_id: int, cache_id: int):
    con = _connect()
    try:
        cur = con.cursor()
        cur.execute("""
        INSERT INTO user_history(user_id, cache_id, processed_at)
        SELECT ?, vc.id, ?
        FROM vod_cache AS vc
        WHERE vc.id = ?
          AND NOT EXISTS (
              SELECT 1
              FROM user_history AS uh
              JOIN vod_cache AS existing_vc ON existing_vc.id = uh.cache_id
              WHERE uh.user_id = ?
                AND existing_vc.vod_id = vc.vod_id
                AND existing_vc.fmt = vc.fmt
          )
        """, (int(user_id), time.time(), int(cache_id), int(user_id)))
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def get_history_for_user(user_id: int, limit: int = 10, offset: int = 0) -> List[Dict[str, Any]]:
    limit = max(1, min(int(limit), 50))
    offset = max(0, int(offset))

    con = _connect()
    try:
        cur = con.cursor()

        cur.execute("""
        SELECT vc.*, uh.id AS history_id, uh.processed_at
        FROM user_history uh
        JOIN vod_cache vc ON vc.id = uh.cache_id
        WHERE uh.user_id=?
        ORDER BY uh.processed_at DESC, uh.id DESC
        LIMIT ? OFFSET ?
        """, (int(user_id), limit, offset))
        return [dict(row) for row in cur.fetchall()]
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def get_history_count(user_id: int) -> int:
    con = _connect()
    try:
        cur = con.cursor()
        cur.execute("SELECT COUNT(*) FROM user_history WHERE user_id=?", (int(user_id),))
        return int(cur.fetchone()[0])
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def get_history_entry_for_user(user_id: int, history_id: int) -> Dict[str, Any] | None:
    con = _connect()
    try:
        cur = con.cursor()
        cur.execute("""
        SELECT vc.*, uh.id AS history_id, uh.processed_at
        FROM user_history uh
        JOIN vod_cache vc ON vc.id = uh.cache_id
        WHERE uh.user_id=? AND uh.id=?
        LIMIT 1
        """, (int(user_id), int(history_id)))
        row = cur.fetchone()
        return dict(row) if row else None
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()
