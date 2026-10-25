"""Operation-owned, disk-backed chat. Connections never cross thread boundaries."""
import json
import logging
import re
import sqlite3
import uuid
from pathlib import Path

from src.file_lock import try_lock_file

_DIRECTORY_RE = re.compile(r"tdb-spool-[0-9a-f]{32}")
_OWNED_FILES = {"owner.lock", "chat.sqlite3", "chat.sqlite3-journal", "chat.sqlite3-wal", "chat.sqlite3-shm"}
_MARKER = b"TDB_CHAT_SPOOL_V1\n"


def encode_row(row: dict) -> tuple[str, str]:
    return row["user"], json.dumps(row, ensure_ascii=False)


class ChatSpool:
    def __init__(self, root):
        self.directory = Path(root).expanduser().resolve() / f"tdb-spool-{uuid.uuid4().hex}"
        self.path = self.directory / "chat.sqlite3"
        self.messages = 0
        self.unique_users = 0
        self._lease = None
        self._created = False

    def open(self):
        self.directory.parent.mkdir(parents=True, exist_ok=True)
        self.directory.mkdir(mode=0o700)
        self._created = True
        self._lease = (self.directory / "owner.lock").open("x+b")
        if not try_lock_file(self._lease):
            raise OSError("Could not reserve temporary chat storage")
        self._lease.write(_MARKER)
        self._lease.flush()
        con = sqlite3.connect(self.path)
        try:
            con.executescript("""
                CREATE TABLE messages (seq INTEGER PRIMARY KEY, payload TEXT NOT NULL);
                CREATE TABLE users (name TEXT PRIMARY KEY COLLATE BINARY) WITHOUT ROWID;
            """)
            con.commit()
        finally:
            con.close()

    def _connect(self):
        # mode=rw prevents an accidentally missing spool from becoming an empty DB.
        con = sqlite3.connect(self.path.as_uri() + "?mode=rw", uri=True)
        try:
            con.execute("PRAGMA cache_size=-2048")
            con.execute("PRAGMA temp_store=FILE")
            con.execute("PRAGMA mmap_size=0")
        except BaseException:
            con.close()
            raise
        return con

    def append_batch(self, batch, cancel_check=None):
        con = self._connect()
        messages = self.messages
        unique_users = self.unique_users
        try:
            with con:
                for user, payload in batch:
                    if cancel_check is not None and cancel_check():
                        raise RuntimeError("Загрузка была отменена.")
                    messages += 1
                    con.execute("INSERT INTO messages(seq, payload) VALUES (?, ?)", (messages, payload))
                    cursor = con.execute("INSERT OR IGNORE INTO users(name) VALUES (?)", (user,))
                    unique_users += cursor.rowcount
            self.messages, self.unique_users = messages, unique_users
        finally:
            con.close()

    def iter_rows(self):
        con = self._connect()
        expected_seq = 1
        try:
            for seq, payload in con.execute("SELECT seq, payload FROM messages ORDER BY seq"):
                if seq != expected_seq:
                    raise sqlite3.DatabaseError("Temporary chat sequence is incomplete")
                expected_seq += 1
                yield json.loads(payload)
            if expected_seq - 1 != self.messages:
                raise sqlite3.DatabaseError("Temporary chat message count does not match")
        finally:
            con.close()

    def size_bytes(self):
        return sum(path.stat().st_size for path in self.directory.iterdir() if path.name in _OWNED_FILES)

    def close(self):
        if not self._created:
            return
        try:
            for name in _OWNED_FILES - {"owner.lock"}:
                (self.directory / name).unlink(missing_ok=True)
        finally:
            if self._lease is not None:
                self._lease.close()
                self._lease = None
        (self.directory / "owner.lock").unlink(missing_ok=True)
        self.directory.rmdir()
        self._created = False


def cleanup_orphaned_chat_spools(root) -> int:
    """Delete only exact, unlocked operation directories with known files."""
    removed = 0
    try:
        for directory in Path(root).expanduser().resolve().iterdir():
            if (not _DIRECTORY_RE.fullmatch(directory.name) or directory.is_symlink()
                    or directory.is_junction() or not directory.is_dir()):
                continue
            try:
                children = tuple(directory.iterdir())
                if any(path.name not in _OWNED_FILES or path.is_symlink() or not path.is_file()
                       for path in children):
                    continue
                with (directory / "owner.lock").open("r+b") as lease:
                    if not try_lock_file(lease):
                        continue
                    if lease.read(len(_MARKER) + 1) != _MARKER:
                        continue
                    for path in children:
                        if path.name != "owner.lock":
                            path.unlink()
                (directory / "owner.lock").unlink()
                directory.rmdir()
                removed += 1
            except OSError:
                logging.getLogger(__name__).warning("Could not clean an orphaned chat spool")
    except OSError:
        pass
    return removed
