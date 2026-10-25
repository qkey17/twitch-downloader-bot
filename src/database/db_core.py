import sqlite3
from pathlib import Path
from src.config import DB_PATH


def _connect() -> sqlite3.Connection:
    db_path = Path(DB_PATH).expanduser()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db_path)
    try:
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys = ON;")
        return con
    except BaseException:
        con.close()
        raise


def init_db():
    con = _connect()
    try:
        con.execute("BEGIN IMMEDIATE")
        cur = con.cursor()

        cur.execute("""
        CREATE TABLE IF NOT EXISTS vod_cache (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            vod_id TEXT NOT NULL,
            fmt TEXT NOT NULL,
            vod_url TEXT NOT NULL,
            title TEXT,
            created_at TEXT,
            channel TEXT,
            length_seconds INTEGER,
            messages INTEGER,
            unique_users INTEGER,
            parts INTEGER,
            html_url TEXT,
            cached_at REAL NOT NULL
        )
        """)

        cur.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_vod_cache_unique
        ON vod_cache(vod_id, fmt)
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS vod_files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            cache_id INTEGER NOT NULL,
            part_index INTEGER NOT NULL,
            file_id TEXT NOT NULL,
            file_name TEXT,
            FOREIGN KEY(cache_id) REFERENCES vod_cache(id) ON DELETE CASCADE
        )
        """)

        cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_vod_files_cache_id
        ON vod_files(cache_id)
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS user_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            cache_id INTEGER NOT NULL,
            processed_at REAL NOT NULL,
            FOREIGN KEY(cache_id) REFERENCES vod_cache(id) ON DELETE CASCADE
        )
        """)

        cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_user_history_user_id
        ON user_history(user_id, processed_at)
        """)

        from .db_results import create_schema, migrate_legacy
        create_schema(con)
        migrate_legacy(con)
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()
