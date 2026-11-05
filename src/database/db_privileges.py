"""Persistent delegation for /allvods in the existing application database."""

from contextlib import closing

from .db_core import _connect


def create_schema(con):
    con.execute("""
        CREATE TABLE IF NOT EXISTS privileged_users (
            user_id INTEGER PRIMARY KEY CHECK (user_id > 0)
        )
    """)


# Bound SQLite lock waits in these short synchronous Telegram handlers.
_TIMEOUT = 0.25


def has_access(user_id):
    with closing(_connect(timeout=_TIMEOUT)) as con:
        return con.execute(
            "SELECT 1 FROM privileged_users WHERE user_id = ?", (user_id,)
        ).fetchone() is not None


def grant(user_id):
    with closing(_connect(timeout=_TIMEOUT)) as con, con:
        return con.execute(
            "INSERT INTO privileged_users (user_id) VALUES (?) "
            "ON CONFLICT (user_id) DO NOTHING", (user_id,)
        ).rowcount == 1


def revoke(user_id):
    with closing(_connect(timeout=_TIMEOUT)) as con, con:
        return con.execute(
            "DELETE FROM privileged_users WHERE user_id = ?", (user_id,)
        ).rowcount == 1


def list_users():
    with closing(_connect(timeout=_TIMEOUT)) as con:
        return [row[0] for row in con.execute("SELECT user_id FROM privileged_users ORDER BY user_id")]
