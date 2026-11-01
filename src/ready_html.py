"""Read-only, bounded-memory access to one registered generation under its lease."""
import hashlib
import os
import re
import stat
from src.document_name import document_name

from src.database import db_results as results


MAX_CLOUD_HTML_BYTES = 40 * 1024 * 1024


class HtmlUnavailable(Exception):
    pass


class HtmlTooLarge(HtmlUnavailable):
    pass


def _row(vod_id, generation_id=None):
    con = results._connect()
    try:
        if generation_id is None:
            row = con.execute("""SELECT r.* FROM html_current h JOIN html_results r
                ON r.id=h.result_id AND r.vod_id=h.vod_id AND r.fmt=h.fmt
                WHERE h.vod_id=? AND h.fmt='html_online'""", (vod_id,)).fetchone()
        else:
            row = con.execute("SELECT * FROM html_results WHERE id=? AND vod_id=? AND fmt='html_online'",
                              (generation_id, vod_id)).fetchone()
        if (not row or row['state'] != 'ready' or not row['managed']
                or con.execute("SELECT 1 FROM html_deletions WHERE result_id=? AND phase!='cancelled'",
                               (row['id'],)).fetchone()):
            raise HtmlUnavailable()
        return dict(row)
    finally:
        con.close()


def _identity(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


class ReadyHtml:
    """Owned by the caller even while open() runs in a drained worker thread.

    A cancellation must join that worker before close(); otherwise an acquired
    descriptor returned just after cancellation could escape its owner.
    """
    def __init__(self, vod_id, generation_id=None):
        self.vod_id = results.normalize_vod_id(vod_id)
        if generation_id is not None and (not isinstance(generation_id, str)
                                          or not results._KEY.fullmatch(generation_id)):
            raise ValueError('Invalid generation identifier')
        self.generation_id = generation_id
        self.handle = self.lease = None
        self.filename = None
        self.document_filename = None

    def open(self, stop):
        row = _row(self.vod_id, self.generation_id)
        self.generation_id = row['id']  # Pin once, never silently follow html_current again.
        if (row['origin'] == 'generated'
                and row['lease_root'] != str(results._lease_path(row['id']).parent)):
            raise HtmlUnavailable()
        self.lease = results._recovery_lease(row['id'])
        if self.lease is None or stop.is_set() or _row(self.vod_id, row['id']) != row:
            raise HtmlUnavailable()
        path = results.result_path(row)
        if path is None or not results._reference_matches(row):
            raise HtmlUnavailable()
        # Use a directory fd on POSIX so the final open cannot follow a replaced root.
        directory = None
        try:
            flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0)
            if os.name == 'posix':
                directory = os.open(path.parent, flags | os.O_DIRECTORY)
                before = os.stat(path.name, dir_fd=directory, follow_symlinks=False)
                fd = os.open(path.name, flags, dir_fd=directory)
            else:
                before = path.lstat()
                fd = os.open(path, flags)
            self.handle = os.fdopen(fd, 'rb')
        finally:
            if directory is not None:
                os.close(directory)
        info = os.fstat(self.handle.fileno())
        if (not stat.S_ISREG(before.st_mode) or not stat.S_ISREG(info.st_mode)
                or _identity(before) != _identity(info) or info.st_size <= 0
                or info.st_size != row['size_bytes']):
            raise HtmlUnavailable()
        if info.st_size > MAX_CLOUD_HTML_BYTES:
            raise HtmlTooLarge()
        if row['sha256']:
            if not re.fullmatch(r'[0-9a-f]{64}', row['sha256']):
                raise HtmlUnavailable()
            digest = hashlib.sha256()
            while chunk := self.handle.read(65536):
                if stop.is_set():
                    raise HtmlUnavailable()
                digest.update(chunk)
            if digest.hexdigest() != row['sha256']:
                raise HtmlUnavailable()
        elif row['origin'] == 'legacy':
            # The existing bounded envelope proof, not an invented historical hash.
            prefix = self.handle.read(4096).lstrip(b'\xef\xbb\xbf \t\r\n').lower()
            self.handle.seek(max(0, info.st_size - 4096))
            if (not prefix.startswith((b'<!doctype html', b'<html'))
                    or not self.handle.read(4096).rstrip().lower().endswith(b'</html>')):
                raise HtmlUnavailable()
        else:
            raise HtmlUnavailable()
        named = path.lstat()
        if (stop.is_set() or not stat.S_ISREG(named.st_mode)
                or _identity(info) != _identity(os.fstat(self.handle.fileno()))
                or _identity(info) != _identity(named)):
            raise HtmlUnavailable()
        self.handle.seek(0)
        self.filename = f'chat_{self.vod_id}_{row["id"]}.html'
        self.document_filename = document_name(row.get('meta_json'),self.filename)

    def close(self):
        try:
            if self.handle is not None:
                self.handle.close()
        finally:
            if self.lease is not None:
                self.lease.close()
